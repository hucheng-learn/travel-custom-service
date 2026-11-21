import asyncio
import re
from dataclasses import dataclass

from integration.regions import Area, AreaCatalog
from integration.travel_client import TravelClient, TravelClientError, TravelHTTPError
from plan.product_models import HOTEL_TYPE_NAMES, PRODUCT_NAMES, TRANSPORT_KINDS, ProductRequest
from task.action.order_status import ActionResult, _amount, _currency, _positive_id, display_business_time, read_items


ACTION_NAME = "search_public_products"
OUTCOMES = frozenset({"ok", "not_found", "unavailable", "need_selection"})
PAGE_SIZE = 20
SCENIC_CITY_PAGE_SIZE = 100
LOOKUP_SECONDS = 12
NAME_FIELDS = {
    "hotel": ("hotelName", "hotelId"), "scenic": ("scenicName", "scenicSpotId"),
    "flight": ("flightNo", "departureId"), "train": ("trainNo", "departureId"),
    "bus": ("routeName", "departureId"), "transfer": ("serviceName", "serviceId"),
}
# 将已知的酒店设施代码显示为旅客熟悉的名称，其它设施沿用原名称。
HOTEL_FACILITY_NAMES = {
    "wifi": "无线网络", "parking": "停车场", "breakfast": "早餐", "gym": "健身房",
    "pool": "游泳池", "bar": "酒吧", "courtyard": "庭院", "tea_room": "茶室",
}
CABIN_NAMES = {"economy": "经济舱", "premium_economy": "高端经济舱", "business": "商务舱", "first": "头等舱"}


@dataclass(frozen=True)
class ProductCheck:
    missing: str | None
    prompt: str = ""
    areas: dict[str, Area] | None = None


