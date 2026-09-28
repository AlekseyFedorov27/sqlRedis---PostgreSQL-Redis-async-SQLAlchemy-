# sqlRedis — чат-сессии на PostgreSQL + Redis (async SQLAlchemy)

Учебный проект: асинхронный репозиторий для чат-сообщений на стеке
**FastAPI-стиля** — `asyncpg` + `SQLAlchemy 2.0 (async)` + `PostgreSQL 18` + `Redis`.
Демонстрирует:

- схему БД с `uuidv7()` в качестве первичных ключей;
- каскадные FK (`users → sessions → messages/runs`);
- JSONB-поля с GIN-индексами;
- кэш «последних N сообщений» в Redis с TTL и инвалидацией;
- `scan_iter` вместо `KEYS` для безопасной инвалидации;
- корректное закрытие пула соединений (`engine.dispose()`, `REDIS.aclose()`).

---

## Стек

| Слой | Технология |
|------|-----------|
| Язык | Python 3.12+ |
| БД | PostgreSQL 18 (нужен для `uuidv7()`) |
| Драйвер | `asyncpg` |
| ORM | SQLAlchemy 2.0 (async) |
| Кэш | Redis 7+ (`redis.asyncio`) |
| Запуск | `asyncio.run(main())` |

> **Важно:** `uuidv7()` появился в PostgreSQL 18. На более старых версиях
> замените `server_default=text("uuidv7()")` на `gen_random_uuid()`
> (расширение `pgcrypto`) или генерируйте UUID в Python (`default=uuid.uuid4`).

---

## Структура проекта

```
.
├── models.py        # SQLAlchemy-модели: User, Session, Message, Run, Checkpoint
├── start.py         # репозиторий + main() с демонстрацией кэша
├── requirements.txt
└── README.md
```

### Схема БД

```
users ──< sessions ──< messages
                  └──< runs
checkpoints (самоссылка parent_checkpoint_id)
```

- `users.id` — `BIGINT IDENTITY`
- `sessions.id`, `messages.id`, `runs.id`, `checkpoints.id` — `UUID` с `uuidv7()`
- `messages.role` ограничен `CHECK (role IN ('system','user','assistant','tool'))`
- `runs.status` ограничен `CHECK (status IN ('pending','running','success','error','cancelled'))`
- JSONB-поля (`metadata`, `state`) покрыты GIN-индексами
- Композитный индекс `idx_messages_created_at (session_id, created_at)`
  покрывает `WHERE session_id = ? ORDER BY created_at DESC`

---

## Установка

### 1. Клонировать репозиторий

```bash
git clone https://github.com/<your-username>/sqlRedis.git
cd sqlRedis
```

### 2. Виртуальное окружение

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
```

### 3. Зависимости

```bash
pip install -r requirements.txt
```

`requirements.txt`:

```
sqlalchemy[asyncio]>=2.0
asyncpg>=0.29
redis>=5.0
```

### 4. PostgreSQL

Создать БД:

```bash
createdb ai_agent_pg
# или через psql
psql -U postgres -c "CREATE DATABASE ai_agent_pg;"
```

Убедиться, что версия ≥ 18 (нужен `uuidv7()`):

```sql
SELECT version();
```

### 5. Redis

```bash
# через Docker
docker run -d --name redis -p 6379:6379 redis:7

# или локально
redis-server
```

### 6. Строка подключения

По умолчанию в `start.py`:

```python
DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/ai_agent_pg"
```

Поменяйте под свои креды или вынесите в переменные окружения.

---

## Создание таблиц

Таблицы в проекте создаются через `Base.metadata.create_all()` — в учебных
целях. В реальном проекте используйте Alembic.

Разовый скрипт:

```python
import asyncio
from models import Base
from start import engine

async def init():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

asyncio.run(init())
```

---

## Запуск

```bash
python start.py
```

Скрипт:

1. чистит `messages`, `runs`, `sessions`, `users` и Redis;
2. создаёт `User → Session → Message`;
3. проверяет кэш: промах → попадание → инвалидация → снова промах;
4. проверяет, что `scan_iter` не удаляет ключи чужих сессий.

### Ожидаемый вывод

```
01a0e7e7-... привет, assistant: привет! user: как дела?
0.009s
данные: <Message id=01a0e7e7-... role='user' content='привет, assistant: привет! use'>
1 сообщений

[1] промах кэша, из БД: 1 сообщений
    ключ создан, TTL = 30с
[2] попадание в кэш, из Redis: 1 сообщений

[3] после add_message, ключ существует = False (ожидается False)
[4] снова промах кэша, из БД: 2 сообщений
         user: привет, assistant: привет! user: как дела?
    assistant: всё хорошо!

[6] ключ другой сессии уцелел: True (ожидается True)
```

---

## Ключевые решения

### `uuidv7()` вместо `uuid4()`

UUIDv7 содержит timestamp в старших битах, поэтому ID монотонно растут.
Это даёт локальность вставки в B-tree индекс — в отличие от случайного `uuid4()`,
который фрагментирует индекс.

### `expire_on_commit=False`

Иначе после `await session.commit()` обращение к `msg.id` вызовет ленивую
подгрузку, а она упадёт, если объект detached (сессия уже закрыта).

### `scan_iter` вместо `KEYS`

`KEYS cache:*` блокирует Redis на всё время обхода. `SCAN` — итеративный,
не блокирует. В проде — только `SCAN`/`scan_iter`.

### Кэш в хронологическом порядке

`get_by_session` сортирует `created_at DESC` (свежие первыми — то, что нужно
для «последних N»). Но перед отдачей в LLM/кэш разворачиваем через
`reversed()`, чтобы контекст шёл в хронологии: вопрос → ответ.

### TTL 30 секунд

Компромисс: короткий TTL снижает риск устаревания при забытой инвалидации,
длинный — повышает hit rate. Для чата 30с обычно достаточно.

---

## Что можно улучшить

- [ ] Вынести `DATABASE_URL`, `REDIS_URL` в переменные окружения через `pydantic-settings`
- [ ] Alembic вместо `create_all()`
- [ ] Слой сервисов (`MessageService`) вместо свободных функций
- [ ] `__repr__` для `Session`, `User`, `Run` (у `Message` уже есть)
- [ ] Метрики hit/miss кэша
- [ ] `EXPLAIN ANALYZE` на больших объёмах — убедиться, что
      `idx_messages_created_at` используется
- [ ] Тесты: `pytest-asyncio` + `testcontainers` для PostgreSQL и Redis

---

## Лицензия

MIT — делайте что хотите.
Что стоит добавить в репозиторий
requirements.txt — обязательно, чтобы pip install -r requirements.txt работал:

text
sqlalchemy[asyncio]>=2.0
asyncpg>=0.29
redis>=5.0
.gitignore — чтобы не заливать мусор:

gitignore
__pycache__/
*.py[cod]
.venv/
venv/
.env
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.idea/
.vscode/
*.log
.env.example — если вынесете креды в переменные окружения:

text
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/ai_agent_pg
REDIS_URL=redis://localhost:6379/0
Команды для заливки на GitHub
bash
# в папке проекта
git init
git add README.md requirements.txt .gitignore models.py start.py
git commit -m "Initial commit: async SQLAlchemy + PostgreSQL + Redis chat repo"

# создать репозиторий на github.com (без README, без .gitignore)
git branch -M main
git remote add origin https://github.com/<your-username>/sqlRedis.git
git push -u origin main