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
CREATE TABLE IF NOT EXISTS promo_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    html_text TEXT,
    media_path TEXT,
    media_type TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    """SQLite store for settings and a library of promotion posts."""

    def __init__(self, path: str):
        self.path = path

    async def init(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SQLITE_SCHEMA)
            # One-time migration; deleting every post must not restore the legacy post.
            cur = await db.execute("SELECT value FROM settings WHERE key='posts_migrated_v1'")
            if not await cur.fetchone():
                await db.execute(
                    """INSERT INTO promo_posts (kind,html_text,media_path,media_type,enabled)
                       SELECT kind,html_text,media_path,media_type,1
                       FROM promo_post WHERE id=1
                         AND NOT EXISTS (SELECT 1 FROM promo_posts)"""
                )
                await db.execute(
                    "INSERT INTO settings(key,value) VALUES('posts_migrated_v1','1')"
                )
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

    async def add_post(self, kind, html_text, media_path=None, media_type=None):
        async with aiosqlite.connect(self.path) as conn:
            cur = await conn.execute(
                "INSERT INTO promo_posts(kind,html_text,media_path,media_type,enabled) VALUES(?,?,?,?,1)",
                (kind, html_text, media_path, media_type),
            )
            await conn.commit()
            return cur.lastrowid

    async def list_posts(self, enabled_only=False):
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            query = "SELECT * FROM promo_posts"
            if enabled_only:
                query += " WHERE enabled=1"
            query += " ORDER BY id ASC"
            cur = await conn.execute(query)
            return [dict(row) for row in await cur.fetchall()]

    async def get_post_by_id(self, post_id):
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM promo_posts WHERE id=?", (post_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_post(self):
        """Compatibility helper for older callers; first enabled post."""
        posts = await self.list_posts(enabled_only=True)
        return posts[0] if posts else None

    async def delete_post(self, post_id):
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM promo_posts WHERE id=?", (post_id,))
            row = await cur.fetchone()
            if not row:
                return None
            await conn.execute("DELETE FROM promo_posts WHERE id=?", (post_id,))
            await conn.commit()
            return dict(row)

    async def toggle_post(self, post_id):
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM promo_posts WHERE id=?", (post_id,))
            row = await cur.fetchone()
            if not row:
                return None
            new_enabled = 0 if row["enabled"] else 1
            await conn.execute("UPDATE promo_posts SET enabled=? WHERE id=?", (new_enabled, post_id))
            await conn.commit()
            return bool(new_enabled)

    async def next_reply_post(self, mode="rotate"):
        """Pick an enabled post for an incoming opted-in DM auto-reply.

        Persist the rotation counter atomically, including through bot restarts.
        """
        import random
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            if mode == "rotate":
                await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute("SELECT * FROM promo_posts WHERE enabled=1 ORDER BY id")
            posts = [dict(row) for row in await cur.fetchall()]
            if not posts:
                if mode == "rotate":
                    await conn.rollback()
                return None
            if mode == "random":
                return random.choice(posts)
            cur = await conn.execute("SELECT value FROM settings WHERE key='reply_post_cursor'")
            row = await cur.fetchone()
            cursor = int(row[0]) if row else 0
            chosen = posts[cursor % len(posts)]
            await conn.execute(
                "INSERT INTO settings(key,value) VALUES('reply_post_cursor',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(cursor + 1),),
            )
            await conn.commit()
            return chosen


class MongoSessionStore:
    """MongoDB-backed encrypted Telethon session storage.

    PyMongo itself is synchronous; tiny metadata operations are moved to a worker
    thread so the Telegram event loop stays responsive.
    """

    def __init__(self, uri: str, db_name: str = "promo_bot", owner_id: int | None = None, primary_owner: bool = False):
        self.uri = uri
        self.db_name = db_name
        self.client = MongoClient(uri, serverSelectionTimeoutMS=10000)
        self.collection = self.client[db_name]["sessions"]
        self.owner_id = owner_id
        self.primary_owner = primary_owner

    def _owner_filter(self):
        if self.primary_owner:
            # Backward compatibility: pre-multiuser sessions have no owner_id.
            return {"$or": [{"owner_id": self.owner_id}, {"owner_id": {"$exists": False}}]}
        return {"owner_id": self.owner_id}

    async def init(self):
        def _init():
            self.client.admin.command("ping")
            self.collection.create_index([("tg_user_id", ASCENDING)], unique=True)
            self.collection.create_index([("enabled", ASCENDING)])
            self.collection.create_index([("owner_id", ASCENDING), ("enabled", ASCENDING)])

        await asyncio.to_thread(_init)

    async def add_session(self, label, session_enc, tg_user_id, username, first_name):
        now = datetime.now(timezone.utc)

        def _add():
            # One stored session per Telegram user account. Re-adding refreshes it.
            self.collection.update_one(
                dict(self._owner_filter(), tg_user_id=int(tg_user_id)) if not self.primary_owner else {"$and": [self._owner_filter(), {"tg_user_id": int(tg_user_id)}]},
                {
                    "$set": {
                        "label": label,
                        "session_enc": session_enc,
                        "tg_user_id": int(tg_user_id),
                        "owner_id": self.owner_id,
                        "username": username,
                        "first_name": first_name,
                        "enabled": True,
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
            doc = self.collection.find_one({"$and": [self._owner_filter(), {"tg_user_id": int(tg_user_id)}]}, {"_id": 1})
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
            query = {"$and": [self._owner_filter(), {"enabled": True}]} if enabled_only else self._owner_filter()
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
            return self.collection.delete_one({"$and": [self._owner_filter(), query]}).deleted_count

        return await asyncio.to_thread(_delete)

    async def close(self):
        await asyncio.to_thread(self.client.close)
