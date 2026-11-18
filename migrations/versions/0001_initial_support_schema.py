"""Create the initial support schema."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME


# 这是首版迁移，没有上一版；独立保留此版本的结构，不随 ORM 模型变化。
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def timestamp_column(name: str) -> sa.Column:
    # 插入记录时由 MySQL 填入 UTC 时间，保留微秒精度。
    return sa.Column(name, DATETIME(fsp=6), nullable=False, server_default=sa.text("(UTC_TIMESTAMP(6))"))


def upgrade() -> None:
    # UUID 在 MySQL 中存为不带连字符的 CHAR(32)。
    uuid_type = sa.Uuid(native_uuid=False)
    table_options = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}

    # 先建会话主表，再建引用它的轮次、操作记录和工单表。
    op.create_table(
        "support_sessions",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("state_json", sa.JSON, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("processing_turn_id", uuid_type),
        sa.Column("processing_token", uuid_type),
        sa.Column("processing_deadline", DATETIME(fsp=6)),
        timestamp_column("created_at"),
        timestamp_column("updated_at"),
        **table_options,
    )
    op.create_index("ix_support_sessions_user_updated", "support_sessions", ["user_id", "updated_at"])

    op.create_table(
        "support_turns",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("session_id", uuid_type, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("message_id", uuid_type, nullable=False),
        sa.Column("request_hash", sa.CHAR(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_json", sa.JSON, nullable=False),
        sa.Column("response_json", sa.JSON(none_as_null=True)),
        sa.Column("error_code", sa.String(64)),
        timestamp_column("created_at"),
        timestamp_column("updated_at"),
        sa.ForeignKeyConstraint(["session_id"], ["support_sessions.id"], name="fk_support_turns_session"),
        sa.UniqueConstraint("session_id", "message_id", name="uq_support_turns_session_message"),
        sa.UniqueConstraint("session_id", "seq", name="uq_support_turns_session_seq"),
        **table_options,
    )

    op.create_table(
        "support_operations",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("session_id", uuid_type, nullable=False),
        sa.Column("task_id", uuid_type, nullable=False),
        sa.Column("confirmation_id", uuid_type, nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("target_json", sa.JSON, nullable=False),
        sa.Column("result_json", sa.JSON(none_as_null=True)),
        timestamp_column("created_at"),
        timestamp_column("updated_at"),
        sa.ForeignKeyConstraint(["session_id"], ["support_sessions.id"], name="fk_support_operations_session"),
        sa.UniqueConstraint("session_id", "confirmation_id", name="uq_support_operations_session_confirmation"),
        **table_options,
    )

    op.create_table(
        "support_tickets",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ticket_no", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("session_id", uuid_type, nullable=False),
        sa.Column("order_id", sa.BigInteger),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_operation_id", uuid_type, nullable=False),
        timestamp_column("created_at"),
        timestamp_column("updated_at"),
        sa.ForeignKeyConstraint(["session_id"], ["support_sessions.id"], name="fk_support_tickets_session"),
        sa.ForeignKeyConstraint(
            ["source_operation_id"], ["support_operations.id"], name="fk_support_tickets_operation"
        ),
        sa.UniqueConstraint("ticket_no", name="uq_support_tickets_ticket_no"),
        sa.UniqueConstraint("source_operation_id", name="uq_support_tickets_source_operation"),
        **table_options,
    )
    op.create_index("ix_support_tickets_session", "support_tickets", ["session_id"])

    op.create_table(
        "support_ticket_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ticket_id", sa.BigInteger, nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(64)),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32), nullable=False),
        timestamp_column("created_at"),
        sa.ForeignKeyConstraint(["ticket_id"], ["support_tickets.id"], name="fk_support_ticket_events_ticket"),
        **table_options,
    )
    op.create_index("ix_support_ticket_events_ticket_created", "support_ticket_events", ["ticket_id", "created_at"])


def downgrade() -> None:
    # 回滚会删除表及其数据；按外键依赖反向删除，先子表后主表。
    op.drop_table("support_ticket_events")
    op.drop_table("support_tickets")
    op.drop_table("support_operations")
    op.drop_table("support_turns")
    op.drop_table("support_sessions")
