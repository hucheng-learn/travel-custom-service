import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from integration.travel_client import TravelClient, TravelClientError, TravelHTTPError
from plan.models import ORDER_NUMBER_TOKEN, OrderReference


type Reply = dict[str, Any]
type Outcome = Literal["ok", "not_found", "search_limited", "unavailable", "need_selection"]

ACTION_NAME = "query_order_status"
OUTCOMES = frozenset({"ok", "not_found", "search_limited", "unavailable"})
PAGE_SIZE = 50
MAX_PAGES = 5
LOOKUP_SECONDS = 12
ORDER_STATES = {
    "pending_payment": "待支付",
    "paid": "已支付",
    "in_progress": "进行中",
    "finished": "已完成",
    "cancelled": "已取消",
}
SUCCESSFUL_PAYMENTS = frozenset({"success", "paid", "succeeded"})


@dataclass(frozen=True)
class ActionResult:
    outcome: Outcome
    messages: tuple[Reply, ...] = ()
    order_id: int | None = None


def read_items(payload: dict, key: str) -> list[dict]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("中台列表格式不符合约定")
    return value


def _positive_id(value: object) -> int:
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("中台主键格式不符合约定")
    return value


def _amount(value: object) -> str | None:
    if not isinstance(value, (int, float, str)) or len(str(value)) > 32:
        return None
    try:
        money = Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return format(money, "f") if money.is_finite() else None


def _currency(value: object) -> str | None:
    if isinstance(value, str) and len(value) == 3 and value.isascii() and value.isalpha() and value.isupper():
        return value
    return None


def display_business_time(value: object, business_timezone: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if value == parsed.date().isoformat():
        return value  # 中台只提供日期时，不人为补一个午夜时间。
    # 无时区时间只用于文字展示，并标明它来自中台业务时区，不当作 UTC。
    rendered = parsed.isoformat(sep=" ", timespec="seconds")
    return f"{rendered}（{business_timezone}）" if parsed.tzinfo is None else rendered


class OrderQueryAction:
    """共用订单定位、归属校验和查询时限；子类只负责各自的查询结果。"""

    def __init__(self, travel: TravelClient | None, business_timezone: str) -> None:
        self._travel = travel
        self._business_timezone = business_timezone

    async def execute(self, user_id: int, reference: OrderReference) -> ActionResult:
        travel = self._travel
        if travel is None:
            return ActionResult("unavailable")
        try:
            # 每个请求有自己的超时，整个翻页和详情查询也共用一份时间预算。
            async with asyncio.timeout(LOOKUP_SECONDS):
                if reference.kind == "order_id":
                    # 卡片只给 ID；也必须使用本轮用户身份由中台验证归属。
                    return await self._get_detail(travel, user_id, int(reference.value), None)
                for page_no in range(1, MAX_PAGES + 1):
                    page = await travel.list_orders(user_id, page_no=page_no, page_size=PAGE_SIZE)
                    orders = read_items(page, "list")
                    total = page.get("total")
                    if type(total) is not int or total < 0:
                        return ActionResult("unavailable")
                    for order in orders:
                        # 文本是订单号，不是订单主键；只在该用户的订单列表中精确匹配。
                        if order.get("orderNo") == reference.value:
                            return await self._get_detail(travel, user_id, _positive_id(order.get("orderId")), reference.value)
                    if page_no * PAGE_SIZE >= total:
                        return ActionResult("not_found")
                    if not orders:
                        return ActionResult("unavailable")
                return ActionResult("search_limited")
        except TravelHTTPError as error:
            if error.status_code in {401, 403}:
                raise
            return ActionResult("unavailable")
        except (TravelClientError, TimeoutError, ValueError):
            return ActionResult("unavailable")

    async def _get_detail(
        self, travel: TravelClient, user_id: int, order_id: int, expected_number: str | None
    ) -> ActionResult:
        try:
            order = await travel.get_order(user_id, order_id)
            # 中台有时不返回 userId；有该字段时再做一层归属检查。
            owner_id = order.get("userId")
            if owner_id is not None and _positive_id(owner_id) != user_id:
                return ActionResult("not_found")
            order_number = order.get("orderNo")
            if (
                _positive_id(order.get("orderId")) != order_id
                or not isinstance(order_number, str) or ORDER_NUMBER_TOKEN.fullmatch(order_number) is None
                or (expected_number is not None and order_number != expected_number)
            ):
                return ActionResult("unavailable")
            messages = await self._render_order(travel, user_id, order_id, order_number, order)
        except TravelHTTPError as error:
            if error.status_code in {403, 404}:
                return ActionResult("not_found")
            raise
        return ActionResult("ok", messages, order_id)

    async def _render_order(
        self, travel: TravelClient, user_id: int, order_id: int, order_number: str, order: dict
    ) -> tuple[Reply, ...]:
        raise NotImplementedError


class OrderStatusAction(OrderQueryAction):
    async def _render_order(
        self, travel: TravelClient, user_id: int, order_id: int, order_number: str, order: dict
    ) -> tuple[Reply, ...]:
        status_code = order.get("statusCode")
        if not isinstance(status_code, str):
            raise ValueError("中台订单缺少状态字段")
        currency = _currency(order.get("currencyCode"))
        payments = await travel.list_order_payments(user_id, order_id)

        # 只往客服回复放必要字段；不要把旅客姓名、证件等原始中台数据带到前端。
        attributes = {"orderNo": order_number, "statusCode": status_code}
        paid_amount = _amount(order.get("paidAmount"))
        if currency:
            attributes["currencyCode"] = currency
        if paid_amount is not None:
            attributes["paidAmount"] = paid_amount
        lines = [f"订单 {order_number} 的状态：{ORDER_STATES.get(status_code, '待确认')}。"]
        if paid_amount is not None and currency:
            lines.append(f"订单实付：{paid_amount} {currency}。")

        payment_rows = read_items(payments, "list")
        succeeded = [row for row in payment_rows if isinstance(row.get("statusCode"), str)
                     and row["statusCode"] in SUCCESSFUL_PAYMENTS]
        if succeeded:
            lines.append(f"查到 {len(succeeded)} 笔成功支付记录。")
            for row in succeeded[:3]:
                amount = _amount(row.get("amount"))
                paid_at = display_business_time(row.get("paidAt"), self._business_timezone)
                payment_currency = _currency(row.get("currencyCode")) or currency
                if amount is not None and payment_currency:
                    time_note = f"，支付时间：{paid_at}" if paid_at else ""
                    lines.append(f"支付 {amount} {payment_currency}{time_note}。")
                elif paid_at:
                    lines.append(f"支付时间：{paid_at}。")
        else:
            lines.append("没有查到成功的支付记录。" if payment_rows else "没有查到支付记录。")
        return (
            {"type": "text", "text": "\n".join(lines)},
            {"type": "object", "object": {
                "type": "order", "id": str(order_id), "title": f"订单 {order_number}", "attributes": attributes
            }},
        )
