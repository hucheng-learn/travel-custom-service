import asyncio
from dataclasses import dataclass

from integration.regions import Area, AreaCatalog
from integration.travel_client import TravelClient, TravelClientError, TravelHTTPError
from plan.product_models import PRODUCT_NAMES, TRANSPORT_KINDS, ProductRequest
from task.action.order_status import ActionResult, _amount, _currency, _positive_id, display_business_time, read_items


ACTION_NAME = "search_public_products"
OUTCOMES = frozenset({"ok", "not_found", "unavailable", "need_selection"})
PAGE_SIZE = 20
LOOKUP_SECONDS = 12
NAME_FIELDS = {
    "hotel": ("hotelName", "hotelId"), "scenic": ("scenicName", "scenicSpotId"),
    "flight": ("flightNo", "departureId"), "train": ("trainNo", "departureId"),
    "bus": ("routeName", "departureId"), "transfer": ("serviceName", "serviceId"),
}


@dataclass(frozen=True)
class ProductCheck:
    missing: str | None
    prompt: str = ""
    areas: dict[str, Area] | None = None


def check_product(request: ProductRequest, catalog: AreaCatalog) -> ProductCheck:
    kind = request.kind
    if kind is None:
        return ProductCheck("kind", "你想查酒店、景点、机票、火车票、汽车票，还是接送？")
    if kind in TRANSPORT_KINDS:
        required = {"origin": "出发地", "destination": "到达地"}
    elif kind == "transfer" and request.quote:
        required = {"origin": "上车地区", "destination": "下车地区"}
    else:
        required = {"area": "所在城市或区县"}
    selections = {}
    for field, label in required.items():
        name = getattr(request, field)
        if name is None:
            return ProductCheck(field, f"请告诉我{label}。")
        candidates = catalog.resolve(name)
        if not candidates:
            return ProductCheck(field, f"没能识别{label}，请发完整的省市区名称。")
        if len(candidates) > 1:
            displayed = "、".join(area.label for area in candidates[:4])
            return ProductCheck(field, f"这个地名有多个候选：{displayed}。请发完整的{label}。")
        selections[field] = candidates[0]
    if request.business_date is None:
        label = "入住" if kind == "hotel" else "出行"
        return ProductCheck("business_date", f"请告诉我{label}日期，例如「明天」或「2026-12-01」。")
    if kind == "hotel":
        if request.end_date is None:
            return ProductCheck("end_date", "请告诉我哪天退房。")
        if not 0 < (request.end_date - request.business_date).days <= 30:
            return ProductCheck("end_date", "退房日期须晚于入住日期，单次最多查 30 晚。请再给一个退房日期。")
    return ProductCheck(None, areas=selections)


def _text(value: object) -> str | None:
    return " ".join(value.split())[:160] if isinstance(value, str) and value.strip() else None


def _money(row: dict, *keys: str, fallback_currency: str | None = None) -> str | None:
    currency = _currency(row.get("currencyCode")) or fallback_currency
    for key in keys:
        amount = _amount(row.get(key))
        if amount is not None and currency:
            suffix = " 起" if key == "minSalePriceAmount" else ""
            return f"{amount} {currency}{suffix}"
    return None


def _stock(row: dict) -> int | None:
    for key in ("availableRoomCount", "availableTicketCount", "availableInventory"):
        value = row.get(key)
        if type(value) is int and value >= 0:
            return value
    return None


def _reply(text: str) -> tuple[dict, ...]:
    return ({"type": "text", "text": text},)


