import asyncio
import json
import re
from datetime import UTC, datetime, timedelta, tzinfo
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import yaml

from integration.travel_client import TravelClient, TravelClientError, TravelHTTPError
from plan.fixed_order_plan import extract_order_number
from plan.models import REFUND, TICKET, FlowId, OrderReference
from repository.dialogue import ReservedTurn
from repository.models import SupportOperation
from repository.operations import OperationRepository
from task.action.order_status import _positive_id, read_items
from task.flow.executor import FlowResult, FlowTask, text_reply
from task.refunds import REFUND_STATES, Refunds, SalesInputError, money, refund_receipt
from task.sales_state import Confirmation, SalesDraft


START_REFUND = re.compile(r"^(?:请|帮我|我要|我想)?\s*(?:申请退款|提交退款|发起退款|退款申请|退款|退掉)")
START_TICKET = re.compile(r"^(?:请|帮我|我要|我想)?\s*(?:投诉|提交工单|创建工单|售后工单)")
REFUND_STATUS = re.compile(r"^(?:(?:帮我)?(?:查|查询|查看)(?:一下)?退款(?:进度|状态)|退款进度查询)\s*[:：]?\s*(.*)$")
TEXT_CONFIRM = {"确认", "确认提交", "确认退款", "确认创建工单"}
TEXT_REJECT = {"不提交", "拒绝", "放弃提交"}
TICKET_TYPES = {"投诉": "complaint", "售后": "after_sales", "退款咨询": "refund"}
# 每种售后流程必须收集的字段，顺序决定追问顺序；后面补资料和预览都按这份清单判断是否收集完整。
REQUIRED = {
    REFUND: ("order", "item", "refund_type", "amount", "reason"),
    TICKET: ("ticket_type", "order", "description"),
}


def sales_intent(text: str) -> FlowId | None:
    # “退款多久到账”“退款审核通过了吗”是问答，不能启动写操作。
    normalized = text.strip().rstrip("。！!")
    match = START_REFUND.match(normalized)
    if match and (match.end() == len(normalized) or normalized[match.end():match.end() + 1] in {" ", "：", ":"}):
        return REFUND
    if START_TICKET.match(normalized):
        return TICKET
    return None


def load_sales_config(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("ticket_next_step"), str):
        raise ValueError("售后配置缺少工单提示")
    flows = loaded.get("flows", {})
    if set(flows) != set(REQUIRED):
        raise ValueError("售后配置必须定义退款和工单")
    for flow_id, keys in REQUIRED.items():
        if set(flows[flow_id]) != set(keys) or any(not isinstance(value, str) or not value.strip()
                                                for value in flows[flow_id].values()):
            raise ValueError(f"{flow_id} 缺少可用的收集步骤")
    return loaded


