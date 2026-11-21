from dataclasses import dataclass
from datetime import tzinfo
from uuid import UUID

from knowledge.retrieval import KnowledgeProvider
from plan.models import DATE_ITINERARY, FLOW_LABELS, ITINERARY, REFUND, TICKET, PlanRoute, SlotValue
from plan.turn_planner import TurnPlanner
from plan.validator import PlanRejectedError, TurnPlanValidator
from repository.dialogue import ReservedTurn
from task.action.order_status import Reply
from task.after_sales import AfterSales
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
    # 对应技术设计 3.1～3.3：按输入分流，串起规划、校验、执行，再更新会话任务状态。
    # 查询交给 FlowExecutor，退款/工单交给 AfterSales；本层结果由 SupportService 统一落库。

    def __init__(
        self, executor: FlowExecutor, planner: TurnPlanner, knowledge: KnowledgeProvider | None = None,
        zone: tzinfo | None = None, after_sales: AfterSales | None = None,
    ) -> None:
        self._executor = executor
        self._planner = planner
        self._validator = TurnPlanValidator(executor, zone, after_sales)
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
            # 3.7 只发卡片、没说用途：先暂存 pending_order_id，再追问查支付还是行程。
            # 此时还没读订单详情，订单归属要等实际查询时核验。
            state.pending_order_id = int(card_object["id"])
        result = await self._dispatch(state, user_id, input_data, claim)
        if not (order_card_without_task and result.route == "clarify"):
            # 暂存的订单只承接紧随的一轮，避免过期的卡片上下文影响后面的对话。
            state.pending_order_id = None
        return result

    async def _dispatch(
        self, state: DialogueState, user_id: int, input_data: dict, claim: ReservedTurn | None = None,
    ) -> DialogueResult:
        # 技术设计 3.1：卡片已经有类型和ID，直接走卡片校验；所有普通文本统一进入模型。
        # 退款原因、金额补充或插入查订单，也不再由售后模块提前抢走本轮输入。
        if input_data["type"] == "object":
            return await self._object_turn(state, user_id, input_data, claim)
        active = state.active_task
        if active is not None and not self._can_resume(active):
            state.active_task = None
            return DialogueResult(state, "clarify", [text_reply("之前使用的流程已更新，请重新发起这项查询。")]
                                  + self._restore_previous(state))
        text = input_data["text"]
        try:
            question = None
            if active:
                question = (self._sales.waiting_prompt(active) if active.sales and self._sales else
                            self._executor.resume_prompt(active.model_copy(deep=True)))
            plan = await self._planner.plan(text, state, question=question)
            checked = self._validator.check_text(plan, state, text)
        except PlanRejectedError as error:
            return DialogueResult(state, "clarify", [text_reply(str(error))])

        if checked.route == "chitchat":
            return DialogueResult(state, "chitchat", [text_reply("我在。可以查订单、行程、旅游商品，也可以问平台使用规则。")])
        if checked.route == "knowledge":
            if self._knowledge is None:
                return DialogueResult(state, "knowledge", [text_reply("当前没有可用的知识资料。")])
            route, answer = self._knowledge.answer(text, topic=checked.knowledge_topic)
            return DialogueResult(state, route, [text_reply(answer)])
        if checked.route == "clarify":
            # 澄清是有效的理解结果，不能再用关键词覆盖它；降级时说明能力限制，状态保持不变。
            prompts = {
                "multiple": "这条消息里有多个独立请求，请先选一项处理。",
                "model_unavailable": "文字理解服务暂时不可用，请稍后重试。也可以直接发送订单卡片或使用已有确认卡。",
                "invalid_model_output": "这次没能可靠理解你的意思，请再说具体一点；也可以直接发送订单卡片。",
            }
            return DialogueResult(state, "clarify", [text_reply(prompts.get(checked.clarify_reason,
                "还不能确定你的意思，请说明这次要查询、补充或修改什么。"))])

        task = checked.task
        if checked.command == "refund_status":
            if self._sales is None or claim is None:
                return DialogueResult(state, "clarify", [text_reply("当前无法查询退款进度。")])
            return DialogueResult(state, "task", await self._sales.status(user_id, claim, checked.refund_search))
        if checked.command == "start_flow":
            if checked.flow_id in {REFUND, TICKET}:
                if self._sales is None or claim is None:
                    return DialogueResult(state, "clarify", [text_reply("当前无法办理售后，请稍后重试。")])
                if task:
                    self._invalidate_confirmation(task)
                started = self._sales.resume(task) if task else self._sales.start(checked.flow_id)
                messages = self._activate(state, started.task)
                if checked.sales_changes:
                    result = await self._sales.apply(user_id, started.task, checked.sales_changes)
                    finished = self._finish_sales(state, result)
                    return DialogueResult(state, "task", messages + finished.messages)
                return DialogueResult(state, "task", messages + started.messages)
            task = task or self._executor.start(checked.flow_id)
            return await self._run_flow(state, task, user_id, checked.reference, self._activate(state, task))
        if checked.command in {"set_slots", "update_slots"}:
            if task.sales:
                if self._sales is None or claim is None:
                    return DialogueResult(state, "clarify", [text_reply("当前无法办理售后，请稍后重试。")])
                result = await self._sales.apply(user_id, task, checked.sales_changes,
                    replace=checked.command == "update_slots", clear_field=checked.clear_field)
                return self._finish_sales(state, result)
            return await self._run_flow(state, task, user_id, checked.reference, [])
        if checked.command == "preview":
            return self._finish_sales(state, await self._sales.preview(user_id, task))
        if checked.command == "resume_task":
            if not self._can_resume(task):
                self._remove_paused(state, task)
                return DialogueResult(state, "clarify", [text_reply("旧流程已更新，请重新发起。")])
            messages = self._activate(state, task)
            return DialogueResult(state, "task", messages + self._resume_messages(task))
        if checked.command == "pause_task":
            if checked.pause_all:
                return self._pause_queries(state)
            if task is state.active_task:
                state.active_task = None
                self._remove_paused(state, task)
                state.paused_tasks.append(task)
            task.paused_by_user = True
            self._invalidate_confirmation(task)
            return DialogueResult(state, "task", [text_reply(f"已暂停{FLOW_LABELS[task.flow_id]}，需要时可以要求继续。")])
        if checked.command == "cancel_task":
            # 停止的是客服任务，不触发取消预订或退款接口；恢复最近的非主动暂停任务。
            self._invalidate_confirmation(task)
            messages = [text_reply(f"已停止{FLOW_LABELS[task.flow_id]}。")]
            if task is state.active_task:
                state.active_task = None
                messages.extend(self._restore_previous(state))
            else:
                self._remove_paused(state, task)
            return DialogueResult(state, "task", messages)
        raise ValueError("通过校验的计划缺少执行目标")

    async def _object_turn(self, state, user_id, incoming, claim):
        obj = incoming["object"]
        active = state.active_task
        sales = self._sales
        if obj.get("type") == "confirmation":
            if sales is None or claim is None:
                return DialogueResult(state, "clarify", [text_reply("这张确认卡当前不可用。")])
            replay = await sales.replay(user_id, claim, UUID(obj["id"]), obj["decision"])
            if replay is not None:
                if active and active.sales and active.sales.confirmation and str(active.sales.confirmation.id) == obj["id"]:
                    return self._finish_sales(state, replay)
                return DialogueResult(state, "task", replay.messages)
            if active is None or active.sales is None or not sales.can_resume(active):
                return DialogueResult(state, "clarify", [text_reply("这张确认卡已失效或不属于当前任务。")])
            return self._finish_sales(state, await sales.process(user_id, active, incoming, claim))
        if active and not self._can_resume(active):
            state.active_task = None
            return DialogueResult(state, "clarify", [text_reply("之前的流程已更新，请重新发起。")]
                                  + self._restore_previous(state))
        if active and active.sales and sales and claim:
            return self._finish_sales(state, await sales.process(user_id, active, incoming, claim))
        try:
            checked = self._validator.check_order_card(state, obj["id"])
        except PlanRejectedError as error:
            return DialogueResult(state, "clarify", [text_reply(str(error))])
        return await self._run_flow(state, checked.task, user_id, checked.reference, [])


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
        # 3.1/3.7：缺槽时保留任务和等待位置；走到 end 则清空活动任务。
        # 只有 Action 实际查成功带回的 order_id 才记为当前订单，供“具体哪天入住”追问使用。
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
        # 3.3 四轮切换示例的第二轮：查订单 A 时插入查行程 B，保留 A 的 task_id、
        # step_id 和槽位并放入暂停栈，再把 B 设为活动任务；未完成的售后确认须先作废。
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
        # 3.3 第三轮：行程 B 完成后恢复最近一个可继续的 A；跳过主动暂停的任务。
        # 只恢复状态并提示 A 还缺什么，不把刚才给 B 的卡片再次交给执行器。
        # 所以本例必须在第四轮再发一次订单卡片，才能完成 A 的支付查询。
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
