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
    # state 是本轮处理完要写回的会话快照，活动任务、暂停栈和订单线索都在里面。
    state: DialogueState
    # route 是本轮对外暴露的走向（task/clarify/chitchat/knowledge），前端据此区分状态。
    route: PlanRoute
    # messages 是按顺序返回给用户的文本或卡片消息。
    messages: list[Reply]


class DialogueEngine:

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
        self, state: DialogueState, user_id: int, input_data: dict, claim: ReservedTurn | None = None,
    ) -> DialogueResult:
        # input_data 是前端这一条消息的原始结构：文本是 {"type": "text", "text": "..."}，
        # 对象消息是 {"type": "object", "object": {"type": "order" | "confirmation", "id": "..."}}。
        # state 是上一轮落库的会话快照，本轮直接在上面改；claim 是本轮在数据库里的占位记录，
        # 售后确认这类写操作要靠它校验归属和防止重复提交。
        card_object = input_data.get("object", {})
        order_card_without_task = (
            input_data["type"] == "object" and card_object.get("type") == "order" and state.active_task is None
        )
        if order_card_without_task:
            # 没有进行中的查询时先暂存这笔订单，再由校验器给出“查支付还是行程”的澄清。
            state.pending_order_id = int(card_object["id"])
        result = await self._dispatch(state, user_id, input_data, claim)
        if not (order_card_without_task and result.route == "clarify"):
            # 暂存的订单只承接紧随的一轮，避免过期的卡片上下文影响后面的对话。
            state.pending_order_id = None
        return result

    async def _dispatch(
        self, state: DialogueState, user_id: int, input_data: dict, claim: ReservedTurn | None = None,
    ) -> DialogueResult:
        # state.active_task 是本轮开始前的活动任务，paused_tasks 是之前暂停的任务快照，
        # 后面的分支都围绕这两处状态判断该新开、补槽还是切换任务。
        # 「暂停所有任务」这类明确指令不经过模型，先把活动查询挂起。
        if input_data["type"] == "text" and is_pause_request(input_data["text"]):
            return self._pause_queries(state)
        if input_data["type"] == "text" and has_multiple_requests(input_data["text"]):
            # 每轮只处理一项查询，不让模型替用户任选一项，也不丢掉已经收集的条件。
            return DialogueResult(
                state, "clarify", [text_reply("这条消息里有多个查询，请先单独发送其中一个问题。")]
            )
        if self._sales is not None and claim is not None:
            # 退款、工单的收集和确认不交给模型，先让售后模块接管这一轮。
            result = await self._sales_turn(self._sales, state, user_id, input_data, claim)
            if result is not None:
                return result
        active = state.active_task
        if active is not None and not self._can_resume(active):
            # YAML/售后配置已升级（版本号变化）时旧任务不能硬跑，请用户重新发起。
            state.active_task = None
            messages = [text_reply("之前使用的流程已更新，请重新发起这项查询。")]
            messages.extend(self._restore_previous(state))
            return DialogueResult(state, "clarify", messages)

        try:
            if input_data["type"] == "object":
                # 订单卡片、确认卡这类对象消息不经过模型，直接按当前任务生成命令。
                checked = self._validator.check_order_card(state, input_data["object"]["id"])
            else:
                # 文本先交给规划器识别意图，再由校验器确认这个计划能执行。
                text = input_data["text"]
                # step 是活动任务当前停留的收集步骤；只有停在该步骤且槽位是 order_ref 时，
                # 才把 waiting 置为 True，提示规划器这轮可能是在补订单号。
                step = self._executor.waiting_step(active) if active is not None else None
                waiting = step is not None and step.slot == "order_ref"
                # plan 是模型或本地规则给出的计划，只含路线、命令和原文线索；
                # checked 是校验器补全后的结果，带上了具体任务、流程和核验过的槽位值。
                plan = await self._planner.plan(text, state, waiting_for_order=waiting)
                checked = self._validator.check_text(plan, state, text)
        except PlanRejectedError as error:
            # 计划不合法就转为澄清，不给用户发起任何查询。
            return DialogueResult(state, "clarify", [text_reply(str(error))])

        if checked.route == "chitchat":
            # 闲聊用固定话术回应，不改动正在进行的任务。
            return DialogueResult(
                state, "chitchat", [text_reply("我在。可以查订单、行程、旅游商品，也可以问平台使用规则。")]
            )
        if checked.route == "knowledge":
            if self._knowledge is None:
                return DialogueResult(state, "knowledge", [text_reply("当前没有可用的知识资料。")])
            # 知识问答单独回答，不影响正在等待的订单、行程或商品任务。
            route, answer = self._knowledge.answer(input_data["text"])
            return DialogueResult(state, route, [text_reply(answer)])
        if checked.route == "clarify":
            prompt = (
                self._executor.resume_prompt(active) if active else None
            )
            prompt = prompt or "你想查订单、行程、旅游商品，还是平台使用规则？"
            return DialogueResult(state, "clarify", [text_reply(prompt)])

        # 以下按计划的命令分别处理：新开流程、补槽位、恢复或取消任务。
        # checked.task 是校验器定位到的目标任务，checked.reference 是从本轮原文核出来的
        # 槽位值（订单引用、出行日期或商品条件），两者都可能是 None。
        task = checked.task
        if checked.command == "start_flow" and checked.flow_id is not None:
            # 启动新任务；如果已有别的活动任务，_activate 会把它压入暂停栈。
            task = task or self._executor.start(checked.flow_id)
            messages = self._activate(state, task)
            return await self._run_flow(state, task, user_id, checked.reference, messages)
        if checked.command == "set_slots" and task is not None:
            # 补上当前等待的槽位，然后从原步骤继续推进。
            return await self._run_flow(state, task, user_id, checked.reference, [])
        if checked.command == "resume_task" and task is not None:
            if not self._can_resume(task):
                self._remove_paused(state, task)
                return DialogueResult(state, "clarify", [text_reply("旧流程已更新，请重新发起这项查询。")])
            # 恢复暂停的任务，只提示下一步，不替用户执行查询。
            messages = self._activate(state, task)
            messages.extend(self._resume_messages(task))
            return DialogueResult(state, "task", messages)
        if checked.command == "cancel_task" and task is not None:
            # 只结束客服这边的查询任务，不会取消已经预订的订单。
            messages = [text_reply(f"已停止{FLOW_LABELS[task.flow_id]}，没有修改你的订单。")]
            if active is not None and task.task_id == active.task_id:
                state.active_task = None
                messages.extend(self._restore_previous(state))
            else:
                self._remove_paused(state, task)
            return DialogueResult(state, "task", messages)
        raise ValueError("通过校验的计划缺少执行目标")

    async def _sales_turn(
        self, sales: AfterSales, state: DialogueState, user_id: int, input_data: dict, claim: ReservedTurn,
    ) -> DialogueResult | None:
        # active 是当前客服任务；obj 和 text 分别来自对象消息和文本消息，二者最多只有一个有内容。
        active = state.active_task
        obj = input_data.get("object", {})
        text = input_data.get("text", "").strip()
        if obj.get("type") == "confirmation":
            # 确认卡只回传 id 和 decision，真正要提交的目标保存在服务端任务草稿里。
            confirmation_id = UUID(obj["id"])
            # replay 不为空说明这张卡之前已经处理过，只回放旧回执，不再执行写操作。
            replay = await sales.replay(user_id, claim, confirmation_id, obj["decision"])
            if replay is not None:
                # 旧确认只回放自己的回执，不能结束另一个活动任务。
                if active and active.sales and active.sales.confirmation and active.sales.confirmation.id == confirmation_id:
                    return self._finish_sales(state, replay)
                return DialogueResult(state, "task", replay.messages)
            if active is None or not sales.supports(active.flow_id):
                return DialogueResult(state, "clarify", [text_reply("这张确认卡已失效或不属于当前会话。")])

        if match := REFUND_STATUS.fullmatch(text):
            # match[1] 是用户附带的申请编号、订单号或空字符串，空表示查最近的退款申请。
            return DialogueResult(state, "task", await sales.status(user_id, claim, match[1].strip()))
        flow_id = sales_intent(text)
        # choosing_type 表示当前正停在工单类型步骤，用户回“投诉”是在填槽位，不能重新开一个工单。
        choosing_type = active is not None and active.flow_id == TICKET and active.step_id == "ticket_type"
        if flow_id and not (choosing_type and text.rstrip("。！!") == "投诉"):
            # existing 是同类型的旧任务：优先活动任务，其次最近的暂停任务，能恢复就恢复，避免重复新建。
            existing = next((task for task in [active, *reversed(state.paused_tasks)]
                             if task is not None and task.flow_id == flow_id and sales.can_resume(task)), None)
            started = sales.resume(existing) if existing else sales.start(flow_id)
            messages = self._activate(state, started.task)
            return DialogueResult(state, "task", messages + started.messages)
        if active is None or not sales.supports(active.flow_id) or not sales.can_resume(active):
            return None
        # reads 判断这句是不是“先查订单”“看一下行程”这类查询开头；是的话交回主流程，售后不抢这一轮。
        reads = re.match(r"^(?:(?:先|再|顺便|另外|请|帮我|我想)\s*)*(?:查|看)", text)
        if explicit_control(text) is not None or (reads and (ORDER_QUERY.search(text) or ITINERARY_QUERY.search(text))):
            return None
        # 原因和投诉描述留在客服服务内，不交给外部模型当意图样本。
        return self._finish_sales(state, await sales.process(user_id, active, input_data, claim))

    def _finish_sales(self, state: DialogueState, result) -> DialogueResult:
        # result.task 为 None 表示售后任务已经结束（提交成功、拒绝或取消），这时可以恢复之前暂停的查询。
        state.active_task = result.task
        messages = result.messages
        if result.task is None:
            messages = messages + self._restore_previous(state)
        return DialogueResult(state, "task", messages)

    def _can_resume(self, task: FlowTask) -> bool:
        # 售后任务用配置内容算出的版本号，普通任务用 YAML 版本号，两边都会拦下过期任务。
        if self._sales and self._sales.supports(task.flow_id):
            return self._sales.can_resume(task)
        return self._executor.can_resume(task)

    @staticmethod
    def _invalidate_confirmation(task: FlowTask) -> None:
        # 任务被切走或暂停后旧确认卡不再有效，退回 review 步骤，恢复时重新生成。
        if task.sales and task.step_id == "confirm":
            task.sales.confirmation = None
            task.step_id = "review"

    def _pause_queries(self, state: DialogueState) -> DialogueResult:
        # 会话最多只有一个活动任务。task 是当前活动任务；放进暂停栈后 active_task 清空，
        # paused_by_user 置为 True，表示只有用户明确说“继续”才会恢复。
        if task := state.active_task:
            self._remove_paused(state, task)
            state.paused_tasks.append(task)
            state.active_task = None
        for paused in state.paused_tasks:
            paused.paused_by_user = True
            # 主动暂停期间确认卡可能过期，先作废，恢复时重新生成。
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
        # reference 是本轮要补进任务的槽位值；messages 是调用前已经产生的提示，
        # 例如“先暂停某项任务”。
        result = await self._executor.run(task, user_id, reference)
        # result.task 为 None 表示流程走到 end；order_id 是这轮查到的订单，留给后续追问引用。
        state.active_task = result.task
        if result.order_id is not None:
            state.current_order_id = result.order_id
        messages.extend(result.messages)
        if state.active_task is None:
            messages.extend(self._restore_previous(state))
        return DialogueResult(state, "task", messages)

    @staticmethod
    def _remove_paused(state: DialogueState, task: FlowTask) -> None:
        # 同一种流程只保留一个任务，切换或新建时把旧的同类任务从暂停栈里清掉。
        state.paused_tasks = [paused for paused in state.paused_tasks if paused.flow_id != task.flow_id]

    def _activate(self, state: DialogueState, task: FlowTask) -> list[Reply]:
        # 把 task 设为活动任务，并把它从暂停栈里移除；messages 收集“先暂停某项任务”的提示。
        self._remove_paused(state, task)
        task.paused_by_user = False
        active = state.active_task
        messages = []
        # 两种行程查询只是查询方式不同，互相切换时不算换任务，不压暂停栈。
        itinerary_switch = active is not None and {active.flow_id, task.flow_id} == {ITINERARY, DATE_ITINERARY}
        if active is not None and active.flow_id != task.flow_id and not itinerary_switch:
            # 像把手头的任务放到一旁：保留 taskId 和原步骤，稍后从那里继续。
            self._invalidate_confirmation(active)
            state.paused_tasks.append(active)
            messages.append(text_reply(f"先暂停{FLOW_LABELS[active.flow_id]}。"))
        state.active_task = task
        return messages

    def _resume_messages(self, task: FlowTask) -> list[Reply]:
        # 售后任务用配置里的下一步提示，普通任务用流程当前收集步骤的追问。
        messages = [text_reply(f"继续之前的{FLOW_LABELS[task.flow_id]}。")]
        if self._sales and self._sales.supports(task.flow_id):
            return messages + self._sales.resume(task).messages
        if prompt := self._executor.resume_prompt(task):
            messages.append(text_reply(prompt))
        return messages

    def _restore_previous(self, state: DialogueState) -> list[Reply]:
        # 按后进先出遍历暂停栈，跳过用户主动暂停的任务；恢复一项就停下，避免一次自动恢复多项。
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