class AfterSales:
    def __init__(self, config: dict, travel: TravelClient | None, operations: OperationRepository,
                 zone: tzinfo, refund_write_enabled: bool) -> None:
        self._prompts = config["flows"]
        self._versions = {
            flow_id: sha256(json.dumps(prompts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            for flow_id, prompts in self._prompts.items()
        }
        self._travel = travel
        self._operations = operations
        self._refunds = Refunds(travel, operations, zone) if travel is not None else None
        self._refund_write_enabled = refund_write_enabled

    def supports(self, flow_id: FlowId) -> bool:
        return flow_id in self._prompts

    def can_resume(self, task: FlowTask) -> bool:
        # 版本号对不上说明售后配置改过，旧任务不能按新提示继续收集。
        return task.sales is not None and task.flow_version == self._versions.get(task.flow_id)

    def start(self, flow_id: FlowId) -> FlowResult:
        # 新任务从这份流程的第一个必填字段开始，sales 草稿是空的。
        task = FlowTask(flow_id=flow_id, flow_version=self._versions[flow_id], step_id="start", sales=SalesDraft())
        return self._ask(task, REQUIRED[flow_id][0])

    def resume(self, task: FlowTask) -> FlowResult:
        # 暂停期间旧摘要作废；恢复时重新预检并发新确认 ID。
        if task.step_id == "confirm":
            task.step_id = "review"
        if task.step_id == "review":
            return FlowResult(task, [text_reply("已恢复，回复「重新确认」以重新核对售后摘要。")])
        return self._ask(task, task.step_id)

    async def process(self, user_id: int, task: FlowTask, incoming: dict, claim: ReservedTurn) -> FlowResult:
        # draft 是任务收集到现在的售后资料；object_data 来自确认卡或订单卡片；
        # text 是去掉句末标点后的用户原文；claim 用来给写操作做幂等和归属校验。
        draft = task.sales
        if draft is None:
            raise RuntimeError("售后任务缺少草稿")
        object_data = incoming.get("object", {})
        text = incoming.get("text", "").strip().rstrip("。！!")
        try:
            async with asyncio.timeout(22):
                # 确认卡、文本确认或拒绝、确认前修改，其余情况按当前步骤继续收集。
                if object_data.get("type") == "confirmation":
                    # 用户点了确认卡，进入 _confirm 执行真正的提交。
                    return await self._confirm(user_id, task, UUID(object_data["id"]), object_data["decision"], claim)
                if text in TEXT_CONFIRM | TEXT_REJECT:
                    if draft.confirmation is None:
                        return self._ask(task, task.step_id, "请先补齐资料并核对确认摘要。")
                    return await self._confirm(
                        user_id, task, draft.confirmation.id, "reject" if text in TEXT_REJECT else "confirm", claim
                    )
                if task.step_id in {"confirm", "review"}:
                    # 确认前改金额、原因或订单，旧确认卡随之失效。
                    return await self._change_or_review(user_id, task, text, object_data)
                # 按当前步骤收集订单、明细、退款类型、金额或原因等。
                return await self._collect(user_id, task, text, object_data)
        except SalesInputError as error:
            return FlowResult(task, [text_reply(str(error))])
        except TravelHTTPError as error:
            if error.status_code == 401:
                raise
            message = "没有找到本人的订单或明细。" if error.status_code in {403, 404} else "暂时无法办理售后，请稍后再试。"
            return FlowResult(task, [text_reply(message)])
        except (TravelClientError, TimeoutError, ValueError):
            return FlowResult(task, [text_reply("暂未核实售后结果。请稍后重试；如果刚提交了退款，请先查退款进度。")])

    async def replay(self, user_id: int, claim: ReservedTurn, confirmation_id: UUID, decision: str) -> FlowResult | None:
        previous = await self._operations.find(claim.session_id, confirmation_id)
        if previous is None or previous.status == "pending":
            return None
        if decision == "reject":
            return FlowResult(None, [text_reply("这次确认已经处理，不能通过拒绝确认撤销已提交的申请。")])
        if self._refunds is not None and previous.type == "refund":
            previous = await self._refunds.reconcile(user_id, previous)
        return self._receipt(previous)

    async def status(self, user_id: int, claim: ReservedTurn, search: str) -> list[dict]:
        # search 是“查退款进度”后面附带的申请编号、订单号，空字符串表示查最近的申请。
        if self._refunds is None or self._travel is None:
            return [text_reply("暂时无法查询退款进度，请稍后再试。")]
        try:
            async with asyncio.timeout(12):
                if not search:
                    uncertain = await self._operations.latest_uncertain(claim.session_id)
                    if uncertain:
                        return self._receipt(await self._refunds.reconcile(user_id, uncertain)).messages
                if re.fullmatch(r"[0-9]{1,19}", search):
                    return self._refund_messages(refund_receipt(await self._travel.get_refund_request(user_id, int(search))))
                rows, complete = await self._refunds.requests(user_id)
                if search.startswith("订单号"):
                    order_text, _, item_text = search.partition(" 明细 ")
                    reference = self._order_reference(order_text, {})
                    order = await self._owned_order(user_id, reference)
                    rows = [row for row in rows if str(row.get("orderId")) == str(order["orderId"])]
                    if item_text:
                        item_id = _positive_id(item_text.strip())
                        rows = [row for row in rows if _positive_id(row.get("orderItemId")) == item_id]
                elif search:
                    rows = [row for row in rows if row.get("refundRequestNo") == search]
                if not rows:
                    note = "没有查到退款申请。" if complete else "已查范围内没有找到，请发退款申请 ID 核对。"
                    return [text_reply(note)]
                if search and len(rows) == 1:
                    refund_id = _positive_id(rows[0].get("refundRequestId"))
                    return self._refund_messages(refund_receipt(await self._travel.get_refund_request(user_id, refund_id)))
                messages = [text_reply("下面是查到的申请。可发送「查退款进度 申请编号」查看指定申请。")]
                for row in rows[:5]:
                    messages.extend(self._refund_messages(refund_receipt(row)))
                return messages
        except TravelHTTPError as error:
            if error.status_code == 401:
                raise
            if error.status_code in {403, 404}:
                return [text_reply("没有查到本人的退款申请。")]
        except (SalesInputError, TravelClientError, TimeoutError, ValueError):
            pass
        return [text_reply("暂时无法核对退款进度，请稍后重试。")]

    async def _collect(self, user_id: int, task: FlowTask, text: str, obj: dict) -> FlowResult:
        # draft 是售后草稿，step 是当前要收集的字段，text/obj 是本轮回答。
        draft = task.sales
        step = task.step_id
        if obj and not (step == "order" and obj.get("type") == "order"):
            return self._ask(task, step, "当前步骤不需要订单卡片。")
        if step == "order":
            if task.flow_id == TICKET and text in {"无订单", "不关联订单"}:
                draft.no_order = True
            elif (reference := self._order_reference(text, obj)) is not None:
                # reference 是从原文或订单卡片识别出的订单引用，还要再查一次确认是本人的订单。
                order = await self._owned_order(user_id, reference)
                draft.order_id = _positive_id(order.get("orderId"))
                draft.order_no = order.get("orderNo")
                if task.flow_id == REFUND:
                    items = read_items(order, "items")
                    if len(items) == 1:
                        draft.item_id = _positive_id(items[0].get("orderItemId"))
                    elif items:
                        choices = [f"{_positive_id(item.get('orderItemId'))}：{str(item.get('productName', ''))[:80]}"
                                   for item in items[:8]]
                        return self._ask(task, "item", "可选明细：\n" + "\n".join(choices))
                    else:
                        draft.order_id = None
                        return self._ask(task, "order", "没有查到可申请退款的订单项目。")
            else:
                return self._ask(task, step)
        elif step == "item":
            if not re.fullmatch(r"[0-9]{1,19}", text):
                return self._ask(task, step)
            order = await self._owned_order(user_id, OrderReference(kind="order_id", value=str(draft.order_id)))
            item_id = _positive_id(text)
            if not any(_positive_id(item.get("orderItemId")) == item_id for item in read_items(order, "items")):
                return self._ask(task, step, "这个明细不属于选中的订单。")
            draft.item_id = item_id
        elif step == "refund_type":
            if text not in {"全额", "全额退款", "部分", "部分退款"}:
                return self._ask(task, step)
            draft.refund_type = "full" if text.startswith("全额") else "partial"
        elif step == "amount":
            try:
                amount = money(text.removesuffix("元").strip())
            except ValueError:
                return self._ask(task, step, "请输入大于零且最多两位小数的金额。")
            if amount <= 0:
                return self._ask(task, step, "退款金额必须大于零。")
            draft.amount = str(amount)
        elif step == "ticket_type":
            if text not in TICKET_TYPES:
                return self._ask(task, step)
            draft.ticket_type = TICKET_TYPES[text]
        elif step in {"reason", "description"}:
            if not text or len(text) > (500 if step == "reason" else 1000):
                return self._ask(task, step, "说明为空或过长，请简要描述后重发。")
            setattr(draft, step, text)
        else:
            raise ValueError("未知售后步骤")
        for field in REQUIRED[task.flow_id]:
            missing = {
                "order": draft.order_id is None and not draft.no_order,
                "item": draft.item_id is None, "refund_type": draft.refund_type is None,
                "amount": draft.refund_type == "partial" and draft.amount is None, "reason": draft.reason is None,
                "ticket_type": draft.ticket_type is None, "description": draft.description is None,
            }[field]
            if missing:
                return self._ask(task, field)
        return await self._preview(user_id, task)

    async def _change_or_review(self, user_id: int, task: FlowTask, text: str, obj: dict) -> FlowResult:
        draft = task.sales
        if obj.get("type") == "order" or text in {"换订单", "修改订单", "修改明细", "换明细"}:
            draft.confirmation = None
            draft.amount = None
            draft.item_id = None
            if text in {"修改明细", "换明细"} and task.flow_id == REFUND:
                return self._ask(task, "item")
            draft.order_id = None
            draft.order_no = None
            draft.no_order = False
            task.step_id = "order"
            if obj:
                return await self._collect(user_id, task, "", obj)
            if task.flow_id == REFUND:
                # 换单后要重新核对明细和金额，提示用户哪些退款资料可以继续使用。
                return self._ask(task, "order", "已清除原订单、明细和金额，原确认卡已失效。已保留退款方式和原因。")
            return self._ask(task, "order")
        for prefix, field in (("修改金额", "amount"), ("修改原因", "reason"), ("修改描述", "description")):
            if text.startswith(prefix) and field in REQUIRED[task.flow_id]:
                draft.confirmation = None
                setattr(draft, field, None)
                if field == "amount":
                    draft.refund_type = "partial"
                rest = text.removeprefix(prefix).lstrip(" ：:")
                task.step_id = field
                return await self._collect(user_id, task, rest, {}) if rest else self._ask(task, field)
        if text == "重新确认" or task.step_id == "review":
            return await self._preview(user_id, task)
        return FlowResult(task, [text_reply("请用确认卡提交或不提交。若要修改，先发「修改金额」「修改描述」或「修改订单」。")])

    async def _preview(self, user_id: int, task: FlowTask) -> FlowResult:
        # 生成服务端确认卡（5 分钟内有效），此时还没有产生任何写操作。
        # draft 是已收集完整的售后草稿；target 是最终要提交给中台的字段快照；
        # details 是给用户核对的摘要。
        draft = task.sales
        draft.confirmation = None
        task.step_id = "review"
        task.missing_slots = []
        if task.flow_id == REFUND:
            if self._refunds is None:
                raise SalesInputError("暂时无法核对退款信息，请稍后再试。")
            # 预检订单状态、进行中申请和剩余可退金额。
            # item 是选中的订单明细，remaining 是这条明细当前还能退的金额。
            item, remaining, _ = await self._refunds.preflight(user_id, draft.order_id, draft.item_id)
            amount = remaining if draft.refund_type == "full" else money(draft.amount)
            if not 0 < amount <= remaining:
                draft.amount = None
                return self._ask(task, "amount", f"这条明细最多可申请 {remaining}，请修改金额。")
            target = {
                "orderId": draft.order_id, "orderNo": draft.order_no, "itemId": draft.item_id,
                "refundType": draft.refund_type, "amount": str(amount), "reason": draft.reason,
            }
            title = "确认退款申请"
            details = (f"订单 {draft.order_no}，明细 {draft.item_id}（{str(item.get('productName', ''))[:80]}），"
                       f"申请退款 {amount}。原因：{draft.reason}。")
        else:
            target = {"orderId": draft.order_id, "orderNo": draft.order_no, "type": draft.ticket_type, "description": draft.description}
            title = "确认创建客服工单"
            details = f"类型：{draft.ticket_type}；订单：{draft.order_no or '无'}；描述：{draft.description}"
        draft.confirmation = Confirmation(id=uuid4(), expires_at=datetime.now(UTC) + timedelta(minutes=5), target=target)
        task.step_id = "confirm"
        return FlowResult(task, [
            text_reply(details + " 请核对后再确认，当前尚未提交。"),
            {"type": "object", "object": {"type": "confirmation", "id": str(draft.confirmation.id), "title": title,
              "attributes": {"summary": details, "expiresAt": draft.confirmation.expires_at.isoformat()}}},
        ])

    async def _confirm(
        self, user_id: int, task: FlowTask, confirmation_id: UUID, decision: str, claim: ReservedTurn,
    ) -> FlowResult:
        # draft 是售后草稿，card 是当前唯一有效的确认卡，operation 是落库的写操作记录。
        draft = task.sales
        card = draft.confirmation
        if task.step_id != "confirm" or card is None or card.id != confirmation_id:
            return FlowResult(task, [text_reply("该确认卡已失效或不属于当前任务。")])
        if card.expires_at <= datetime.now(UTC):
            draft.confirmation = None
            task.step_id = "review"
            return FlowResult(task, [text_reply("确认已过期，请发「重新确认」。")])
        if decision == "reject":
            return FlowResult(None, [text_reply("已取消本次提交，没有新建申请或工单。")])
        if task.flow_id == REFUND:
            if not self._refund_write_enabled or self._refunds is None:
                # 提交功能关闭时，明确告知这次没有产生退款申请。
                return FlowResult(task, [text_reply("当前无法在这里提交退款申请。这次申请未提交。")])
            try:
                # 先落写操作记录再发中台请求，防重逻辑在 Refunds.submit 里。
                operation = await self._refunds.submit(claim, user_id, task.task_id, card.id, card.target)
            except SalesInputError as error:
                draft.confirmation = None
                task.step_id = "review"
                return FlowResult(task, [text_reply(str(error))])
        else:
            if card.target["orderId"] is not None:
                await self._owned_order(user_id, OrderReference(kind="order_id", value=str(card.target["orderId"])))
            # 工单和创建事件在同一事务里写入；重复确认按确认 ID 回放原工单，不会重复建单。
            operation = await self._operations.create_ticket(claim, user_id, task.task_id, card.id, card.target)
        return self._receipt(operation)

    @staticmethod
    def _refund_messages(result: dict) -> list[dict]:
        summary = f"退款申请 {result['refundRequestNo']}：{REFUND_STATES[result['statusCode']]}，申请金额 {result['amount']}。"
        if result.get("approvedAmount") is not None:
            summary += f"批准金额 {result['approvedAmount']}。"
        if result.get("recordStatus") == "success":
            summary += "关联退款流水已成功；银行入账时间以实际渠道为准。"
        return [
            text_reply(summary),
            {"type": "object", "object": {"type": "refund_request", "id": str(result["refundRequestId"]),
              "title": result["refundRequestNo"], "attributes": {"status": result["statusCode"], "amount": result["amount"]}}},
        ]

    def _receipt(self, operation: SupportOperation) -> FlowResult:
        result = operation.result_json or {}
        if operation.status == "succeeded":
            if operation.type == "refund":
                return FlowResult(None, self._refund_messages(result))
            return FlowResult(None, [
                text_reply(f"工单 {result['ticketNo']} 已创建。{result['nextStep']}"),
                {"type": "object", "object": {"type": "ticket", "id": str(result["ticketId"]), "title": result["ticketNo"],
                  "attributes": {"status": result["status"]}}},
            ])
        if operation.status == "rejected":
            return FlowResult(None, [text_reply(result["note"])])
        return FlowResult(None, [text_reply("退款提交结果待核对，系统不会重复提交。请发送「查退款进度」。")])

    def _ask(self, task: FlowTask, field: str, extra: str = "") -> FlowResult:
        # field 既是接下来要收集的字段名，也是售后配置里提示词的键。
        task.step_id = field
        task.missing_slots = [field] if field in self._prompts[task.flow_id] else []
        prompt = self._prompts[task.flow_id].get(field, "请回复「重新确认」或使用当前确认卡。")
        return FlowResult(task, [text_reply(f"{extra}\n{prompt}" if extra else prompt)])

    @staticmethod
    def _order_reference(text: str, obj: dict) -> OrderReference | None:
        if obj.get("type") == "order":
            return OrderReference(kind="order_id", value=obj["id"])
        number = extract_order_number(text)
        return OrderReference(kind="order_no", value=number) if number else None

    async def _owned_order(self, user_id: int, reference: OrderReference | None) -> dict:
        # reference 可能是订单卡片里的主键，也可能是用户输入的订单号；
        # 订单号要翻页找到对应订单，再统一用主键查详情并校验归属。
        if self._travel is None or reference is None:
            raise SalesInputError("暂不能定位订单，请提供有效的本人订单号。")
        if reference.kind == "order_id":
            order_id = int(reference.value)
        else:
            order_id = None
            for page_no in range(1, 5):
                page = await self._travel.list_orders(user_id, page_no=page_no, page_size=50)
                match = next((row for row in read_items(page, "list") if row.get("orderNo") == reference.value), None)
                if match is not None:
                    order_id = _positive_id(match.get("orderId"))
                    break
                if type(page.get("total")) is not int or page_no * 50 >= page["total"]:
                    break
            if order_id is None:
                raise SalesInputError("已查范围内未找到本人订单，可改发订单卡片。")
        order = await self._travel.get_order(user_id, order_id)
        owner = order.get("userId")
        if _positive_id(order.get("orderId")) != order_id or (owner is not None and _positive_id(owner) != user_id):
            raise SalesInputError("没有找到本人的订单。")
        return order
