# Telegram Promotion Bot v1 — MongoDB Sessions

Private admin bot controlling one or more Telethon user sessions through a persistent Reply Keyboard menu.

## Storage

- **MongoDB:** encrypted Telethon StringSessions + session account metadata
- **SQLite:** lightweight bot settings + multiple saved promo posts
- **Local media folder:** saved promo media files

StringSessions are encrypted with Fernet **before** MongoDB receives them. Keep `FERNET_KEY` private; MongoDB alone cannot decrypt a stored session without that key.

## Features

- Reply-keyboard menu (not inline callback menu)
- Multiple Telethon accounts
- Add account by existing `StringSession` **or** direct phone + OTP login
- Telegram 2FA password flow supported when enabled
- Sessions encrypted with Fernet and stored in MongoDB
- Automatic joined group/supergroup scanning
- Save multiple promo posts: text/photo/video/document/animation
- Configurable repeat interval
- Group-to-group delay
- Start/stop scheduler
- Live stats and auto-updating progress during each round
- Telegram FloodWait respected
- Read-only/restricted groups skipped rather than crashing the round

## Setup — Ubuntu

```bash
sudo apt update
sudo apt install -y python3-venv
cd promo_bot_v1
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python generate_key.py
```

Fill `.env`:

```env
BOT_TOKEN=...
ADMIN_ID=...
API_ID=...
API_HASH=...
FERNET_KEY=...
MONGO_URI=mongodb+srv://USERNAME:PASSWORD@HOST/?retryWrites=true&w=majority
MONGO_DB=promo_bot
```

`DB_PATH=promo.db` is still used only for settings and the saved promo post; sessions no longer go into SQLite.

Run:

```bash
python bot.py
```

Then send `/start` to the bot from the configured admin account.

## Session collection

MongoDB database: value of `MONGO_DB` (default `promo_bot`)

Collection: `sessions`

Example document shape:

```text
{
  _id: ObjectId(...),
  tg_user_id: 123456789,
  label: "Rahul (123456789)",
  username: "...",
  first_name: "...",
  session_enc: "gAAAAA...",   # encrypted, not plain StringSession
  enabled: true,
  created_at: ...,
  updated_at: ...
}
```

Re-adding the same Telegram account updates its stored encrypted session instead of creating duplicate records.

## Menu

- `➕ Add Session` — choose either `🔑 Session String` or `📱 Login with Phone`
  - Session String: paste an existing Telethon StringSession
  - Login with Phone: phone number → Telegram OTP → 2FA password if enabled → generated StringSession is encrypted and saved automatically
- `👤 Sessions` — list MongoDB-backed sessions
- `📝 Save Post` — add a new promotion post (never overwrites existing posts)
- `📚 Manage Posts` — list/preview/enable/disable/delete posts; select Rotate or Random
- `⏱ Interval` — repeat interval in minutes
- `🔄 Rescan Groups` — count current joined groups
- `▶️ Start Promo` / `⏹ Stop Promo`
- `📊 Live Stats`
- `🗑 Remove Session` — accepts shown Mongo ObjectId or Telegram numeric user ID

## Important

If `FERNET_KEY` changes, previously stored session strings cannot be decrypted. Back up this key securely.

For phone login, OTP and 2FA password are used only for the active login flow and are not stored in MongoDB. The bot also attempts to delete those credential messages from the private admin chat after reading them.


## Private user targets

Private users are not taken from every old dialog automatically. A user becomes a recurring private promotion target only when they send `/subscribe` to the connected Telegram account. Sending `/unsubscribe` removes them from the target set. The scanner checks recent inbound history to determine the latest subscription state.

Live stats show group and private-user totals separately.


## Flood-wait / target-cache behavior

