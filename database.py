import asyncio
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_post (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    kind TEXT NOT NULL,
    html_text TEXT,
    media_path TEXT,
    media_type TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    """SQLite store for lightweight local bot settings and the saved promo post."""

    def __init__(self, path: str):
        self.path = path

    async def init(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SQLITE_SCHEMA)
            await db.commit()

    async def set_setting(self, key: str, value: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            await db.commit()

    async def get_setting(self, key: str, default=None):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cur.fetchone()
            return row[0] if row else default

    async def save_post(self, kind, html_text, media_path=None, media_type=None):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO promo_post(id,kind,html_text,media_path,media_type)
                   VALUES(1,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     kind=excluded.kind,
                     html_text=excluded.html_text,
                     media_path=excluded.media_path,
                     media_type=excluded.media_type,
                     updated_at=CURRENT_TIMESTAMP""",
                (kind, html_text, media_path, media_type),
            )
            await db.commit()

    async def get_post(self):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM promo_post WHERE id=1")
            row = await cur.fetchone()
            return dict(row) if row else None


class MongoSessionStore:
    """MongoDB-backed encrypted Telethon session storage.

    PyMongo itself is synchronous; tiny metadata operations are moved to a worker
    thread so the Telegram event loop stays responsive.
    """

    def __init__(self, uri: str, db_name: str = "promo_bot"):
        self.uri = uri
        self.db_name = db_name
        self.client = MongoClient(uri, serverSelectionTimeoutMS=10000)
        self.collection = self.client[db_name]["sessions"]

    async def init(self):
        def _init():
            self.client.admin.command("ping")
            self.collection.create_index([("tg_user_id", ASCENDING)], unique=True)
            self.collection.create_index([("enabled", ASCENDING)])

        await asyncio.to_thread(_init)

    async def add_session(self, label, session_enc, tg_user_id, username, first_name):
        now = datetime.now(timezone.utc)

        def _add():
            # One stored session per Telegram user account. Re-adding refreshes it.
            self.collection.update_one(
                {"tg_user_id": int(tg_user_id)},
                {
                    "$set": {
                        "label": label,
                        "session_enc": session_enc,
                        "tg_user_id": int(tg_user_id),
                        "username": username,
                        "first_name": first_name,
                        "enabled": True,
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
            doc = self.collection.find_one({"tg_user_id": int(tg_user_id)}, {"_id": 1})
            return str(doc["_id"])

        return await asyncio.to_thread(_add)

    @staticmethod
    def _serialize(doc):
        return {
            "id": str(doc["_id"]),
            "label": doc.get("label", "Account"),
            "session_enc": doc["session_enc"],
            "tg_user_id": doc.get("tg_user_id"),
            "username": doc.get("username"),
            "first_name": doc.get("first_name"),
            "enabled": bool(doc.get("enabled", True)),
        }

    async def get_sessions(self, enabled_only=False):
        def _get():
            query = {"enabled": True} if enabled_only else {}
            docs = list(self.collection.find(query).sort("created_at", ASCENDING))
            return [self._serialize(d) for d in docs]

        return await asyncio.to_thread(_get)

    async def delete_session(self, session_id: str):
        from bson import ObjectId
        from bson.errors import InvalidId

        def _delete():
            # Accept either Mongo ObjectId or Telegram numeric user ID.
            try:
                query = {"_id": ObjectId(session_id)}
            except (InvalidId, TypeError):
                if str(session_id).isdigit():
                    query = {"tg_user_id": int(session_id)}
                else:
                    return 0
            return self.collection.delete_one(query).deleted_count

        return await asyncio.to_thread(_delete)

    async def close(self):
        await asyncio.to_thread(self.client.close)
