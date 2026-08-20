# copier-bot

Read-only MT5 position observer that pushes trade alerts to Telegram. A human
executes manually on a separate prop-firm account. **This process never places,
modifies, or closes an order.** See [ARCHITECTURE.md](ARCHITECTURE.md) for the
full v1 spec and [CLAUDE.md](CLAUDE.md) for the working rules.

## Status — Milestones M1 & M2

Implemented so far (ARCHITECTURE.md §9):

- `config.py` — typed config from `config/config.yaml` + `.env` (no secrets in code)
- `models.py` — `Position`, `SymbolSpec`, `PositionEvent`, `SizingDecision`, …
- `store/` — SQLite schema + `Repo` (restart-state recovery, idempotent alerts)
- `feed/replay_feed.py` — replays an MT5 `.xlsx` export, no network
- `core/tracker.py` — position diffing: OPENED / MODIFIED / CLOSED / PRE_EXISTING
- `core/risk.py` — sizing, synthesised protective stop, close estimate (M2)
- `core/governor.py` — daily budget accounting, OK/WARN/SKIP verdicts (M2)
- `notify/formatter.py` — pure message templates, golden-file tested (M2)
- `main.py` — runs ReplayFeed → tracker → risk → governor → formatter → stdout

Not yet implemented: MetaApi live feed (M4), Telegram sink (M3), anomaly
tripwires + health monitor + `log-fill` CLI (M5).

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
