import asyncio
import json
import re
from datetime import UTC, datetime, timedelta, tzinfo
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import yaml

from integration.travel_client import TravelClient, TravelClientError, TravelHTTPError
from plan.input_values import extract_order_number
from plan.models import REFUND, TICKET, FlowId, OrderReference
from repository.dialogue import ReservedTurn
from repository.models import SupportOperation
from repository.operations import OperationRepository
from task.action.order_status import _positive_id, read_items
from task.flow.executor import FlowResult, FlowTask, text_reply
from task.refunds import REFUND_STATES, Refunds, SalesInputError, money, refund_receipt
from task.sales_state import Confirmation, SalesDraft


TICKET_TYPES = {"投诉": "complaint", "售后": "after_sales", "退款咨询": "refund"}
# 每种售后流程必须收集的字段，顺序决定追问顺序；后面补资料和预览都按这份清单判断是否收集完整。
REQUIRED = {
    REFUND: ("order", "item", "refund_type", "amount", "reason"),
    TICKET: ("ticket_type", "order", "description"),
}


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
    # 对应技术设计 3.5/3.6：start 建任务 → apply/_collect 存经过校验的结构化资料
    # → _preview 核对摘要 → _confirm 接受有效确认 → _receipt 返回持久化回执。
    # 收集顺序在代码中，YAML 只给追问文案；确认前尚未创建退款申请或工单。
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

    def waiting_prompt(self, task: FlowTask) -> str:
        # 给理解层提供当前问题，不发送服务端确认目标或已收集的原因、描述正文。
        return self._prompts[task.flow_id].get(task.step_id, "请核对售后摘要；真正提交须使用当前有效确认卡。")

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
        # 这里只接结构化卡片。普通文本必须先经过 TurnPlanner 和 Validator，不能绕开理解层。
        obj = incoming.get("object", {})
        if obj.get("type") == "confirmation":
            return await self._protect(task, self._confirm(user_id, task, UUID(obj["id"]), obj["decision"], claim))
        if obj.get("type") == "order" and task.step_id in {"order", "review", "confirm"}:
            return await self.apply(user_id, task, {"order": OrderReference(kind="order_id", value=obj["id"])},
                                    replace=task.step_id != "order")
        return FlowResult(task, [text_reply("当前步骤不需要这张卡片，请补充正在询问的资料。")])

    async def _protect(self, task, operation):
        try:
            # 文本现在先调用模型（最多12秒）；售后最多16秒，为30秒轮次租约预留提交余量。
            async with asyncio.timeout(16):
                return await operation
        except SalesInputError as error:
            return FlowResult(task, [text_reply(str(error))])
        except TravelHTTPError as error:
            if error.status_code == 401:
                raise
            message = "没有找到本人的订单或明细。" if error.status_code in {403, 404} else "暂时无法办理售后，请稍后再试。"
            return FlowResult(task, [text_reply(message)])
        except (TravelClientError, TimeoutError, ValueError):
            return FlowResult(task, [text_reply("暂未核实售后结果。请稍后重试；如果刚提交了退款，请先查退款进度。")])

    async def apply(self, user_id, task, changes, *, replace=False, clear_field=None):
        # 一句话可补多项资料；先在任务副本上作废旧确认，再按业务依赖顺序应用。
        # 中台校验失败仍保留“确认已失效”的新状态，不能让旧摘要被随后点击提交。
        task = task.model_copy(deep=True)
        return await self._protect(task, self._apply(user_id, task, changes, replace, clear_field))

    async def preview(self, user_id, task):
        # 文字“提交吧”只生成/重发确认卡；此入口永不调用 _confirm 或业务写接口。
        return await self._protect(task, self._advance(user_id, task))

    async def _apply(self, user_id, task, changes, replace, clear_field):
        draft = task.sales
        if draft is None:
            raise RuntimeError("售后任务缺少草稿")
        if changes or clear_field:
            draft.confirmation = None
            task.step_id = "review"
        reset = set(changes) if replace else set()
        if clear_field:
            reset.add(clear_field)
        if "order" in reset:
            draft.order_id = draft.order_no = draft.item_id = draft.amount = None
            draft.no_order = False
        if "item" in reset:
            draft.item_id = draft.amount = None
        if "refund_type" in reset:
            draft.refund_type = draft.amount = None
        for key in reset - {"order", "item", "refund_type"}:
            setattr(draft, key, None)
        if ("amount" in changes or clear_field == "amount") and "refund_type" not in changes:
            draft.refund_type = "partial"
        for key in REQUIRED[task.flow_id]:
            if key not in changes:
                continue
            if key == "item" and draft.order_id is None:
                return self._ask(task, "order")
            task.step_id = key
            value = changes[key]
            obj = {}
            if isinstance(value, OrderReference):
                if value.kind == "order_id":
                    obj = {"type": "order", "id": value.value}
                    value = ""
                else:
                    value = value.value
            result = await self._collect(user_id, task, value, obj, preview=False)
            if task.step_id == key:
                return result  # 当前字段未通过业务校验，不可用后续字段跳过这一步。
        return await self._advance(user_id, task)

    async def replay(self, user_id: int, claim: ReservedTurn, confirmation_id: UUID, decision: str) -> FlowResult | None:
        # 3.5/3.6 重复点击确认卡：按会话和确认 ID 取回旧操作结果。
        # 退款若为 sending/unknown，只查询中台核对；拒绝旧卡不能撤销已提交的申请。
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

    async def _collect(self, user_id: int, task: FlowTask, text: str, obj: dict, *, preview=True) -> FlowResult:
        # 3.5/3.6 这里接收已规范化的字段值，不再用固定词句理解用户原始表达。
        # 退款核验订单、选择明细，再收集类型/金额/原因；工单收集类型、关联订单和描述。
        # 每次追问保存 step_id 和 missing_slots，资料齐了才预览，尚不创建申请或工单。
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
        return await self._advance(user_id, task, preview=preview)

    async def _advance(self, user_id, task, *, preview=True):
        draft = task.sales
        for field in REQUIRED[task.flow_id]:
            missing = {
                "order": draft.order_id is None and not draft.no_order,
                "item": draft.item_id is None, "refund_type": draft.refund_type is None,
                "amount": draft.refund_type == "partial" and draft.amount is None, "reason": draft.reason is None,
                "ticket_type": draft.ticket_type is None, "description": draft.description is None,
            }[field]
            if missing:
                return self._ask(task, field)
        task.step_id = "review"
        task.missing_slots = []
        return await self._preview(user_id, task) if preview else FlowResult(task, [])

    async def _preview(self, user_id: int, task: FlowTask) -> FlowResult:
        # 3.5/3.6 看到确认卡仍未提交：退款先预检并算实际金额，工单整理类型/订单/描述。
        # target 保存在服务端草稿，前端只看到核对摘要和 5 分钟有效的确认 ID。
        # 这些状态会随本轮快照保存，但此时还没有创建退款申请或工单。
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
        # 3.5/3.6 确认门禁：只接受当前 confirm 步骤、匹配的确认 ID 和有效期。
        # “不提交”到此结束；同意后仍用服务端 card.target，不采信前端另带的金额等字段。
        # 退款还要检查写入开关；工单创建前再核验关联订单归属，然后才进入真正的写操作。
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
        # 3.5/3.6 回执来自操作记录：成功显示申请/工单编号，拒绝显示已保存原因。
        # 结果不明也结束这次会话任务，提示查退款进度；这不等于操作已经成功或失败。
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
