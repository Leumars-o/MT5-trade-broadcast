# copier-bot

Read-only MT5 position observer that pushes trade alerts to Telegram. A human
executes manually on a separate prop-firm account. **This process never places,
modifies, or closes an order.** See [ARCHITECTURE.md](ARCHITECTURE.md) for the
full v1 spec and [CLAUDE.md](CLAUDE.md) for the working rules.

## Status — Milestones M1, M2 & M3

Implemented so far (ARCHITECTURE.md §9):

- `config.py` — typed config from `config/config.yaml` + `.env` (no secrets in code)
- `models.py` — `Position`, `SymbolSpec`, `PositionEvent`, `SizingDecision`, …
- `store/` — SQLite schema + `Repo` (restart-state recovery, idempotent alerts)
- `feed/replay_feed.py` — replays an MT5 `.xlsx` export, no network
- `core/tracker.py` — position diffing: OPENED / MODIFIED / CLOSED / PRE_EXISTING
- `core/risk.py` — sizing, synthesised protective stop, close estimate (M2)
- `core/governor.py` — daily budget accounting, OK/WARN/SKIP verdicts (M2)
- `notify/formatter.py` — pure message templates, golden-file tested (M2)
- `notify/telegram.py` — send-only Telegram sink, backoff + token redaction (M3)
- `notify/dispatcher.py` — insert-before-send dedupe; no dupes on restart (M3)
- `feed/metaapi_feed.py` — live READ-ONLY MetaApi streaming feed (M4)
- `health.py` — HealthMonitor: heartbeat, stale-feed switch, daily summary (M5)
- `core/anomaly.py` — tripwires: volume-step, direction/P&L, drift, over-hold (M5)
- `main.py` — feed → tracker → risk → governor → anomaly → formatter → sink

All v1 milestones (M1–M5) are implemented.

### Log a manual fill

```bash
python -m copier.main log-fill --alert-id 42 --fill-price 4399.5 --actual-lots 0.03
python -m copier.main log-fill --list
```

Populates the `executions` table so measured slippage can be reviewed after ~30 fills.

### Live (MetaApi, read-only shadow mode)

```bash
pip install -e ".[live]"        # optional live SDK
# .env: METAAPI_TOKEN + METAAPI_ACCOUNT_ID (investor/read-only credentials)
python -m copier.main --live
```

The account must be provisioned with the **investor password** and show
DEPLOYED/CONNECTED in the MetaApi dashboard first.

### Telegram

```bash
python -m copier.main --send-test          # send one confirmation message
python -m copier.main --send               # dispatch replay alerts (idempotent)
```

Needs `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # fill in secrets when needed (M3+)
```

## Run the M1 replay

```bash
python -m tests.fixtures._make_report          # generate the xlsx fixture
python -m copier.main tests/fixtures/ReportHistory-51104542.xlsx
```

## Test

```bash
pytest -q
```

The test suite runs entirely against `ReplayFeed` + the xlsx fixture — no live
broker connection required.
