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

## Deploy (persistent container)

For a 24/7 shadow soak you need an always-on host. Because MetaApi's cloud holds
the MT5 connection, this is just a **small Linux box** (1 vCPU / 512MB is plenty)
— no Windows/MetaTrader VPS required.

```bash
git clone <repo> && cd mt5-copier-sytem
cp .env.example .env          # fill in METAAPI_*, TELEGRAM_*, HEALTHCHECK_URL
docker compose up -d --build
docker compose logs -f
```

- **State survives restarts**: `copier.db` lives on the `copier-data` volume, so
  a crash/restart recovers open positions and never re-sends an alert
  (insert-before-send dedupe). `restart: unless-stopped` auto-recovers crashes.
- **Container healthcheck**: the image fails its `HEALTHCHECK` if the heartbeat in
  the DB is older than 120s (`docker compose ps` shows healthy/unhealthy).
- **External dead-man's switch** (`HEALTHCHECK_URL`): the bot pings this URL every
  heartbeat. The internal HealthMonitor cannot warn you if the whole process/host
  dies (it dies too) — so point `HEALTHCHECK_URL` at a
  [healthchecks.io](https://healthchecks.io)-style check with your expected
  activity window, and *that* service pages you if the pings stop. This is the
  only thing that catches total host death.

Verify `master.server_timezone` in `config/config.yaml` against your broker
before a live run (TMFinancials is UTC+3 in summer; see the config comment).

### Without Docker (systemd)

For a single process on a box you control, running directly under **systemd** is
simpler and lighter than a container (no runtime overhead) — Docker's value is
isolation/portability, not efficiency. A ready unit lives at
`deploy/copier-bot.service`:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[live]"
cp .env.example .env         # fill in secrets, incl. HEALTHCHECK_URL
# edit User= and the paths in deploy/copier-bot.service to match your box, then:
sudo cp deploy/copier-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now copier-bot
journalctl -u copier-bot -f
```

`Restart=always` recovers crashes; `copier.db` in the working dir keeps state
across restarts (no duplicate alerts). Liveness coverage is identical to the
container path: systemd restart (crash) + internal `FEED STALE` alert (wedged
feed) + external `HEALTHCHECK_URL` dead-man's switch (total host death).

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
