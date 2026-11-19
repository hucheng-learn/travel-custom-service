import asyncio
from typing import Literal

from integration.travel_client import TravelClient
from task.action.order_status import _amount, _positive_id, read_items
from task.refunds import refund_receipt


type ObjectKind = Literal["orders", "products", "refunds", "tickets"]


async def travel_objects(
    travel: TravelClient, user_id: int, kind: ObjectKind, page_no: int,
) -> tuple[list[dict], bool]:
    # 商品来自已订明细。限制每页订单数，避免一次打开侧栏就读取大量订单详情。
    page_size = 5 if kind == "products" else 10
    if kind == "refunds":
        page = await travel.list_refund_requests(user_id, page_no=page_no, page_size=page_size)
        cards = [_refund_card(refund_receipt(row)) for row in read_items(page, "list")]
    else:
        page = await travel.list_orders(user_id, page_no=page_no, page_size=page_size)
        orders = [_order_card(row) for row in read_items(page, "list")]
        cards = orders if kind == "orders" else await _booked_products(travel, user_id, orders)

    total = page.get("total")
    if type(total) is not int or total < 0:
        raise ValueError("中台分页数据无效")
    return cards, page_no * page_size < total


def _order_card(row: dict) -> dict:
    order_id = str(_positive_id(row.get("orderId")))
    order_no = row.get("orderNo")
    if not isinstance(order_no, str) or not order_no:
        raise ValueError("中台订单缺少订单号")
    return {
        "type": "order", "id": order_id, "title": f"订单 {order_no}",
        "attributes": {
            "orderNo": order_no, "status": row.get("statusCode"), "orderType": row.get("orderTypeCode"),
            "payableAmount": _amount(row.get("payableAmount")),
        },
    }


async def _booked_products(travel: TravelClient, user_id: int, orders: list[dict]) -> list[dict]:
    # 同时读取本页详情，中台仍按当前身份做归属校验。
    details = await asyncio.gather(*(travel.get_order(user_id, int(order["id"])) for order in orders))
    cards = []
    for order, detail in zip(orders, details, strict=True):
        owner = detail.get("userId")
        if str(_positive_id(detail.get("orderId"))) != order["id"] or (owner is not None and _positive_id(owner) != user_id):
            raise ValueError("中台订单详情归属不一致")
        for item in read_items(detail, "items"):
            cards.append({
                "type": "product", "id": str(_positive_id(item.get("orderItemId"))),
                "title": str(item.get("productName") or "已订商品"),
                "attributes": {
                    "orderId": order["id"], "orderNo": order["attributes"]["orderNo"],
                    "productType": item.get("productTypeCode"), "amount": _amount(item.get("saleAmount")),
                },
            })
    return cards


def _refund_card(receipt: dict) -> dict:
    return {
        "type": "refund_request", "id": str(receipt["refundRequestId"]), "title": receipt["refundRequestNo"],
        "attributes": {"status": receipt["statusCode"], "amount": receipt["amount"]},
    }
