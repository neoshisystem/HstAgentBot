# HstAgentBot

An asynchronous, high-performance Telegram Bot built with Python (`python-telegram-bot`), `asyncio`, and SQLite (WAL mode) for automated proxy client lifecycle management, multi-tier representative accounting, and smart watchdog monitoring.

## Features

- **Multi-tier Role Architecture**:
  - **Super Admin**: Full operational control, balance adjustments, database and client audits, plan pricing management, card-to-card receipt review (`[✅ Approve]` / `[❌ Reject]`).
  - **Representatives (Resellers)**: Dedicated topic/DM management, client isolation, credit limits, automated invoicing, debt settlement via receipt submission.
  - **End-Users**: Self-service subscription lookup, direct link access, usage status reporting, personal wallet top-up, and one-click subscription URL reset.
- **Financial & Dual-Wallet Subsystem**:
  - Card-to-card payment workflow with tracking codes and admin verification cards.
  - Dual-wallet accounting: Representative balance/settlement & End-User personal wallet.
  - Representative credit limit (`credit_limit_toman`) enforcement.
- **Queue & Reservation**:
  - Direct renewal vs. queued reserve packages.
  - The active background worker checks pending reservations and serializes activation against manual cancellation/activation.
  - The separate early-warning monitor for expiring accounts is not implemented yet.
- **Subscription Security & Identity Utilities**:
  - Real-time subId regeneration to revoke compromised subscription links instantly.
- **Zero Hardcoded Secrets**:
  - 100% generic codebase dynamically configured via `.env` and SQLite database tables.

## Quick Start

1. Clone repository:
   ```bash
   git clone https://github.com/neoshisystem/HstAgentBot.git
   cd HstAgentBot
   ```

2. Create and configure virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. Configure environment variables:
   ```bash
   cp .env.example .env
   # Edit .env with your bot token and panel details
   ```

4. Initialize database and start:
   ```bash
   python database.py
   python rep_bot.py
   ```

## External integrations

The X-UI and backup helpers are server-side integrations and are not included in this repository. Set `HSTAGENTBOT_INTEGRATIONS_PATH` in `.env` to the trusted directory containing those modules. The application can start without the optional integration files being importable, but any operation that needs a missing integration will fail clearly.

Set `SUB_DOMAIN`, `SUB_PORT` and `SUB_PATH` to the public subscription endpoint. The application does not invent a default public subscription domain.

## Validation and operation recovery

Run the automated checks locally with:

```bash
python -m compileall -q .
python -m pytest -q
```

GitHub Actions runs the same compilation and test checks for pushes to the main, beta and recovery branches and for pull requests targeting main/beta.

Some X-UI actions can time out after the external panel has already applied the request. In that case, the bot retains a persistent operation lock rather than blindly retrying and risking a duplicate change:

- Super Admin: use `/operations` to inspect unresolved operation locks.
- Inspect the exact client state in X-UI and reconcile local account/financial/reservation state.
- Only after verifying the result, use `/unlock <operation-key> VERIFIED_NOT_APPLIED: <evidence>` or `/unlock <operation-key> RECONCILED: <evidence>`.
- The unlock command only removes the bot-side hold and writes an audit record. It does not modify X-UI, balances or reservation state.

Do not release an operation lock based only on elapsed time or a missing Telegram response.

## License
MIT
