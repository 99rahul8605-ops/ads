import asyncio
import html
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telethon import TelegramClient, errors, types
from telethon.sessions import StringSession

from database import Database, MongoSessionStore

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("promo-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
DB_PATH = os.getenv("DB_PATH", "promo.db")
MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB = os.getenv("MONGO_DB", "promo_bot")
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "media"))
DEFAULT_INTERVAL_MINUTES = int(os.getenv("DEFAULT_INTERVAL_MINUTES", "60"))
GROUP_DELAY_SECONDS = max(3, int(os.getenv("GROUP_DELAY_SECONDS", "8")))
FERNET_KEY = os.environ["FERNET_KEY"].encode()
fernet = Fernet(FERNET_KEY)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

db = Database(DB_PATH)
session_store = MongoSessionStore(MONGO_URI, MONGO_DB)

MENU = ReplyKeyboardMarkup(
    [
        ["➕ Add Session", "👤 Sessions"],
        ["📝 Save Post", "👁 View Post"],
        ["⏱ Interval", "🔄 Rescan Groups"],
        ["▶️ Start Promo", "⏹ Stop Promo"],
        ["📊 Live Stats", "🗑 Remove Session"],
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

@dataclass
class RuntimeStats:
    running: bool = False
    round_no: int = 0
    started_at: float | None = None
    current_session: str = "-"
    current_group: str = "-"
    total_groups: int = 0
    attempted: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    flood_waits: int = 0
    last_error: str = "-"
    next_round_at: float | None = None
    cached_group_count: dict = field(default_factory=dict)

stats = RuntimeStats()
promo_task: asyncio.Task | None = None
state: dict[int, str] = {}


def admin_only(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


def esc(s):
    return html.escape(str(s or ""))


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
    pct = (stats.attempted / stats.total_groups * 100) if stats.total_groups else 0
    elapsed = fmt_duration(time.time() - stats.started_at) if stats.started_at else "-"
    next_run = "-"
    if stats.next_round_at:
        next_run = fmt_duration(stats.next_round_at - time.time())
    return (
        "📊 <b>LIVE PROMOTION STATS</b>\n\n"
        f"Status: {'🟢 Running' if stats.running else '🔴 Stopped'}\n"
        f"Round: <b>{stats.round_no}</b>\n"
        f"Progress: <b>{stats.attempted}/{stats.total_groups}</b> ({pct:.1f}%)\n"
        f"✅ Sent: <b>{stats.sent}</b>\n"
        f"❌ Failed: <b>{stats.failed}</b>\n"
        f"⏭ Skipped: <b>{stats.skipped}</b>\n"
        f"⏳ Flood waits: <b>{stats.flood_waits}</b>\n\n"
        f"👤 Session: <code>{esc(stats.current_session)}</code>\n"
        f"👥 Current: {esc(stats.current_group)}\n"
        f"⏱ Elapsed: {elapsed}\n"
        f"🔁 Next round in: {next_run}\n"
        f"⚠️ Last error: <code>{esc(stats.last_error)[:500]}</code>"
    )


async def make_client(session_string: str) -> TelegramClient:
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Session is not authorized")
    return client


def decrypt_session(value: str) -> str:
    try:
        return fernet.decrypt(value.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("FERNET_KEY does not match stored sessions") from e


async def scan_groups(client: TelegramClient):
    groups = []
    async for dialog in client.iter_dialogs(limit=None):
        entity = dialog.entity
        # Basic groups and megagroups only. Broadcast channels and private chats are excluded.
        if isinstance(entity, types.Chat):
            groups.append((entity, dialog.name or str(dialog.id)))
        elif isinstance(entity, types.Channel) and getattr(entity, "megagroup", False):
            groups.append((entity, dialog.name or str(dialog.id)))
    return groups


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


async def do_round(app: Application):
    sessions = await session_store.get_sessions(enabled_only=True)
    post = await db.get_post()
    if not sessions:
        raise RuntimeError("No active sessions")
    if not post:
        raise RuntimeError("No promo post saved")

    stats.round_no += 1
    stats.started_at = time.time()
    stats.attempted = stats.sent = stats.failed = stats.skipped = stats.flood_waits = 0
    stats.total_groups = 0
    stats.last_error = "-"

    prepared = []
    for row in sessions:
        label = row["label"]
        client = None
        try:
            session_string = decrypt_session(row["session_enc"])
            client = await make_client(session_string)
            groups = await scan_groups(client)
            stats.cached_group_count[row["id"]] = len(groups)
            prepared.append((row, client, groups))
            stats.total_groups += len(groups)
        except Exception as e:
            stats.failed += 1
            stats.last_error = f"{label}: {type(e).__name__}: {e}"
            if client:
                await client.disconnect()

    progress_msg = None
    try:
        progress_msg = await app.bot.send_message(
            ADMIN_ID,
            stats_text(),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    last_edit = 0.0
    for row, client, groups in prepared:
        try:
            stats.current_session = row["label"]
            for entity, name in groups:
                if not stats.running:
                    return
                stats.current_group = name
                try:
                    await send_saved_post(client, entity, post)
                    stats.sent += 1
                except errors.FloodWaitError as e:
                    stats.flood_waits += 1
                    stats.last_error = f"FloodWait {e.seconds}s at {name}"
                    # Respect Telegram's required wait; no bypass/retry tricks.
                    await asyncio.sleep(e.seconds)
                    if not stats.running:
                        return
                    try:
                        await send_saved_post(client, entity, post)
                        stats.sent += 1
                    except Exception as retry_e:
                        stats.failed += 1
                        stats.last_error = f"{type(retry_e).__name__}: {retry_e}"
                except (errors.ChatWriteForbiddenError, errors.UserBannedInChannelError, errors.ChatAdminRequiredError) as e:
                    stats.skipped += 1
                    stats.last_error = f"{type(e).__name__}: {name}"
                except Exception as e:
                    stats.failed += 1
                    stats.last_error = f"{type(e).__name__}: {e}"
                finally:
                    stats.attempted += 1

                now = time.time()
                if progress_msg and now - last_edit >= 3:
                    try:
                        await progress_msg.edit_text(stats_text(), parse_mode=ParseMode.HTML)
                    except Exception:
                        pass
                    last_edit = now
                await asyncio.sleep(GROUP_DELAY_SECONDS)
        finally:
            await client.disconnect()

    if progress_msg:
        try:
            await progress_msg.edit_text(stats_text(), parse_mode=ParseMode.HTML)
        except Exception:
            pass


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return
    await update.message.reply_text(
        "📢 <b>Promotion Bot</b>\n\nUse the menu below. Joined groups/supergroups are detected automatically; private chats and broadcast channels are not included.",
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
    groups = await scan_groups(client)
    return label, len(groups)


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

    # Conversation states first
    current = state.get(uid)

    if text == "⬅️ Back":
        state.pop(uid, None)
        await cleanup_login_flow(uid)
        await update.message.reply_text("Main menu.", reply_markup=MENU)
        return

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
            groups = await scan_groups(client)
            await msg.edit_text(
                f"✅ Session added.\n\n👤 {esc(label)}\n👥 Joined groups detected: <b>{len(groups)}</b>",
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
            label, count = await save_logged_in_client(client)
            state.pop(uid, None)
            await cleanup_login_flow(uid)
            await context.bot.send_message(uid, f"✅ Login successful and session saved.\n\n👤 {esc(label)}\n👥 Joined groups detected: <b>{count}</b>", parse_mode=ParseMode.HTML, reply_markup=MENU)
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
            label, count = await save_logged_in_client(client)
            state.pop(uid, None)
            await cleanup_login_flow(uid)
            await context.bot.send_message(uid, f"✅ Login successful and session saved.\n\n👤 {esc(label)}\n👥 Joined groups detected: <b>{count}</b>", parse_mode=ParseMode.HTML, reply_markup=MENU)
        except errors.PasswordHashInvalidError:
            await context.bot.send_message(uid, "❌ Wrong 2FA password. Try again or press ⬅️ Back.", reply_markup=ADD_SESSION_MENU)
        except Exception as e:
            state.pop(uid, None)
            await cleanup_login_flow(uid)
            await context.bot.send_message(uid, f"❌ Login failed:\n<code>{esc(type(e).__name__ + ': ' + str(e))}</code>", parse_mode=ParseMode.HTML, reply_markup=MENU)
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
            count = stats.cached_group_count.get(r["id"], "?")
            uname = f"@{r['username']}" if r.get("username") else "no username"
            lines.append(f"#{r['id']} • {esc(r['first_name'] or r['label'])} • {esc(uname)} • groups: {count}")
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
            "Send the promotion post now.\n\nSupported: text, photo, video, document, animation. Formatting is preserved as HTML where Telegram exposes it.",
            reply_markup=MENU,
        )

    elif text == "👁 View Post":
        post = await db.get_post()
        if not post:
            await update.message.reply_text("No post saved yet.", reply_markup=MENU)
            return
        info = f"📝 Type: {post['kind']}\n📎 Media: {'Yes' if post.get('media_path') else 'No'}\n\n"
        await update.message.reply_text(info + (post.get("html_text") or "(no caption)"), parse_mode=ParseMode.HTML, reply_markup=MENU)

    elif text == "⏱ Interval":
        current_interval = await db.get_setting("interval_minutes", str(DEFAULT_INTERVAL_MINUTES))
        state[uid] = "await_interval"
        await update.message.reply_text(
            f"Current interval: {current_interval} min.\n\nSend new interval in minutes (minimum 5).",
            reply_markup=MENU,
        )

    elif text == "🔄 Rescan Groups":
        rows = await session_store.get_sessions(enabled_only=True)
        if not rows:
            await update.message.reply_text("No sessions added.", reply_markup=MENU)
            return
        msg = await update.message.reply_text("⏳ Scanning joined groups...", reply_markup=MENU)
        out = []
        total = 0
        for r in rows:
            client = None
            try:
                client = await make_client(decrypt_session(r["session_enc"]))
                groups = await scan_groups(client)
                stats.cached_group_count[r["id"]] = len(groups)
                total += len(groups)
                out.append(f"✅ {r['label']}: {len(groups)}")
            except Exception as e:
                out.append(f"❌ {r['label']}: {type(e).__name__}")
            finally:
                if client:
                    await client.disconnect()
        await msg.edit_text("🔄 <b>SCAN COMPLETE</b>\n\n" + "\n".join(map(esc, out)) + f"\n\nTotal: <b>{total}</b>", parse_mode=ParseMode.HTML)

    elif text == "▶️ Start Promo":
        if promo_task and not promo_task.done():
            await update.message.reply_text("🟢 Promotion is already running.", reply_markup=MENU)
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

    elif text == "📊 Live Stats":
        await update.message.reply_text(stats_text(), parse_mode=ParseMode.HTML, reply_markup=MENU)

    else:
        await update.message.reply_text("Use the menu buttons below.", reply_markup=MENU)


async def save_post_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update) or not update.message:
        return
    uid = update.effective_user.id
    if state.get(uid) != "await_post":
        return
    state.pop(uid, None)
    m = update.message

    html_text = m.text_html or m.caption_html or ""
    media_path = None
    media_type = None
    kind = "text"

    try:
        if m.photo:
            kind = media_type = "photo"
            tgfile = await m.photo[-1].get_file()
            media_path = str(MEDIA_DIR / f"promo_{m.message_id}.jpg")
            await tgfile.download_to_drive(media_path)
        elif m.video:
            kind = media_type = "video"
            tgfile = await m.video.get_file()
            suffix = Path(m.video.file_name or "video.mp4").suffix or ".mp4"
            media_path = str(MEDIA_DIR / f"promo_{m.message_id}{suffix}")
            await tgfile.download_to_drive(media_path)
        elif m.document:
            kind = media_type = "document"
            tgfile = await m.document.get_file()
            suffix = Path(m.document.file_name or "file.bin").suffix or ".bin"
            media_path = str(MEDIA_DIR / f"promo_{m.message_id}{suffix}")
            await tgfile.download_to_drive(media_path)
        elif m.animation:
            kind = media_type = "animation"
            tgfile = await m.animation.get_file()
            media_path = str(MEDIA_DIR / f"promo_{m.message_id}.mp4")
            await tgfile.download_to_drive(media_path)
        elif not m.text:
            await m.reply_text("❌ Unsupported post type. Send text/photo/video/document/animation.", reply_markup=MENU)
            return

        old = await db.get_post()
        await db.save_post(kind, html_text, media_path, media_type)
        if old and old.get("media_path") and old["media_path"] != media_path:
            try:
                Path(old["media_path"]).unlink(missing_ok=True)
            except Exception:
                pass
        context.user_data["post_saved_message_id"] = m.message_id
        await m.reply_text("✅ Promotion post saved.", reply_markup=MENU)
    except Exception as e:
        await m.reply_text(f"❌ Could not save post:\n{type(e).__name__}: {e}", reply_markup=MENU)


async def post_init(app: Application):
    await db.init()
    await session_store.init()
    if await db.get_setting("interval_minutes") is None:
        await db.set_setting("interval_minutes", str(DEFAULT_INTERVAL_MINUTES))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    # Save-post handler must run before the generic text/menu handler.
    app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION), save_post_message), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_post_message), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu), group=1)
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
