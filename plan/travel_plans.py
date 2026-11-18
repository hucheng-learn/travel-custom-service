import re

from integration.regions import AreaCatalog
from knowledge.retrieval import looks_like_knowledge
from plan.fixed_order_plan import ITINERARY_QUERY, ORDER_QUERY, fallback_plan
from plan.models import DATE_ITINERARY, ITINERARY, PRODUCT, TurnPlan
from plan.product_models import TRANSPORT_KINDS, ProductClues, ProductKind, ProductRequest
from plan.travel_dates import find_date_texts
from task.flow.executor import DialogueState


KINDS = {
    "hotel": re.compile(r"酒店|宾馆|民宿|饭店|房型|住宿"),
    "scenic": re.compile(r"景点|景区|门票|公园|博物馆"),
    "flight": re.compile(r"机票|航班|飞机|舱位"),
    "train": re.compile(r"火车|高铁|动车|车次|(?<![A-Za-z0-9])[GDKZTCS][0-9]{1,5}(?![0-9])", re.IGNORECASE),
    "bus": re.compile(r"汽车票|客运|大巴|巴士|长途汽车"),
    "transfer": re.compile(r"接送|接机|送机|接站|送站|包车|上车|下车"),
}
TRAIN_CODE = re.compile(r"(?<![A-Za-z0-9])[GDKZTCS][0-9]{1,5}(?![A-Za-z0-9])", re.IGNORECASE)
FLIGHT_CODE = re.compile(r"(?<![A-Za-z0-9])[A-Z0-9]{2}[0-9]{3,4}(?![A-Za-z0-9])", re.IGNORECASE)
DETAIL_WORDS = re.compile(r"详情|设施|房型|票种|座席|座位|座舱|舱位|几点|开放|时刻|车型|载客|规则|退改|免费取消")
TRANSFER_PRICE = re.compile(r"多少钱|价格|报价|费用|怎么收费")
REQUEST_SEPARATOR = re.compile(r"[；;→？?\r\n]+")


def has_multiple_requests(text: str) -> bool:
    parts = [part.strip() for part in REQUEST_SEPARATOR.split(text) if part.strip()]
    if len(parts) < 2:
        return False
    # 只数能够独立发起的查询。“明天入住；后天退房”仍是给同一任务补条件。
    empty = DialogueState()
    matches = 0
    for part in parts:
        plan = stage_three_plan(part, empty, None) or fallback_plan(part, waiting_for_order=False)
        if plan.route == "knowledge" or plan.command == "start_flow":
            matches += 1
            if matches == 2:
                return True
    return False


def stage_three_plan(text: str, state: DialogueState, areas: AreaCatalog | None) -> TurnPlan | None:
    active = state.active_task
    dates = find_date_texts(text)
    locations = areas.mentions(text) if areas else []
    kind: ProductKind | None = next(
        (kind for kind, pattern in KINDS.items() if pattern.search(text)), None
    )
    if re.search(r"^(?:请|帮我|我要|我想)\s*(?:提交退款|申请退款|退款|退掉)", text.strip()):
        return TurnPlan(route="clarify")  # 阶段三只读，不能把写操作当作已执行。
    if looks_like_knowledge(text) and (kind is None or (not dates and not locations)):
        return TurnPlan(route="knowledge")
    if kind:
        # 独立的 G527 也可能是正在等待的订单号；有“几点”等其它字时才当新车次查询。
        standalone_number = TRAIN_CODE.fullmatch(text.strip())
        if standalone_number and active and active.flow_id != PRODUCT:
            return None
        previous = active.slots.get("product_request") if active and active.flow_id == PRODUCT else None
        saved = previous if isinstance(previous, ProductRequest) and previous.kind == kind else None
        return TurnPlan(
            route="task", command="start_flow", flow_id=PRODUCT,
            product=_product_clues(text, kind, dates, locations, saved, active.missing_slots if active else None),
        )
    if len(dates) == 1 and (re.search(r"行程|出行|出发安排", text) or (
        active and active.flow_id in {ITINERARY, DATE_ITINERARY} and text.strip(" 。？！?!") == dates[0]
    )):
        return TurnPlan(route="task", command="start_flow", flow_id=DATE_ITINERARY, date_text=dates[0])
    if "按日期" in text and ("行程" in text or "出行" in text):
        return TurnPlan(route="task", command="start_flow", flow_id=DATE_ITINERARY)
    if active is None or active.flow_id != PRODUCT or ITINERARY_QUERY.search(text) or ORDER_QUERY.search(text):
        return None
    previous = active.slots.get("product_request")
    if not isinstance(previous, ProductRequest) or previous.kind is None:
        return None
    if not dates and not locations and active.missing_slots != ["keyword"]:
        return None
    return TurnPlan(
        route="task", command="set_slots",
        product=_product_clues(text, previous.kind, dates, locations, previous, active.missing_slots),
    )


def _product_clues(
    text: str, kind: ProductKind, dates: list[str], locations: list[str],
    previous: ProductRequest | None, missing: list[str] | None,
) -> ProductClues:
    # 根据品类和刚才追问的字段分配地名与日期，不让“上海”覆盖已经填好的出发地。
    clues = ProductClues(kind=kind, detail=bool(DETAIL_WORDS.search(text)))
    clues.quote = kind == "transfer" and (bool(TRANSFER_PRICE.search(text)) or bool(previous and previous.quote))
    if locations:
        if kind in TRANSPORT_KINDS or clues.quote:
            if len(locations) >= 2:
                clues.origin, clues.destination = locations[:2]
            elif re.search(r"到达|目的地|下车", text) or (previous and previous.origin and not previous.destination):
                clues.destination = locations[0]
            else:
                clues.origin = locations[0]
        else:
            clues.area = locations[0]
    if dates:
        if kind == "hotel" and len(dates) >= 2:
            clues.date_text, clues.end_date_text = dates[:2]
        elif kind == "hotel" and ("退房" in text or "离店" in text or (missing == ["end_date"] and "入住" not in text)):
            clues.end_date_text = dates[0]
        else:
            clues.date_text = dates[0]
    pattern = TRAIN_CODE if kind == "train" else FLIGHT_CODE if kind == "flight" else None
    if pattern and (code := pattern.search(text)):
        clues.keyword = code[0]
    elif match := re.search(r"[「《“]([^」》”]{1,80})[」》”]", text):
        clues.keyword = match[1]
    elif missing == ["keyword"] and not dates and 0 < len(text.strip()) <= 80:
        clues.keyword = text.strip()
    return clues
