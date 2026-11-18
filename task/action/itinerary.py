from integration.travel_client import TravelClient
from task.action.order_status import OrderQueryAction, Reply, display_business_time, read_items


ACTION_NAME = "query_order_itinerary"
MAX_DISPLAY_ITEMS = 10


class OrderItineraryAction(OrderQueryAction):
    async def _render_order(
        self, travel: TravelClient, user_id: int, order_id: int, order_number: str, order: dict
    ) -> tuple[Reply, ...]:
        # 行程只读订单明细，不查支付；不向前端透传旅客姓名、证件等原始数据。
        items = read_items(order, "items")
        lines = [f"订单 {order_number} 的行程："]
        for item in items[:MAX_DISPLAY_ITEMS]:
            raw_name = item.get("productName")
            name = " ".join(raw_name.split())[:120] if isinstance(raw_name, str) else ""
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
            lines.append("中台没有返回这笔订单的行程明细。")
        elif len(items) > MAX_DISPLAY_ITEMS:
            lines.append(f"这里只展示前 {MAX_DISPLAY_ITEMS} 条，请到订单页查看其余明细。")
        return (
            {"type": "text", "text": "\n".join(lines)},
            {"type": "object", "object": {
                "type": "order", "id": str(order_id), "title": f"订单 {order_number}",
                "attributes": {"orderNo": order_number},
            }},
        )
