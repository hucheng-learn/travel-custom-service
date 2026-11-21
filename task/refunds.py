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
    # 对应技术设计 3.5：preflight 核对能退多少 → submit 再预检、记录发送权并发 POST
    # → reconcile 核对不明结果。成功只表示退款申请已创建，到账仍以审核和退款流水为准。
    def __init__(self, travel: TravelClient, operations: OperationRepository, zone: tzinfo) -> None:
        self._travel = travel
        self._operations = operations
        self._zone = zone

    async def requests(self, user_id: int, requested_from: str | None = None) -> tuple[list[dict], bool]:
        # rows 汇总多页申请；total 是中台返回的总数；complete 表示是否已经翻到最后一页。
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
        # 3.5 生成确认卡前和确认提交时都要查：本人订单、明细状态、进行中申请、剩余金额。
        # 返回当前可退金额及已有申请 ID 基线；金额来自查询，不使用图中示例金额做业务判断。
        # 三个请求并发：订单详情、订单退款记录、本人退款申请列表。
        order, refunds, requests = await asyncio.gather(
            self._travel.get_order(user_id, order_id),
            self._travel.list_order_refund_records(user_id, order_id),
            self.requests(user_id),
        )
        if _positive_id(order.get("orderId")) != order_id or (
            order.get("userId") is not None and _positive_id(order["userId"]) != user_id
        ):
            raise SalesInputError("未找到本人的订单。")
        # item 是要退款的订单明细，状态必须是已支付、已出票或已完成。
        item = next((row for row in read_items(order, "items") if _positive_id(row.get("orderItemId")) == item_id), None)
        if item is None or item.get("statusCode") not in {"paid", "ticketed", "completed"}:
            raise SalesInputError("这条明细不存在或当前状态不支持申请退款。")
        # previous 是已查到的退款申请，complete 表示列表是否完整；不完整时不能判断有没有重复申请。
        previous, complete = requests
        if not complete:
            raise SalesInputError("暂时无法核对已有退款记录，这次申请未提交。请先到订单页查看退款进度。")
        # selected 是这条明细已有的退款申请，有 pending/approved 时不能重复提交。
        selected = [row for row in previous if _positive_id(row.get("orderItemId")) == item_id]
        if any(row.get("statusCode") in {"pending", "approved"} for row in selected):
            raise SalesInputError("这条明细已有进行中的退款申请，不能重复提交。")
        # already_refunded 是这条明细历史成功退款的总额，remaining 是还能申请的金额。
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
        # 3.5 同意提交分支：先核对旧操作，再重新预检，最后争取本次操作的发送权。
        # 操作记录约束本服务的确认重放；中台 POST 没有跨系统幂等键，不能承诺全局只提交一次。
        order_id, item_id = int(target["orderId"]), int(target["itemId"])
        # uncertain 是同订单同明细之前没确认结果的写操作，存在时先核对，不再重复提交。
        uncertain = await self._operations.latest_uncertain(claim.session_id, order_id=order_id, item_id=item_id)
        if uncertain is not None:
            # 之前的写操作还不确定，不能换一个确认号再发一次。
            return await self.reconcile(user_id, uncertain)
        # 提交前重新预检：确认之后金额和进行中申请可能已变化。
        # remaining 是当前剩余可退金额，baseline 是提交前已存在的退款申请 ID 集合。
        _, remaining, baseline = await self.preflight(user_id, order_id, item_id)
        amount = money(target["amount"])
        if not 0 < amount <= remaining or (target["refundType"] == "full" and amount != remaining):
            raise SalesInputError("可申请金额有变化，请重新确认退款金额。")

        # 第一步：落 pending 操作记录，(session_id, confirmation_id) 唯一，重复确认直接复用旧记录。
        row = await self._operations.prepare_refund(claim, task_id, confirmation_id, target)
        # sending_target 补上发送时间和提交前的申请基线，后续核对超时结果时要用。
        sending_target = {**target, "sentAt": datetime.now(UTC).isoformat(), "baseline": sorted(baseline)}
        # 第二步：条件更新抢占发送权，只有赢得 pending → sending 的执行者能发中台 POST。
        if not await self._operations.mark_sending(claim, row.id, sending_target):
            existing = await self._operations.find(claim.session_id, confirmation_id)
            return await self.reconcile(user_id, existing or row)
        try:
            # 3.5 真正创建退款申请的时点：前面已提交操作记录，并成功抢占 sending。
            # 使用服务端摘要发 POST，回执必须与订单、明细和金额一致才能认作明确成功。
            result = await self._travel.create_refund_request(user_id, order_id, item_id, float(amount), target["reason"])
            receipt = refund_receipt(result)
            if (receipt["orderId"], receipt["itemId"], receipt["amount"]) != (order_id, item_id, str(amount)):
                raise ValueError("退款回执目标与提交目标不一致")
        except TravelHTTPError as error:
            if error.status_code in {400, 401, 403, 404, 422}:
                # 中台明确拒绝：落 rejected 终态，不再重试。
                return await self._operations.complete_refund(
                    row.id, "rejected", {"note": "退款申请未能提交，请核对订单是否符合退款条件。"}
                )
            # 超时或结果不明：先标 unknown，再按中台现有申请核对。
            return await self._mark_unknown(user_id, row.id)
        except (TravelClientError, ValueError):
            return await self._mark_unknown(user_id, row.id)
        # 创建成功：落 succeeded 终态并保存回执。
        saved = await self._operations.complete_refund(row.id, "succeeded", receipt)
        logger.info("refund_operation operation_id=%s status=%s", row.id, saved.status)
        return saved

    async def _mark_unknown(self, user_id: int, operation_id: UUID) -> SupportOperation:
        row = await self._operations.complete_refund(operation_id, "unknown", {})
        logger.warning("refund_operation operation_id=%s status=%s", operation_id, row.status)
        return await self.reconcile(user_id, row)

    async def reconcile(self, user_id: int, row: SupportOperation) -> SupportOperation:
        # 3.5 结果不明分支：只查询 sending/unknown 对应的中台申请，不重发退款 POST。
        # 订单、明细、金额、时间窗和提交前基线都匹配，且完整列表中只有一个候选，才认领成功。
        # 否则继续保留 unknown，让用户稍后查进度，不能把网络超时当作“申请未提交”。
        if row.status not in {"sending", "unknown"}:
            return row
        # target 是提交时保存的订单、明细、金额和申请基线。
        target = row.target_json
        # 只核对发送时点之后的新申请；时间、金额、明细之一对不上就不认领。
        try:
            # sent 是发起提交的时间；from_local 往前留 3 秒，换算成中台用的业务时区再做过滤。
            sent = datetime.fromisoformat(target["sentAt"])
            from_local = (sent - timedelta(seconds=3)).astimezone(self._zone).strftime("%Y-%m-%d %H:%M:%S")
            async with asyncio.timeout(5):
                rows, complete = await self.requests(user_id, from_local)
            # candidates 按退款申请 ID 收集可能是本次提交产生的回执。
            candidates = {}
            for item in rows:
                if str(item.get("orderId")) != str(target["orderId"]) or str(item.get("orderItemId")) != str(target["itemId"]):
                    continue
                receipt = refund_receipt(item)
                # stamp 是这条申请的提交时间，没有时区时按业务时区补上再比较。
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
