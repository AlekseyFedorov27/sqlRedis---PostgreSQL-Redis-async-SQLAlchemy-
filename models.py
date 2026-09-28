from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    # BIGINT, потому что в БД id BIGINT IDENTITY.
    # Mapped[int] без BigInteger дал бы INTEGER (4 байта) и расхождение с БД.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, unique=True)

    # DateTime(timezone=True) = TIMESTAMPTZ.
    # Без timezone=True SQLAlchemy даёт TIMESTAMP без таймзоны — расхождение с БД.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuidv7()"),
    )

    # FK на users.id — тип обязан совпадать с BIGINT.
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
    )

    title: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Атрибут metadata_ маппится на колонку "metadata" (зарезервировано в SQLAlchemy).
    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        server_default=text("'{}'::jsonb"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        # GIN-индекс на JSONB — из Дня 7.
        Index(
            "idx_sessions_metadata",
            "metadata",
            postgresql_using="gin",
        ),
        # FK-индекс создаётся руками (PG сам не делает).
        Index("idx_sessions_user_id", "user_id"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuidv7()"),
    )

    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )

    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        # Конечный автомат роли — на уровне БД.
        CheckConstraint(
            "role IN ('system','user','assistant','tool')",
            name="messages_role_check",
        ),
        # Композитный (session_id, created_at) покрывает WHERE session_id
        # и ORDER BY created_at бесплатно.
        Index("idx_messages_created_at", "session_id", "created_at"),
        Index(
            "idx_messages_metadata",
            "metadata",
            postgresql_using="gin",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Message id={self.id} session_id={self.session_id} "
            f"role={self.role!r} content={self.content[:30]!r}>"
        )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuidv7()"),
    )

    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
    )

    status: Mapped[str] = mapped_column(
        Text,
        server_default=text("'pending'"),
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
    )

    # NULL = «ещё не случилось» — паттерн из Дня 6.
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','success','error','cancelled')",
            name="runs_status_check",
        ),
        Index("idx_runs_session_id", "session_id"),
        # Частичный индекс на «горячее» подмножество — из Дня 7.
        Index(
            "idx_runs_status",
            "status",
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )


class Checkpoint(Base):
    __tablename__ = "checkpoints"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuidv7()"),
    )

    thread_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))

    # Самоссылка: ветвление состояния графа LangGraph.
    parent_checkpoint_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("checkpoints.id", ondelete="SET NULL"),
        nullable=True,
    )

    state: Mapped[dict] = mapped_column(JSONB)

    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        server_default=text("'{}'::jsonb"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        Index("idx_checkpoints_thread_id", "thread_id", "created_at"),
        Index(
            "idx_checkpoints_state",
            "state",
            postgresql_using="gin",
        ),
    )