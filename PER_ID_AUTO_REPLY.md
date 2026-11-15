# Per-ID Auto Reply

Open **💬 Auto Reply → 🎯 Per-ID Reply**. Select any connected real Telegram account and send the custom text (1–4000 characters).

- All real accounts use the existing shared Account Reply by default.
- A custom message affects only the selected connected Telegram ID/session.
- To return that ID to the default reply, select it again and send `CLEAR`.
- To leave without changes, send `cancel` or use Back.
- Overrides take priority over the shared saved-promotion-post reply mode on that specific real ID. Other IDs keep their current configured default behavior.
- All custom values persist in the existing tenant-scoped MongoDB settings store across restarts.
- The existing control bot reply remains separate and unchanged.

Note: This controls the reply *sent from* each connected real Telegram ID, not separate replies for individual people messaging the same ID.
