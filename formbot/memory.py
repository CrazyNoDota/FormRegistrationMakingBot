import json
import aiosqlite
from pathlib import Path
from typing import Optional

_db_path: str = ""


async def init(path: str) -> None:
    global _db_path
    _db_path = path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    schema = (Path(__file__).parent.parent / "schema.sql").read_text()
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(schema)
        await db.commit()


async def get_value(user_id: int, key: str) -> Optional[str]:
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT value FROM profile WHERE user_id=? AND field_key=?", (user_id, key)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_value(user_id: int, key: str, value: str) -> None:
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO profile (user_id, field_key, value, updated_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (user_id, key, value),
        )
        await db.commit()


async def get_all(user_id: int) -> dict[str, str]:
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT field_key, value FROM profile WHERE user_id=? ORDER BY field_key",
            (user_id,),
        ) as cur:
            return {r[0]: r[1] for r in await cur.fetchall()}


async def delete_value(user_id: int, key: str) -> bool:
    async with aiosqlite.connect(_db_path) as db:
        cur = await db.execute(
            "DELETE FROM profile WHERE user_id=? AND field_key=?", (user_id, key)
        )
        await db.commit()
        return cur.rowcount > 0


async def save_session(user_id: int, form_url: str, state: dict) -> None:
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO sessions (user_id, form_url, state_json, updated_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (user_id, form_url, json.dumps(state)),
        )
        await db.commit()


async def load_session(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT form_url, state_json FROM sessions WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                state = json.loads(row[1])
                state["form_url"] = row[0]
                return state
            return None


async def clear_session(user_id: int) -> None:
    async with aiosqlite.connect(_db_path) as db:
        await db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        await db.commit()


async def log_submission(user_id: int, form_url: str, status: str) -> None:
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "INSERT INTO form_submissions (user_id, form_url, status) VALUES (?, ?, ?)",
            (user_id, form_url, status),
        )
        await db.commit()
