from dataclasses import dataclass
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from integration.regions import AreaCatalog
from plan.models import SLOT_FOR_FLOW, FlowId, SlotValue
from plan.product_models import ProductRequest
from task.action.date_itinerary import DateItineraryAction
from task.action.order_status import ActionResult, OrderQueryAction, Reply
from task.action.products import ProductAction, check_product
from task.flow.loader import ActionStep, CollectStep, EndStep, LoadedFlow, ResponseStep, StartStep
from task.sales_state import SalesDraft


MAX_STEPS_PER_TURN = 24
# 给旅客说明查到了什么及下一步怎么做，区分没有匹配结果和暂时无法查询。
TEXT_TEMPLATES = {
    "order_not_found": "没在你的订单里查到这个订单号，请核对后再发一次。",
    "order_search_limited": "这次只查了最近一部分订单，还不能确定是否存在。你可以核对订单号后再试。",
    "order_unavailable": "暂时无法查询订单或支付状态。请稍后再发一次订单号。",
    "itinerary_unavailable": "暂时无法查询行程。请稍后再发一次订单号或订单卡片。",
    "date_itinerary_unavailable": "暂时没能完整读取订单行程。请稍后再发查询日期，或提供订单号。",
    "product_not_found": "没有查到符合这些条件的可订商品。可以换日期或补充条件再查。",
    "product_unavailable": "暂时无法查询商品，请稍后再试。",
}
RESULT_TEMPLATES = {"order_status", "order_itinerary", "date_itinerary", "product_result", "product_options"}
TEMPLATES = set(TEXT_TEMPLATES) | RESULT_TEMPLATES


class FlowTask(BaseModel):
    task_id: UUID = Field(default_factory=uuid4)
    flow_id: FlowId
    flow_version: str
    step_id: str
    slots: dict[str, SlotValue] = Field(default_factory=dict)
    missing_slots: list[str] | None = None
    # 主动暂停与切换任务时的临时暂停不同：主动暂停需要用户明确恢复。
    paused_by_user: bool = False
    sales: SalesDraft | None = None


class DialogueState(BaseModel):
    # 新字段有默认值，之前阶段一只保存 active_task 的会话仍能读出来。
    active_task: FlowTask | None = None
    paused_tasks: list[FlowTask] = Field(default_factory=list)
    current_order_id: int | None = None


@dataclass(frozen=True)
class FlowResult:
    task: FlowTask | None
    messages: list[Reply]
    order_id: int | None = None


def text_reply(text: str) -> Reply:
    return {"type": "text", "text": text}


class FlowExecutor:
    def __init__(
        self, flows: dict[str, LoadedFlow], actions: dict[str, OrderQueryAction | DateItineraryAction | ProductAction],
        areas: AreaCatalog | None = None,
    ) -> None:
        self._flows = flows
        self._actions = actions
        self._areas = areas

    def has_flow(self, flow_id: FlowId) -> bool:
        return flow_id in self._flows

    def start(self, flow_id: FlowId) -> FlowTask:
        flow = self._flows[flow_id]
        return FlowTask(flow_id=flow_id, flow_version=flow.version, step_id=flow.start_id)

    def can_resume(self, task: FlowTask) -> bool:
        # 暂时只装载当前版本，找不到旧版本时请用户重开，不用新配置硬跑旧步骤。
        flow = self._flows.get(task.flow_id)
        return flow is not None and task.flow_version == flow.version and task.step_id in flow.steps

    def waiting_step(self, task: FlowTask) -> CollectStep | None:
        if not self.can_resume(task):
            return None
        step = self._flows[task.flow_id].steps[task.step_id]
        return step if isinstance(step, CollectStep) else None

    def resume_prompt(self, task: FlowTask) -> str | None:
        step = self.waiting_step(task)
        if step is None:
            return None
        if task.missing_slots == ["keyword"]:
            return "请回复要查的具体商品名称或班次编号。"
        return self._collect_prompt(task, step) or step.prompt

    def _collect_prompt(self, task: FlowTask, step: CollectStep) -> str | None:
        if step.slot == "product_request" and self._areas is not None:
            saved = task.slots.get(step.slot)
            request = saved if isinstance(saved, ProductRequest) else ProductRequest()
            check = check_product(request, self._areas)
            if check.missing:
                task.missing_slots = [check.missing]
                return check.prompt
        if step.slot not in task.slots:
            task.missing_slots = [step.slot]
            return step.prompt
        return None

    def _apply_area_reply(self, task: FlowTask, slot: str, reference: SlotValue) -> SlotValue:
        previous = task.slots.get(slot)
        if (self._areas is not None and task.missing_slots == ["area"]
                and isinstance(previous, ProductRequest) and isinstance(reference, ProductRequest)
                and previous.kind == reference.kind and previous.area and reference.area
                and previous.business_date == reference.business_date):
            # 补槽沿用原地区的候选范围，明确改了日期或品类时才按新条件查询。
            return reference.model_copy(update={"area": self._areas.refine(previous.area, reference.area)})
        return reference

    async def run(self, task: FlowTask, user_id: int, reference: SlotValue | None) -> FlowResult:
        flow = self._flows[task.flow_id]
        messages: list[Reply] = []
        outcome: ActionResult | None = None
        # 即使 YAML 跳转成环，一轮也最多走有限个节点，避免一直占着会话。
        for _ in range(MAX_STEPS_PER_TURN):
            step = flow.steps[task.step_id]
            match step:
                case StartStep():
                    task.step_id = step.next
                case CollectStep():
                    if reference is not None:
                        task.slots[step.slot] = self._apply_area_reply(task, step.slot, reference)
                        reference = None
                    if prompt := self._collect_prompt(task, step):
                        messages.append(text_reply(prompt))
                        return FlowResult(task, messages)
                    task.missing_slots = None
                    task.step_id = step.next
                case ActionStep():
                    # 一个流程也可以收集日期或商品条件，不再默认所有 Action 都接收订单号。
                    outcome = await self._actions[step.action].execute(user_id, task.slots[SLOT_FOR_FLOW[task.flow_id]])
                    task.step_id = step.routes[outcome.outcome]
                case ResponseStep():
                    if step.template in RESULT_TEMPLATES:
                        if outcome is None or outcome.outcome not in {"ok", "need_selection"}:
                            raise ValueError("结果模板需要有效的中台查询结果")
                        messages.extend(outcome.messages)
                    elif step.template == "product_not_found" and outcome and outcome.messages:
                        messages.extend(outcome.messages)
                    else:
                        messages.append(text_reply(TEXT_TEMPLATES[step.template]))
                    task.step_id = step.next
                    if step.pause:
                        # 商品条件分多轮收集，换日期或选候选时保留其它条件。
                        slot = SLOT_FOR_FLOW[task.flow_id]
                        if slot != "product_request":
                            task.slots.pop(slot, None)
                        task.missing_slots = ["keyword"] if step.template == "product_options" else [slot]
                        return FlowResult(task, messages)
                case EndStep():
                    return FlowResult(None, messages, outcome.order_id if outcome else None)
        raise ValueError("订单流程超过单轮最大推进步数，请检查 YAML 跳转")
