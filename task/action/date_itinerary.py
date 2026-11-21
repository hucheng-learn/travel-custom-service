import asyncio
from datetime import date, tzinfo

from integration.travel_client import TravelClient, TravelClientError, TravelHTTPError
from plan.product_models import DateRequest
from plan.travel_dates import upstream_travel_date
from task.action.order_status import ActionResult, _positive_id, read_items


ACTION_NAME = "query_itinerary_by_date"
OUTCOMES = frozenset({"ok", "unavailable"})
PAGE_SIZE = 12
MAX_PAGES = 3
MAX_PARALLEL = 4
LOOKUP_SECONDS = 12


def _date_note(item: dict, requested: date, zone: tzinfo) -> str | None:
    start = upstream_travel_date(item.get("travelTime"), zone)
    end = upstream_travel_date(item.get("travelEndTime"), zone)
    # 对应技术设计 3.7 日期示例：1 月 7 日入住、10 日退房，7 日标入住，8/9 日在住，
    # 10 日单独标退房，11 日不匹配这条住宿；其它品类按开始/出发日匹配。
    if item.get("productTypeCode") == "hotel_room" and start and end and end >= start:
        if requested == end:
            return "当天退房"
        if start <= requested < end:
            return "当天入住" if requested == start else "当天在住"
    if start == requested:
        return "当天出行"
    return None


class DateItineraryAction:
    # 对应技术设计 3.7 第三条查询：“2027-01-08 的出行安排”只需要日期。
    # 分页查本人订单 → 分批并发读详情 → 按明细出行/住宿日期匹配；不按订单创建日期过滤。
    def __init__(self, travel: TravelClient | None, zone: tzinfo) -> None:
        self._travel = travel
        self._zone = zone

    async def execute(self, user_id: int, request: DateRequest) -> ActionResult:
        travel = self._travel
        if travel is None:
            return ActionResult("unavailable")
        matches: list[str] = []
        checked: set[int] = set()
        ended = False
        gaps = False
        try:
            async with asyncio.timeout(LOOKUP_SECONDS):
                for page_no in range(1, MAX_PAGES + 1):
                    # createdFrom/createdTo 筛的是下单时间，不能拿来找某一天出行的订单。
                    page = await travel.list_orders(user_id, page_no=page_no, page_size=PAGE_SIZE)
                    rows = read_items(page, "list")
                    total = page.get("total")
                    if type(total) is not int or total < 0 or (not rows and total > (page_no - 1) * PAGE_SIZE):
                        raise ValueError("订单分页字段不正确")
                    ids = list(dict.fromkeys(_positive_id(row.get("orderId")) for row in rows))
                    if len(ids) != len(rows) or checked.intersection(ids):
                        gaps = True
                    ids = [order_id for order_id in ids if order_id not in checked]
                    for offset in range(0, len(ids), MAX_PARALLEL):
                        batch = ids[offset:offset + MAX_PARALLEL]
                        # 批内并发，批间串行，避免订单多时同时打满中台连接。
                        results = await asyncio.gather(*(
                            self._read_order(travel, user_id, order_id, request.travel_date) for order_id in batch
                        ), return_exceptions=True)
                        for order_id, result in zip(batch, results):
                            if isinstance(result, TravelHTTPError) and result.status_code == 401:
                                raise result
                            if isinstance(result, (TravelClientError, ValueError)):
                                gaps = True
                            elif isinstance(result, BaseException):
                                raise result
                            else:
                                checked.add(order_id)
                                matches.extend(result)
                    if page_no * PAGE_SIZE >= total:
                        ended = True
                        break
        except TravelHTTPError as error:
            if error.status_code in {401, 403}:
                raise
            if not checked:
                return ActionResult("unavailable")
            gaps = True
        except (TimeoutError, TravelClientError, ValueError):
            if not checked:
                return ActionResult("unavailable")
            gaps = True

        # 3.7 汇总：查完且无遗漏，才能明确说当天没有安排；分页达到上限或部分读取失败，
        # 必须说明只覆盖已查范围，并提示可改按订单号核对，不能把漏查说成没有行程。
        day = request.travel_date.isoformat()
        complete = ended and not gaps
        if matches:
            lines = [f"{day} 查到 {len(matches)} 条出行或住宿安排：", *matches[:10]]
            if len(matches) > 10:
                lines.append("这里只展示前 10 条。")
        elif complete:
            lines = [f"{day} 没查到出行或住宿安排。"]
        else:
            lines = [f"已查范围内暂未找到 {day} 的出行安排。"]
        if not complete:
            lines.append("本次只查到部分订单，无法确认其余订单是否有行程；可以按订单号查指定订单。")
        return ActionResult("ok", ({"type": "text", "text": "\n".join(lines)},))

    async def _read_order(self, travel: TravelClient, user_id: int, order_id: int, requested: date) -> list[str]:
        order = await travel.get_order(user_id, order_id)
        owner = order.get("userId")
        if _positive_id(order.get("orderId")) != order_id or (owner is not None and _positive_id(owner) != user_id):
            raise ValueError("订单详情与当前用户或订单不符")
        if order.get("statusCode") == "cancelled":
            return []
        lines = []
        for item in read_items(order, "items"):
            if item.get("statusCode") in {"cancelled", "refunded"}:
                continue
            note = _date_note(item, requested, self._zone)
            if note:
                raw_name = item.get("productName")
                name = " ".join(raw_name.split())[:120] if isinstance(raw_name, str) else "出行项目"
                lines.append(f"- {name or '出行项目'}：{note}")
        return lines