def check_product(request: ProductRequest, catalog: AreaCatalog) -> ProductCheck:
    # 对应技术设计 3.4 左侧：先核对品类、唯一地区和所需日期，缺哪项就追问哪项。
    # “鼓楼区”有多个候选时返回 missing=area，执行器保存原日期并等待完整地名。
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
            return ProductCheck(field, f"这个地名有多个候选：{displayed}。请回复其中一个完整地名。")
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
            suffix = "起" if key == "minSalePriceAmount" else ""
            unit = "元" if currency == "CNY" else currency
            return f"{amount} {unit}{suffix}"
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
    # 对应技术设计 3.4：已收集的商品条件 → 中台搜索 → 列表或唯一商品的详情/报价。
    # 多个候选要等用户选定，不能用第一项的价格代替用户真正想查的商品。
    def __init__(self, travel: TravelClient | None, catalog: AreaCatalog, business_timezone: str) -> None:
        self._travel = travel
        self._catalog = catalog
        self._timezone = business_timezone

    async def execute(self, user_id: int, request: ProductRequest) -> ActionResult:
        # 再检查条件后按品类映射接口参数；返回实际查询结果和受控 outcome，
        # 由 FlowExecutor 决定结束、等用户选择，还是解释查不到/暂不可用。
        result = check_product(request, self._catalog)
        kind, travel, day = request.kind, self._travel, request.business_date
        if travel is None or result.areas is None or kind is None or day is None:
            return ActionResult("unavailable")
        areas = result.areas
        scenic_district = None
        params: dict[str, str | int] = {"pageNo": 1, "pageSize": PAGE_SIZE}
        if kind == "hotel" and request.end_date:
            params.update(areaId=areas["area"].id, checkInDate=day.isoformat(), checkOutDate=request.end_date.isoformat())
            # 中台按星级、类型分别查库存，keyword 仍只匹配酒店名称。
            if request.hotel_star_rating:
                params["starRatingCodes"] = request.hotel_star_rating
            if request.hotel_type:
                params["hotelTypeCodes"] = request.hotel_type
        elif kind == "scenic":
            area = areas["area"]
            city = self._catalog.city_for(area)
            params.update(areaId=city.id, travelDate=day.isoformat())
            if city != area:
                # 3.4 选定福州市鼓楼区后：景点接口按福州市查，再按返回地址筛选鼓楼区，
                # 不能把区县 ID 当城市 ID 传入，否则会把可订景点误查成空列表。
                scenic_district = area
                params["pageSize"] = SCENIC_CITY_PAGE_SIZE
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
                if scenic_district is not None:
                    rows = await self._scenic_in_district(travel, params, scenic_district)
                    total = len(rows)
                else:
                    data = await travel.search_products(kind, params)
                    rows = read_items(data, "list")
                    total = data.get("total")
                name_field, _ = NAME_FIELDS[kind]
                if request.keyword and kind in {"flight", "transfer"}:
                    rows = [row for row in rows if request.keyword.casefold() in (_text(row.get(name_field)) or "").casefold()]
                if not rows:
                    if scenic_district is None and type(total) is int and total > PAGE_SIZE:
                        return ActionResult("need_selection", _reply("当前只查了第一页，暂没找到指定商品。可补充其它条件再查。"))
                    if kind == "scenic" and (scenic_district is not None or not request.keyword):
                        return ActionResult("not_found", _reply(
                            f"没有查到 {day.isoformat()} 在{areas['area'].label}可订的景点。可以换日期或地区再查。"
                        ))
                    return ActionResult("not_found")
                # 对具体房型、舱位或报价，先锁定唯一商品，不能拿列表第一项代替用户选择。
                show_detail = request.detail or (kind == "transfer" and request.quote) or bool(request.keyword)
                if show_detail and len(rows) == 1:
                    return await self._detail(travel, request, areas, rows[0])
                header = f"{day.isoformat()} 的{PRODUCT_NAMES[kind]}结果："
                if kind == "hotel":
                    header = f"{day.isoformat()} 入住、{request.end_date.isoformat()} 退房的酒店结果："
                elif scenic_district is not None:
                    header = f"{day.isoformat()} 在{scenic_district.label}可订的景点："
                lines = [header, *(self._summarize(kind, row) for row in rows[:5])]
                if (type(total) is int and total > len(rows[:5])) or len(rows) > 5:
                    lines.append("这里只展示本次查询的部分结果。")
                if show_detail:
                    lines.append("有多个候选，请回复具体商品名称或班次编号。")
                    return ActionResult("need_selection", _reply("\n".join(lines)))
                if kind == "hotel" and (request.hotel_star_rating or request.hotel_type):
                    # 等旅客选酒店，再沿用这次的入住和退房日期查询房型。
                    lines.append("回复酒店名称即可查看房型和首晚价格。")
                    return ActionResult("need_selection", _reply("\n".join(lines)))
                return ActionResult("ok", _reply("\n".join(lines)))
        except TravelHTTPError as error:
            if error.status_code == 404:
                return ActionResult("not_found")
            return ActionResult("unavailable")
        except (TravelClientError, TimeoutError, ValueError):
            return ActionResult("unavailable")

    @staticmethod
    async def _scenic_in_district(travel: TravelClient, params: dict[str, str | int], district: Area) -> list[dict]:
        matches = []
        page_no = 1
        while True:
            data = await travel.search_products("scenic", {**params, "pageNo": page_no})
            rows = read_items(data, "list")
            total = data.get("total")
            if type(total) is not int or total < 0 or (not rows and total > (page_no - 1) * SCENIC_CITY_PAGE_SIZE):
                raise ValueError("中台景点分页结果不完整")
            matches.extend(row for row in rows if district.matches_district_address(_text(row.get("address")) or ""))
            # 区县候选可能在城市列表的后续页。找到第六条后已能展示前五条并提示还有结果。
            if len(matches) > 5 or page_no * SCENIC_CITY_PAGE_SIZE >= total:
                return matches
            page_no += 1

    def _summarize(self, kind: str, row: dict, *, include_inventory: bool = True) -> str:
        name = _text(row.get(NAME_FIELDS[kind][0])) or PRODUCT_NAMES[kind]
        pieces = [f"- {name}"]
        if kind == "hotel":
            if (star := _text(row.get("starRatingCode"))) in {"3", "4", "5"}:
                pieces.append(f"{star} 星")
            if label := HOTEL_TYPE_NAMES.get(_text(row.get("hotelTypeCode"))):
                pieces.append(label)
        for label, field in (("出发", "departureTime"), ("到达", "arrivalTime")):
            if value := display_business_time(row.get(field), self._timezone):
                pieces.append(f"{label} {value}")
        if not include_inventory:
            return "，".join(pieces)
        if price := _money(row, "minSalePriceAmount", "salePriceAmount"):
            pieces.append(price)
        if (stock := _stock(row)) is not None:
            pieces.append(f"可订 {stock} 间" if kind == "hotel" else f"可用量 {stock}")
        for field in ("availableCabinClasses", "availableSeatClasses"):
            available = row.get(field)
            if isinstance(available, list):
                labels = [value for item in available[:8] if (value := _text(item))]
                if labels:
                    pieces.append("可选舱位或席别：" + "、".join(labels))
        if kind == "transfer" and type(row.get("passengerCapacity")) is int:
            pieces.append(f"可载 {row['passengerCapacity']} 人")
        return "，".join(pieces)

    async def _detail(self, travel: TravelClient, request: ProductRequest, areas: dict[str, Area], row: dict) -> ActionResult:
        kind, day = request.kind, request.business_date
        if kind is None or day is None:
            return ActionResult("unavailable")
        product_id = _positive_id(row.get(NAME_FIELDS[kind][1]))
        detail = await travel.get_product(kind, product_id)
        header = f"{day.isoformat()} 的{PRODUCT_NAMES[kind]}详情"
        if kind == "hotel" and request.end_date:
            # 首晚价取决于入住日，可订房量也与住宿日期有关，因此显示用户查的完整日期。
            header = f"{day.isoformat()} 入住，{request.end_date.isoformat()} 退房"
        # 航班详情按各舱位的实时库存展示价格，避免把列表起价当成可购买价格。
        lines = [header, self._summarize(kind, row, include_inventory=kind != "flight")]
        for label, field in (("地址", "address"), ("开放", "openTime"), ("关闭", "closeTime"),
                             ("办理入住", "checkInTime"), ("办理退房", "checkOutTime"), ("车型", "vehicleTypeCode")):
            if value := _text(detail.get(field)):
                if field in {"openTime", "closeTime"} and re.fullmatch(r"[0-9]{1,2}:[0-9]{2}:00", value):
                    value = value[:-3].zfill(5)
                lines.append(f"{label}：{value}")
        if kind == "hotel" and request.end_date:
            data = await travel.get_hotel_room_types(product_id, day.isoformat(), request.end_date.isoformat())
            tags = detail.get("facilityTags")
            if isinstance(tags, list):
                facilities = [HOTEL_FACILITY_NAMES.get(value.casefold(), value)
                              for tag in tags[:8] if (value := _text(tag))]
                if facilities:
                    lines.append("设施：" + "、".join(facilities))
            items = read_items(data, "list")
            name_field, price_field = "roomTypeName", "firstNightSalePriceAmount"
        elif kind == "scenic":
            # 从景点详情展示实际评级和简介，缺少信息时跳过。
            for label, field in (("景区等级", "ratingCode"), ("景点简介", "summary")):
                if value := _text(detail.get(field)):
                    lines.append(f"{label}：{value}")
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
                    lines.append(f"接送费用：{price}")
                else:
                    lines.append("暂未查到这段接送的价格。")
                if (stock := _stock(quote)) is not None:
                    lines.append(f"可用量：{stock}")
            return ActionResult("ok", _reply("\n".join(lines)))

        for entry in items[:5]:
            label = _text(entry.get(name_field)) or ("房型" if kind == "hotel" else "可选项目")
            stock = _stock(entry)
            if kind == "flight":
                label = CABIN_NAMES.get(label, label)
                if stock == 0:
                    lines.append(f"{label}，不可售")
                    continue
            price = _money(entry, price_field, fallback_currency=_currency(row.get("currencyCode")))
            pieces = [label]
            if price:
                pieces.append(f"首晚 {price}" if kind == "hotel" else price)
            if kind == "flight" and stock is not None:
                pieces.append(f"余 {stock} 张")
            elif stock is not None:
                pieces.append(f"可订 {stock} 间" if kind == "hotel" else f"可用量 {stock}")
            lines.append("，".join(pieces))
        if not items:
            lines.append("暂未查到这段日期的房型信息。" if kind == "hotel" else "暂未查到当天的可选项目。")
        if request.detail:
            self._add_booking_rules(detail, lines)
        return ActionResult("ok", _reply("\n".join(lines)))

    @staticmethod
    def _add_booking_rules(detail: dict, lines: list[str]) -> None:
        # 只展示商品明确提供的预订条件，避免从取消时间推断是否免费。
        rule = detail.get("bookingRule")
        if not isinstance(rule, dict):
            return
        for label, field in (("最晚预订", "latestBookingTime"), ("房间保留至", "holdUntilTime"),
                             ("最少入住晚数", "minStayNights"), ("单次最多预订间数", "maxRoomCount")):
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
                lines.append(f"取消时间要求：提前 {hours} 小时")
