# Telegram Promotion Bot v1 — MongoDB Sessions

Private admin bot controlling one or more Telethon user sessions through a persistent Reply Keyboard menu.

## Storage

- **MongoDB:** encrypted Telethon StringSessions + session account metadata
- **SQLite:** lightweight bot settings + saved promo post metadata
- **Local media folder:** saved promo media files

StringSessions are encrypted with Fernet **before** MongoDB receives them. Keep `FERNET_KEY` private; MongoDB alone cannot decrypt a stored session without that key.

## Features

- Reply-keyboard menu (not inline callback menu)
- Multiple Telethon `StringSession` accounts
- Sessions encrypted with Fernet and stored in MongoDB
- Automatic joined group/supergroup scanning
- Save one promo post: text/photo/video/document/animation
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

- `➕ Add Session` — paste a Telethon StringSession; bot validates then encrypts it into MongoDB
- `👤 Sessions` — list MongoDB-backed sessions
- `📝 Save Post` — save promotion post
- `👁 View Post` — inspect saved post
- `⏱ Interval` — repeat interval in minutes
- `🔄 Rescan Groups` — count current joined groups
- `▶️ Start Promo` / `⏹ Stop Promo`
- `📊 Live Stats`
- `🗑 Remove Session` — accepts shown Mongo ObjectId or Telegram numeric user ID

## Important

If `FERNET_KEY` changes, previously stored session strings cannot be decrypted. Back up this key securely.
