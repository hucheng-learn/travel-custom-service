import re
from dataclasses import dataclass
from datetime import tzinfo
from uuid import UUID

from knowledge.retrieval import KnowledgeProvider
from plan.fixed_order_plan import ITINERARY_QUERY, ORDER_QUERY, explicit_control, is_pause_request
from plan.models import DATE_ITINERARY, FLOW_LABELS, ITINERARY, TICKET, PlanRoute, SlotValue
from plan.travel_plans import has_multiple_requests
from plan.turn_planner import TurnPlanner
from plan.validator import PlanRejectedError, TurnPlanValidator
from repository.dialogue import ReservedTurn
from task.action.order_status import Reply
from task.after_sales import REFUND_STATUS, AfterSales, sales_intent
from task.flow.executor import DialogueState, FlowExecutor, FlowTask, text_reply


@dataclass(frozen=True)
class DialogueResult:
    state: DialogueState
    route: PlanRoute
    messages: list[Reply]


class DialogueEngine:
    """把通过校验的指令应用到会话，再交给对应的 YAML 流程。"""

    def __init__(
        self, executor: FlowExecutor, planner: TurnPlanner, knowledge: KnowledgeProvider | None = None,
        zone: tzinfo | None = None, after_sales: AfterSales | None = None,
    ) -> None:
        self._executor = executor
        self._planner = planner
        self._validator = TurnPlanValidator(executor, zone)
        self._knowledge = knowledge
        self._sales = after_sales

    async def run(
        self, state: DialogueState, user_id: int, incoming: dict, claim: ReservedTurn | None = None,
    ) -> DialogueResult:
        card = incoming.get("object", {})
        order_card_without_task = (
            incoming["type"] == "object" and card.get("type") == "order" and state.active_task is None
        )
        if order_card_without_task:
            # 没有进行中的查询时先暂存这笔订单，再由校验器给出“查支付还是行程”的澄清。
            state.pending_order_id = int(card["id"])
        result = await self._dispatch(state, user_id, incoming, claim)
        if not (order_card_without_task and result.route == "clarify"):
            # 暂存的订单只承接紧随的一轮，避免过期的卡片上下文影响后面的对话。
            state.pending_order_id = None
        return result

    async def _dispatch(
        self, state: DialogueState, user_id: int, incoming: dict, claim: ReservedTurn | None = None,
    ) -> DialogueResult:
        if incoming["type"] == "text" and is_pause_request(incoming["text"]):
            return self._pause_queries(state)
        if incoming["type"] == "text" and has_multiple_requests(incoming["text"]):
            # 每轮只处理一项查询，不让模型替用户任选一项，也不丢掉已经收集的条件。
            return DialogueResult(
                state, "clarify", [text_reply("这条消息里有多个查询，请先单独发送其中一个问题。")]
            )
        if self._sales is not None and claim is not None:
            result = await self._sales_turn(self._sales, state, user_id, incoming, claim)
            if result is not None:
                return result
        active = state.active_task
        if active is not None and not self._can_resume(active):
            state.active_task = None
            messages = [text_reply("之前使用的流程已更新，请重新发起这项查询。")]
            messages.extend(self._restore_previous(state))
            return DialogueResult(state, "clarify", messages)

        try:
            if incoming["type"] == "object":
                checked = self._validator.check_order_card(state, incoming["object"]["id"])
            else:
                text = incoming["text"]
                step = self._executor.waiting_step(active) if active is not None else None
                waiting = step is not None and step.slot == "order_ref"
                plan = await self._planner.plan(text, state, waiting_for_order=waiting)
                checked = self._validator.check_text(plan, state, text)
        except PlanRejectedError as error:
            return DialogueResult(state, "clarify", [text_reply(str(error))])

        if checked.route == "chitchat":
            return DialogueResult(
                state, "chitchat", [text_reply("我在。可以查订单、行程、旅游商品，也可以问平台使用规则。")]
            )
        if checked.route == "knowledge":
            if self._knowledge is None:
                return DialogueResult(state, "knowledge", [text_reply("当前没有可用的知识资料。")])
            # 知识问题单独回答，不消耗或替换正在等待的订单、行程、商品任务。
            route, answer = self._knowledge.answer(incoming["text"])
            return DialogueResult(state, route, [text_reply(answer)])
        if checked.route == "clarify":
            prompt = (
                self._executor.resume_prompt(active) if active else None
            )
            prompt = prompt or "你想查订单、行程、旅游商品，还是平台使用规则？"
            return DialogueResult(state, "clarify", [text_reply(prompt)])

        task = checked.task
        if checked.command == "start_flow" and checked.flow_id is not None:
            task = task or self._executor.start(checked.flow_id)
            messages = self._activate(state, task)
            return await self._run_flow(state, task, user_id, checked.reference, messages)
        if checked.command == "set_slots" and task is not None:
            return await self._run_flow(state, task, user_id, checked.reference, [])
        if checked.command == "resume_task" and task is not None:
            if not self._can_resume(task):
                self._remove_paused(state, task)
                return DialogueResult(state, "clarify", [text_reply("旧流程已更新，请重新发起这项查询。")])
            messages = self._activate(state, task)
            messages.extend(self._resume_messages(task))
            return DialogueResult(state, "task", messages)
        if checked.command == "cancel_task" and task is not None:
            messages = [text_reply(f"已停止{FLOW_LABELS[task.flow_id]}，没有修改你的订单。")]
            if active is not None and task.task_id == active.task_id:
                state.active_task = None
                messages.extend(self._restore_previous(state))
            else:
                self._remove_paused(state, task)
            return DialogueResult(state, "task", messages)
        raise ValueError("通过校验的计划缺少执行目标")

    async def _sales_turn(
        self, sales: AfterSales, state: DialogueState, user_id: int, incoming: dict, claim: ReservedTurn,
    ) -> DialogueResult | None:
        active = state.active_task
        obj = incoming.get("object", {})
        text = incoming.get("text", "").strip()
        if obj.get("type") == "confirmation":
            confirmation_id = UUID(obj["id"])
            replay = await sales.replay(user_id, claim, confirmation_id, obj["decision"])
            if replay is not None:
                # 旧确认只回放自己的回执，不能结束另一个活动任务。
                if active and active.sales and active.sales.confirmation and active.sales.confirmation.id == confirmation_id:
                    return self._finish_sales(state, replay)
                return DialogueResult(state, "task", replay.messages)
            if active is None or not sales.supports(active.flow_id):
                return DialogueResult(state, "clarify", [text_reply("这张确认卡已失效或不属于当前会话。")])

        if match := REFUND_STATUS.fullmatch(text):
            return DialogueResult(state, "task", await sales.status(user_id, claim, match[1].strip()))
        flow_id = sales_intent(text)
        # “投诉”既可以发起工单，也可以是在回答工单类型；后者不能重开流程。
        choosing_type = active is not None and active.flow_id == TICKET and active.step_id == "ticket_type"
        if flow_id and not (choosing_type and text.rstrip("。！!") == "投诉"):
            existing = next((task for task in [active, *reversed(state.paused_tasks)]
                             if task is not None and task.flow_id == flow_id and sales.can_resume(task)), None)
            started = sales.resume(existing) if existing else sales.start(flow_id)
            messages = self._activate(state, started.task)
            return DialogueResult(state, "task", messages + started.messages)
        if active is None or not sales.supports(active.flow_id) or not sales.can_resume(active):
            return None
        reads = re.match(r"^(?:(?:先|再|顺便|另外|请|帮我|我想)\s*)*(?:查|看)", text)
        if explicit_control(text) is not None or (reads and (ORDER_QUERY.search(text) or ITINERARY_QUERY.search(text))):
            return None
        # 原因和投诉描述留在客服服务内，不交给外部模型当意图样本。
        return self._finish_sales(state, await sales.process(user_id, active, incoming, claim))

    def _finish_sales(self, state: DialogueState, result) -> DialogueResult:
        state.active_task = result.task
        messages = result.messages
        if result.task is None:
            messages = messages + self._restore_previous(state)
        return DialogueResult(state, "task", messages)

    def _can_resume(self, task: FlowTask) -> bool:
        if self._sales and self._sales.supports(task.flow_id):
            return self._sales.can_resume(task)
        return self._executor.can_resume(task)

    @staticmethod
    def _invalidate_confirmation(task: FlowTask) -> None:
        if task.sales and task.step_id == "confirm":
            task.sales.confirmation = None
            task.step_id = "review"

    def _pause_queries(self, state: DialogueState) -> DialogueResult:
        # 会话最多只有一个活动任务。放回暂停栈并保留条件，不触发商品查询或自动恢复。
        if task := state.active_task:
            self._remove_paused(state, task)
            state.paused_tasks.append(task)
            state.active_task = None
        for paused in state.paused_tasks:
            paused.paused_by_user = True
            self._invalidate_confirmation(paused)
        count = len(state.paused_tasks)
        text = (
            f"{count} 项查询已暂停。想接着查时，可以说「继续上一个任务」。"
            if count else "当前没有可以暂停的查询。"
        )
        return DialogueResult(state, "task", [text_reply(text)])

    async def _run_flow(
        self, state: DialogueState, task: FlowTask, user_id: int, reference: SlotValue | None, messages: list[Reply]
    ) -> DialogueResult:
        result = await self._executor.run(task, user_id, reference)
        state.active_task = result.task
        if result.order_id is not None:
            state.current_order_id = result.order_id
        messages.extend(result.messages)
        if state.active_task is None:
            messages.extend(self._restore_previous(state))
        return DialogueResult(state, "task", messages)

    @staticmethod
    def _remove_paused(state: DialogueState, task: FlowTask) -> None:
        state.paused_tasks = [paused for paused in state.paused_tasks if paused.flow_id != task.flow_id]

    def _activate(self, state: DialogueState, task: FlowTask) -> list[Reply]:
        self._remove_paused(state, task)
        task.paused_by_user = False
        active = state.active_task
        messages = []
        itinerary_switch = active is not None and {active.flow_id, task.flow_id} == {ITINERARY, DATE_ITINERARY}
        if active is not None and active.flow_id != task.flow_id and not itinerary_switch:
            # 像把手头的任务放到一旁：保留 taskId 和原步骤，稍后从那里继续。
            self._invalidate_confirmation(active)
            state.paused_tasks.append(active)
            messages.append(text_reply(f"先暂停{FLOW_LABELS[active.flow_id]}。"))
        state.active_task = task
        return messages

    def _resume_messages(self, task: FlowTask) -> list[Reply]:
        messages = [text_reply(f"继续之前的{FLOW_LABELS[task.flow_id]}。")]
        if self._sales and self._sales.supports(task.flow_id):
            return messages + self._sales.resume(task).messages
        if prompt := self._executor.resume_prompt(task):
            messages.append(text_reply(prompt))
        return messages

    def _restore_previous(self, state: DialogueState) -> list[Reply]:
        messages = []
        for previous in reversed(state.paused_tasks.copy()):
            if previous.paused_by_user:
                continue
            self._remove_paused(state, previous)
            if self._can_resume(previous):
                state.active_task = previous
                # 只恢复状态并提示，不把第二项任务的订单号拿去执行第一项。
                return messages + self._resume_messages(previous)
            messages.append(text_reply("一项暂停的旧查询已失效，可以重新发起。"))
        return messages
