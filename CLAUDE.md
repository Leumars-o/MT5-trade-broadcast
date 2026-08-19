# copier-bot

Read-only MT5 position observer that pushes trade alerts to Telegram.
A human executes manually on a separate prop-firm account.

## Absolute rules
- This process NEVER places, modifies, or closes an order. No trading API
  calls. If a task seems to need one, stop and ask.
- Connections use investor (read-only) credentials only.
- Never log, print, or send credentials. Redact in the logging config.
- Lot sizes always round DOWN to lot_step.
- Every entry alert must include a protective stop price. No exceptions.

## Conventions
- Python 3.11+, async/await, type hints everywhere, `ruff` + `mypy` clean.
- Timestamps: UTC internally (aware datetimes), converted only for display.
- Pure functions in `core/` and `notify/formatter.py` — no I/O, no clock reads.
  Pass `now` as an argument so tests are deterministic.
- Money as `Decimal`, prices/points as `float`.
- New behaviour needs a test. Tracker changes need a restart test AND a
  reconnect test.

## Testing
- `ReplayFeed` + `tests/fixtures/ReportHistory-51104542.xlsx` is the primary
  harness. Do not require a live broker connection to run the suite.
- `pytest -q` must pass before any commit.

## Context
`mae_points` in config is derived from a 2-trade sample and is a placeholder,
not a validated risk parameter. Do not build logic that assumes it is accurate.

## Status
Milestone M1 is implemented: config, models, SQLite store, ReplayFeed over the
xlsx fixture, and the PositionTracker (restart / reconnect / dedupe / partial
close). MetaApi, Telegram, risk sizing, governor, anomaly, and health are NOT
implemented yet — see ARCHITECTURE.md §9 for the milestone order.
