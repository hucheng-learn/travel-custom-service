import re
from dataclasses import dataclass
from datetime import date, tzinfo

from plan.fixed_order_plan import CALENDAR_DATE, CURRENT_ORDER_WORDS, explicit_control, is_stay_date_question
from plan.models import (
    DATE_ITINERARY, ITINERARY, PRODUCT, SLOT_FOR_FLOW, Command, FlowId, OrderReference, PlanRoute, SlotValue, TurnPlan,
)
from plan.product_models import DateRequest, ProductClues, ProductRequest
from plan.travel_dates import business_zone, calendar_today, parse_date_text
from plan.travel_plans import hotel_filters
from task.flow.executor import DialogueState, FlowExecutor, FlowTask


class PlanRejectedError(ValueError):
    """计划不符合当前会话，直接给用户澄清提示，不发起查询。"""


@dataclass(frozen=True)
class CheckedTurn:
    route: PlanRoute
    command: Command | None = None
    flow_id: FlowId | None = None
    task: FlowTask | None = None
    reference: SlotValue | None = None


class TurnPlanValidator:
    def __init__(self, executor: FlowExecutor, zone: tzinfo | None = None) -> None:
        self._executor = executor
        self._zone = zone or business_zone("Asia/Shanghai")

    def check_text(self, plan: TurnPlan, state: DialogueState, text: str) -> CheckedTurn:
        has_reference = any(value is not None for value in (plan.order_number, plan.date_text, plan.product)) or plan.use_current_order
        if plan.route != "task":
            if plan.command or plan.flow_id or plan.task_id or has_reference:
                raise PlanRejectedError("没能确定你的查询目标，请再说具体一点。")
            return CheckedTurn(plan.route)

        if plan.command == "start_flow":
            if plan.flow_id is None or not self._executor.has_flow(plan.flow_id) or plan.task_id:
                raise PlanRejectedError("目前可以查订单支付或指定订单的行程。你想查哪项？")
            target = self._find_task(state, plan)
            if target is not None and not self._executor.can_resume(target):
                target = None  # 明确新起同类查询时，可以不用旧版本的任务。
            reference = self._reference_for(plan, plan.flow_id, state, text, target)
            if target is not None and reference is not None:
                self._check_slot(target, reference)
            return CheckedTurn("task", "start_flow", plan.flow_id, target, reference)

        if plan.command == "set_slots":
            task = state.active_task
            if task is None or plan.task_id or plan.flow_id not in {None, task.flow_id}:
                raise PlanRejectedError("当前没有能够补充这条信息的任务，请先说明要查什么。")
            reference = self._reference_for(plan, task.flow_id, state, text, task)
            if reference is None:
                raise PlanRejectedError(self._executor.resume_prompt(task) or "请补充要查询的条件。")
            self._check_slot(task, reference)
            return CheckedTurn("task", "set_slots", task=task, reference=reference)

        if plan.command in {"resume_task", "cancel_task"} and not has_reference:
            # 取消必须来自明确的本地控制指令，避免把“取消预订”误当成本会话取消。
            if plan.command == "cancel_task" and explicit_control(text) != plan:
                raise PlanRejectedError("我不能取消预订。若要停止客服查询，请说「取消当前任务」。")
            if plan.command == "resume_task" and not re.search(r"继续|恢复|接着|回到", text):
                raise PlanRejectedError("如果要继续之前的查询，请说「继续上一个任务」。")
            if plan.task_id is not None and str(plan.task_id).casefold() not in text.casefold():
                raise PlanRejectedError("请在这条消息里给出要操作的任务 ID。")
            target = self._find_task(state, plan)
            if target is None:
                raise PlanRejectedError("没有找到可以操作的查询任务。")
            return CheckedTurn("task", plan.command, task=target)

        raise PlanRejectedError("没能确定要执行哪项查询，请再说具体一点。")

    def check_order_card(self, state: DialogueState, order_id: str) -> CheckedTurn:
        task = state.active_task
        if task is None:
            raise PlanRejectedError("已看到订单卡片。请先说你想查订单支付，还是查这笔订单的行程。")
        # 卡片不经过模型，归属由读取订单详情时的 X-User-Id 校验。
        reference = OrderReference(kind="order_id", value=order_id)
        self._check_slot(task, reference)
        return CheckedTurn("task", "set_slots", task=task, reference=reference)

    def _check_slot(self, task: FlowTask, reference: SlotValue) -> None:
        step = self._executor.waiting_step(task)
        slot = "order_ref" if isinstance(reference, OrderReference) else (
            "travel_date" if isinstance(reference, DateRequest) else "product_request"
        )
        if step is None or step.slot != slot or SLOT_FOR_FLOW[task.flow_id] != slot:
            raise PlanRejectedError("当前步骤不需要这条信息，请先明确要查哪项。")
        existing = task.slots.get(step.slot)
        if isinstance(existing, OrderReference) and existing != reference:
            raise PlanRejectedError("这和当前任务的订单不同，请先取消当前任务，再查询另一笔订单。")

    def _reference_for(
        self, plan: TurnPlan, flow_id: FlowId, state: DialogueState, text: str, task: FlowTask | None,
    ) -> SlotValue | None:
        # 槽位要同时符合当前流程和原文；模型提供的日期、地名不能凭空使用。
        if flow_id == DATE_ITINERARY:
            if plan.product or plan.order_number is not None or plan.use_current_order:
                raise PlanRejectedError("按日期查行程请提供出行日，不要混用订单号。")
            return DateRequest(travel_date=self._parse_date(plan.date_text, text)) if plan.date_text is not None else None
        if flow_id == PRODUCT:
            if plan.order_number is not None or plan.use_current_order or plan.date_text is not None:
                raise PlanRejectedError("商品咨询请提供地区和查询日期。")
            if plan.product is None:
                return None
            previous = task.slots.get("product_request") if task else None
            return self._merge_product(plan.product, text, previous if isinstance(previous, ProductRequest) else None)
        if plan.product or plan.date_text is not None:
            raise PlanRejectedError("这项查询需要订单号或订单卡片。")
        return self._text_reference(plan, flow_id, state, text)

    def _parse_date(self, raw: str, text: str) -> date:
        if not raw or raw not in text:
            raise PlanRejectedError("请直接告诉我日期，例如「明天」或「下周一」。")
        try:
            return parse_date_text(raw, calendar_today(self._zone))
        except (ValueError, OverflowError):
            raise PlanRejectedError("日期无效，请换成正确日期，例如「2026-12-01」。") from None

    def _merge_product(self, clues: ProductClues, text: str, previous: ProductRequest | None) -> ProductRequest:
        # 换品类不沿用旧地区或日期。补同一商品的条件时保留前几轮已确认的信息。
        saved = previous or ProductRequest()
        if clues.kind and saved.kind and clues.kind != saved.kind:
            saved = ProductRequest()
        changes = {}
        if clues.kind:
            changes["kind"] = clues.kind
        if (clues.kind or saved.kind) == "hotel":
            filters = hotel_filters(text)
            # 城市和完整日期表示重新查酒店。补日期或选酒店时，沿用已确认的筛选条件。
            if clues.area and clues.date_text and clues.end_date_text:
                changes.update(hotel_star_rating=None, hotel_type=None, keyword=None)
            if filters:
                changes.update(filters, keyword=None)
        for field in ("area", "origin", "destination", "keyword"):
            value = getattr(clues, field)
            if value is not None:
                if not value.strip() or value not in text:
                    raise PlanRejectedError("请在消息里写明查询地点或商品名称。")
                changes[field] = value.strip()
        if clues.date_text is not None:
            changes["business_date"] = self._parse_date(clues.date_text, text)
        if clues.end_date_text is not None:
            changes["end_date"] = self._parse_date(clues.end_date_text, text)
        if clues.detail:
            changes["detail"] = True
        if clues.quote:
            changes["quote"] = True
        return saved.model_copy(update=changes)

    @staticmethod
    def _find_task(state: DialogueState, plan: TurnPlan) -> FlowTask | None:
        active = state.active_task
        tasks = [active] if active else []
        tasks.extend(reversed(state.paused_tasks))
        if plan.task_id is not None or plan.flow_id is not None:
            return next((task for task in tasks if
                         (plan.task_id is None or task.task_id == plan.task_id)
                         and (plan.flow_id is None or task.flow_id == plan.flow_id)), None)
        if plan.command == "resume_task" and state.paused_tasks:
            return state.paused_tasks[-1]
        return active

    @staticmethod
    def _text_reference(plan: TurnPlan, flow_id: FlowId, state: DialogueState, text: str) -> OrderReference | None:
        if plan.use_current_order:
            # 省略订单的入住日期追问只能引用已验证的行程订单，其他查询仍需明确指代。
            refers_to_order = CURRENT_ORDER_WORDS.search(text) or (flow_id == ITINERARY and is_stay_date_question(text))
            if plan.order_number is not None or state.current_order_id is None or not refers_to_order:
                raise PlanRejectedError("还不能确定你指的是哪一笔订单，请发订单号或订单卡片。")
            return OrderReference(kind="order_id", value=str(state.current_order_id))
        number = plan.order_number
        if number is None:
            return None
        # 模型猜出来的编号不能用：核对它是原文中的完整片段，不能是另一个编号的子串。
        token = rf"(?<![A-Za-z0-9_-]){re.escape(number)}(?![A-Za-z0-9_-])"
        if not number or CALENDAR_DATE.fullmatch(number) or not re.search(token, text):
            raise PlanRejectedError("请发这笔订单的完整订单号，或发送订单卡片。")
        try:
            return OrderReference(kind="order_no", value=number)
        except ValueError:
            raise PlanRejectedError("订单号格式不正确，请核对后再发。") from None
