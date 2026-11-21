from datetime import datetime

from integration.travel_client import TravelClient
from task.action.order_status import OrderQueryAction, Reply, display_business_time, read_items


ACTION_NAME = "query_order_itinerary"
MAX_DISPLAY_ITEMS = 10


def _display_text(value: object) -> str:
    return " ".join(value.split())[:120] if isinstance(value, str) else ""


def _stay_date(value: object) -> str | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date().isoformat()
        except ValueError:
            return None
    return None


def _hotel_line(item: dict) -> str:
    hotel = _display_text(item.get("hotelName"))
    room = _display_text(item.get("roomTypeName"))
    name = f"{hotel}，房型 {room}" if hotel and room else _display_text(item.get("productName"))
    pieces = [name or hotel or room or "酒店住宿"]
    # 酒店明细里的起止时间表示入住和退房日期，午夜只是中台的存储方式。
    for label, field in (("入住", "travelTime"), ("退房", "travelEndTime")):
        if day := _stay_date(item.get(field)):
            pieces.append(f"{label} {day}")
    if traveler := _display_text(item.get("travelerName")):
        # 行程会写入聊天历史，只显示已校验订单关联的脱敏姓名。
        masked = f"{traveler[0]}**" if len(traveler) > 1 else "*"
        pieces.append(f"入住人 {masked}")
    return "- " + "，".join(pieces)


class OrderItineraryAction(OrderQueryAction):
    # 对应技术设计 3.7 “这笔订单哪天入住”：先由父类核验订单，再展示实际明细行程。
    # 酒店按 travelTime/travelEndTime 展示入住/退房日期，不能用下单时间推测。
    async def _render_order(
        self, travel: TravelClient, user_id: int, order_id: int, order_number: str, order: dict
    ) -> tuple[Reply, ...]:
        # 行程只读已校验归属的订单明细，回复只提取展示所需字段。
        items = read_items(order, "items")
        lines = [f"订单 {order_number} 的行程："]
        for item in items[:MAX_DISPLAY_ITEMS]:
            if item.get("productTypeCode") == "hotel_room":
                lines.append(_hotel_line(item))
                continue
            name = _display_text(item.get("productName"))
            travel_from = display_business_time(item.get("travelTime"), self._business_timezone)
            travel_to = display_business_time(item.get("travelEndTime"), self._business_timezone)
            line = f"- {name or '出行项目'}"
            if travel_from:
                line += f"：开始 {travel_from}"
            if travel_to:
                line += f"；结束 {travel_to}" if travel_from else f"：结束 {travel_to}"
            if not travel_from and not travel_to:
                line += "：暂未返回出行时间"
            lines.append(line)
        if not items:
            lines.append("没有查到这笔订单的行程明细。")
        elif len(items) > MAX_DISPLAY_ITEMS:
            lines.append(f"这里只展示前 {MAX_DISPLAY_ITEMS} 条，请到订单页查看其余明细。")
        return (
            {"type": "text", "text": "\n".join(lines)},
            {"type": "object", "object": {
                "type": "order", "id": str(order_id), "title": f"订单 {order_number}",
                "attributes": {"orderNo": order_number},
            }},
        )
