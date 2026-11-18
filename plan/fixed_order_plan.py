import re
from uuid import UUID

from plan.models import ITINERARY, ORDER_STATUS, FlowId, TurnPlan


# 模型不可用时保留基本查询；明确的“继续/取消”指令也由本地直接识别。
ORDER_QUERY = re.compile(r"(?:查(?:询|看)?|看)(?:一下)?(?:我的)?订单|订单状态")
ITINERARY_QUERY = re.compile(r"(?:查(?:询|看)?|看)(?:一下)?(?:我的)?(?:行程|出行安排)|行程(?:是什么|怎么安排)")
ORDER_NUMBER = re.compile(r"(?:订单号\s*[:：]?\s*)?([A-Za-z0-9][A-Za-z0-9_-]{0,63})[。！!]?")
CALENDAR_DATE = re.compile(r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}")
CURRENT_ORDER_WORDS = re.compile(r"这[笔个张份]|那[笔个张份]|刚才|上一笔|该订单|它")
# 匹配独立的入住或退房日期追问，避免把酒店预订条件或通用规则当成订单行程。
STAY_DATE_QUESTION = re.compile(
    r"(?:具体)?(?:是)?(?:哪一天|哪天|什么时候)(?:入住|退房|离店)(?:的)?"
    r"|(?:入住|退房|离店)(?:日期|时间)?(?:具体)?(?:是)?(?:哪一天|哪天|什么时候)"
)
CONTROL = re.compile(
    r"(?:请\s*|帮我\s*)?(取消|结束|停止|继续|恢复|接着|回到)\s*"
    r"(?:(?:这次|这个|当前|刚才|之前|上一个|上次|原来)的?)?\s*"
    r"(?:(客服任务|任务|查询|查订单|订单查询|查行程|行程查询|出行安排|退款申请|客服工单|工单)\s*)?"
    r"([0-9a-fA-F-]{36})?[。！!，,\s]*"
)
PAUSE_REQUEST = re.compile(
    r"(?:请|帮我)?\s*(?:先)?暂停(?:一下)?"
    r"(?:(?:当前|这次|这个|所有|全部)(?:的)?(?:客服)?(?:任务|查询))?[。！!\s]*"
)


def is_pause_request(text: str) -> bool:
    # 只匹配客服任务指令，不把“暂停航班”等业务描述当作会话操作。
    return PAUSE_REQUEST.fullmatch(text.strip()) is not None


def explicit_control(text: str) -> TurnPlan | None:
    normalized = text.strip()
    if normalized.rstrip("。！!") in {"不查了", "别查了", "不用查了"}:
        return TurnPlan(route="task", command="cancel_task")
    match = CONTROL.fullmatch(normalized)
    if not match:
        return None
    verb, target, raw_id = match.groups()
    if verb in {"取消", "结束", "停止"} and target is None and raw_id is None:
        return None
    flow_id: FlowId | None = None
    if target in {"查订单", "订单查询"}:
        flow_id = ORDER_STATUS
    elif target in {"查行程", "行程查询", "出行安排"}:
        flow_id = ITINERARY
    elif target == "退款申请":
        flow_id = "refund_request"
    elif target in {"客服工单", "工单"}:
        flow_id = "ticket_submit"
    try:
        task_id = UUID(raw_id) if raw_id else None
    except ValueError:
        return TurnPlan(route="clarify")
    command = "cancel_task" if verb in {"取消", "结束", "停止"} else "resume_task"
    # “取消订单”不匹配控制语法；该接口绝不取消中台订单。
    return TurnPlan(route="task", command=command, flow_id=flow_id, task_id=task_id)


def extract_order_number(text: str) -> str | None:
    match = ORDER_NUMBER.fullmatch(text.strip().lstrip(":：").strip())
    if match and not CALENDAR_DATE.fullmatch(match[1]):
        return match[1]
    return None


def is_stay_date_question(text: str) -> bool:
    return STAY_DATE_QUESTION.fullmatch(text.strip().rstrip("。！!？?").strip()) is not None


def explicit_order_itinerary(text: str) -> TurnPlan | None:
    # 简短的查行程指令直接进入订单行程流程，先收集订单号。带日期的查询由日期流程处理。
    request = re.sub(r"^(?:(?:请|帮我|麻烦)\s*)+", "", text.strip()).rstrip("。！!？?").strip()
    query = ITINERARY_QUERY.match(request)
    if query is None:
        return None
    suffix = request[query.end():].strip()
    order_number = extract_order_number(suffix)
    if suffix and order_number is None:
        return None
    return TurnPlan(route="task", command="start_flow", flow_id=ITINERARY, order_number=order_number)


def fallback_plan(text: str, *, waiting_for_order: bool) -> TurnPlan:
    query = ITINERARY_QUERY.search(text)
    flow_id = ITINERARY
    if query is None:
        query = ORDER_QUERY.search(text)
        flow_id = ORDER_STATUS
    if query:
        order_number = extract_order_number(text[query.end():])
        return TurnPlan(
            route="task", command="start_flow", flow_id=flow_id, order_number=order_number,
            use_current_order=order_number is None and CURRENT_ORDER_WORDS.search(text) is not None,
        )
    if text.strip().rstrip("。！!").casefold() in {"你好", "您好", "谢谢", "多谢", "嗨", "hi", "hello", "thanks"}:
        return TurnPlan(route="chitchat")
    if waiting_for_order and (order_number := extract_order_number(text)):
        return TurnPlan(route="task", command="set_slots", order_number=order_number)
    return TurnPlan(route="clarify")
