from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repository.models import SupportSession, SupportTurn


# 单轮查询时间预算比处理标记短。进程异常退出后，新请求可在到期后接管。
TURN_LEASE = timedelta(seconds=30)


class SessionNotFoundError(LookupError):
    pass


class MessageIdConflictError(ValueError):
    pass


class TurnInProgressError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReservedTurn:
    session_id: UUID
    turn_id: UUID
    token: UUID
    version: int
    snapshot: dict


def utc_now() -> datetime:
    # MySQL DATETIME 不存时区；写入前转成 UTC 并去掉时区信息，返回 API 时再补回。
    return datetime.now(UTC).replace(tzinfo=None)


class DialogueRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(self, user_id: int, snapshot: dict) -> SupportSession:
        now = utc_now()
        row = SupportSession(
            id=uuid4(), user_id=user_id, state_json=snapshot, version=0, created_at=now, updated_at=now
        )
        async with self._sessions.begin() as db:
            db.add(row)
        return row

    async def get(self, session_id: UUID, user_id: int) -> SupportSession:
        async with self._sessions() as db:
            return await self._owned(db, session_id, user_id)

    async def list_sessions(self, user_id: int, offset: int, limit: int) -> list[tuple[UUID, datetime, dict | None]]:
        # 只为本页会话取首条已完成输入生成标题，不读取整段对话或任务快照。
        first_input = (
            select(SupportTurn.input_json)
            .where(SupportTurn.session_id == SupportSession.id, SupportTurn.status == "completed")
            .order_by(SupportTurn.seq)
            .limit(1)
            .scalar_subquery()
        )
        query = (
            select(SupportSession.id, SupportSession.updated_at, first_input)
            .where(SupportSession.user_id == user_id)
            .order_by(SupportSession.updated_at.desc(), SupportSession.id.desc())
            .offset(offset).limit(limit)
        )
        async with self._sessions() as db:
            return list((await db.execute(query)).tuples())

    async def history(
        self, session_id: UUID, user_id: int, before_seq: int | None, page_size: int
    ) -> tuple[list[SupportTurn], int | None]:
        async with self._sessions() as db:
            await self._owned(db, session_id, user_id)
            query = select(SupportTurn).where(SupportTurn.session_id == session_id, SupportTurn.status == "completed")
            if before_seq is not None:
                query = query.where(SupportTurn.seq < before_seq)
            rows = list(await db.scalars(query.order_by(SupportTurn.seq.desc()).limit(page_size + 1)))
        # 额外读一条判断还有没有下一页；展示时按对话发生顺序排列。
        next_before_seq = rows[page_size - 1].seq if len(rows) > page_size else None
        return list(reversed(rows[:page_size])), next_before_seq

    async def reserve(
        self, session_id: UUID, user_id: int, message_id: UUID, request_hash: str, input_data: dict
    ) -> ReservedTurn | dict:
        # 锁会话只为预留轮次，提交后再查中台，不在数据库事务里等待网络。
        async with self._sessions.begin() as db:
            session = await self._owned(db, session_id, user_id, lock=True)
            turn = await db.scalar(select(SupportTurn).where(
                SupportTurn.session_id == session_id, SupportTurn.message_id == message_id
            ))
            if turn is not None:
                if turn.request_hash != request_hash:
                    raise MessageIdConflictError
                if turn.status == "completed" and turn.response_json is not None:
                    return turn.response_json

            now = utc_now()
            if session.processing_turn_id is not None:
                if session.processing_deadline is not None and session.processing_deadline > now:
                    raise TurnInProgressError
                expired = await db.get(SupportTurn, session.processing_turn_id)
                if expired is not None and expired.status == "processing":
                    expired.status = "failed"
                    expired.error_code = "TURN_EXPIRED"
                    expired.updated_at = now

            latest = await db.scalar(select(func.max(SupportTurn.seq)).where(SupportTurn.session_id == session_id)) or 0
            if turn is None:
                turn = SupportTurn(
                    id=uuid4(), session_id=session_id, seq=latest + 1, message_id=message_id,
                    request_hash=request_hash, input_json=input_data, created_at=now,
                )
                db.add(turn)
            elif turn.seq < latest:
                # 失败的旧消息允许重试，但成功后要出现在较新的历史位置。
                turn.seq = latest + 1
            turn.status = "processing"
            turn.error_code = None
            turn.updated_at = now

            token = uuid4()
            session.processing_turn_id = turn.id
            session.processing_token = token
            session.processing_deadline = now + TURN_LEASE
            return ReservedTurn(session_id, turn.id, token, session.version, session.state_json)

    async def finish(self, claim: ReservedTurn, snapshot: dict, response: dict) -> None:
        async with self._sessions.begin() as db:
            session, turn = await self._read_claim(db, claim)
            if session is None or turn is None:
                raise TurnInProgressError
            now = utc_now()
            # 快照和回复同事务写入，前端不会读到“回复已保存但任务没推进”的中间状态。
            session.state_json = snapshot
            session.version = claim.version + 1
            session.updated_at = now
            turn.response_json = response
            turn.status = "completed"
            turn.updated_at = now
            self._clear_claim(session)

    async def fail(self, claim: ReservedTurn, code: str) -> None:
        async with self._sessions.begin() as db:
            session, turn = await self._read_claim(db, claim)
            if session is not None and turn is not None:
                turn.status = "failed"
                turn.error_code = code
                turn.updated_at = utc_now()
                self._clear_claim(session)

    @staticmethod
    async def _owned(db: AsyncSession, session_id: UUID, user_id: int, *, lock: bool = False) -> SupportSession:
        query = select(SupportSession).where(SupportSession.id == session_id, SupportSession.user_id == user_id)
        row = await db.scalar(query.with_for_update() if lock else query)
        if row is None:
            # 不向其他用户区分“会话不存在”和“会话属于别人”。
            raise SessionNotFoundError
        return row

    @staticmethod
    async def _read_claim(
        db: AsyncSession, claim: ReservedTurn
    ) -> tuple[SupportSession | None, SupportTurn | None]:
        row = await db.get(SupportSession, claim.session_id, with_for_update=True)
        # 标记到期被接管后，原执行者即使迟到返回，也不能覆盖新快照。
        if (
            row is None or row.version != claim.version
            or row.processing_turn_id != claim.turn_id or row.processing_token != claim.token
        ):
            return None, None
        return row, await db.get(SupportTurn, claim.turn_id)

    @staticmethod
    def _clear_claim(row: SupportSession) -> None:
        row.processing_turn_id = None
        row.processing_token = None
        row.processing_deadline = None
