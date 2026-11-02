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
- **Smart Queue & Reservation**:
  - Direct renewal vs. queued reserve packages.
  - Asynchronous background watchdog activating reserved plans when accounts hit traffic/time thresholds.
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

## License
MIT