- Joined dialogs are not fetched on every promotion round.
- First Start Promo builds an in-memory target cache; following rounds reuse it.
- Manual `Rescan Targets` forces a fresh scan.
- Default automatic cache refresh is 6 hours (`TARGET_CACHE_TTL_SECONDS=21600`).
- Sessions broadcast in parallel; targets inside one session are sent sequentially with `GROUP_DELAY_SECONDS`.
- Telegram FloodWait is never bypassed; only the affected session worker sleeps for the required time.

## Incoming DM auto reply (Ads Promotion Bot)

- When a person privately messages the **control bot**, it replies with `DM_AUTO_REPLY_TEXT`; admin actions and menu commands are unaffected.
- When a person privately messages any **connected real account**, a background Telethon listener replies with `ACCOUNT_DM_AUTO_REPLY_TEXT`. Set `DM_AUTO_REPLY_USE_SAVED_POST=true` to reply with the saved promo post instead. Only inbound private messages trigger this feature; it does not proactively start conversations.
- Defaults: 5-minute cooldown per sender and session, configurable via `DM_REPLY_COOLDOWN_SECONDS`; both services may be turned off individually. Bot accounts and group messages are ignored. Added sessions activate within one minute; removed sessions stop within one minute.
- Session listener does not rescan dialogs. The existing scheduled group broadcast and manual DM broadcast are unchanged.
- Do not run multiple bot instances simultaneously against the same Telegram bot token, and safeguard `.env` and stored sessions.


## Admin menu: Auto Reply

Send `/start` then press **💬 Auto Reply**. Choose **✏️ Account Reply** for connected real IDs or **✏️ Control Bot Reply** for the control bot, then send the message. Settings persist in SQLite (`DB_PATH`) and update on new DMs without restarting. **👁 Reply Preview**, **🔄 Use Saved Post**, ON/OFF toggles, and cooldown are also available. Existing `.env` values are only defaults. Keep `promo.db` when migrating servers, or migrate this settings file alongside MongoDB sessions.


## Multi-post rotation and random sending

Open `/start` → **📝 Save Post** to add each text/photo/video/document/animation post.
You can add multiple posts; previous posts are not overwritten.

Open **📚 Manage Posts** to access:

- **📋 List Posts:** see all post IDs, active/paused states, and short previews.
- **👁 Preview Post:** send the ID to preview the full saved text or media.
- **🔘 Toggle Post:** send the ID to enable/disable a post without deleting it.
- **🗑 Delete Post:** send the ID to permanently remove it and its locally stored media (only when broadcasts are stopped).
- **🔀 Sending Mode:** choose **🔄 Rotate** or **🎲 Random**. The mode is saved in `promo.db`.

**Rotate (default):** consecutive targets get successive enabled posts, and the
starting post advances on each scheduled round, so a given group gets a
*different post on the next scheduled round* when two or more are enabled.

**Random:** each target gets a random enabled post; the recurring scheduler
avoids sending the same post to a given session/group on consecutive rounds
while this bot process remains running (when more than one post is enabled).

Changes to enabled posts or mode are picked up at the **start of the next round**.
The manual one-time DM broadcast and the connected-account saved-post auto-reply
also use the selected post mode. Text auto-replies are unchanged when custom
text mode is selected.

Live Stats displays the active post count, sending mode, and latest selected
post ID. FloodWait handling, target cache, parallel accounts, and per-account
sequential target sending are retained.

### Upgrading from the single-post Ads Bot

Your existing `promo.db` is migrated automatically on first startup and the
previous single saved post becomes the first item in the new post library.
Existing MongoDB encrypted sessions are not modified.

**Back up and keep all three when updating or moving servers:** `.env`, `promo.db`,
and the `media/` directory. MongoDB remains your encrypted session store. Replace
`bot.py`, `database.py`, and any updated dependencies, then restart the bot.
Do **not** start a second instance with the same bot token.

Use the scheduler only in groups that allow promotion; avoid repeated unwanted
private messages. Telegram may enforce FloodWaits even at conservative delays.
