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
Sizing is **balance-proportional with a risk cap** (revised from a 74-trade
sample, 2 Jun–14 Aug 2026): the master trades a constant ~0.00077 lots/$1k, so
the copy uses that rate on destination equity (`size_multiplier` = multiple of
native, default 1×), clamped by `utilisation_target`. `stop_basis_points: 3.90`
is the p100 realised loss (the master runs a virtual ~4pt stop) — a bound on
exits, NOT floating MAE. `mae_points` stays `null` until true excursion is
measured; never size off it. A leveraged `size_multiplier` is not risk-free —
the 86.5% win / 1.79 R:R was only observed at 1×. The sample is one subscriber
instance, not independently verified.

## Status
Milestones M1, M2, and M3 are implemented:
- M1: config, models, SQLite store, ReplayFeed over the xlsx fixture, and the
  PositionTracker (restart / reconnect / dedupe / partial close).
- M2: `core/risk.py` (sizing, synthesised protective stop, close estimate —
  ported from `mt5_risk_audit.py`), `core/governor.py` (daily budget verdicts),
  and `notify/formatter.py` (pure message templates, golden-file tested).
- M3: `notify/telegram.py` (send-only Telegram over httpx, backoff on
  429/5xx/transport errors, token never logged), `notify/dispatcher.py`
  (insert-before-send idempotency → restart produces no duplicate sends).
  `main.py --send` dispatches live; `--send-test` sends one message.

- M4: `feed/metaapi_feed.py` — live READ-ONLY MetaApi streaming feed behind the
  same `PositionFeed`/`FeedUpdate` contract as ReplayFeed. `main.py --live`
  connects with investor credentials and dispatches in shadow mode. The SDK is
  an optional dep (`pip install -e ".[live]"`), imported only on the live path.

- M5 (in progress): `health.py` — HealthMonitor (heartbeat, stale-feed
  dead-man's switch, disconnect/reconnect alerts, daily summary surfacing the
  stop-basis provenance). Wired as a parallel task in `--live`. Anomaly
  tripwires and the `log-fill` CLI are still TODO.

See ARCHITECTURE.md §9 for the milestone order.

Read-only is enforced by (1) investor password at the broker and (2) no
trading-method references in `src/` (guarded by `test_no_order_placement`).
`metaapi_feed.py` must never call a trade method — the streaming connection
object exposes them, but we only touch connect/synchronise/read/close.

Credential hygiene: httpx/httpcore INFO logs are silenced in
`logging_config.py` because they would print the bot token in the request URL.
Live CLOSED alerts currently lack the exact exit price (it lives in MetaApi's
deal stream, not `terminal_state.positions`) — a later enrichment.
