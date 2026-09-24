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


## Conservative per-account pacing and Telegram limits

- Menu: **⏳ Group Delay**. Default **30 seconds**, configurable from 15 to 600 seconds. This is the minimum gap between actual sends by each account.
- Each ID has one outgoing pacing lane shared by its group sends and incoming DM auto-replies. Different account workers remain independent.
- Manual one-time DM broadcasts have their own minimum delay, default **60 seconds**.
- On `FloodWaitError`, the affected account immediately stops the current round. Telegram's wait plus a safety gap is saved in local `promo.db` and survives a bot restart; there is no immediate retry.
- On `PeerFloodError`, the affected account is held for at least 24 hours. A hold is a safety measure **not** a way to remove restrictions; check the account's actual Telegram status before sending again.
- Slow-mode/write-forbidden groups are skipped; no attempt is made to bypass their limits.
- **No configuration can guarantee Telegram won't impose restrictions.** Send only to groups where you have permission to advertise.

Add these optional settings to `.env`:

```env
GROUP_DELAY_SECONDS=30
DM_BROADCAST_DELAY_SECONDS=60
RESTRICTION_PAUSE_HOURS=24
```

Changing the delay from the admin menu stores the setting in `promo.db`, overriding `.env` after the first run.


## DM auto-reply while group promotion or manual DM broadcast runs

All sends and incoming DM events for one real account now share **one persistent
Telethon client**. The account's outgoing pacing queue prioritizes replies from
people who message the account, without bypassing its existing rate limits.
A reply may still wait for the remaining group-message gap or Telegram FloodWait.
The reply cooldown starts after a successful send (not while a reply waits).

`DM_REPLY_SEND_GAP_SECONDS=5` can be changed in `.env`. Unlike the separate
5-minute `DM_REPLY_COOLDOWN_SECONDS`, this controls pacing between outgoing sends.
There is no need to toggle off scheduled promotions to receive incoming DMs.

The admin control bot's separate auto-reply remains enabled independently.

## Multi-user access (owner-controlled)

Only the `ADMIN_ID` owner can grant or revoke access. There is **no access-request
flow** and other users cannot approve anyone. Send these commands in the bot's
private chat:

- `/add 123456789` — grant this Telegram user ID operator access.
- `/rem 123456789` — immediately revoke access and stop that operator's jobs
  and account listeners; their encrypted data is kept for possible reapproval.
- `/users` — list all approved operators.

A newly approved operator can open `/start` and use the same menu, but will see
**only their own** Telegram sessions, saved posts, promotion settings and stats.
Owner data remains in the original `promo.db`, `media/` and existing MongoDB
session records. New operators each receive `operators/<uid>/promo.db`,
`operators/<uid>/media/` and independently tagged MongoDB session records.
Their recurring promotion tasks, one-off DM broadcast status and auto-reply
listeners are isolated from other operators. A session already stored for one
operator cannot be claimed by another; every Telegram account should only be
connected by its owner.

Access permissions survive restarts in MongoDB's `approved_operators` collection.
Save the original `.env`, `promo.db` and `media` folder when upgrading, and
include the `database.py` update in this release. `FERNET_KEY` must remain the
same to decrypt the owner and operator accounts.

Optional `.env` setting: `OPERATORS_DIR=operators` controls the per-operator
SQLite/media directory. Operator data is local to your deployment; back up
`operators/` along with `promo.db`, `media/` and MongoDB.


## Automatic new-group detection

Each connected account watches Telegram join/add events and incoming group activity. When
that account joins a new group or supergroup, its own cached group target list is updated
automatically; other operators' accounts and groups are not affected. A self-leave event
removes the departed group. A new group found during an active promotion becomes eligible
for the **next promotion round** (the currently executing round uses a snapshot).

Telegram does not consistently deliver self-join events for every channel or reconnect.
A background reconciliation of that account's joined-group dialogs runs approximately every
10 minutes by default, controlled by `AUTO_GROUP_SYNC_SECONDS=600` (minimum 300). This
means no manual `🔄 Rescan Targets` is necessary in ordinary use. The button remains for
optional immediate manual correction. Only groups/supergroups are promotion targets;
broadcast-only channels and private chats are not added to recurring promotions.

Accounts still obey the configured per-group delay and Telegram FloodWait/anti-spam pauses.
Automatic discovery does not guarantee the account has permission to post in a new group.

## Per-account progress, statistics and logs

All enabled sessions start their group-promotion workers **concurrently**. Inside each
Telegram account, group messages remain sequential and respect that account's
configured minimum gap and any Telegram FloodWait. Two different Telegram
accounts do not wait for each other's per-account pacing lock. Incoming private
replies may take priority on the same account, without bypassing cooldowns.

On **Start Promo**, each account receives its own editable progress message in
the operator's control-bot chat. Its status shows sent, failed, skipped, total
and the last attempted group. Manual **DM Broadcast** uses separately tracked
per-account metrics, so it does not overwrite promotion history. The combined
**📊 Live Stats** remains available; use **📊 Account Stats** for individual
accounts and **📜 Account Logs** to retrieve recent send results for each
account and mode.

The two types of logs are persisted independently:

- Owner: `logs/<mongo-session-id>-promo.log` and `logs/<mongo-session-id>-dm.log`
- Operator: `operators/<operator-uid>/logs/<mongo-session-id>-promo.log` and
  `operators/<operator-uid>/logs/<mongo-session-id>-dm.log`

Logs contain timestamps, target names, send outcomes and a brief error type,
not the encrypted Telegram session. The bot shows the latest ten entries per
account and mode. The latest per-account counters are in memory since the
last restart; durable log files remain available after restarting.

**Why a group can see a 30–60-minute gap:** A 30-second gap across 60 groups
requires *at least* 30 minutes for one account's round. The separately configured
repeat interval starts **after** the round finishes, so a 30-minute interval
can result in at least 60 minutes between two posts in the same group. Setting
the gap too low can trigger Telegram rate limits; the bot does not bypass
FloodWait or account restrictions. See `.env.example` for pacing settings.
