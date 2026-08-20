# Copier Bot — v1 Architecture Spec

**Scope of v1:** read positions from an MT5 master account using investor
(read-only) credentials, detect open/modify/close events, compute the
position sizing a $50k prop account would need, and push a formatted alert to
Telegram. A human executes manually.

**Explicitly NOT in v1:** placing orders, writing to any account, connecting to
the destination broker, multi-master support, web UI, backtesting engine.

The system is a **read-only observer with a notification sink**. Every design
decision below follows from that. If a change would let this process place an
order, it is out of scope by definition.

---

## 1. Why this shape

Three constraints drive the architecture:

1. **Compliance boundary.** All automation lives on the master side (a
   third-party retail account, read-only). The funded prop account sees only
   manual human execution. Nothing automated may ever cross that line.
2. **The master sets no stop loss.** The risk engine must *synthesise* a
   protective stop for the destination. This is not copying the master's risk
   management; it is wrapping the master's signal in our own.
3. **Silent failure is the main enemy.** A copier that dies at 03:00 and says
   nothing is worse than no copier. Liveness monitoring is a v1 requirement,
   not a nice-to-have.

---

## 2. Stack

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Shares the risk logic with the existing `mt5_risk_audit.py`; MetaApi ships a first-class async SDK |
| Broker access | MetaApi cloud (`metaapi-cloud-sdk`) | No Windows VPS to maintain; websocket streaming; read-only connections supported |
| Persistence | SQLite via `sqlite3` (stdlib) | Single-process, zero-ops, survives restart. Postgres only if this ever goes multi-instance |
| Config | `pydantic-settings` | Typed config, env-var loading, fails fast on a bad value |
| Notifications | `python-telegram-bot` (or raw Bot API via `httpx`) | Either is fine; raw HTTP is fewer deps for send-only |
| Scheduling | `asyncio` | Single event loop, no Celery/cron for v1 |
| Tests | `pytest` + `pytest-asyncio` | |
| Logging | `structlog` → JSON to stdout | Machine-greppable when debugging a missed signal |

**Alternative worth considering:** a Windows VPS running MT5 with an MQL5
polling EA that POSTs to a local HTTP endpoint. Cheaper monthly, but you
maintain MQL5 and a VPS. The adapter interface in §4 keeps this option open —
implement `PositionFeed` a second time and nothing else changes.

---

## 3. Repository layout

```
copier-bot/
├── CLAUDE.md                  # project instructions (see §11)
├── pyproject.toml
├── .env.example               # never commit .env
├── README.md
├── config/
│   └── config.yaml            # non-secret config
├── src/copier/
│   ├── __init__.py
│   ├── config.py              # pydantic settings
│   ├── models.py              # Position, Signal, Alert dataclasses
│   ├── feed/
│   │   ├── base.py            # PositionFeed protocol
│   │   ├── metaapi_feed.py    # live implementation
│   │   └── replay_feed.py     # replays an MT5 xlsx export — used by tests
│   ├── core/
│   │   ├── tracker.py         # position diffing + event emission
│   │   ├── risk.py            # sizing, protective stop, breakeven maths
│   │   ├── governor.py        # daily budget accounting
│   │   └── anomaly.py         # volume-step + direction-mismatch tripwires
│   ├── notify/
│   │   ├── base.py            # Notifier protocol
│   │   ├── telegram.py
│   │   └── formatter.py       # message templates (no I/O — pure, testable)
│   ├── store/
│   │   ├── schema.sql
│   │   └── repo.py
│   ├── health.py              # heartbeat + dead-man's switch
│   └── main.py                # wiring + run loop
└── tests/
    ├── fixtures/ReportHistory-51104542.xlsx
    ├── test_tracker.py
    ├── test_risk.py
    ├── test_governor.py
    └── test_formatter.py
```

---

## 4. Data flow

```
  MetaApi (investor creds, READ ONLY)
            │  websocket: position snapshots
            ▼
  ┌──────────────────────┐
  │ PositionFeed         │  normalises broker payload → Position
  └──────────┬───────────┘
             ▼
  ┌──────────────────────┐
  │ PositionTracker      │  diffs snapshot vs last known state
  │                      │  emits OPENED / MODIFIED / CLOSED
  │                      │  dedupes by (position_id, event_type)
  └──────────┬───────────┘
             ▼
  ┌──────────────────────┐
  │ AnomalyDetector      │  volume-step, direction/PnL, spec drift
  └──────────┬───────────┘
             ▼
  ┌──────────────────────┐
  │ RiskEngine           │  lot size, protective stop price, $ risk
  └──────────┬───────────┘
             ▼
  ┌──────────────────────┐
  │ DailyGovernor        │  budget remaining → OK / WARN / SKIP verdict
  └──────────┬───────────┘
             ▼
  ┌──────────────────────┐      ┌──────────────┐
  │ Formatter → Telegram │─────▶│   SQLite     │  every signal + alert logged
  └──────────────────────┘      └──────────────┘
```

