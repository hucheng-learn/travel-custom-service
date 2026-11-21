from datetime import UTC
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repository.dialogue import DialogueRepository, ReservedTurn, TurnInProgressError, utc_now
from repository.models import SupportOperation, SupportTicket, SupportTicketEvent


class TicketNotFoundError(LookupError):
    pass


def ticket_view(row: SupportTicket, next_step: str) -> dict:
    return {
        "ticketId": row.id, "ticketNo": row.ticket_no, "orderId": row.order_id, "type": row.type,
        "description": row.description, "status": row.status, "nextStep": next_step,
        "createdAt": row.created_at.replace(tzinfo=UTC).isoformat(),
        "updatedAt": row.updated_at.replace(tzinfo=UTC).isoformat(),
    }


class OperationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], next_step: str) -> None:
        self._sessions = sessions
        self._next_step = next_step

    async def find(self, session_id: UUID, confirmation_id: UUID) -> SupportOperation | None:
        # 只由已经通过会话归属检查的对话轮次调用。
        async with self._sessions() as db:
            return await self._find(db, session_id, confirmation_id)

    async def latest_uncertain(
        self, session_id: UUID, *, order_id: int | None = None, item_id: int | None = None,
    ) -> SupportOperation | None:
        async with self._sessions() as db:
            query = select(SupportOperation).where(
                SupportOperation.session_id == session_id, SupportOperation.type == "refund",
                SupportOperation.status.in_(("sending", "unknown")),
            )
            if order_id is not None and item_id is not None:
                query = query.where(SupportOperation.target_json["orderId"].as_integer() == order_id,
                                    SupportOperation.target_json["itemId"].as_integer() == item_id)
            return await db.scalar(query.order_by(SupportOperation.created_at.desc()).limit(1))

    async def prepare_refund(
        self, claim: ReservedTurn, task_id: UUID, confirmation_id: UUID, target: dict,
    ) -> SupportOperation:
        async with self._sessions.begin() as db:
            await self._check_claim(db, claim)
            existing = await self._find(db, claim.session_id, confirmation_id)
            if existing is not None:
                return existing
            row = self._new(claim, task_id, confirmation_id, "refund", target)
            db.add(row)
            return row

    async def mark_sending(self, claim: ReservedTurn, operation_id: UUID, target: dict) -> bool:
        # 3.5 抢占发送权：先验证本轮凭据，再只允许 pending → sending，事务提交后才发 POST。
        # 条件更新未命中的执行者读取已有记录，不另外发一份退款申请。
        async with self._sessions.begin() as db:
            await self._check_claim(db, claim)
            # 只有赢得 pending → sending 的执行者能发 POST。已发送/未知状态绝不重发。
            result = await db.execute(update(SupportOperation).where(
                SupportOperation.id == operation_id, SupportOperation.session_id == claim.session_id,
                SupportOperation.status == "pending",
            ).values(status="sending", target_json=target, updated_at=utc_now()))
            return result.rowcount == 1

    async def complete_refund(self, operation_id: UUID, status: str, result: dict) -> SupportOperation:
        async with self._sessions.begin() as db:
            row = await db.get(SupportOperation, operation_id, with_for_update=True)
            if row is None:
                raise RuntimeError("找不到售后操作记录")
            if row.status in {"sending", "unknown"}:
                row.status = status
                row.result_json = result
                row.updated_at = utc_now()
            return row

    async def create_ticket(
        self, claim: ReservedTurn, user_id: int, task_id: UUID, confirmation_id: UUID, target: dict,
    ) -> SupportOperation:
        # 3.6 真正创建工单的时点：有效确认之后，先按确认 ID 查重，再在一个事务里写
        # 操作记录、open 工单、created 事件和回执。中途失败一起回滚，重复确认取回原结果。
        async with self._sessions.begin() as db:
            await self._check_claim(db, claim)
            existing = await self._find(db, claim.session_id, confirmation_id)
            if existing is not None:
                return existing
            operation = self._new(claim, task_id, confirmation_id, "ticket", target)
            db.add(operation)
            await db.flush()
            now = utc_now()
            ticket = SupportTicket(
                ticket_no=f"TK{now:%Y%m%d}{operation.id.hex.upper()}",
                user_id=user_id, session_id=claim.session_id, order_id=target["orderId"],
                type=target["type"], description=target["description"], status="open",
                source_operation_id=operation.id, created_at=now, updated_at=now,
            )
            db.add(ticket)
            await db.flush()
            # 工单、创建事件及回执同事务；失败时一起回滚，不会出现无审计工单。
            db.add(SupportTicketEvent(
                ticket_id=ticket.id, event_type="created", actor_type="user", actor_id=str(user_id),
                to_status="open", created_at=now,
            ))
            operation.status = "succeeded"
            operation.result_json = ticket_view(ticket, self._next_step)
            return operation

    async def get_ticket(self, user_id: int, ticket_id: int) -> dict:
        # 3.6 点击“查看处理进度”：只读取当前用户的同一张工单，不新建操作或工单。
        async with self._sessions() as db:
            row = await db.scalar(select(SupportTicket).where(SupportTicket.id == ticket_id, SupportTicket.user_id == user_id))
            if row is None:
                raise TicketNotFoundError
            return ticket_view(row, self._next_step)

    async def list_tickets(self, user_id: int, offset: int, limit: int) -> list[dict]:
        # 侧栏只取当前用户的编号和状态，不传工单描述等内容。
        query = (
            select(SupportTicket.id, SupportTicket.ticket_no, SupportTicket.status)
            .where(SupportTicket.user_id == user_id)
            .order_by(SupportTicket.id.desc())
            .offset(offset).limit(limit)
        )
        async with self._sessions() as db:
            return [
                {"type": "ticket", "id": str(ticket_id), "title": number, "attributes": {"status": status}}
                for ticket_id, number, status in (await db.execute(query)).tuples()
            ]

    @staticmethod
    async def _check_claim(db: AsyncSession, claim: ReservedTurn) -> None:
        session, turn = await DialogueRepository._read_claim(db, claim)
        if session is None or turn is None:
            raise TurnInProgressError

    @staticmethod
    async def _find(db: AsyncSession, session_id: UUID, confirmation_id: UUID) -> SupportOperation | None:
        return await db.scalar(select(SupportOperation).where(
            SupportOperation.session_id == session_id, SupportOperation.confirmation_id == confirmation_id
        ))

    @staticmethod
    def _new(claim: ReservedTurn, task_id: UUID, confirmation_id: UUID, kind: str, target: dict) -> SupportOperation:
        now = utc_now()
        return SupportOperation(
            id=uuid4(), session_id=claim.session_id, task_id=task_id, confirmation_id=confirmation_id,
            type=kind, status="pending", target_json=target, created_at=now, updated_at=now,
        )
