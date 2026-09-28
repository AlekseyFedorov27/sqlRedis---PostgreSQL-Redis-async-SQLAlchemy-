from uuid import UUID
import asyncio
import json
import time

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
import redis.asyncio as redis

# Session импортируем под псевдонимом, чтобы не затенять AsyncSession из SQLAlchemy.
from models import Message, Base, Session as SessionModel, Run, User


DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/ai_agent_pg"

# Асинхронный движок. echo=False — не логировать каждый SQL-запрос.
engine = create_async_engine(DATABASE_URL, echo=False)

# expire_on_commit=False — после commit объекты остаются доступными
# (иначе обращение к res.id после commit вызовет ленивую подгрузку и упадёт вне сессии).
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# Redis-клиент. decode_responses=True — возвращает str, а не bytes.
REDIS = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)


# ─────────── сериализация ───────────

def _serialize(msg: Message) -> dict:
    """ORM-объект Message → dict, пригодный для json.dumps."""
    return {
        "id": str(msg.id),
        "session_id": str(msg.session_id),
        "role": msg.role,
        "content": msg.content,
        "metadata": msg.metadata_,
        "created_at": msg.created_at.isoformat(),
    }


# ─────────── репозиторий ───────────

async def create_message(
    session: AsyncSession,
    session_id: UUID,
    role: str,
    content: str,
    meta: dict | None = None,
) -> Message:
    """Вставить сообщение и вернуть ORM-объект с заполненными id/created_at."""
    msg = Message(
        session_id=session_id,
        role=role,
        content=content,
        metadata_=meta if meta is not None else {},
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)   # подтянуть server_default: id, created_at
    return msg


async def get_by_session(
    session: AsyncSession,
    session_id: UUID,
    limit: int = 10,
) -> list[Message]:
    """Последние N сообщений сессии (в порядке DESC — свежие первыми)."""
    stmt = (
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_recent(session: AsyncSession, limit: int = 50) -> list[Message]:
    """Последние N сообщений по всей таблице (для отладки)."""
    stmt = (
        select(Message)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def add_message(session_id: UUID, role: str, content: str) -> Message:
    """Вставить сообщение и инвалидировать все кэши этой сессии."""
    async with SessionLocal() as session:
        msg = await create_message(session, session_id, role, content, {})

    # Ключи кэша имеют вид cache:messages:{session_id}:{limit}.
    # scan_iter (не KEYS!) — безопасно для продакшена: не блокирует Redis.
    async for key in REDIS.scan_iter(f"cache:messages:{session_id}:*"):
        await REDIS.delete(key)

    return msg


async def get_recent_messages(session_id: UUID, limit: int = 10) -> list[dict]:
    """Последние N сообщений сессии через кэш (TTL 30 сек)."""
    key = f"cache:messages:{session_id}:{limit}"

    # 1. Пробуем взять из Redis
    hit = await REDIS.get(key)
    if hit is not None:
        return json.loads(hit)

    # 2. Промах — идём в БД
    async with SessionLocal() as session:
        messages = await get_by_session(session, session_id, limit)

    # 3. В БД взяли DESC (последние N), но в кэш/LLM кладём хронологически (ASC).
    data = [_serialize(m) for m in reversed(messages)]
    await REDIS.set(key, json.dumps(data), ex=30)
    return data


# ─────────── main ───────────

async def main():
    try:
        # ── Часть 1: подготовка данных в БД ──
        async with SessionLocal() as session:
            await REDIS.flushdb()   # чистим кэш перед экспериментом

            # Очистка в порядке, обратном FK:
            # messages → runs → sessions → users.
            await session.execute(delete(Message))
            await session.execute(delete(Run))
            await session.execute(delete(SessionModel))
            await session.execute(delete(User))
            await session.commit()

            # 1. User. name и email — NOT NULL; email — UNIQUE.
            user = User(
                name="test",
                email="test@example.com",
            )
            session.add(user)
            await session.flush()   # получить user.id до commit, без записи в БД насовсем

            # 2. Session. user_id — NOT NULL, FK на users.id.
            session_id = UUID("01a0cf46-62c4-76cf-aeb9-117ef16d88d1")
            sess = SessionModel(
                id=session_id,
                user_id=user.id,
                title="test",
            )
            session.add(sess)
            await session.commit()

            # 3. Первое сообщение.
            start = time.perf_counter()
            res = await create_message(
                session,
                session_id,
                "user",   # role обязана быть из CHECK: system/user/assistant/tool
                "привет, assistant: привет! user: как дела?",
                {},
            )
            recent = await get_recent(session, limit=50)
            elapsed = time.perf_counter() - start

            print(res.id, res.content)
            print(f"{elapsed:.3f}s")
            print(f"данные: {res}")
            print(len(recent), "сообщений")

        # ── Часть 2: проверка кэша Redis ──
        key = f"cache:messages:{session_id}:10"

        msgs1 = await get_recent_messages(session_id, limit=10)   # промах → БД
        print("\n[1] промах кэша, из БД:", len(msgs1), "сообщений")

        ttl = await REDIS.ttl(key)
        print(f"    ключ создан, TTL = {ttl}с")

        msgs2 = await get_recent_messages(session_id, limit=10)   # попадание → Redis
        print("[2] попадание в кэш, из Redis:", len(msgs2), "сообщений")

        # ── Часть 3: инвалидация кэша при вставке ──
        await add_message(session_id, "assistant", "всё хорошо!")

        exists = await REDIS.exists(key)
        print(f"\n[3] после add_message, ключ существует = {bool(exists)} (ожидается False)")

        msgs3 = await get_recent_messages(session_id, limit=10)   # снова промах → БД
        print("[4] снова промах кэша, из БД:", len(msgs3), "сообщений")
        for m in msgs3:
            print(f"    {m['role']:>9}: {m['content'][:50]}")

        await REDIS.set("cache:messages:other-session:10", "x")
        await add_message(session_id, "user", "тест")
        other = await REDIS.exists("cache:messages:other-session:10")
        print("\n[6] ключ другой сессии уцелел:", bool(other), "(ожидается True)")

    finally:
        # Явно закрываем пул соединений asyncpg и Redis,
        # чтобы не ловить RuntimeError: Event loop is closed при выходе.
        await engine.dispose()
        await REDIS.aclose()


if __name__ == "__main__":
    asyncio.run(main())