`HealthMonitor` runs on a parallel task and is not in this path.

---

## 5. Core components

### 5.1 `feed/base.py` — PositionFeed

```python
class PositionFeed(Protocol):
    async def connect(self) -> None: ...
    async def snapshot(self) -> list[Position]: ...
    async def stream(self) -> AsyncIterator[list[Position]]: ...
    async def symbol_spec(self, symbol: str) -> SymbolSpec: ...
```

`SymbolSpec` carries `contract_size`, `lot_step`, `min_lot`, `max_lot`,
`digits`, `tick_value`. **Fetch these from the broker. Do not hardcode 100
oz/lot** — keep a fallback table but log a warning when it is used.

`ReplayFeed` reads an MT5 xlsx export and emits the snapshots that would have
produced it, driven by a fake clock. This is what makes the whole system
testable without a live account, and it is a v1 deliverable, not a stretch goal.

### 5.2 `core/tracker.py` — PositionTracker

Holds `dict[position_id, Position]`. On each snapshot, diff:

- **id present now, absent before** → `OPENED`
- **id present both, fields changed** → `MODIFIED` (report which fields; volume
  decrease = partial close)
- **id absent now, present before** → `CLOSED`

Hard requirements:

- **Restart safety.** Load last known state from SQLite on boot. A position
  whose `open_time` predates process start and which was never alerted must be
  emitted as `PRE_EXISTING` (a distinct, quieter alert), never as `OPENED`.
  Getting this wrong means a restart spams you with fake entry signals for
  positions already running.
- **Reconnect safety.** A dropped websocket must not produce a phantom
  `CLOSED` for every open position. Only emit `CLOSED` from a snapshot
  received on a healthy connection; on reconnect, treat the first snapshot as
  a resync, not a diff source.
- **Idempotency.** `(position_id, event_type)` is a unique key in the alerts
  table. Insert before send; if the insert conflicts, skip the send.

### 5.3 `core/risk.py` — RiskEngine

Port the logic from `mt5_risk_audit.py`. Inputs: master position, symbol spec,
config. Outputs a `SizingDecision`.

**Sizing model — balance-proportional with a risk cap** (revised after a
74-trade sample, 2 Jun–14 Aug 2026, overturned the original risk-based model):

```
native_lots             native_lots_per_1k × (destination_equity/1000) × size_multiplier
cap_lots                (daily_dd_limit × utilisation_target) / (stop_distance × contract_size)
destination_lots        floor_DOWN(min(native_lots, cap_lots))     # never up
binding_constraint      "proportional" | "risk_cap"  (whichever bound)
protective_stop_price    entry ± (stop_basis_points × buffer_multiplier)
risk_usd                stop_distance × contract_size × lots
utilisation_pct         risk_usd / daily_dd_limit
commission_estimate     lots × commission_per_lot
```

The master sizes **balance-proportionally** — a constant ~0.00077 lots per $1k
(CoV 0.75% across 74 trades), *not* risk-based. So the faithful copy is that
same rate on the destination equity (`size_multiplier = 1.0` = native), then
clamped so per-trade risk never exceeds `utilisation_target` of the daily
budget. `size_multiplier > 1` **leverages** the strategy — the measured edge
(86.5% win, 1.79 R:R) was only observed at 1×; every lot size discussed pre-data
was 11–60× native. The alert surfaces the native multiple so leverage is visible.

Rules:

- **Always round lots down.** Rounding up silently breaches the budget.
- **Refuse to size** if `symbol_spec` is unavailable, or if the proportional
  size rounds below `min_lot` — emit an INFO alert rather than sizing arbitrary.
- The protective stop is **mandatory output**. Every alert carries a stop price.
  There is no code path that produces an entry alert without one.
- The stop basis is config-driven (`stop_basis_points`), derived from the
  **realised-loss distribution** (p100 = 3.90 pts; the losses stop dead there, so
  the master runs a virtual ~4pt stop). Log which value was used every decision.
- **This is not MAE.** The loss distribution bounds *exits*, not floating
  excursion. `mae_points` stays `null` until true MAE is measured; do not size
  off it. Close estimates apply a `fee_drag_pct` (~26%, weekly PF deduction) so
  net runs ~¾ of gross.

### 5.4 `core/governor.py` — DailyGovernor

Tracks, per trading day (broker server day, not local day):

- committed risk from open signals
- realised loss so far, if destination equity is configured

Verdicts: `OK` / `WARN` (would exceed soft threshold) / `SKIP` (would exceed
hard threshold). The verdict goes in the alert; v1 never blocks anything,
because v1 places no orders — it advises.

For v1, destination equity comes from config (`destination_equity`), manually
updated. Reading the funded account live is v1.5.

