import asyncio
import logging
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal, InvalidOperation
from uuid import UUID

from integration.travel_client import TravelClient, TravelClientError, TravelHTTPError
from repository.dialogue import ReservedTurn
from repository.models import SupportOperation
from repository.operations import OperationRepository
from task.action.order_status import _positive_id, read_items


logger = logging.getLogger("uvicorn.error")
PAGE_SIZE = 50
MAX_PAGES = 4
REFUND_STATES = {"pending": "待处理", "approved": "审核通过", "rejected": "审核驳回", "success": "退款完成"}


class SalesInputError(ValueError):
    """可解释的售后条件问题，显示文案由服务端控制。"""


def money(value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal("99999999999.99"):
            raise ValueError("金额超出允许范围")
        cents = amount.quantize(Decimal("0.01"))
        if amount != cents:
            raise ValueError("金额最多两位小数")
        return cents
    except InvalidOperation:
        raise ValueError("金额格式有误") from None


def refund_receipt(row: dict) -> dict:
    # 不返回中台响应里的任意字段，只保存核验和展示所需的回执。
    request_id = _positive_id(row.get("refundRequestId"))
    request_no = row.get("refundRequestNo")
    status = row.get("statusCode")
    if not isinstance(request_no, str) or not request_no.strip() or len(request_no) > 100 or status not in REFUND_STATES:
        raise ValueError("退款回执不完整")
    receipt = {
        "refundRequestId": request_id, "refundRequestNo": request_no, "statusCode": status,
        "orderId": _positive_id(row.get("orderId")), "itemId": _positive_id(row.get("orderItemId")),
        "amount": str(money(row.get("requestedAmount"))),
    }
    if row.get("approvedAmount") is not None:
        receipt["approvedAmount"] = str(money(row["approvedAmount"]))
    record = row.get("refundRecord")
    if isinstance(record, dict) and isinstance(record.get("statusCode"), str):
        receipt["recordStatus"] = record["statusCode"]
    return receipt


class Refunds:
    def __init__(self, travel: TravelClient, operations: OperationRepository, zone: tzinfo) -> None:
        self._travel = travel
        self._operations = operations
        self._zone = zone

    async def requests(self, user_id: int, requested_from: str | None = None) -> tuple[list[dict], bool]:
        rows = []
        for page_no in range(1, MAX_PAGES + 1):
            page = await self._travel.list_refund_requests(
                user_id, page_no=page_no, page_size=PAGE_SIZE, requested_from=requested_from,
            )
            total = page.get("total")
            if type(total) is not int or total < 0:
                raise ValueError("退款申请分页数据无效")
            rows.extend(read_items(page, "list"))
            if page_no * PAGE_SIZE >= total:
                return rows, len(rows) == total
        return rows, False

    async def preflight(self, user_id: int, order_id: int, item_id: int) -> tuple[dict, Decimal, set[int]]:
        order, refunds, requests = await asyncio.gather(
            self._travel.get_order(user_id, order_id),
            self._travel.list_order_refund_records(user_id, order_id),
            self.requests(user_id),
        )
        if _positive_id(order.get("orderId")) != order_id or (
            order.get("userId") is not None and _positive_id(order["userId"]) != user_id
        ):
            raise SalesInputError("未找到本人的订单。")
        item = next((row for row in read_items(order, "items") if _positive_id(row.get("orderItemId")) == item_id), None)
        if item is None or item.get("statusCode") not in {"paid", "ticketed", "completed"}:
            raise SalesInputError("这条明细不存在或当前状态不支持申请退款。")
        previous, complete = requests
        if not complete:
            raise SalesInputError("暂时无法核对已有退款记录，这次申请未提交。请先到订单页查看退款进度。")
        selected = [row for row in previous if _positive_id(row.get("orderItemId")) == item_id]
        if any(row.get("statusCode") in {"pending", "approved"} for row in selected):
            raise SalesInputError("这条明细已有进行中的退款申请，不能重复提交。")
        already_refunded = sum(
            (money(row.get("amount")) for row in read_items(refunds, "list")
             if row.get("statusCode") == "success" and _positive_id(row.get("orderItemId")) == item_id),
            Decimal("0.00"),
        )
        remaining = money(item.get("saleAmount")) - already_refunded
        if remaining <= 0:
            raise SalesInputError("这条明细已没有剩余可申请金额。")
        return item, remaining, {_positive_id(row.get("refundRequestId")) for row in selected}

    async def submit(
        self, claim: ReservedTurn, user_id: int, task_id: UUID, confirmation_id: UUID, target: dict,
    ) -> SupportOperation:
        order_id, item_id = int(target["orderId"]), int(target["itemId"])
        uncertain = await self._operations.latest_uncertain(claim.session_id, order_id=order_id, item_id=item_id)
        if uncertain is not None:
            # 之前的写操作还不确定，不能换一个确认号再发一次。
            return await self.reconcile(user_id, uncertain)
        _, remaining, baseline = await self.preflight(user_id, order_id, item_id)
        amount = money(target["amount"])
        if not 0 < amount <= remaining or (target["refundType"] == "full" and amount != remaining):
            raise SalesInputError("可申请金额有变化，请重新确认退款金额。")

        row = await self._operations.prepare_refund(claim, task_id, confirmation_id, target)
        sending_target = {**target, "sentAt": datetime.now(UTC).isoformat(), "baseline": sorted(baseline)}
        if not await self._operations.mark_sending(claim, row.id, sending_target):
            existing = await self._operations.find(claim.session_id, confirmation_id)
            return await self.reconcile(user_id, existing or row)
        try:
            result = await self._travel.create_refund_request(user_id, order_id, item_id, float(amount), target["reason"])
            receipt = refund_receipt(result)
            if (receipt["orderId"], receipt["itemId"], receipt["amount"]) != (order_id, item_id, str(amount)):
                raise ValueError("退款回执目标与提交目标不一致")
        except TravelHTTPError as error:
            if error.status_code in {400, 401, 403, 404, 422}:
                return await self._operations.complete_refund(
                    row.id, "rejected", {"note": "退款申请未能提交，请核对订单是否符合退款条件。"}
                )
            return await self._mark_unknown(user_id, row.id)
        except (TravelClientError, ValueError):
            return await self._mark_unknown(user_id, row.id)
        saved = await self._operations.complete_refund(row.id, "succeeded", receipt)
        logger.info("refund_operation operation_id=%s status=%s", row.id, saved.status)
        return saved

    async def _mark_unknown(self, user_id: int, operation_id: UUID) -> SupportOperation:
        row = await self._operations.complete_refund(operation_id, "unknown", {})
        logger.warning("refund_operation operation_id=%s status=%s", operation_id, row.status)
        return await self.reconcile(user_id, row)

    async def reconcile(self, user_id: int, row: SupportOperation) -> SupportOperation:
        if row.status not in {"sending", "unknown"}:
            return row
        target = row.target_json
        # 只核对发送时点之后的新申请；时间、金额、明细之一对不上就不认领。
        try:
            sent = datetime.fromisoformat(target["sentAt"])
            from_local = (sent - timedelta(seconds=3)).astimezone(self._zone).strftime("%Y-%m-%d %H:%M:%S")
            async with asyncio.timeout(5):
                rows, complete = await self.requests(user_id, from_local)
            candidates = {}
            for item in rows:
                if str(item.get("orderId")) != str(target["orderId"]) or str(item.get("orderItemId")) != str(target["itemId"]):
                    continue
                receipt = refund_receipt(item)
                stamp = datetime.fromisoformat(item["requestedAt"])
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=self._zone)
                if (
                    receipt["refundRequestId"] not in target["baseline"] and receipt["amount"] == target["amount"]
                    and sent - timedelta(seconds=3) <= stamp <= sent + timedelta(seconds=90)
                ):
                    candidates[receipt["refundRequestId"]] = receipt
            if complete and len(candidates) == 1:
                return await self._operations.complete_refund(row.id, "succeeded", next(iter(candidates.values())))
        except (TravelClientError, TimeoutError, ValueError, TypeError, KeyError):
            pass
        return await self._operations.complete_refund(row.id, "unknown", {})