class ProductAction:
    def __init__(self, travel: TravelClient | None, catalog: AreaCatalog, business_timezone: str) -> None:
        self._travel = travel
        self._catalog = catalog
        self._timezone = business_timezone

    async def execute(self, user_id: int, request: ProductRequest) -> ActionResult:
        result = check_product(request, self._catalog)
        kind, travel, day = request.kind, self._travel, request.business_date
        if travel is None or result.areas is None or kind is None or day is None:
            return ActionResult("unavailable")
        areas = result.areas
        params: dict[str, str | int] = {"pageNo": 1, "pageSize": PAGE_SIZE}
        if kind == "hotel" and request.end_date:
            params.update(areaId=areas["area"].id, checkInDate=day.isoformat(), checkOutDate=request.end_date.isoformat())
        elif kind == "scenic":
            params.update(areaId=areas["area"].id, travelDate=day.isoformat())
        elif kind in TRANSPORT_KINDS:
            params.update(departureAreaId=areas["origin"].id, arrivalAreaId=areas["destination"].id,
                          departureDate=day.isoformat())
        elif kind == "transfer":
            scope = areas.get("area")
            if request.quote:
                # 区县用于报价规则，服务搜索则按该上车地区所属城市。
                pickup = areas["origin"]
                parts = pickup.label.split(" / ")
                city = self._catalog.resolve("".join(parts[:2])) if len(parts) > 2 else self._catalog.resolve(parts[0])
                scope = city[0] if pickup.level > 2 and len(city) == 1 else pickup
            if scope is None:
                return ActionResult("unavailable")
            params.update(areaId=scope.id, businessDate=day.isoformat())
        filter_key = {"hotel": "keyword", "scenic": "keyword", "train": "trainNo", "bus": "routeName"}.get(kind)
        if request.keyword and filter_key:
            params[filter_key] = request.keyword
        try:
            async with asyncio.timeout(LOOKUP_SECONDS):
                data = await travel.search_products(kind, params)
                rows = read_items(data, "list")
                total = data.get("total")
                name_field, _ = NAME_FIELDS[kind]
                if request.keyword and kind in {"flight", "transfer"}:
                    rows = [row for row in rows if request.keyword.casefold() in (_text(row.get(name_field)) or "").casefold()]
                if not rows:
                    if type(total) is int and total > PAGE_SIZE:
                        return ActionResult("need_selection", _reply("当前只查了第一页，暂没找到指定商品。可补充其它条件再查。"))
                    return ActionResult("not_found")
                # 对具体房型、舱位或报价，先锁定唯一商品，不能拿列表第一项代替用户选择。
                show_detail = request.detail or (kind == "transfer" and request.quote) or bool(request.keyword)
                if show_detail and len(rows) == 1:
                    return await self._detail(travel, request, areas, rows[0])
                header = f"{day.isoformat()} 的{PRODUCT_NAMES[kind]}结果："
                if kind == "hotel":
                    header = f"{day.isoformat()} 入住、{request.end_date.isoformat()} 退房的酒店结果："
                lines = [header, *(self._summarize(kind, row) for row in rows[:5])]
                if (type(total) is int and total > len(rows[:5])) or len(rows) > 5:
                    lines.append("这里只展示本次查询的部分结果。")
                if show_detail:
                    lines.append("有多个候选，请回复具体商品名称或班次编号。")
                    return ActionResult("need_selection", _reply("\n".join(lines)))
                return ActionResult("ok", _reply("\n".join(lines)))
        except TravelHTTPError as error:
            if error.status_code == 404:
                return ActionResult("not_found")
            return ActionResult("unavailable")
        except (TravelClientError, TimeoutError, ValueError):
            return ActionResult("unavailable")

    def _summarize(self, kind: str, row: dict) -> str:
        name = _text(row.get(NAME_FIELDS[kind][0])) or PRODUCT_NAMES[kind]
        pieces = [f"- {name}"]
        for label, field in (("出发", "departureTime"), ("到达", "arrivalTime")):
            if value := display_business_time(row.get(field), self._timezone):
                pieces.append(f"{label} {value}")
        if price := _money(row, "minSalePriceAmount", "salePriceAmount"):
            pieces.append(price)
        if (stock := _stock(row)) is not None:
            pieces.append(f"可用量 {stock}")
        for field in ("availableCabinClasses", "availableSeatClasses"):
            available = row.get(field)
            if isinstance(available, list):
                labels = [value for item in available[:8] if (value := _text(item))]
                if labels:
                    pieces.append("可选舱位或席别：" + "、".join(labels))
        if kind == "transfer" and type(row.get("passengerCapacity")) is int:
            pieces.append(f"可载 {row['passengerCapacity']} 人")
        return "；".join(pieces)

    async def _detail(self, travel: TravelClient, request: ProductRequest, areas: dict[str, Area], row: dict) -> ActionResult:
        kind, day = request.kind, request.business_date
        if kind is None or day is None:
            return ActionResult("unavailable")
        product_id = _positive_id(row.get(NAME_FIELDS[kind][1]))
        detail = await travel.get_product(kind, product_id)
        lines = [f"{day.isoformat()} 的中台结果：", self._summarize(kind, row)]
        for label, field in (("地址", "address"), ("开放", "openTime"), ("关闭", "closeTime"),
                             ("办理入住", "checkInTime"), ("办理退房", "checkOutTime"), ("车型", "vehicleTypeCode")):
            if value := _text(detail.get(field)):
                lines.append(f"{label}：{value}")
        if kind == "hotel" and request.end_date:
            data = await travel.get_hotel_room_types(product_id, day.isoformat(), request.end_date.isoformat())
            if tags := detail.get("facilityTags"):
                if isinstance(tags, list):
                    lines.append("设施：" + "、".join(value for tag in tags[:8] if (value := _text(tag))))
            items = read_items(data, "list")
            name_field, price_field = "roomTypeName", "firstNightSalePriceAmount"
        elif kind == "scenic":
            data = await travel.get_scenic_ticket_types(product_id, day.isoformat())
            items = read_items(data, "list")
            name_field, price_field = "ticketTypeName", "salePriceAmount"
        elif kind in TRANSPORT_KINDS:
            items = read_items(detail, "cabins" if kind == "flight" else "seats")
            name_field = "cabinClassCode" if kind == "flight" else "seatClassCode"
            price_field = "salePriceAmount"
        else:
            if request.quote:
                quote = await travel.get_transfer_price(product_id, areas["origin"].id, areas["destination"].id, day.isoformat())
                if price := _money(quote, "salePriceAmount"):
                    lines.append(f"中台报价：{price}")
                else:
                    lines.append("中台没有返回这段接送路线的价格。")
                if (stock := _stock(quote)) is not None:
                    lines.append(f"可用量：{stock}")
            return ActionResult("ok", _reply("\n".join(lines)))

        for entry in items[:5]:
            label = _text(entry.get(name_field)) or "库存项"
            price = _money(entry, price_field, fallback_currency=_currency(row.get("currencyCode")))
            stock = _stock(entry)
            pieces = [label]
            if price:
                pieces.append(f"首晚 {price}" if kind == "hotel" else price)
            if stock is not None:
                pieces.append(f"可用量 {stock}")
            lines.append("；".join(pieces))
        if not items:
            lines.append("中台未返回该日期的明细库存。")
        if request.detail:
            self._add_booking_rules(detail, lines)
        return ActionResult("ok", _reply("\n".join(lines)))

    @staticmethod
    def _add_booking_rules(detail: dict, lines: list[str]) -> None:
        # 只翻译已知字段；中台没给退改条件时，不从通用 FAQ 推断具体商品的规则。
        rule = detail.get("bookingRule")
        if not isinstance(rule, dict):
            return
        for label, field in (("最晚预订", "latestBookingTime"), ("保留到", "holdUntilTime"),
                             ("最少入住晚数", "minStayNights"), ("最多预订房间数", "maxRoomCount")):
            value = rule.get(field)
            if (isinstance(value, str) and value.strip()) or type(value) is int:
                lines.append(f"{label}：{value}")
        payload = rule.get("rulePayload")
        if isinstance(payload, dict):
            for label, field in (("取消规则", "cancel"), ("改期说明", "change"), ("退改说明", "refund")):
                if value := _text(payload.get(f"{field}_rule") or payload.get(f"{field}Rule")):
                    lines.append(f"{label}：{value}")
            hours = payload.get("cancel_before_hours")
            if type(hours) is int and hours >= 0:
                lines.append(f"取消提前量：{hours} 小时；中台未用这个字段说明是否收费。")