### 5.5 `core/anomaly.py` — tripwires

Fire a distinct CRITICAL alert, separate from trade signals, when:

- volume step vs previous master trade ≥ 2.0× (martingale/pyramiding watch)
- on close, `sign(price_move × direction) != sign(profit)` (direction field
  disagrees with P&L — would mean mirrored trades run backwards)
- implied contract size drifts >2% from the broker spec
- a position stays open beyond `max_expected_hold_minutes`

### 5.6 `health.py` — HealthMonitor

- Heartbeat written to SQLite every 30s.
- Telegram alert if no broker snapshot received in `stale_feed_minutes`.
- Daily "still alive" summary at a configured time, including signal count and
  max observed signal age.
- Alert on every disconnect and reconnect, with downtime duration.

This is the component that stops a dead process from being invisible.

---

## 6. Data model

```sql
CREATE TABLE positions (
  position_id   TEXT PRIMARY KEY,
  symbol        TEXT NOT NULL,
  direction     TEXT NOT NULL CHECK (direction IN ('buy','sell')),
  volume        REAL NOT NULL,
  open_price    REAL NOT NULL,
  open_time     TEXT NOT NULL,        -- ISO8601 UTC
  sl            REAL,
  tp            REAL,
  close_price   REAL,
  close_time    TEXT,
  profit        REAL,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  state         TEXT NOT NULL         -- open | closed | pre_existing
);

CREATE TABLE alerts (
  id                INTEGER PRIMARY KEY,
  position_id       TEXT NOT NULL,
  event_type        TEXT NOT NULL,    -- opened | modified | closed | pre_existing | anomaly | health
  broker_event_time TEXT,
  detected_at       TEXT NOT NULL,
  sent_at           TEXT,
  signal_age_ms     INTEGER,
  payload_json      TEXT NOT NULL,
  send_status       TEXT NOT NULL,    -- pending | sent | failed
  UNIQUE (position_id, event_type)
);

CREATE TABLE sizing_decisions (
  alert_id        INTEGER REFERENCES alerts(id),
  destination_lots REAL, protective_stop REAL, risk_usd REAL,
  utilisation_pct REAL, stop_basis_points REAL, buffer_multiplier REAL,
  size_multiplier REAL, binding_constraint TEXT, governor_verdict TEXT
);

CREATE TABLE executions (           -- manually filled in later
  alert_id     INTEGER REFERENCES alerts(id),
  acted        INTEGER,             -- 0/1
  fill_price   REAL,
  fill_time    TEXT,
  actual_lots  REAL,
  notes        TEXT
);
```

The `executions` table is the point of the whole exercise: after 30 trades it
tells you your real slippage and whether manual execution is viable at all.
Provide a `copier log-fill` CLI command to populate it.

---

## 7. Configuration

```yaml
master:
  metaapi_account_id: ${METAAPI_ACCOUNT_ID}
  server_timezone: "Etc/GMT-3"        # VERIFY against the broker
  poll_interval_seconds: 2

symbols:
  XAUUSD.f:
    destination_symbol: XAUUSD
    contract_size_fallback: 100

risk:
  stop_basis_points: 3.90             # p100 realised loss (74 trades), NOT MAE
  buffer_multiplier: 1.5              # 3.90 × 1.5 = 5.85pt synthesised stop
  native_lots_per_1k: 0.00077         # master's balance-proportional rate
  size_multiplier: 1.0                # 1× = native; >1 leverages the strategy
  commission_per_lot: 10.0
  fee_drag_pct: 0.26                  # weekly PF deduction ≈ 26% of gross
  daily_dd_limit: 2500
  max_dd_limit: 5000
  utilisation_target: 0.15            # per-trade RISK CAP; see note in §10
  destination_equity: 50000
  mae_points: null                    # floating MAE STILL UNMEASURED

governor:
  soft_threshold_pct: 0.60
  hard_threshold_pct: 0.85

telegram:
  bot_token: ${TELEGRAM_BOT_TOKEN}
  chat_id: ${TELEGRAM_CHAT_ID}

health:
  stale_feed_minutes: 5
  daily_summary_time: "22:00"
  max_expected_hold_minutes: 240

display_timezone: "Africa/Lagos"
```

**Secrets live in `.env` only.** Never logged, never committed, never included
in a Telegram message.

---

## 8. Message contract

Entry:

```
🔴 SELL XAUUSD — 0.40 lots
Master:  0.001798 @ 4399.36
Stop:    4408.64  (9.28 pts)
Risk:    $371  (15% of daily)
Budget:  $2,129 remaining today
Age:     1.4s   ·   #96484066
```

Close:

```
✅ CLOSE SELL XAUUSD  ·  #96484066
Master exit: 4392.34   (+7.02 pts)
Your est:    +$218 net
Held:        2h 14m
Age:         0.9s
```

