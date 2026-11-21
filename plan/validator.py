from dataclasses import dataclass, field
from datetime import date, tzinfo

from plan.input_values import CALENDAR_DATE, amount_from_source, is_original_token
from plan.models import (
    DATE_ITINERARY, PRODUCT, REFUND, TICKET, SLOT_FOR_FLOW, Command, FlowId, OrderReference, PlanRoute, SlotValue, TurnPlan,
)
from plan.product_models import DateRequest, ProductClues, ProductRequest
from plan.travel_dates import CHINESE_DATE, FULL_DATE, business_zone, calendar_today, parse_date_text
from task.flow.executor import DialogueState, FlowExecutor, FlowTask
from task.refunds import money


class PlanRejectedError(ValueError):
    """候选计划无法通过数据、来源或任务范围校验，交由引擎澄清。"""


@dataclass(frozen=True)
class CheckedTurn:
    route: PlanRoute
    command: Command | None = None
    flow_id: FlowId | None = None
    task: FlowTask | None = None
    reference: SlotValue | None = None
    sales_changes: dict = field(default_factory=dict)
    clear_field: str | None = None
    pause_all: bool = False
    refund_search: str = ""
    knowledge_topic: str = "general"
    clarify_reason: str | None = None


class TurnPlanValidator:
    # 技术设计 3.2：验证可执行范围与原文依据，不再要求用户命中“继续/取消”等固定词句。
    # 模型负责语义；代码负责类型、编号边界、金额、任务状态与后续中台归属核验。
    def __init__(self, executor: FlowExecutor, zone: tzinfo | None = None, after_sales=None) -> None:
        self._executor = executor
        self._zone = zone or business_zone("Asia/Shanghai")
        self._sales = after_sales

    @staticmethod
    def _source(raw: str | None, text: str) -> None:
        if not raw or not raw.strip() or raw not in text:
            raise PlanRejectedError("没能核对这条信息来自你的哪段话，请再说具体一点。")

    def check_text(self, plan: TurnPlan, state: DialogueState, text: str) -> CheckedTurn:
        payload = any((plan.order_number is not None, plan.use_current_order, plan.date_text is not None,
                       plan.date_value is not None, plan.product is not None, plan.sales is not None))
        if plan.route != "task":
            if plan.command or plan.flow_id or plan.task_id or payload or plan.clear_field or plan.pause_all or plan.refund_lookup:
                raise PlanRejectedError("没有确定唯一的处理目标，请再说具体一点。")
            if plan.knowledge_topic and plan.route != "knowledge":
                raise PlanRejectedError("知识分类与本轮处理目标不一致。")
            if plan.clarify_reason and plan.route != "clarify":
                raise PlanRejectedError("澄清原因与本轮处理目标不一致。")
            return CheckedTurn(plan.route, knowledge_topic=plan.knowledge_topic or "general", clarify_reason=plan.clarify_reason)
        if plan.knowledge_topic or plan.clarify_reason:
            raise PlanRejectedError("任务计划不能混入问答或澄清字段。")
        if plan.pause_all and plan.command != "pause_task":
            raise PlanRejectedError("当前命令不能暂停所有任务。")
        if plan.clear_field and plan.command != "update_slots":
            raise PlanRejectedError("清除字段必须是修改当前任务。")
        if plan.refund_lookup and plan.command != "refund_status":
            raise PlanRejectedError("退款进度条件不能用于其他命令。")

        if plan.command == "refund_status":
            if self._sales is None or payload or plan.task_id or plan.flow_id:
                raise PlanRejectedError("退款进度条件无效。")
            lookup = plan.refund_lookup
            if lookup is None:
                return CheckedTurn("task", "refund_status")
            refs = [lookup.order_number, lookup.request_id, lookup.request_no]
            if sum(value is not None for value in refs) > 1 or (lookup.item_id and not lookup.order_number):
                raise PlanRejectedError("请一次指定一个订单或退款申请。")
            for value in [*refs, lookup.item_id]:
                if value is not None and not is_original_token(value, text):
                    raise PlanRejectedError("请给出原文中的完整订单号或申请编号。")
            for value in [lookup.request_id, lookup.item_id]:
                if value is not None and not (value.isascii() and value.isdecimal() and 0 < int(value) < 2**63):
                    raise PlanRejectedError("申请或明细 ID 无效。")
            if lookup.order_number and CALENDAR_DATE.fullmatch(lookup.order_number):
                raise PlanRejectedError("日期不是订单号。")
            search = lookup.request_id or lookup.request_no or ""
            if lookup.order_number:
                search = "订单号 " + lookup.order_number + (" 明细 " + lookup.item_id if lookup.item_id else "")
            return CheckedTurn("task", "refund_status", refund_search=search)

        if plan.command in {"resume_task", "cancel_task", "pause_task", "preview"}:
            if payload:
                raise PlanRejectedError("任务控制与补充条件请分别表达。")
            self._source(plan.intent_text, text)
            target = self._find_task(state, plan)
            if target is None:
                raise PlanRejectedError("没有找到可以操作的任务。")
            if plan.command == "preview" and (target is not state.active_task or target.sales is None):
                raise PlanRejectedError("请先恢复要核对的售后任务。")
            return CheckedTurn("task", plan.command, task=target, pause_all=plan.pause_all)

        if plan.command == "start_flow":
            if plan.task_id or plan.flow_id is None or not self._supported(plan.flow_id):
                raise PlanRejectedError("这项任务当前不可用，请说明要查订单、行程、商品，还是办理售后。")
            target = self._find_task(state, plan)
            if target is not None and not self._can_resume(target):
                target = None
            if plan.flow_id in {REFUND, TICKET}:
                changes = self._sales_changes(plan, plan.flow_id, state, text, target)
                return CheckedTurn("task", "start_flow", plan.flow_id, target, sales_changes=changes)
            if plan.sales:
                raise PlanRejectedError("只读查询不能携带售后资料。")
            reference = self._reference_for(plan, plan.flow_id, state, text, target)
            if target is not None and reference is not None:
                self._check_slot(target, reference, allow_update=False)
            return CheckedTurn("task", "start_flow", plan.flow_id, target, reference)

        if plan.command in {"set_slots", "update_slots"}:
            task = state.active_task
            if task is None or plan.task_id not in {None, task.task_id} or plan.flow_id not in {None, task.flow_id}:
                raise PlanRejectedError("当前没有可补充或修改这条信息的任务。")
            if plan.command == "update_slots":
                self._source(plan.intent_text, text)
            if task.sales:
                changes = self._sales_changes(plan, task.flow_id, state, text, task)
                allowed = {"order", "item", "refund_type", "amount", "reason"} if task.flow_id == REFUND else {
                    "order", "ticket_type", "description"}
                if plan.clear_field and plan.clear_field not in allowed:
                    raise PlanRejectedError("当前售后任务没有这个字段。")
                if not changes and not plan.clear_field:
                    raise PlanRejectedError("请补充要修改或填写的具体资料。")
                return CheckedTurn("task", plan.command, task=task, sales_changes=changes, clear_field=plan.clear_field)
            if plan.sales or plan.clear_field:
                raise PlanRejectedError("查询任务不接受售后字段。")
            plan = self._correct_product_date(plan, task, text)
            reference = self._reference_for(plan, task.flow_id, state, text, task)
            if reference is None:
                raise PlanRejectedError(self._executor.resume_prompt(task) or "请补充查询条件。")
            self._check_slot(task, reference, allow_update=plan.command == "update_slots")
            return CheckedTurn("task", plan.command, task=task, reference=reference)
        raise PlanRejectedError("没有识别出可执行的命令，请再说具体一点。")

    def _correct_product_date(self, plan: TurnPlan, task: FlowTask, text: str) -> TurnPlan:
        # 只纠正“正在等商品日期、用户整句只给日期”的字段错放；不猜测意图、不接管任务切换。
        # 返回计划副本，失败时保留原任务；日期仍须经过原文、换算及商品条件的正常校验。
        saved = task.slots.get("product_request")
        if (plan.command != "set_slots" or task.flow_id != PRODUCT or task.sales is not None
                or task.missing_slots != ["business_date"] or not isinstance(saved, ProductRequest)
                or saved.business_date is not None or not plan.date_text or plan.date_text != text.strip()
                or plan.order_number is not None or plan.use_current_order or plan.intent_text is not None):
            return plan
        clues = plan.product or ProductClues()
        if clues.model_dump(exclude_none=True, exclude_defaults=True).keys() - {"date_text", "date_value"}:
            return plan
        # 兼容范围只包含可独立识别的日期，不能把“查明天的行程”等句子当成单纯补日期。
        try:
            parse_date_text(plan.date_text, calendar_today(self._zone))
        except (ValueError, OverflowError):
            return plan
        parsed = self._parse_date(plan.date_text, text, plan.date_value)
        if clues.date_value is not None and clues.date_text is None:
            raise PlanRejectedError("没能识别这次的出行日期，请再提供一次，例如“明天”或“2026-12-01”。")
        if clues.date_text is not None and self._parse_date(clues.date_text, text, clues.date_value) != parsed:
            raise PlanRejectedError("这次提供了不同的出行日期，请确认具体哪一天。")
        corrected = clues.model_copy(update={"date_text": plan.date_text, "date_value": parsed})
        return plan.model_copy(update={"date_text": None, "date_value": None, "product": corrected})

    def _supported(self, flow_id):
        return self._executor.has_flow(flow_id) or bool(self._sales and self._sales.supports(flow_id))

    def _can_resume(self, task):
        return self._sales.can_resume(task) if task.sales and self._sales else self._executor.can_resume(task)

    def _sales_changes(self, plan, flow_id, state, text, task):
        if plan.product or plan.date_text or plan.date_value:
            raise PlanRejectedError("售后资料不能混入商品或行程日期条件。")
        changes = {}
        reference = self._text_reference(plan, flow_id, state, text)
        if reference:
            changes["order"] = reference
        clues = plan.sales
        if clues:
            if clues.item_id is not None:
                if not is_original_token(clues.item_id, text) or not clues.item_id.isdecimal() or not 0 < int(clues.item_id) < 2**63:
                    raise PlanRejectedError("请给出原文中的完整订单明细 ID。")
                changes["item"] = clues.item_id
            enums = {"refund_type": {"full": "全额", "partial": "部分退款"},
                     "ticket_type": {"complaint": "投诉", "after_sales": "售后", "refund": "退款咨询"},
                     "no_order": {"true": "无订单"}}
            for name, values in enums.items():
                clue = getattr(clues, name)
                if clue:
                    self._source(clue.source, text)
                    if clue.value not in values:
                        raise PlanRejectedError("售后选项无效，请重新说明。")
                    changes["order" if name == "no_order" else name] = values[clue.value]
            if clues.no_order and reference:
                raise PlanRejectedError("不能同时指定订单和无订单。")
            if clues.amount:
                self._source(clues.amount.source, text)
                try:
                    amount = money(clues.amount.value)
                    if amount <= 0 or amount_from_source(clues.amount.source) != amount:
                        raise ValueError
                except ValueError:
                    raise PlanRejectedError("没能核对退款金额，请明确填写金额，例如 50 元。") from None
                changes["amount"] = str(amount)
            for name in ("reason", "description"):
                value = getattr(clues, name)
                if value is not None:
                    self._source(value, text)
                    changes[name] = value
        allowed = {"order", "item", "refund_type", "amount", "reason"} if flow_id == REFUND else {"order", "ticket_type", "description"}
        if changes.keys() - allowed or (flow_id == REFUND and changes.get("order") == "无订单"):
            raise PlanRejectedError("这条资料不适用于当前售后任务。")
        if changes.get("refund_type") == "全额" and "amount" in changes:
            raise PlanRejectedError("全额退款由实际可退金额计算；指定金额时请说明申请部分退款。")
        if task and plan.command != "update_slots":
            draft = task.sales
            previous = {"item": str(draft.item_id) if draft.item_id else None,
                        "refund_type": {"full": "全额", "partial": "部分退款"}.get(draft.refund_type),
                        "amount": draft.amount, "reason": draft.reason, "description": draft.description,
                        "ticket_type": {"complaint": "投诉", "after_sales": "售后", "refund": "退款咨询"}.get(draft.ticket_type)}
            for key, value in changes.items():
                if key == "order":
                    if draft.no_order and value != "无订单":
                        raise PlanRejectedError("更换订单请明确说明要修改关联订单。")
                    if draft.order_id is not None:
                        same = isinstance(value, OrderReference) and value.value == (
                            str(draft.order_id) if value.kind == "order_id" else draft.order_no)
                        if not same:
                            raise PlanRejectedError("这和当前订单不同，请明确说明要更换订单。")
                elif previous.get(key) is not None and previous[key] != value:
                    raise PlanRejectedError("这和已填写的资料不同，请明确说明要修改哪项。")
        return changes

    def check_order_card(self, state: DialogueState, order_id: str) -> CheckedTurn:
        task = state.active_task
        if task is None:
            raise PlanRejectedError("已看到订单卡片。请先说你想查订单支付，还是查这笔订单的行程。")
        reference = OrderReference(kind="order_id", value=order_id)
        self._check_slot(task, reference)
        return CheckedTurn("task", "set_slots", task=task, reference=reference)

    def _check_slot(self, task, reference, *, allow_update=False):
        step = self._executor.waiting_step(task)
        slot = "order_ref" if isinstance(reference, OrderReference) else (
            "travel_date" if isinstance(reference, DateRequest) else "product_request")
        if step is None or step.slot != slot or SLOT_FOR_FLOW[task.flow_id] != slot:
            raise PlanRejectedError("当前步骤不需要这条信息，请先明确要查哪项。")
        previous = task.slots.get(slot)
        if not allow_update and isinstance(previous, OrderReference) and previous != reference:
            raise PlanRejectedError("这和当前任务的订单不同，请明确说明要更换订单。")

    def _reference_for(self, plan, flow_id, state, text, task):
        if flow_id == DATE_ITINERARY:
            if plan.product or plan.order_number is not None or plan.use_current_order:
                raise PlanRejectedError("按日期查行程请提供出行日，不要混用订单号。")
            if plan.date_text is None and plan.date_value is not None:
                raise PlanRejectedError("归一化日期缺少原文依据。")
            return DateRequest(travel_date=self._parse_date(plan.date_text, text, plan.date_value)) if plan.date_text else None
        if flow_id == PRODUCT:
            if plan.order_number is not None or plan.use_current_order:
                raise PlanRejectedError("查景点、酒店等商品请提供地区和日期；如果要查已有订单，请说明要查订单还是行程。")
            if plan.date_text or plan.date_value:
                saved = task.slots.get("product_request") if task else None
                label = "入住" if isinstance(saved, ProductRequest) and saved.kind == "hotel" else "出行"
                raise PlanRejectedError(f"没能识别这次的{label}日期，请再提供一次，例如“明天”或“2026-12-01”。")
            previous = task.slots.get("product_request") if task else None
            return self._merge_product(plan.product, text, previous if isinstance(previous, ProductRequest) else None) if plan.product else None
        if plan.product or plan.date_text or plan.date_value:
            raise PlanRejectedError("这项查询需要订单号或订单卡片。")
        return self._text_reference(plan, flow_id, state, text)

    def _parse_date(self, raw, text, normalized=None):
        self._source(raw, text)
        try:
            parsed = parse_date_text(raw, calendar_today(self._zone))
        except (ValueError, OverflowError):
            # 不要求模型把“下个礼拜二”改成原文不存在的“下周二”；原表达与 ISO 值分开。
            # 常规日期解析器识别不了且没有明确归一化日期时，仍然追问，不默认为今天。
            absolute = FULL_DATE.search(raw) or CHINESE_DATE.search(raw)
            if absolute:
                try:
                    parsed = parse_date_text(absolute[0], calendar_today(self._zone))
                except (ValueError, OverflowError):
                    raise PlanRejectedError("日期无效，请核对具体哪一天。") from None
            elif normalized is None:
                raise PlanRejectedError("没能确定具体日期，请补充哪一天。") from None
            else:
                return normalized
        if normalized is not None and normalized != parsed:
            raise PlanRejectedError("日期换算与原文不一致，请重新说明日期。")
        return parsed

    def _merge_product(self, clues: ProductClues, text: str, previous):
        saved = previous or ProductRequest()
        if clues.kind and saved.kind and clues.kind != saved.kind:
            saved = ProductRequest()
        changes = {}
        if clues.kind:
            changes["kind"] = clues.kind
        if (clues.kind or saved.kind) == "hotel" and clues.area and clues.date_text and clues.end_date_text:
            changes.update(hotel_star_rating=None, hotel_type=None, keyword=None)
        if clues.hotel_star_rating or clues.hotel_type:
            # 改筛选条件时清除旧候选酒店名；本轮明确给了具体酒店名时，下面再使用原文覆盖。
            # 只改星级/类型仍沿用已确认的日期，避免旧快照把筛选词当酒店名继续查询。
            changes["keyword"] = None
        for name in ("area", "origin", "destination", "keyword"):
            value = getattr(clues, name)
            if value is not None:
                self._source(value, text)
                changes[name] = value.strip()
        for name, source_name in (("hotel_star_rating", "hotel_star_text"), ("hotel_type", "hotel_type_text")):
            value = getattr(clues, name)
            source = getattr(clues, source_name)
            if source is not None and value is None:
                raise PlanRejectedError("酒店筛选条件缺少对应值。")
            if value is not None:
                if (clues.kind or saved.kind) != "hotel":
                    raise PlanRejectedError("酒店筛选条件不能用于其他商品。")
                self._source(source, text)
                changes[name] = value
        for name, raw, normalized in (("business_date", clues.date_text, clues.date_value),
                                      ("end_date", clues.end_date_text, clues.end_date_value)):
            if normalized and not raw:
                raise PlanRejectedError("归一化日期缺少原文依据。")
            if raw:
                changes[name] = self._parse_date(raw, text, normalized)
        if clues.detail:
            changes["detail"] = True
        if clues.quote:
            changes["quote"] = True
        return saved.model_copy(update=changes)

    @staticmethod
    def _find_task(state, plan):
        active = state.active_task
        tasks = ([active] if active else []) + list(reversed(state.paused_tasks))
        if plan.task_id is not None or plan.flow_id is not None:
            return next((task for task in tasks if (plan.task_id is None or task.task_id == plan.task_id)
                         and (plan.flow_id is None or task.flow_id == plan.flow_id)), None)
        return state.paused_tasks[-1] if plan.command == "resume_task" and state.paused_tasks else active

    @staticmethod
    def _text_reference(plan, flow_id, state, text):
        if plan.use_current_order:
            order_id = state.pending_order_id if state.pending_order_id is not None else state.current_order_id
            if plan.order_number is not None or order_id is None:
                raise PlanRejectedError("还不能确定你指的是哪笔订单，请提供订单号或卡片。")
            # 模型识别省略与指代，代码只允许引用服务端已经保存的订单线索；依然须中台核验归属。
            return OrderReference(kind="order_id", value=str(order_id))
        number = plan.order_number
        if number is None:
            return None
        if CALENDAR_DATE.fullmatch(number) or not is_original_token(number, text):
            raise PlanRejectedError("请提供原文中的完整订单号，或发送订单卡片。")
        return OrderReference(kind="order_no", value=number)
