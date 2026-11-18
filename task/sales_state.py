from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class Confirmation(BaseModel):
    # 金额、明细和描述保存在服务端。前端只回传 id 与决定。
    id: UUID
    expires_at: datetime
    target: dict


class SalesDraft(BaseModel):
    order_id: int | None = None
    order_no: str | None = None
    no_order: bool = False
    item_id: int | None = None
    refund_type: Literal["full", "partial"] | None = None
    amount: str | None = None
    reason: str | None = None
    ticket_type: Literal["complaint", "after_sales", "refund"] | None = None
    description: str | None = None
    confirmation: Confirmation | None = None
