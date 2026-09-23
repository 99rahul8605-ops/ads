import asyncio
import html
import logging
import os
import time
import random
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telethon import TelegramClient, errors, types, events
from telethon.sessions import StringSession

from database import Database, MongoSessionStore
from pacing import AccountPacer

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
DB_PATH = os.getenv("DB_PATH", "promo.db")
MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB = os.getenv("MONGO_DB", "promo_bot")
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "media"))
DEFAULT_INTERVAL_MINUTES = int(os.getenv("DEFAULT_INTERVAL_MINUTES", "60"))
GROUP_DELAY_SECONDS = max(15, int(os.getenv("GROUP_DELAY_SECONDS", "30")))
DM_BROADCAST_DELAY_SECONDS = max(30, int(os.getenv("DM_BROADCAST_DELAY_SECONDS", "60")))
DM_REPLY_SEND_GAP_SECONDS = max(2, int(os.getenv("DM_REPLY_SEND_GAP_SECONDS", "5")))
RESTRICTION_PAUSE_HOURS = max(24, int(os.getenv("RESTRICTION_PAUSE_HOURS", "24")))
FERNET_KEY = os.environ["FERNET_KEY"].encode()
fernet = Fernet(FERNET_KEY)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


class SecretRedactingFormatter(logging.Formatter):
    """Redact secrets even when they appear inside exception tracebacks or URLs."""

    def __init__(self, fmt: str, secrets: list[str]):
        super().__init__(fmt)
        self._secrets = [x for x in secrets if x]

    def format(self, record):
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return text


def configure_logging():
    # HTTPX logs Telegram Bot API URLs; those URLs contain the bot token.
    # Keep request-level libraries quiet and redact secrets from every root log.
    for name in ("httpx", "httpcore"):
        noisy = logging.getLogger(name)
        noisy.setLevel(logging.CRITICAL)
        noisy.propagate = False
        for h in list(noisy.handlers):
            noisy.removeHandler(h)


    # Telethon logs automatic flood-wait sleeps at INFO. Keep warnings/errors,
    # while the cache below prevents repeated GetDialogsRequest calls.
    logging.getLogger("telethon.client.users").setLevel(logging.WARNING)

    formatter = SecretRedactingFormatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        [BOT_TOKEN, API_HASH, MONGO_URI, FERNET_KEY.decode("utf-8", errors="ignore")],
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.setFormatter(formatter)


configure_logging()
log = logging.getLogger("promo-bot")

pacer = AccountPacer()
db = Database(DB_PATH)
session_store = MongoSessionStore(MONGO_URI, MONGO_DB)

