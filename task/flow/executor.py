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
    # task_id 是任务唯一标识，暂停、恢复、取消和日志都靠它定位任务。
    task_id: UUID = Field(default_factory=uuid4)
    # flow_id 对应 flow_config 里的一套 YAML 流程，比如订单状态查询或商品咨询。
    flow_id: FlowId
    # flow_version 是加载 YAML 时按内容算出的版本号，配置更新后旧任务不能继续跑。
    flow_version: str
    # step_id 表示任务当前停在流程的哪个步骤。
    step_id: str
    # slots 保存已经收集到的槽位，键是槽位名（order_ref/travel_date/product_request）。
    slots: dict[str, SlotValue] = Field(default_factory=dict)
    # missing_slots 记录上一步追问的槽位；商品查询用它区分缺地区还是缺酒店名。
    missing_slots: list[str] | None = None
    # paused_by_user 区分用户主动暂停和切换任务时的被动暂停：前者只有用户说“继续”才会恢复。
    paused_by_user: bool = False
    # sales 只在退款、工单任务里出现，保存收集中的售后草稿和确认卡。
    sales: SalesDraft | None = None


class DialogueState(BaseModel):
    # 新字段有默认值，之前阶段一只保存 active_task 的会话仍能读出来。
    # active_task 是当前正在进行的任务，一个会话最多只有一个。
    active_task: FlowTask | None = None
    # paused_tasks 是切换任务时压入的旧任务，恢复时从后往前找。
    paused_tasks: list[FlowTask] = Field(default_factory=list)
    # current_order_id 是最近一次查询成功带出的订单主键，用来承接“这笔订单”这类追问。
    current_order_id: int | None = None
    # 没有进行中任务时收到的订单卡片先暂存，只承接紧随的下一条消息。
    pending_order_id: int | None = None


@dataclass(frozen=True)
class FlowResult:
    # task 为 None 表示流程已经结束；messages 是这轮要返回的消息。
    task: FlowTask | None
    messages: list[Reply]
    # order_id 是流程结束时查到的订单主键，由引擎记到 current_order_id。
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
        # 只对停在收集步骤的任务生成追问；其他步骤没有可补的槽位。
        step = self.waiting_step(task)
        if step is None:
            return None
        # missing_slots 为 ["keyword"] 表示商品条件已经齐了，只差用户选具体商品。
        if task.missing_slots == ["keyword"]:
            return "请回复要查的具体商品名称或班次编号。"
        return self._collect_prompt(task, step) or step.prompt

    def _collect_prompt(self, task: FlowTask, step: CollectStep) -> str | None:
        # 商品槽位先检查还缺哪个条件：check.missing 是缺的字段名，check.prompt 是对应的追问。
        if step.slot == "product_request" and self._areas is not None:
            # saved 是上一轮已经保存的商品条件，没有就按空条件开始。
            saved = task.slots.get(step.slot)
            request = saved if isinstance(saved, ProductRequest) else ProductRequest()
            check = check_product(request, self._areas)
            if check.missing:
                task.missing_slots = [check.missing]
                return check.prompt
        if step.slot not in task.slots:
            # 槽位完全没值时，记下缺的是哪个槽位，返回步骤自带的追问。
            task.missing_slots = [step.slot]
            return step.prompt
        return None

    def _apply_area_reply(self, task: FlowTask, slot: str, reference: SlotValue) -> SlotValue:
        # previous 是同一槽位上一轮保存的值。只有本轮确实在补地区、品类和日期都没变时，
        # 才把新回答当作原地区候选范围的细化，避免把两个不相干的地点混在一起查。
        previous = task.slots.get(slot)
        if (self._areas is not None and task.missing_slots == ["area"]
                and isinstance(previous, ProductRequest) and isinstance(reference, ProductRequest)
                and previous.kind == reference.kind and previous.area and reference.area
                and previous.business_date == reference.business_date):
            # 补槽沿用原地区的候选范围，明确改了日期或品类时才按新条件查询。
            return reference.model_copy(update={"area": self._areas.refine(previous.area, reference.area)})
        return reference

    async def run(self, task: FlowTask, user_id: int, reference: SlotValue | None) -> FlowResult:
        # 按 YAML 定义的步骤逐步推进，遇到缺槽位、暂停或结束才停下。
        # flow 是 task.flow_id 对应的步骤表；messages 收集本轮要返回的消息；
        # outcome 保存最近一次 Action 的查询结果，供后面的 response 步骤取用。
        # reference 是本轮补上的槽位值，只会被第一个 collect 步骤消费一次。
        flow = self._flows[task.flow_id]
        messages: list[Reply] = []
        outcome: ActionResult | None = None
        # 即使 YAML 跳转成环，一轮也最多走有限个节点，避免一直占着会话。
        for _ in range(MAX_STEPS_PER_TURN):
            # step 是 task.step_id 当前指向的步骤对象，每走一步都会重新取。
            step = flow.steps[task.step_id]
            match step:
                case StartStep():
                    # start：空节点，直接跳到流程第一个实际步骤。
                    task.step_id = step.next
                case CollectStep():
                    # collect：先吸收本轮补上的槽位，仍缺则追问并把任务停在本步骤等下一轮。
                    # reference 消费后置空，避免同一份值被后续收集步骤重复使用。
                    if reference is not None:
                        task.slots[step.slot] = self._apply_area_reply(task, step.slot, reference)
                        reference = None
                    if prompt := self._collect_prompt(task, step):
                        messages.append(text_reply(prompt))
                        return FlowResult(task, messages)
                    task.missing_slots = None
                    task.step_id = step.next
                case ActionStep():
                    # 调用注册好的 Action 访问中台，结果只能走 YAML 里声明过的分支。
                    # 一个流程也可以收集日期或商品条件，不再默认所有 Action 都接收订单号。
                    # outcome.outcome 是 Action 返回的结果标签，step.routes 把它映射到下一步。
                    outcome = await self._actions[step.action].execute(user_id, task.slots[SLOT_FOR_FLOW[task.flow_id]])
                    task.step_id = step.routes[outcome.outcome]
                case ResponseStep():
                    # 渲染固定文案或 Action 返回的业务卡片：结果模板取 outcome.messages，
                    # 其他模板走 TEXT_TEMPLATES 里的固定话术。
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
                        # 展示候选项后停住等用户选择；多轮收集商品条件时，此前确认的条件继续保留。
                        slot = SLOT_FOR_FLOW[task.flow_id]
                        if slot != "product_request":
                            task.slots.pop(slot, None)
                        task.missing_slots = ["keyword"] if step.template == "product_options" else [slot]
                        return FlowResult(task, messages)
                case EndStep():
                    # end：任务完成，返回 order_id 供会话记录“当前订单”承接后续追问。
                    return FlowResult(None, messages, outcome.order_id if outcome else None)
        raise ValueError("订单流程超过单轮最大推进步数，请检查 YAML 跳转")
