# Individual account promotion control

Open **👥 Accounts → 👤 Sessions**. Each account shows a toggle:
- **⏸ Stop Promo**: stops only this account's group-promotion worker (before the next send), without disconnecting or disabling its session.
- **▶️ Resume Promo**: lets the account participate in the next scheduled group-promotion round; it does not restart the full campaign.

The choice is saved to the existing MongoDB `sessions` documents in `promo_enabled` (defaults to true). No database migration is needed.

The toggle controls recurring group promotion only; other features such as manual DM broadcasts and automatic replies are unaffected. Already in-flight Telegram API requests cannot be unsent. Bot operators can control only their own stored sessions. Telegram bot control callbacks are private-chat-only.