MENU = ReplyKeyboardMarkup(
    [
        ["➕ Add Session", "👤 Sessions"],
        ["📝 Save Post", "📚 Manage Posts"],
        ["⏱ Interval", "⏳ Group Delay"],
        ["🔄 Rescan Targets", "📊 Live Stats"],
        ["▶️ Start Promo", "⏹ Stop Promo"],
        ["📨 DM Broadcast"],
        ["💬 Auto Reply"],
        ["🗑 Remove Session"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

POST_MENU = ReplyKeyboardMarkup(
    [
        ["📝 Save Post", "📋 List Posts"],
        ["👁 Preview Post", "🔘 Toggle Post"],
        ["🗑 Delete Post", "🔀 Sending Mode"],
        ["⬅️ Back"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

POST_MODE_MENU = ReplyKeyboardMarkup(
    [["🔄 Rotate", "🎲 Random"], ["⬅️ Back"]],
    resize_keyboard=True,
    is_persistent=True,
)

AUTO_REPLY_MENU = ReplyKeyboardMarkup(
    [
        ["✏️ Account Reply", "✏️ Control Bot Reply"],
        ["👁 Reply Preview", "🔄 Use Saved Post"],
        ["🟢 Toggle Account Reply", "🟢 Toggle Bot Reply"],
        ["⏱ Reply Cooldown", "📡 Reply Status"],
        ["⬅️ Back"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

ADD_SESSION_MENU = ReplyKeyboardMarkup(
    [
        ["🔑 Session String", "📱 Login with Phone"],
        ["⬅️ Back"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

login_flows: dict[int, dict] = {}
# In-memory target cache: session Mongo ID -> {groups, users, scanned_at}.
# Cached InputPeer objects avoid a fresh GetDialogsRequest on every promotion round.
target_cache: dict[str, dict] = {}
TARGET_CACHE_TTL = max(300, int(os.getenv("TARGET_CACHE_TTL_SECONDS", "21600")))

# Direct replies only to people who initiate a private conversation.
DM_AUTO_REPLY_ENABLED = os.getenv("DM_AUTO_REPLY_ENABLED", "true").lower() in {"true", "1", "yes", "on"}
ACCOUNT_DM_AUTO_REPLY_ENABLED = os.getenv("ACCOUNT_DM_AUTO_REPLY_ENABLED", "true").lower() in {"true", "1", "yes", "on"}
DM_REPLY_COOLDOWN_SECONDS = max(60, int(os.getenv("DM_REPLY_COOLDOWN_SECONDS", "300")))
DM_AUTO_REPLY_TEXT = os.getenv(
    "DM_AUTO_REPLY_TEXT",
    "👋 Hi! Thanks for reaching out. Your message has been received. We will reply when available."
).replace("\\n", "\n")
ACCOUNT_DM_AUTO_REPLY_TEXT = os.getenv(
    "ACCOUNT_DM_AUTO_REPLY_TEXT",
    "👋 Hi! Thanks for messaging. We will get back to you soon."
).replace("\\n", "\n")
DM_AUTO_REPLY_USE_SAVED_POST = os.getenv("DM_AUTO_REPLY_USE_SAVED_POST", "false").lower() in {"true", "1", "yes", "on"}
async def reply_setting(key: str, env_default: str) -> str:
    return await db.get_setting(key, env_default)


async def reply_enabled(key: str, default: bool) -> bool:
    return (await reply_setting(key, "true" if default else "false")).lower() in {"true", "1", "yes", "on"}


async def reply_cooldown() -> int:
    try:
        return max(60, int(await reply_setting("dm_reply_cooldown_seconds", str(DM_REPLY_COOLDOWN_SECONDS))))
    except (ValueError, TypeError):
        return DM_REPLY_COOLDOWN_SECONDS


async def reply_panel_text() -> str:
    acc = await reply_enabled("account_dm_enabled", ACCOUNT_DM_AUTO_REPLY_ENABLED)
    bot = await reply_enabled("control_bot_dm_enabled", DM_AUTO_REPLY_ENABLED)
    saved_post = await reply_enabled("dm_use_saved_post", DM_AUTO_REPLY_USE_SAVED_POST)
    cooldown = await reply_cooldown()
    return (
        "💬 <b>AUTO REPLY SETTINGS</b>\n\n"
        f"👤 Connected real IDs: <b>{'ON' if acc else 'OFF'}</b>\n"
        f"🤖 Control bot: <b>{'ON' if bot else 'OFF'}</b>\n"
        f"📝 Real-ID reply mode: <b>{'Saved promo post' if saved_post else 'Custom text'}</b>\n"
        f"⏱ Cooldown: <b>{cooldown // 60} min</b>\n\n"
        f"📡 Real-ID listeners connected: <b>{sum(c.is_connected() for c in _account_dm_clients.values())}</b>\n"
        f"✅ Account replies sent since restart: <b>{_reply_sent_counts['accounts']}</b>\n\n"
        "Choose a button to edit. Changes apply without restart."
    )


# A single persistent Telethon connection per account is shared by group promotion,
# manual DM broadcasts and incoming-DM handlers. Separate simultaneous clients using
# the same StringSession can compete for updates, making auto-replies unreliable.
_account_dm_clients: dict[str, TelegramClient] = {}
_account_client_locks: dict[str, asyncio.Lock] = {}
_account_reply_timestamps: dict[tuple[str, int], float] = {}
_account_reply_pending: set[tuple[str, int]] = set()
_reply_sent_counts = {"accounts": 0, "control_bot": 0, "failed": 0}
_bot_reply_timestamps: dict[int, float] = {}

scan_locks: dict[str, asyncio.Lock] = {}
# Avoid back-to-back repeats per target in Random mode (while the process is running).
last_random_post_by_target: dict[tuple[str, str], int] = {}

@dataclass
class RuntimeStats:
    running: bool = False
    round_no: int = 0
    started_at: float | None = None
    current_session: str = "-"
    current_group: str = "-"
    current_post: str = "-"
    post_mode: str = "rotate"
    active_posts: int = 0
    total_groups: int = 0
    total_users: int = 0
    group_attempted: int = 0
    group_sent: int = 0
    group_failed: int = 0
    group_skipped: int = 0
    user_attempted: int = 0
    user_sent: int = 0
    user_failed: int = 0
    user_skipped: int = 0
    attempted: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    flood_waits: int = 0
    paused_sessions: int = 0
    group_delay_seconds: int = GROUP_DELAY_SECONDS
    last_error: str = "-"
    next_round_at: float | None = None
    cached_group_count: dict = field(default_factory=dict)
    cached_user_count: dict = field(default_factory=dict)

stats = RuntimeStats()
promo_task: asyncio.Task | None = None
state: dict[int, str] = {}


def admin_only(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


def esc(s):
    return html.escape(str(s or ""))


async def posts_summary():
    posts = await db.list_posts()
    mode = await db.get_setting("post_mode", "rotate")
    if not posts:
        return "📚 <b>POST LIBRARY</b>\n\nNo posts saved. Select 📝 Save Post to add one."
    lines = [f"📚 <b>POST LIBRARY</b> ({len(posts)} total)",
             f"Sending mode: <b>{esc(mode.title())}</b>", ""]
    for post in posts:
        preview = (post.get("html_text") or "(media only)")
        preview = html.unescape(preview)
        preview = preview.replace("\n", " ")
        if len(preview) > 65:
            preview = preview[:62] + "..."
        status = "✅ ON" if post.get("enabled") else "⏸ OFF"
        lines.append(f"<b>#{post['id']}</b> {status} · {esc(post['kind'])} · {esc(preview)}")
    return "\n".join(lines)


async def preview_saved_post(message, post):
    label = f"👁 Preview #<b>{post['id']}</b> ({'enabled' if post.get('enabled') else 'disabled'})"
    await message.reply_text(label, parse_mode=ParseMode.HTML, reply_markup=POST_MENU)
    media_path = post.get("media_path")
    caption = post.get("html_text") or None
    if media_path:
        if not Path(media_path).exists():
            await message.reply_text("⚠️ Saved media file is missing from this server.", reply_markup=POST_MENU)
            return
        with open(media_path, "rb") as fh:
            if post["kind"] == "photo":
                await message.reply_photo(fh, caption=caption, parse_mode=ParseMode.HTML)
            elif post["kind"] == "video":
                await message.reply_video(fh, caption=caption, parse_mode=ParseMode.HTML)
            elif post["kind"] == "animation":
                await message.reply_animation(fh, caption=caption, parse_mode=ParseMode.HTML)
            else:
                await message.reply_document(fh, caption=caption, parse_mode=ParseMode.HTML)
    elif caption:
        await message.reply_text(caption, parse_mode=ParseMode.HTML, reply_markup=POST_MENU)


def fmt_duration(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def stats_text() -> str:
    total_targets = stats.total_groups + stats.total_users
    pct = (stats.attempted / total_targets * 100) if total_targets else 0
    elapsed = fmt_duration(time.time() - stats.started_at) if stats.started_at else "-"
    next_run = "-"
    if stats.next_round_at:
        next_run = fmt_duration(stats.next_round_at - time.time())
    return (
        "📊 <b>LIVE PROMOTION STATS</b>\n\n"
        f"Status: {'🟢 Running' if stats.running else '🔴 Stopped'}\n"
        f"Round: <b>{stats.round_no}</b>\n"
        f"📝 Active posts: <b>{stats.active_posts}</b> | Mode: <b>{esc(stats.post_mode.title())}</b>\n"
        f"📮 Current post: <b>{esc(stats.current_post)}</b>\n"
        f"Overall: <b>{stats.attempted}/{total_targets}</b> ({pct:.1f}%)\n\n"
        "👥 <b>GROUPS</b>\n"
        f"Total: <b>{stats.total_groups}</b> | Tried: <b>{stats.group_attempted}</b>\n"
        f"✅ {stats.group_sent}  ❌ {stats.group_failed}  ⏭ {stats.group_skipped}\n\n"
        "👤 <b>OPT-IN USERS</b>\n"
        f"Total: <b>{stats.total_users}</b> | Tried: <b>{stats.user_attempted}</b>\n"
        f"✅ {stats.user_sent}  ❌ {stats.user_failed}  ⏭ {stats.user_skipped}\n\n"
        f"⏳ Flood waits: <b>{stats.flood_waits}</b> | Paused IDs: <b>{stats.paused_sessions}</b>\n"
        f"⏳ Minimum group gap: <b>{stats.group_delay_seconds}s</b>\n"
        f"🔐 Session: <code>{esc(stats.current_session)}</code>\n"
        f"🎯 Current: {esc(stats.current_group)}\n"
        f"⏱ Elapsed: {elapsed}\n"
        f"🔁 Next round in: {next_run}\n"
        f"⚠️ Last error: <code>{esc(stats.last_error)[:500]}</code>"
    )


async def make_client(session_string: str) -> TelegramClient:
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH, flood_sleep_threshold=0)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Session is not authorized")
    return client


async def get_account_client(row: dict) -> TelegramClient:
    """Get or create the one long-lived, event-enabled client for this account.

    The per-account lock prevents both the listener service and a broadcast
    starting simultaneously from opening duplicate sessions.
    """
    sid = str(row["id"])
    lock = _account_client_locks.setdefault(sid, asyncio.Lock())
    async with lock:
        existing = _account_dm_clients.get(sid)
        if existing and existing.is_connected():
            return existing
        if existing:
            _account_dm_clients.pop(sid, None)
            try:
                await existing.disconnect()
            except Exception:
                pass
        client = await make_client(decrypt_session(row["session_enc"]))
        # Register on the SAME client that broadcasts will use. The handler
        # checks its enabled setting on every incoming message.
        client.add_event_handler(account_reply_handler(sid), events.NewMessage(incoming=True))
        _account_dm_clients[sid] = client
        log.info("Account DM listener ready for %s", row.get("label", sid))
        return client


async def close_account_client(sid: str):
    sid = str(sid)
    lock = _account_client_locks.setdefault(sid, asyncio.Lock())
    async with lock:
        client = _account_dm_clients.pop(sid, None)
        if client:
            await client.disconnect()


def decrypt_session(value: str) -> str:
    try:
        return fernet.decrypt(value.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("FERNET_KEY does not match stored sessions") from e


async def _input_peer(entity):
    """Build a reusable InputPeer so later rounds do not need entity resolution."""
    if isinstance(entity, types.Chat):
        return types.InputPeerChat(entity.id)
    if isinstance(entity, types.Channel):
        return types.InputPeerChannel(entity.id, entity.access_hash or 0)
    if isinstance(entity, types.User):
        return types.InputPeerUser(entity.id, entity.access_hash or 0)
    return entity


async def scan_targets(client: TelegramClient):
    """Scan dialogs once and cache joined groups only for recurring promotion.

    Private dialogs are intentionally not part of the recurring scheduler. A separate
    one-time DM broadcast action can scan existing private dialogs when explicitly used.
    """
    groups = []
    async for dialog in client.iter_dialogs(limit=None):
        entity = dialog.entity
        name = dialog.name or str(dialog.id)
        if isinstance(entity, types.Chat):
            groups.append((await _input_peer(entity), name))
            continue
        if isinstance(entity, types.Channel) and getattr(entity, "megagroup", False):
            groups.append((await _input_peer(entity), name))
    return groups, []


async def scan_existing_dm_users(client: TelegramClient):
    """Return existing private user dialogs with one GetDialogs traversal."""
    users = []
    me = await client.get_me()
    async for dialog in client.iter_dialogs(limit=None):
        entity = dialog.entity
        if not isinstance(entity, types.User):
            continue
        if entity.id == me.id or getattr(entity, "bot", False) or getattr(entity, "deleted", False):
            continue
        display = " ".join(x for x in [getattr(entity, "first_name", None), getattr(entity, "last_name", None)] if x)
        users.append((await _input_peer(entity), display or getattr(entity, "username", None) or str(entity.id)))
    return users


async def get_cached_targets(row: dict, client: TelegramClient, force: bool = False):
    """Return cached targets, refreshing at most once per TTL unless force=True."""
    sid = str(row["id"])
    lock = scan_locks.setdefault(sid, asyncio.Lock())
    async with lock:
        cached = target_cache.get(sid)
        now = time.time()
        if cached and not force and now - cached["scanned_at"] < TARGET_CACHE_TTL:
            return cached["groups"], cached["users"], False
        groups, users = await scan_targets(client)
        target_cache[sid] = {"groups": groups, "users": users, "scanned_at": now}
        stats.cached_group_count[row["id"]] = len(groups)
        stats.cached_user_count[row["id"]] = len(users)
        return groups, users, True


async def get_group_delay() -> int:
    """Per-session minimum gap between group sends, editable from the admin menu."""
    try:
        return min(600, max(15, int(await db.get_setting("group_delay_seconds", str(GROUP_DELAY_SECONDS)))))
    except (ValueError, TypeError):
        return GROUP_DELAY_SECONDS


async def get_pause_remaining(session_id: str) -> int:
    """Persisted in settings so restarting the bot never clears a FloodWait."""
    try:
        paused_until = float(await db.get_setting(f"account_pause:{session_id}", "0"))
    except (ValueError, TypeError):
        return 0
    return max(0, int(paused_until - time.time() + 0.999))


async def pause_account(session_id: str, seconds: float, reason: str):
    seconds = max(1, int(seconds))
    key = f"account_pause:{session_id}"
    try:
        old_until = float(await db.get_setting(key, "0"))
    except (ValueError, TypeError):
        old_until = 0
    until = max(old_until, time.time() + seconds)
    await db.set_setting(key, str(until))
    await db.set_setting(f"account_pause_reason:{session_id}", reason)
    log.warning("Session %s paused for %ss: %s", session_id, max(1, int(until-time.time())), reason)


class AccountPausedError(Exception):
    def __init__(self, remaining: int):
        self.remaining = int(remaining)
        super().__init__(f"Account cooling down ({self.remaining}s left)")


async def guarded_account_send(session_id: str, operation):
    """Re-check account restrictions at execution time, after any queue wait."""
    remaining = await get_pause_remaining(session_id)
    if remaining:
        raise AccountPausedError(remaining)
    return await operation()


async def send_saved_post(client: TelegramClient, entity, post: dict):
    text = post.get("html_text") or ""
    media_path = post.get("media_path")
    if media_path:
        if not Path(media_path).exists():
            raise FileNotFoundError(f"Saved media missing: {media_path}")
        await client.send_file(
            entity,
            media_path,
            caption=text or None,
            parse_mode="html",
        )
    else:
        await client.send_message(entity, text, parse_mode="html", link_preview=False)


def pick_post(posts: list[dict], mode: str, index: int, round_number: int = 1, previous_id=None):
    if not posts:
        raise RuntimeError("No active promotion posts")
    if mode == "random":
        choices = [post for post in posts if post["id"] != previous_id] if len(posts) > 1 else posts
        return random.choice(choices or posts)
    return posts[(index + round_number - 1) % len(posts)]


async def _run_session_round(row, client, groups, users, posts, mode, progress_msg, edit_lock):
    """Independent account worker; sequential, paced group sends and no immediate flood retries."""
    targets = [("group", e, n) for e, n in groups] + [("user", e, n) for e, n in users]
    sid = str(row["id"])
    last_edit = 0.0
    delay = stats.group_delay_seconds
    try:
        for target_index, (target_type, entity, name) in enumerate(targets):
            if not stats.running:
                return
            stats.current_session = row["label"]
            stats.current_group = f"{'👥' if target_type == 'group' else '👤'} {name}"
            target_key = (sid, str(entity))
            post = pick_post(posts, mode, target_index, stats.round_no,
                             last_random_post_by_target.get(target_key))
            if mode == "random":
                last_random_post_by_target[target_key] = post["id"]
            stats.current_post = f"#{post['id']}"
            stop_this_account = False
            try:
                # A single pacing lock is shared with this account's incoming DM auto-reply.
                await pacer.run(
                    sid, delay if target_type == "group" else DM_BROADCAST_DELAY_SECONDS,
                    lambda: guarded_account_send(sid, lambda: send_saved_post(client, entity, post)),
                )
                stats.sent += 1
                if target_type == "group":
                    stats.group_sent += 1
                else:
                    stats.user_sent += 1
            except AccountPausedError as e:
                stats.skipped += 1
                if target_type == "group":
                    stats.group_skipped += 1
                else:
                    stats.user_skipped += 1
                stats.paused_sessions += 1
                stats.last_error = f"{row['label']}: account cooling down ({e.remaining}s left)"
                stop_this_account = True
            except errors.SlowModeWaitError as e:
                # Slow mode is a chat-level restriction; skip this chat for this round.
                stats.skipped += 1
                stats.group_skipped += 1
                stats.last_error = f"{row['label']}: {name} has slow mode ({e.seconds}s)"
            except errors.FloodWaitError as e:
                # Do not immediately retry the same target after FloodWait. Respect
                # Telegram's stated cooldown, persist it, and stop this account's round.
                stats.flood_waits += 1
                stats.skipped += 1
                if target_type == "group":
                    stats.group_skipped += 1
                else:
                    stats.user_skipped += 1
                wait = int(e.seconds) + delay
                await pause_account(sid, wait, f"Telegram FloodWait ({e.seconds}s)")
                stats.last_error = f"{row['label']}: FloodWait {e.seconds}s; account paused"
                stats.paused_sessions += 1
                stop_this_account = True
            except errors.PeerFloodError:
                # PeerFlood indicates an account-level anti-spam restriction.
                # Delays cannot lift that restriction; pause this account.
                await pause_account(sid, RESTRICTION_PAUSE_HOURS * 3600,
                                    "Telegram account restriction (PeerFlood); review account status")
                stats.skipped += 1
                if target_type == "group":
                    stats.group_skipped += 1
                else:
                    stats.user_skipped += 1
                stats.last_error = f"{row['label']}: account restricted; sending paused"
                stats.paused_sessions += 1
                stop_this_account = True
            except (errors.ChatWriteForbiddenError, errors.UserBannedInChannelError,
                    errors.ChatAdminRequiredError):
                stats.skipped += 1
                if target_type == "group":
                    stats.group_skipped += 1
                else:
                    stats.user_skipped += 1
                stats.last_error = f"{row['label']}: write not allowed: {name}"
            except Exception as e:
                stats.failed += 1
                if target_type == "group":
                    stats.group_failed += 1
                else:
                    stats.user_failed += 1
                stats.last_error = f"{row['label']}: {type(e).__name__}: {e}"
            finally:
                stats.attempted += 1
                if target_type == "group":
                    stats.group_attempted += 1
                else:
                    stats.user_attempted += 1

            now = time.time()
            if progress_msg and (now - last_edit >= 3 or stop_this_account):
                async with edit_lock:
                    try:
                        await progress_msg.edit_text(stats_text(), parse_mode=ParseMode.HTML)
                    except Exception:
                        pass
                last_edit = now
            if stop_this_account:
                break
            # No explicit sleep here: AccountPacer enforces the minimum gap
            # between completed sends, including interleaved DM auto-replies.
    finally:
        # Persistent shared client continues receiving DMs during and after the round.
        pass


async def do_round(app: Application):
    sessions = await session_store.get_sessions(enabled_only=True)
    posts = await db.list_posts(enabled_only=True)
    mode = await db.get_setting("post_mode", "rotate")
    if mode not in {"rotate", "random"}:
        mode = "rotate"
    if not sessions:
        raise RuntimeError("No active sessions")
    if not posts:
        raise RuntimeError("No enabled promotion posts")

    stats.post_mode = mode
    stats.active_posts = len(posts)
    stats.round_no += 1
    stats.started_at = time.time()
    stats.group_attempted = stats.group_sent = stats.group_failed = stats.group_skipped = 0
    stats.user_attempted = stats.user_sent = stats.user_failed = stats.user_skipped = 0
    stats.attempted = stats.sent = stats.failed = stats.skipped = stats.flood_waits = 0
    stats.total_groups = 0
    stats.total_users = 0
    stats.last_error = "-"
    stats.paused_sessions = 0
    stats.group_delay_seconds = await get_group_delay()

    prepared = []
    # Connect sessions concurrently. A target scan occurs only when cache is missing/stale.
    async def prepare(row):
        client = None
        try:
            remaining = await get_pause_remaining(str(row["id"]))
            if remaining:
                return row, None, [], [], ("paused", remaining)
            client = await get_account_client(row)
            groups, users, _ = await get_cached_targets(row, client, force=False)
            return row, client, groups, users, None
        except errors.FloodWaitError as e:
            await pause_account(str(row["id"]), e.seconds + stats.group_delay_seconds,
                                f"Telegram FloodWait while scanning/connecting ({e.seconds}s)")
            return row, None, [], [], ("paused", int(e.seconds))
        except Exception as e:
            return row, None, [], [], e

    results = await asyncio.gather(*(prepare(row) for row in sessions))
    for row, client, groups, users, err in results:
        if isinstance(err, tuple) and err[0] == "paused":
            stats.paused_sessions += 1
            stats.last_error = f"{row['label']}: account cooling down ({fmt_duration(err[1])} left)"
            continue
        if err:
            stats.failed += 1
            stats.last_error = f"{row['label']}: {type(err).__name__}: {err}"
            continue
        prepared.append((row, client, groups, users))
        stats.total_groups += len(groups)
        stats.total_users += len(users)

    progress_msg = None
    try:
        progress_msg = await app.bot.send_message(ADMIN_ID, stats_text(), parse_mode=ParseMode.HTML)
    except Exception:
        pass

    edit_lock = asyncio.Lock()
    # All sessions run simultaneously; each individual session sends sequentially.
    await asyncio.gather(*(_run_session_round(row, client, groups, users, posts, mode, progress_msg, edit_lock)
                           for row, client, groups, users in prepared))

    if progress_msg:
        try:
            await progress_msg.edit_text(stats_text(), parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def run_one_time_dm_broadcast(app: Application):
    """Manual one-time DM send to previously initiated private dialogs."""
    sessions = await session_store.get_sessions(enabled_only=True)
    posts = await db.list_posts(enabled_only=True)
    mode = await db.get_setting("post_mode", "rotate")
    if mode not in {"rotate", "random"}:
        mode = "rotate"
    if not sessions:
        raise RuntimeError("No active sessions")
    if not posts:
        raise RuntimeError("No enabled promotion posts")
    stats.post_mode = mode
    stats.active_posts = len(posts)

    stats.started_at = time.time()
    stats.current_group = "-"
    stats.group_attempted = stats.group_sent = stats.group_failed = stats.group_skipped = 0
    stats.user_attempted = stats.user_sent = stats.user_failed = stats.user_skipped = 0
    stats.attempted = stats.sent = stats.failed = stats.skipped = stats.flood_waits = 0
    stats.total_groups = 0
    stats.total_users = 0
    stats.last_error = "-"
    stats.paused_sessions = 0
    stats.group_delay_seconds = await get_group_delay()

    prepared = []
    async def prepare_dm(row):
        client = None
        try:
            remaining = await get_pause_remaining(str(row["id"]))
            if remaining:
                return row, None, [], ("paused", remaining)
            client = await get_account_client(row)
            users = await scan_existing_dm_users(client)
            return row, client, users, None
        except errors.FloodWaitError as e:
            await pause_account(str(row["id"]), e.seconds + DM_BROADCAST_DELAY_SECONDS,
                                f"Telegram FloodWait while scanning DMs ({e.seconds}s)")
            return row, None, [], ("paused", int(e.seconds))
        except Exception as e:
            return row, None, [], e

    results = await asyncio.gather(*(prepare_dm(row) for row in sessions))
    for row, client, users, err in results:
        if isinstance(err, tuple) and err[0] == "paused":
            stats.paused_sessions += 1
            stats.last_error = f"{row['label']}: account cooling down ({fmt_duration(err[1])} left)"
            continue
        if err:
            stats.failed += 1
            stats.last_error = f"{row['label']}: {type(err).__name__}: {err}"
            continue
        prepared.append((row, client, users))
        stats.total_users += len(users)

    progress_msg = None
    try:
        progress_msg = await app.bot.send_message(ADMIN_ID, "📨 <b>DM BROADCAST STARTED</b>\n\n" + stats_text(), parse_mode=ParseMode.HTML)
    except Exception:
        pass

    edit_lock = asyncio.Lock()

    async def worker(row, client, users):
        sid = str(row["id"])
        last_edit = 0.0
        try:
            remaining = await get_pause_remaining(sid)
            if remaining:
                stats.paused_sessions += 1
                stats.last_error = f"{row['label']}: paused for {fmt_duration(remaining)}"
                return
            for target_index, (entity, name) in enumerate(users):
                stats.current_session = row["label"]
                stats.current_group = f"👤 {name}"
                post = pick_post(posts, mode, target_index, stats.round_no)
                stats.current_post = f"#{post['id']}"
                stop_this_account = False
                try:
                    await pacer.run(sid, DM_BROADCAST_DELAY_SECONDS,
                                    lambda: guarded_account_send(sid, lambda: send_saved_post(client, entity, post)))
                    stats.sent += 1
                    stats.user_sent += 1
                except AccountPausedError as e:
                    stats.skipped += 1
                    stats.user_skipped += 1
                    stats.paused_sessions += 1
                    stats.last_error = f"{row['label']}: account cooling down ({e.remaining}s left)"
                    stop_this_account = True
                except errors.FloodWaitError as e:
                    stats.flood_waits += 1
                    stats.skipped += 1
                    stats.user_skipped += 1
                    await pause_account(sid, e.seconds + DM_BROADCAST_DELAY_SECONDS,
                                        f"Telegram FloodWait during manual DM ({e.seconds}s)")
                    stats.last_error = f"{row['label']}: FloodWait {e.seconds}s; account paused"
                    stats.paused_sessions += 1
                    stop_this_account = True
                except errors.PeerFloodError:
                    await pause_account(sid, RESTRICTION_PAUSE_HOURS * 3600,
                                        "Telegram account restriction (PeerFlood)")
                    stats.skipped += 1
                    stats.user_skipped += 1
                    stats.paused_sessions += 1
                    stats.last_error = f"{row['label']}: account restricted; manual DM stopped"
                    stop_this_account = True
                except Exception as e:
                    stats.failed += 1
                    stats.user_failed += 1
                    stats.last_error = f"{row['label']}: {type(e).__name__}: {e}"
                finally:
                    stats.attempted += 1
                    stats.user_attempted += 1
                now = time.time()
                if progress_msg and (now - last_edit >= 3 or stop_this_account):
                    async with edit_lock:
                        try:
                            await progress_msg.edit_text("📨 <b>DM BROADCAST</b>\n\n" + stats_text(),
                                                         parse_mode=ParseMode.HTML)
                        except Exception:
                            pass
                    last_edit = now
                if stop_this_account:
                    break
        finally:
            # Keep listening for incoming DMs after the manual broadcast.
            pass

    await asyncio.gather(*(worker(row, client, users) for row, client, users in prepared))
    stats.current_group = "-"
    stats.current_session = "-"
    if progress_msg:
        try:
            await progress_msg.edit_text("✅ <b>DM BROADCAST COMPLETE</b>\n\n" + stats_text(), parse_mode=ParseMode.HTML)
        except Exception:
            pass



async def dm_broadcast_runner(app: Application):
    try:
        await run_one_time_dm_broadcast(app)
    except Exception as exc:
        log.warning("Manual DM broadcast failed: %s", type(exc).__name__)
        try:
            await app.bot.send_message(ADMIN_ID, "⚠️ Manual DM broadcast stopped because of an error.")
        except Exception:
            pass
    finally:
        app.bot_data["dm_broadcast_running"] = False


async def scheduler(app: Application):
    stats.running = True
    try:
        while stats.running:
            try:
                await do_round(app)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                stats.last_error = f"{type(e).__name__}: {e}"
                try:
                    await app.bot.send_message(ADMIN_ID, f"⚠️ Promotion round error:\n<code>{esc(stats.last_error)}</code>", parse_mode=ParseMode.HTML)
                except Exception:
                    pass

            if not stats.running:
                break
            interval = int(await db.get_setting("interval_minutes", str(DEFAULT_INTERVAL_MINUTES)))
            interval = max(5, interval)
            stats.next_round_at = time.time() + interval * 60
            while stats.running and time.time() < stats.next_round_at:
                await asyncio.sleep(min(5, max(0.2, stats.next_round_at - time.time())))
    finally:
        stats.running = False
        stats.current_group = "-"
        stats.current_session = "-"
        stats.next_round_at = None


async def bot_dm_auto_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages sent directly to the *admin control bot*."""
    if not await reply_enabled("control_bot_dm_enabled", DM_AUTO_REPLY_ENABLED) or not update.message or not update.effective_user:
        return
    if update.effective_user.id == ADMIN_ID or update.effective_user.is_bot:
        return
    if update.effective_chat.type != "private":
        return
    now = time.monotonic()
    uid = update.effective_user.id
    if now - _bot_reply_timestamps.get(uid, -1e20) < await reply_cooldown():
        return
    # Prevent duplicate replies to consecutive incoming media/text messages.
    _bot_reply_timestamps[uid] = now
    try:
        message = await reply_setting("control_bot_dm_text", DM_AUTO_REPLY_TEXT)
        await update.message.reply_text(message, disable_web_page_preview=True)
        _reply_sent_counts["control_bot"] += 1
    except Exception as e:
        _reply_sent_counts["failed"] += 1
        _bot_reply_timestamps.pop(uid, None)
        log.warning("Control bot auto-reply failed: %s", type(e).__name__)


def account_reply_handler(session_key: str):
    """Incoming private DM handler, active even during long broadcast rounds."""
    async def on_dm(event):
        if not event.is_private or event.out:
            return
        if not await reply_enabled("account_dm_enabled", ACCOUNT_DM_AUTO_REPLY_ENABLED):
            return
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False) or getattr(sender, "deleted", False):
            return
        key = (session_key, int(event.sender_id))
        now = time.monotonic()
        if key in _account_reply_pending:
            return
        if now - _account_reply_timestamps.get(key, -1e20) < await reply_cooldown():
            return
        if await get_pause_remaining(session_key):
            # A Telegram FloodWait/PeerFlood applies to replies as well.
            return
        _account_reply_pending.add(key)
        try:
            if await reply_enabled("dm_use_saved_post", DM_AUTO_REPLY_USE_SAVED_POST):
                mode = await db.get_setting("post_mode", "rotate")
                post = await db.next_reply_post(mode)
                if post:
                    await pacer.run(
                        session_key, DM_REPLY_SEND_GAP_SECONDS,
                        lambda: guarded_account_send(session_key, lambda: send_saved_post(event.client, event.input_sender or sender, post)),
                        priority=True,
                    )
                    _account_reply_timestamps[key] = time.monotonic()
                    _reply_sent_counts["accounts"] += 1
                    return
            message = await reply_setting("account_dm_text", ACCOUNT_DM_AUTO_REPLY_TEXT)
            await pacer.run(
                session_key, DM_REPLY_SEND_GAP_SECONDS,
                lambda: guarded_account_send(session_key, lambda: event.reply(message, link_preview=False)),
                priority=True,
            )
            # A failed/delayed reply must not start a 5-minute cooldown.
            _account_reply_timestamps[key] = time.monotonic()
            _reply_sent_counts["accounts"] += 1
        except AccountPausedError:
            # Pause may begin while this reply waits behind a group message.
            return
        except errors.PeerFloodError:
            await pause_account(session_key, RESTRICTION_PAUSE_HOURS * 3600,
                                "Telegram account restriction (PeerFlood) during DM auto-reply")
        except errors.FloodWaitError as e:
            await pause_account(session_key, e.seconds + GROUP_DELAY_SECONDS,
                                f"Telegram FloodWait during DM auto-reply ({e.seconds}s)")
            log.warning("DM auto-reply paused for session %s: FloodWait %ss", session_key, e.seconds)
        except Exception as e:
            _reply_sent_counts["failed"] += 1
            log.warning("Account DM auto-reply failed session=%s: %s", session_key, type(e).__name__)
        finally:
            _account_reply_pending.discard(key)
    return on_dm


async def account_auto_reply_service(app: Application):
    """Keep one Telegram connection per enabled account, shared by all workloads."""
    try:
        while True:
            try:
                rows = await session_store.get_sessions(enabled_only=True)
                enabled = {str(row["id"]): row for row in rows}
                for sid in list(_account_dm_clients):
                    if sid not in enabled:
                        # Do not interrupt an active broadcast; revoke afterward.
                        if not stats.running and not app.bot_data.get("dm_broadcast_running"):
                            await close_account_client(sid)
                for sid, row in enabled.items():
                    try:
                        await get_account_client(row)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("Account listener unavailable for %s: %s",
                                    row.get("label", sid), type(e).__name__)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Account listener manager error: %s", type(e).__name__)
            await asyncio.sleep(10)
    except asyncio.CancelledError:
        raise
    finally:
        # This manager is the sole owner of persistent account connections.
        await asyncio.gather(*(close_account_client(sid) for sid in list(_account_dm_clients)),
                             return_exceptions=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await bot_dm_auto_reply(update, context)
    await update.message.reply_text(
        "📢 <b>Promotion Bot</b>\n\nUse the menu below. Joined groups/supergroups are cached so every promo round does not rescan dialogs. Recurring scheduler targets joined groups only. Use 📨 DM Broadcast for a one-time send to existing private dialogs.",
        parse_mode=ParseMode.HTML,
        reply_markup=MENU,
    )


async def cleanup_login_flow(uid: int):
    flow = login_flows.pop(uid, None)
    if flow and flow.get("client"):
        try:
            await flow["client"].disconnect()
        except Exception:
            pass


async def save_logged_in_client(client: TelegramClient):
    me = await client.get_me()
    session_string = client.session.save()
    if not session_string:
        raise RuntimeError("Could not export StringSession")
    label = f"{me.first_name or 'Account'} ({me.id})"
    enc = fernet.encrypt(session_string.encode()).decode()
    await session_store.add_session(label, enc, me.id, me.username, me.first_name)
    # Do not scan dialogs here. The cache is built on first Start or manual Rescan,
    # which avoids duplicate GetDialogsRequest bursts during account setup.
    return label, None, None


async def safe_delete_message(message):
    try:
        await message.delete()
    except Exception:
        pass


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global promo_task
    if not admin_only(update) or not update.message:
        return
    uid = update.effective_user.id
    text = update.message.text or ""
    if context.user_data.pop("post_saved_message_id", None) == update.message.message_id:
        return

    # Auto-reply submenu: always process Back before text input.
    current = state.get(uid)
    if text == "⬅️ Back":
        state.pop(uid, None)
        await cleanup_login_flow(uid)
        await update.message.reply_text("Main menu.", reply_markup=MENU)
        return

    if current in {"await_account_reply", "await_control_reply"}:
        if text.casefold() in {"cancel", "/cancel", "❌ cancel"}:
            state.pop(uid, None)
            await update.message.reply_text("Cancelled.", reply_markup=AUTO_REPLY_MENU)
            return
        if not text.strip() or len(text) > 4000:
            await update.message.reply_text("Send a text reply of 1–4000 characters, or Back to cancel.", reply_markup=AUTO_REPLY_MENU)
            return
        key = "account_dm_text" if current == "await_account_reply" else "control_bot_dm_text"
        await db.set_setting(key, text)
        if current == "await_account_reply":
            # Explicit text save switches off saved-post mode to avoid confusion.
            await db.set_setting("dm_use_saved_post", "false")
        state.pop(uid, None)
        await update.message.reply_text("✅ Auto-reply saved. New incoming DMs will use it.\n\n" + await reply_panel_text(), parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU)
        return

    if current == "await_reply_cooldown":
        try:
            minutes = int(text)
            if not 1 <= minutes <= 1440:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter whole minutes (1–1440), or Back.", reply_markup=AUTO_REPLY_MENU)
            return
        await db.set_setting("dm_reply_cooldown_seconds", str(minutes * 60))
        state.pop(uid, None)
        await update.message.reply_text("✅ Cooldown updated.\n\n" + await reply_panel_text(), parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU)
        return

    if text == "💬 Auto Reply":
        state.pop(uid, None)
        await update.message.reply_text(await reply_panel_text(), parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU)
        return
    if text == "✏️ Account Reply":
        state[uid] = "await_account_reply"
        await update.message.reply_text("Send the auto-reply text for connected real IDs. It will be saved when received. Press Back to cancel.", reply_markup=AUTO_REPLY_MENU)
        return
    if text == "✏️ Control Bot Reply":
        state[uid] = "await_control_reply"
        await update.message.reply_text("Send the auto-reply text for people DMing this control bot. Press Back to cancel.", reply_markup=AUTO_REPLY_MENU)
        return
    if text == "👁 Reply Preview":
        a = await reply_setting("account_dm_text", ACCOUNT_DM_AUTO_REPLY_TEXT)
        b = await reply_setting("control_bot_dm_text", DM_AUTO_REPLY_TEXT)
        await update.message.reply_text(
            "👁 <b>REPLY PREVIEW</b>\n\n👤 <b>Real IDs:</b>\n<pre>" + esc(a) +
            "</pre>\n🤖 <b>Control bot:</b>\n<pre>" + esc(b) + "</pre>",
            parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU,
        )
        return
    if text == "🔄 Use Saved Post":
        old = await reply_enabled("dm_use_saved_post", DM_AUTO_REPLY_USE_SAVED_POST)
        if not old and not await db.get_post():
            await update.message.reply_text("❌ Save a promotion post first.", reply_markup=AUTO_REPLY_MENU)
            return
        await db.set_setting("dm_use_saved_post", "false" if old else "true")
        await update.message.reply_text(await reply_panel_text(), parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU)
        return
    if text in {"🟢 Toggle Account Reply", "🟢 Toggle Bot Reply"}:
        key, default = (("account_dm_enabled", ACCOUNT_DM_AUTO_REPLY_ENABLED) if text == "🟢 Toggle Account Reply" else ("control_bot_dm_enabled", DM_AUTO_REPLY_ENABLED))
        old = await reply_enabled(key, default)
        await db.set_setting(key, "false" if old else "true")
        await update.message.reply_text(await reply_panel_text(), parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU)
        return
    if text == "📡 Reply Status":
        rows = await session_store.get_sessions(enabled_only=True)
        online = {sid for sid, client in _account_dm_clients.items() if client.is_connected()}
        account_enabled = await reply_enabled("account_dm_enabled", ACCOUNT_DM_AUTO_REPLY_ENABLED)
        control_enabled = await reply_enabled("control_bot_dm_enabled", DM_AUTO_REPLY_ENABLED)
        await update.message.reply_text(
            "📡 <b>AUTO-REPLY STATUS</b>\n\n"
            f"👤 Account replies: <b>{'ON' if account_enabled else 'OFF'}</b>\n"
            f"🤖 Control bot replies: <b>{'ON' if control_enabled else 'OFF'}</b>\n"
            f"🔌 Connected accounts: <b>{len(online)}/{len(rows)}</b>\n"
            f"⏳ DM replies queued: <b>{len(_account_reply_pending)}</b>\n"
            f"✅ Account replies sent: <b>{_reply_sent_counts['accounts']}</b>\n"
            f"✅ Control replies sent: <b>{_reply_sent_counts['control_bot']}</b>\n"
            f"⚠️ Auto-reply errors: <b>{_reply_sent_counts['failed']}</b>\n\n"
            "Queued replies follow the account's current group cooldown. "
            "If an account is FloodWait/PeerFlood paused, replies pause too.",
            parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU,
        )
        return
    if text == "⏱ Reply Cooldown":
        state[uid] = "await_reply_cooldown"
        await update.message.reply_text("Send cooldown in minutes (1–1440).", reply_markup=AUTO_REPLY_MENU)
        return

    # Post-management flow. Back was already processed at the top of this function.
    if current in {"await_preview_post_id", "await_toggle_post_id", "await_delete_post_id"}:
        if text.casefold() in {"cancel", "/cancel", "❌ cancel"}:
            state.pop(uid, None)
            await update.message.reply_text("Cancelled.", reply_markup=POST_MENU)
            return
        try:
            post_id = int(text.strip().lstrip("#"))
        except ValueError:
            await update.message.reply_text("Send the numeric post ID from 📋 List Posts, or press Back.", reply_markup=POST_MENU)
            return
        post = await db.get_post_by_id(post_id)
        if not post:
            await update.message.reply_text("❌ Post not found. Try an ID from the list.", reply_markup=POST_MENU)
            return
        if current == "await_preview_post_id":
            state.pop(uid, None)
            await preview_saved_post(update.message, post)
            return
        if stats.running or context.application.bot_data.get("dm_broadcast_running"):
            await update.message.reply_text("⏹ Stop the ongoing broadcast before modifying posts.", reply_markup=POST_MENU)
            state.pop(uid, None)
            return
        if current == "await_toggle_post_id":
            if post["enabled"] and len(await db.list_posts(enabled_only=True)) <= 1:
                await update.message.reply_text("❌ Keep at least one post enabled. Add another post before disabling this one.", reply_markup=POST_MENU)
                state.pop(uid, None)
                return
            enabled = await db.toggle_post(post_id)
            state.pop(uid, None)
            await update.message.reply_text(f"✅ Post #{post_id} is now {'ON' if enabled else 'OFF'}.", reply_markup=POST_MENU)
            return
        if current == "await_delete_post_id":
            deleted = await db.delete_post(post_id)
            state.pop(uid, None)
            if deleted and deleted.get("media_path"):
                try:
                    Path(deleted["media_path"]).unlink(missing_ok=True)
                except OSError:
                    pass
            await update.message.reply_text(f"🗑 Post #{post_id} deleted.", reply_markup=POST_MENU)
            return

    if text in {"📚 Manage Posts", "👁 View Post"}:
        state.pop(uid, None)
        await update.message.reply_text(await posts_summary(), parse_mode=ParseMode.HTML, reply_markup=POST_MENU)
        return
    if text == "📋 List Posts":
        await update.message.reply_text(await posts_summary(), parse_mode=ParseMode.HTML, reply_markup=POST_MENU)
        return
    if text in {"👁 Preview Post", "🔘 Toggle Post", "🗑 Delete Post"}:
        state[uid] = {"👁 Preview Post":"await_preview_post_id", "🔘 Toggle Post":"await_toggle_post_id", "🗑 Delete Post":"await_delete_post_id"}[text]
        await update.message.reply_text("Send the post ID from 📋 List Posts. Press Back to cancel.", reply_markup=POST_MENU)
        return
    if text == "🔀 Sending Mode":
        current_mode = await db.get_setting("post_mode", "rotate")
        await update.message.reply_text(f"Current mode: {current_mode.title()}\n\nChoose Rotate or Random.", reply_markup=POST_MODE_MENU)
        return
    if text in {"🔄 Rotate", "🎲 Random"}:
        new_mode = "rotate" if text == "🔄 Rotate" else "random"
        await db.set_setting("post_mode", new_mode)
        await update.message.reply_text(f"✅ Sending mode: {new_mode.title()}. Changes apply on the next round.", reply_markup=POST_MENU)
        return

    # Other conversation states follow.

    if current == "choose_add_method":
        if text == "🔑 Session String":
            state[uid] = "await_session"
            await update.message.reply_text(
                "Send the Telethon <b>StringSession</b>.\n\nIt will be validated, encrypted, and stored in MongoDB.",
                parse_mode=ParseMode.HTML,
                reply_markup=ADD_SESSION_MENU,
            )
        elif text == "📱 Login with Phone":
            state[uid] = "await_phone"
            await update.message.reply_text(
                "📱 Send the phone number with country code.\nExample: <code>+919876543210</code>\n\n⚠️ The OTP will be entered in this private bot chat. OTP and 2FA password are never stored in the database.",
                parse_mode=ParseMode.HTML,
                reply_markup=ADD_SESSION_MENU,
            )
        else:
            await update.message.reply_text("Choose one of the two options below.", reply_markup=ADD_SESSION_MENU)
        return

    if current == "await_session":
        state.pop(uid, None)
        session_string = text.strip()
        await safe_delete_message(update.message)
        msg = await context.bot.send_message(uid, "⏳ Validating session...", reply_markup=MENU)
        client = None
        try:
            client = await make_client(session_string)
            me = await client.get_me()
            label = f"{me.first_name or 'Account'} ({me.id})"
            enc = fernet.encrypt(session_string.encode()).decode()
            await session_store.add_session(label, enc, me.id, me.username, me.first_name)
            await msg.edit_text(
                f"✅ Session added.\n\n👤 {esc(label)}\n🎯 Target cache will be built on first Start Promo or manual Rescan Targets.",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            await msg.edit_text(f"❌ Invalid/unusable session:\n<code>{esc(type(e).__name__ + ': ' + str(e))}</code>", parse_mode=ParseMode.HTML)
        finally:
            if client:
                await client.disconnect()
        return

    if current == "await_phone":
        phone = text.strip().replace(" ", "")
        await safe_delete_message(update.message)
        if not phone.startswith("+") or not phone[1:].isdigit():
            await context.bot.send_message(uid, "❌ Invalid number. Send it with country code, e.g. <code>+919876543210</code>.", parse_mode=ParseMode.HTML, reply_markup=ADD_SESSION_MENU)
            return
        await cleanup_login_flow(uid)
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
            login_flows[uid] = {"client": client, "phone": phone, "phone_code_hash": sent.phone_code_hash}
            state[uid] = "await_otp"
            await context.bot.send_message(
                uid,
                "✅ Login code sent by Telegram.\n\nSend the OTP here. You can type it normally, e.g. <code>12345</code>.",
                parse_mode=ParseMode.HTML,
                reply_markup=ADD_SESSION_MENU,
            )
        except Exception as e:
            await client.disconnect()
            state.pop(uid, None)
            await context.bot.send_message(uid, f"❌ Could not send OTP:\n<code>{esc(type(e).__name__ + ': ' + str(e))}</code>", parse_mode=ParseMode.HTML, reply_markup=MENU)
        return

    if current == "await_otp":
        code = ''.join(ch for ch in text if ch.isdigit())
        await safe_delete_message(update.message)
        flow = login_flows.get(uid)
        if not flow:
            state.pop(uid, None)
            await context.bot.send_message(uid, "❌ Login session expired. Start Add Session again.", reply_markup=MENU)
            return
        if not code:
            await context.bot.send_message(uid, "❌ Send the numeric OTP.", reply_markup=ADD_SESSION_MENU)
            return
        client = flow["client"]
        try:
            await client.sign_in(phone=flow["phone"], code=code, phone_code_hash=flow["phone_code_hash"])
            label, group_count, user_count = await save_logged_in_client(client)
            state.pop(uid, None)
            await cleanup_login_flow(uid)
            await context.bot.send_message(uid, f"✅ Login successful and session saved.\n\n👤 {esc(label)}\n🎯 Target cache will be built on first Start Promo or manual Rescan Targets.", parse_mode=ParseMode.HTML, reply_markup=MENU)
        except errors.SessionPasswordNeededError:
            state[uid] = "await_2fa"
            await context.bot.send_message(uid, "🔐 Two-step verification is enabled. Send your Telegram 2FA password.", reply_markup=ADD_SESSION_MENU)
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError) as e:
            if isinstance(e, errors.PhoneCodeExpiredError):
                state.pop(uid, None)
                await cleanup_login_flow(uid)
                await context.bot.send_message(uid, "❌ OTP expired. Start Add Session again to request a new code.", reply_markup=MENU)
            else:
                await context.bot.send_message(uid, "❌ Wrong OTP. Try again or press ⬅️ Back.", reply_markup=ADD_SESSION_MENU)
        except Exception as e:
            state.pop(uid, None)
            await cleanup_login_flow(uid)
            await context.bot.send_message(uid, f"❌ Login failed:\n<code>{esc(type(e).__name__ + ': ' + str(e))}</code>", parse_mode=ParseMode.HTML, reply_markup=MENU)
        return

    if current == "await_2fa":
        password = text
        await safe_delete_message(update.message)
        flow = login_flows.get(uid)
        if not flow:
            state.pop(uid, None)
            await context.bot.send_message(uid, "❌ Login session expired. Start Add Session again.", reply_markup=MENU)
            return
        client = flow["client"]
        try:
            await client.sign_in(password=password)
            label, group_count, user_count = await save_logged_in_client(client)
            state.pop(uid, None)
            await cleanup_login_flow(uid)
            await context.bot.send_message(uid, f"✅ Login successful and session saved.\n\n👤 {esc(label)}\n🎯 Target cache will be built on first Start Promo or manual Rescan Targets.", parse_mode=ParseMode.HTML, reply_markup=MENU)
        except errors.PasswordHashInvalidError:
            await context.bot.send_message(uid, "❌ Wrong 2FA password. Try again or press ⬅️ Back.", reply_markup=ADD_SESSION_MENU)
        except Exception as e:
            state.pop(uid, None)
            await cleanup_login_flow(uid)
            await context.bot.send_message(uid, f"❌ Login failed:\n<code>{esc(type(e).__name__ + ': ' + str(e))}</code>", parse_mode=ParseMode.HTML, reply_markup=MENU)
        return

    if current == "await_group_delay":
        try:
            seconds = int(text.strip())
            if not 15 <= seconds <= 600:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Send a whole number of seconds (15–600), or press Back.", reply_markup=MENU)
            return
        await db.set_setting("group_delay_seconds", str(seconds))
        stats.group_delay_seconds = seconds
        state.pop(uid, None)
        await update.message.reply_text(f"✅ Minimum group delay set to {seconds} seconds. Applies next round.", reply_markup=MENU)
        return

    if current == "await_interval":
        state.pop(uid, None)
        try:
            minutes = int(text.strip())
            if minutes < 5:
                raise ValueError
            await db.set_setting("interval_minutes", str(minutes))
            await update.message.reply_text(f"✅ Interval set to {minutes} minutes.", reply_markup=MENU)
        except ValueError:
            await update.message.reply_text("❌ Send a whole number of minutes (minimum 5).", reply_markup=MENU)
        return

    if current == "await_remove":
        state.pop(uid, None)
        try:
            session_ref = text.strip()
            deleted = await session_store.delete_session(session_ref)
            target_cache.clear()
            if not deleted:
                raise ValueError("Session not found")
            await update.message.reply_text(f"✅ Session {session_ref} removed.", reply_markup=MENU)
        except Exception:
            await update.message.reply_text("❌ Send the Mongo session ID shown in Sessions, or its numeric Telegram user ID.", reply_markup=MENU)
        return

    if text == "➕ Add Session":
        state[uid] = "choose_add_method"
        await update.message.reply_text(
            "How do you want to add the Telegram account?",
            reply_markup=ADD_SESSION_MENU,
        )

    elif text == "👤 Sessions":
        rows = await session_store.get_sessions()
        if not rows:
            await update.message.reply_text("No sessions added.", reply_markup=MENU)
            return
        lines = ["👤 <b>SESSIONS</b>\n"]
        for r in rows:
            gcount = stats.cached_group_count.get(r["id"], "?")
            ucount = stats.cached_user_count.get(r["id"], "?")
            uname = f"@{r['username']}" if r.get("username") else "no username"
            lines.append(f"#{r['id']} • {esc(r['first_name'] or r['label'])} • {esc(uname)} • groups: {gcount} • users: {ucount}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=MENU)

    elif text == "🗑 Remove Session":
        rows = await session_store.get_sessions()
        if not rows:
            await update.message.reply_text("No sessions to remove.", reply_markup=MENU)
            return
        state[uid] = "await_remove"
        lines = ["Send the session ID to remove (Mongo ID or Telegram user ID):"] + [f"{r['id']} — {r['label']} (TG: {r.get('tg_user_id')})" for r in rows]
        await update.message.reply_text("\n".join(lines), reply_markup=MENU)

    elif text == "📝 Save Post":
        state[uid] = "await_post"
        await update.message.reply_text(
            "Send another promotion post now. Existing posts will not be replaced.\n\nSupported: text, photo, video, document, animation. Use Back to cancel.",
            reply_markup=POST_MENU,
        )

    elif text == "⏳ Group Delay":
        current_delay = await get_group_delay()
        state[uid] = "await_group_delay"
        await update.message.reply_text(
            f"Current minimum delay: {current_delay} seconds between group sends on each ID.\n\n"
            "Send a new value in seconds (15–600). Lower delay does not guarantee Telegram acceptance.",
            reply_markup=MENU,
        )

    elif text == "⏱ Interval":
        current_interval = await db.get_setting("interval_minutes", str(DEFAULT_INTERVAL_MINUTES))
        state[uid] = "await_interval"
        await update.message.reply_text(
            f"Current interval: {current_interval} min.\n\nSend new interval in minutes (minimum 5).",
            reply_markup=MENU,
        )

    elif text == "🔄 Rescan Targets":
        rows = await session_store.get_sessions(enabled_only=True)
        if not rows:
            await update.message.reply_text("No sessions added.", reply_markup=MENU)
            return
        msg = await update.message.reply_text("⏳ Refreshing recurring group target cache...")
        out = []
        total_groups = 0
        total_users = 0
        for r in rows:
            client = None
            try:
                client = await get_account_client(r)
                groups, users, _ = await get_cached_targets(r, client, force=True)
                total_groups += len(groups)
                total_users += len(users)
                out.append(f"✅ {r['label']}: {len(groups)} groups")
            except Exception as e:
                out.append(f"❌ {r['label']}: {type(e).__name__}")
            finally:
                # Managed client remains connected for the DM listener.
                pass
        scan_text = (
            "🔄 <b>SCAN COMPLETE</b>\n\n"
            + "\n".join(map(esc, out))
            + f"\n\n👥 Groups: <b>{total_groups}</b>"
        )
        try:
            await msg.edit_text(scan_text, parse_mode=ParseMode.HTML)
        except BadRequest as e:
            log.warning("Scan status message could not be edited: %s", type(e).__name__)
            await update.message.reply_text(scan_text, parse_mode=ParseMode.HTML, reply_markup=MENU)

    elif text == "▶️ Start Promo":
        if (promo_task and not promo_task.done()) or context.application.bot_data.get("dm_broadcast_running"):
            await update.message.reply_text("🟢 A broadcast is already running.", reply_markup=MENU)
            return
        if not await db.get_post():
            await update.message.reply_text("❌ Save a promotion post first.", reply_markup=MENU)
            return
        if not await session_store.get_sessions(enabled_only=True):
            await update.message.reply_text("❌ Add at least one session first.", reply_markup=MENU)
            return
        stats.running = True
        promo_task = asyncio.create_task(scheduler(context.application), name="promo_scheduler")
        await update.message.reply_text("▶️ Promotion started. Use 📊 Live Stats to check progress.", reply_markup=MENU)

    elif text == "⏹ Stop Promo":
        stats.running = False
        if promo_task and not promo_task.done():
            promo_task.cancel()
            try:
                await promo_task
            except asyncio.CancelledError:
                pass
        promo_task = None
        await update.message.reply_text("⏹ Promotion stopped.", reply_markup=MENU)

    elif text == "📨 DM Broadcast":
        if context.application.bot_data.get("dm_broadcast_running"):
            await update.message.reply_text("🟢 Manual DM broadcast is already running.", reply_markup=MENU)
            return
        if stats.running:
            await update.message.reply_text("⏹ Stop the recurring group promotion before running a one-time DM broadcast.", reply_markup=MENU)
            return
        if not await db.get_post():
            await update.message.reply_text("❌ Save a promotion post first.", reply_markup=MENU)
            return
        if not await session_store.get_sessions(enabled_only=True):
            await update.message.reply_text("❌ Add at least one session first.", reply_markup=MENU)
            return
        await update.message.reply_text("📨 One-time broadcast to existing private DM dialogs started. Use 📊 Live Stats for progress.", reply_markup=MENU)
        context.application.bot_data["dm_broadcast_running"] = True
        context.application.bot_data["dm_broadcast_task"] = asyncio.create_task(
            dm_broadcast_runner(context.application), name="one_time_dm_broadcast")

    elif text == "📊 Live Stats":
        stats.active_posts = len(await db.list_posts(enabled_only=True))
        stats.post_mode = await db.get_setting("post_mode", "rotate")
        await update.message.reply_text(stats_text(), parse_mode=ParseMode.HTML, reply_markup=MENU)

    else:
        await update.message.reply_text("Use the menu buttons below.", reply_markup=MENU)


async def save_post_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update) or not update.message:
        return
    uid = update.effective_user.id
    if state.get(uid) != "await_post":
        return
    m = update.message
    if m.text in {"⬅️ Back", "❌ Cancel"}:
        state.pop(uid, None)
        return

    html_text = m.text_html or m.caption_html or ""
    media_path = None
    media_type = None
    kind = "text"

    try:
        if m.photo:
            kind = media_type = "photo"
            tgfile = await m.photo[-1].get_file()
            media_path = str(MEDIA_DIR / f"promo_{int(time.time()*1000)}_{m.message_id}.jpg")
            await tgfile.download_to_drive(media_path)
        elif m.video:
            kind = media_type = "video"
            tgfile = await m.video.get_file()
            suffix = Path(m.video.file_name or "video.mp4").suffix or ".mp4"
            media_path = str(MEDIA_DIR / f"promo_{int(time.time()*1000)}_{m.message_id}{suffix}")
            await tgfile.download_to_drive(media_path)
        elif m.document:
            kind = media_type = "document"
            tgfile = await m.document.get_file()
            suffix = Path(m.document.file_name or "file.bin").suffix or ".bin"
            media_path = str(MEDIA_DIR / f"promo_{int(time.time()*1000)}_{m.message_id}{suffix}")
            await tgfile.download_to_drive(media_path)
        elif m.animation:
            kind = media_type = "animation"
            tgfile = await m.animation.get_file()
            media_path = str(MEDIA_DIR / f"promo_{int(time.time()*1000)}_{m.message_id}.mp4")
            await tgfile.download_to_drive(media_path)
        elif not m.text:
            await m.reply_text("❌ Unsupported post type. Send text/photo/video/document/animation.", reply_markup=MENU)
            return

        post_id = await db.add_post(kind, html_text, media_path, media_type)
        state.pop(uid, None)
        context.user_data["post_saved_message_id"] = m.message_id
        await m.reply_text(f"✅ Post #{post_id} added. All existing posts are retained.\n\nSelect 🔀 Sending Mode to choose Rotate or Random.", reply_markup=POST_MENU)
    except Exception as e:
        await m.reply_text(f"❌ Could not save post:\n{type(e).__name__}: {e}", reply_markup=MENU)


async def post_init(app: Application):
    await db.init()
    await session_store.init()
    if await db.get_setting("interval_minutes") is None:
        await db.set_setting("interval_minutes", str(DEFAULT_INTERVAL_MINUTES))
    if await db.get_setting("group_delay_seconds") is None:
        await db.set_setting("group_delay_seconds", str(GROUP_DELAY_SECONDS))
    stats.group_delay_seconds = await get_group_delay()
    app.bot_data["dm_listener_task"] = asyncio.create_task(account_auto_reply_service(app), name="account_dm_auto_reply")


async def post_shutdown(app: Application):
    global promo_task
    stats.running = False
    outgoing = [promo_task, app.bot_data.pop("dm_broadcast_task", None)]
    for job in outgoing:
        if job and not job.done():
            job.cancel()
    await asyncio.gather(*(job for job in outgoing if job), return_exceptions=True)
    promo_task = None
    task = app.bot_data.pop("dm_listener_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    # Defensive cleanup for partial startup or a previously failed listener manager.
    await asyncio.gather(*(close_account_client(sid) for sid in list(_account_dm_clients)),
                         return_exceptions=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    # Save-post handler must run before the generic text/menu handler.
    app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION), save_post_message), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_post_message), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu), group=1)
    # A stranger messaging the control bot receives a cooldown-limited response.
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, bot_dm_auto_reply), group=2)
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
