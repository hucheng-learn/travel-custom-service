from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# 这里描述应用读写的表结构；实际建表由 Alembic 迁移完成。
class Base(DeclarativeBase):
    pass


# 一条记录对应一个会话，保存客服处理到哪里。
class SupportSession(Base):
    __tablename__ = "support_sessions"
    __table_args__ = (
        sa.Index("ix_support_sessions_user_updated", "user_id", "updated_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[UUID] = mapped_column(sa.Uuid(native_uuid=False), primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger)  # 旅游中台的用户 ID，不跨库建外键。
    # 保存活动任务、暂停任务和待确认信息；version 用于检测并发更新。
    state_json: Mapped[dict] = mapped_column(sa.JSON)
    version: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    # 记录哪一轮正在处理。超时后可由新执行者接管，令牌用于识别过期的执行者。
    processing_turn_id: Mapped[UUID | None] = mapped_column(sa.Uuid(native_uuid=False))
    processing_token: Mapped[UUID | None] = mapped_column(sa.Uuid(native_uuid=False))
    processing_deadline: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=sa.text("(UTC_TIMESTAMP(6))"))
    # 此默认值只在插入时生效，后续修改记录时由业务代码更新时间。
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=sa.text("(UTC_TIMESTAMP(6))"))


# 一条记录对应一次对话请求，包含用户输入和本轮处理结果。
class SupportTurn(Base):
    __tablename__ = "support_turns"
    __table_args__ = (
        # 同一会话按 message_id 去重，按 seq 排序和分页。
        sa.UniqueConstraint("session_id", "message_id", name="uq_support_turns_session_message"),
        sa.UniqueConstraint("session_id", "seq", name="uq_support_turns_session_seq"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[UUID] = mapped_column(sa.Uuid(native_uuid=False), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        sa.Uuid(native_uuid=False), sa.ForeignKey("support_sessions.id", name="fk_support_turns_session")
    )
    seq: Mapped[int] = mapped_column(sa.Integer)
    message_id: Mapped[UUID] = mapped_column(sa.Uuid(native_uuid=False))
    # 用请求摘要区分“原请求重试”和“同一个消息 ID 被用来发送不同内容”。
    request_hash: Mapped[str] = mapped_column(sa.CHAR(64))
    status: Mapped[str] = mapped_column(sa.String(32))
    input_json: Mapped[dict] = mapped_column(sa.JSON)
    # 尚无回复时存 SQL NULL，而不是 JSON 值 null。
    response_json: Mapped[dict | None] = mapped_column(sa.JSON(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(sa.String(64))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=sa.text("(UTC_TIMESTAMP(6))"))
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=sa.text("(UTC_TIMESTAMP(6))"))


# 退款申请、提交工单等写操作的记录，供重试时回放或核对结果。
class SupportOperation(Base):
    __tablename__ = "support_operations"
    __table_args__ = (
        # 同一会话的一次用户确认只能对应一条写操作记录。
        sa.UniqueConstraint("session_id", "confirmation_id", name="uq_support_operations_session_confirmation"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[UUID] = mapped_column(sa.Uuid(native_uuid=False), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        sa.Uuid(native_uuid=False), sa.ForeignKey("support_sessions.id", name="fk_support_operations_session")
    )
    task_id: Mapped[UUID] = mapped_column(sa.Uuid(native_uuid=False))
    confirmation_id: Mapped[UUID] = mapped_column(sa.Uuid(native_uuid=False))
    type: Mapped[str] = mapped_column(sa.String(32))
    status: Mapped[str] = mapped_column(sa.String(32))
    target_json: Mapped[dict] = mapped_column(sa.JSON)
    result_json: Mapped[dict | None] = mapped_column(sa.JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=sa.text("(UTC_TIMESTAMP(6))"))
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=sa.text("(UTC_TIMESTAMP(6))"))


# 工单主记录，保存当前状态；历史变更另存工单事件表。
class SupportTicket(Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        # 工单编号供用户查阅；一次写操作最多创建一张工单。
        sa.UniqueConstraint("ticket_no", name="uq_support_tickets_ticket_no"),
        sa.UniqueConstraint("source_operation_id", name="uq_support_tickets_source_operation"),
        sa.Index("ix_support_tickets_session", "session_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    ticket_no: Mapped[str] = mapped_column(sa.String(64))
    user_id: Mapped[int] = mapped_column(sa.BigInteger)
    session_id: Mapped[UUID] = mapped_column(
        sa.Uuid(native_uuid=False), sa.ForeignKey("support_sessions.id", name="fk_support_tickets_session")
    )
    order_id: Mapped[int | None] = mapped_column(sa.BigInteger)  # 可选的中台订单 ID，不跨库建外键。
    type: Mapped[str] = mapped_column(sa.String(32))
    description: Mapped[str] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.String(32))
    source_operation_id: Mapped[UUID] = mapped_column(
        sa.Uuid(native_uuid=False), sa.ForeignKey("support_operations.id", name="fk_support_tickets_operation")
    )
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=sa.text("(UTC_TIMESTAMP(6))"))
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=sa.text("(UTC_TIMESTAMP(6))"))


# 工单审计记录：谁在什么时候做了什么，状态从哪里变到哪里。
class SupportTicketEvent(Base):
    __tablename__ = "support_ticket_events"
    __table_args__ = (
        sa.Index("ix_support_ticket_events_ticket_created", "ticket_id", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("support_tickets.id", name="fk_support_ticket_events_ticket")
    )
    event_type: Mapped[str] = mapped_column(sa.String(32))
    actor_type: Mapped[str] = mapped_column(sa.String(32))
    actor_id: Mapped[str | None] = mapped_column(sa.String(64))
    from_status: Mapped[str | None] = mapped_column(sa.String(32))  # 创建工单时没有旧状态。
    to_status: Mapped[str] = mapped_column(sa.String(32))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=sa.text("(UTC_TIMESTAMP(6))"))