Anomaly:

```
⚠️ VOLUME STEP 2.0×
Previous: 0.000899 → now 0.001798
Third consecutive doubling. Review before acting.
```

`formatter.py` must be pure functions returning strings — no network, no
clock reads (pass `now` in). That keeps message formatting fully unit-tested.

Every message carries **signal age**. If it routinely exceeds ~5s, the
pipeline has a problem you would otherwise never see.

---

## 9. Build milestones

Ship each one working before starting the next.

**M1 — Skeleton + ReplayFeed.** Config loading, models, SQLite schema,
`ReplayFeed` over the existing xlsx fixture, tracker emitting events to stdout.
Zero network. Full test coverage on the tracker's restart and reconnect cases.

**M2 — Risk engine + formatter.** Port sizing from `mt5_risk_audit.py`. Pure,
unit-tested. Golden-file tests on message output.

**M3 — Telegram sink.** Send-only, retry with backoff, dedupe via the alerts
table. Verify a restart mid-run produces no duplicate messages.

**M4 — MetaApi live feed, read-only.** Connect with investor credentials.
Assert at startup that the connection cannot trade. Run against the live master
in **shadow mode** — alerts to a private test channel — for at least two weeks.

**M5 — Health monitor + anomaly tripwires + `log-fill` CLI.**

Only after M5 has run clean for a month should any of this inform real money.

---

## 10. Hard constraints for the implementer

1. **No order-placement code, ever.** No imports of MetaApi trading methods.
   Add a test that greps the source for them and fails if found.
2. **All timestamps stored UTC**, rendered in `display_timezone`. Broker server
   time is a third zone — convert explicitly, never assume.
3. **Round lot sizes down.** Always.
4. **Never emit an entry alert without a protective stop price.**
5. **Idempotency before send**, not after. Insert the alert row, then send.
6. **`stop_basis_points: 3.90` is the p100 realised loss from a 74-trade sample
   — a bound on exits, not floating heat. `mae_points` is still `null`.** True
   MAE (excursion) remains unmeasured; do not size off it. Surface the stop
   basis in the daily summary so its provenance cannot be quietly forgotten.
7. **`size_multiplier` defaults to 1.0 (native).** That is the only regime where
   the 86.5% win / 1.79 R:R was observed. Raising it leverages the strategy
   (11–60× native was discussed pre-data, on no evidence the edge survives gold
   slippage at size). `utilisation_target: 0.15` is the per-trade risk cap that
   backstops it — do not raise either without a measured MAE distribution across
   50+ trades including losers.
8. **Log credentials never.** Redact at the logger level, not the call site.

> **Data provenance caveat.** The 74-trade sample is one subscriber's instance
> of a centrally-distributed signal — far better than a vendor chart, but not
> independently verified, and it still lacks floating MAE. Treat the model as
> evidence-based, not proven.

---

## 11. `CLAUDE.md` for the repo

Put this at the repo root so every Claude Code session inherits it:

```markdown
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
Sizing is balance-proportional with a risk cap (`size_multiplier` = multiple of
native; default 1×). `stop_basis_points` (3.90) is the p100 realised loss from a
74-trade sample — a bound on exits, NOT floating MAE, which is still unmeasured
(`mae_points: null`). Do not size off `mae_points` until it is measured, and do
not treat a leveraged `size_multiplier` as risk-free — the edge was seen at 1×.
```

---

## 12. Initialization prompt for Claude Code

```
Read ARCHITECTURE.md and CLAUDE.md in full before writing any code.

Build Milestone M1 only:
  - pyproject.toml with the dependencies listed in §2
  - config loading per §7 (pydantic-settings, .env.example, no secrets)
  - models.py: Position, SymbolSpec, PositionEvent, SizingDecision
  - store/schema.sql + repo.py per §6
  - feed/base.py protocol and feed/replay_feed.py driven by the xlsx fixture
  - core/tracker.py per §5.2
  - a main.py that runs ReplayFeed → tracker → stdout

Write tests first for the tracker. Cover specifically:
  - restart with an open position → PRE_EXISTING, not OPENED
  - websocket reconnect → no phantom CLOSED events
  - duplicate snapshot → no duplicate events
  - partial close → MODIFIED with volume delta, not CLOSED

Do NOT implement MetaApi, Telegram, risk sizing, or the governor yet.
Stop when `pytest -q` is green and report what you built.
```

---

## 13. What v1.5 adds

- Read-only connection to the destination account so the governor uses real
  equity instead of a config value
- `log-fill` slippage report: measured latency and cost per trade
- Re-derived `mae_points` from a real MAE distribution once the sample supports it
- Multi-symbol support, if the master ever trades anything but gold

Order placement stays out of scope until the manual process has been running
long enough to prove the signals are worth acting on at all.
