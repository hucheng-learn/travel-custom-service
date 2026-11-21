import re
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from plan.product_models import DateRequest, ProductClues, ProductRequest


type FlowId = Literal[
    "order_status_query", "itinerary_query", "itinerary_date_query", "product_query", "refund_request", "ticket_submit",
]
type PlanRoute = Literal["task", "clarify", "chitchat", "knowledge"]
type Command = Literal[
    "start_flow", "set_slots", "update_slots", "resume_task", "cancel_task", "pause_task", "preview", "refund_status",
]

ORDER_STATUS: FlowId = "order_status_query"
ITINERARY: FlowId = "itinerary_query"
DATE_ITINERARY: FlowId = "itinerary_date_query"
PRODUCT: FlowId = "product_query"
REFUND: FlowId = "refund_request"
TICKET: FlowId = "ticket_submit"
FLOW_LABELS = {
    ORDER_STATUS: "订单与支付查询", ITINERARY: "订单行程查询",
    DATE_ITINERARY: "按日期查行程", PRODUCT: "商品咨询", REFUND: "退款申请", TICKET: "客服工单",
}
SLOT_FOR_FLOW = {ORDER_STATUS: "order_ref", ITINERARY: "order_ref", DATE_ITINERARY: "travel_date", PRODUCT: "product_request"}
ORDER_NUMBER_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
ORDER_ID_TOKEN = re.compile(r"[0-9]{1,19}")


class OrderReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["order_no", "order_id"]
    value: str

    @model_validator(mode="after")
    def validate_value(self) -> "OrderReference":
        # 文本订单号不是数据库 ID；两种线索要走不同的查询路径。
        if self.kind == "order_no":
            valid = ORDER_NUMBER_TOKEN.fullmatch(self.value) is not None
        else:
            valid = ORDER_ID_TOKEN.fullmatch(self.value) and 0 < int(self.value) < 2**63
        if not valid:
            raise ValueError("订单引用格式不正确")
        return self


class GroundedValue(BaseModel):
    # 枚举值可以规范化，例如“都退了”→ full，但原始表达必须同时保留，供校验来源。
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=200)


class SalesClues(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str | None = Field(None, max_length=19)
    refund_type: GroundedValue | None = None
    amount: GroundedValue | None = None
    ticket_type: GroundedValue | None = None
    no_order: GroundedValue | None = None
    reason: str | None = Field(None, max_length=500)
    description: str | None = Field(None, max_length=1000)


class RefundLookup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_number: str | None = Field(None, max_length=64)
    request_id: str | None = Field(None, max_length=19)
    request_no: str | None = Field(None, max_length=100)
    item_id: str | None = Field(None, max_length=19)


class TurnPlan(BaseModel):
    # 模型只能选择命令并提取线索，不能传 userId、调用地址、Action 名或最终回复。
    model_config = ConfigDict(extra="forbid")
    route: PlanRoute
    command: Command | None = None
    flow_id: FlowId | None = None
    order_number: str | None = Field(default=None, max_length=64)
    task_id: UUID | None = None
    use_current_order: bool = False
    date_text: str | None = Field(default=None, max_length=20,
                                  description="仅用于 itinerary_date_query 查已有订单行程的日期原文；商品查询用 product.date_text。")
    date_value: date | None = Field(default=None,
                                   description="仅用于 itinerary_date_query 的 ISO 日期；商品查询用 product.date_value。")
    product: ProductClues | None = Field(default=None,
                                        description="商品查询条件；景点、酒店和交通日期均放在此对象内，补日期时沿用已保存的地区和品类。")
    sales: SalesClues | None = None
    clear_field: Literal["order", "item", "refund_type", "amount", "reason", "ticket_type", "description"] | None = None
    intent_text: str | None = Field(None, max_length=500)
    pause_all: bool = False
    refund_lookup: RefundLookup | None = None
    knowledge_topic: Literal["general", "refund_arrival"] | None = None
    clarify_reason: Literal["ambiguous", "multiple", "model_unavailable", "invalid_model_output"] | None = None


type SlotValue = OrderReference | DateRequest | ProductRequest
