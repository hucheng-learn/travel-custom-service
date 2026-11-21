"""模型故障时的有限输入通道，不负责理解任意自然语言。"""

from plan.input_values import extract_order_number
from plan.models import GroundedValue, ITINERARY, ORDER_STATUS, REFUND, TICKET, SalesClues, TurnPlan


def degraded_plan(text, state, *, reason):
    # 只接受界面提示的明确快捷语、完整订单号和当前等待的简单值，不能用关键词猜句意。
    word = text.strip().rstrip("。！!")
    starts = {"查订单": ORDER_STATUS, "帮我查订单": ORDER_STATUS, "查行程": ITINERARY,
              "帮我查行程": ITINERARY, "申请退款": REFUND, "我想退款": REFUND, "提交工单": TICKET}
    if word in starts:
        return TurnPlan(route="task", command="start_flow", flow_id=starts[word])
    controls = {"取消当前任务": "cancel_task", "继续上一个任务": "resume_task", "暂停当前任务": "pause_task"}
    if word in controls:
        return TurnPlan(route="task", command=controls[word], intent_text=word)
    active = state.active_task
    if active:
        if active.sales:
            if active.step_id == "order" and (number := extract_order_number(word)):
                return TurnPlan(route="task", command="set_slots", order_number=number)
            if active.step_id == "refund_type" and word in {"全额", "部分退款"}:
                return TurnPlan(route="task", command="set_slots", sales=SalesClues(
                    refund_type=GroundedValue(value="full" if word == "全额" else "partial", source=word)))
        elif active.flow_id in {ORDER_STATUS, ITINERARY} and (number := extract_order_number(word)):
            return TurnPlan(route="task", command="set_slots", order_number=number)
    return TurnPlan(route="clarify", clarify_reason=reason)
