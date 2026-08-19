#!/usr/bin/env python3
"""
mt5_risk_audit.py — parse an MT5 "Trade History Report" (.xlsx) and audit it
for copy-trading / prop-account position sizing.

Usage:
    python mt5_risk_audit.py REPORT.xlsx [--m1 XAUUSD_M1.csv] [--account 50000]
                             [--daily-dd 2500] [--max-dd 5000] [--buffer 2.0]

What it does
------------
1. Parses the Positions block (authoritative direction, volume, prices, SL/TP).
2. Sanity-checks every trade: direction vs P&L sign, implied contract size,
   commission per lot, missing stops.
3. Reports the stats that actually decide position size: win rate, R:R,
   MAE distribution (if M1 data supplied), volume sequence behaviour.
4. Produces the $50k scaling table at several daily-DD utilisation levels.

The M1 CSV is optional. Without it, MAE cannot be computed — MT5 reports do
not contain it. Expected CSV columns: time,open,high,low,close  (UTC or
server time; pass --tz-shift hours if the report and CSV disagree).
"""

import argparse
import sys

import numpy as np
import pandas as pd

CONTRACT_SIZES = {"XAUUSD": 100.0, "XAGUSD": 5000.0}  # oz per standard lot


# ---------------------------------------------------------------- parsing

def _strip_suffix(symbol: str) -> str:
    """XAUUSD.f -> XAUUSD"""
    return str(symbol).split(".")[0].upper()


def parse_mt5_report(path: str) -> tuple[pd.DataFrame, dict]:
    """Return (positions_df, meta). Locates blocks by their header rows so it
    survives MT5 adding or removing rows between versions."""
    raw = pd.read_excel(path, sheet_name=0, header=None)
    col0 = raw[0].astype(str)

    meta = {}
    for key in ("Name:", "Account:", "Company:", "Date:"):
        hit = raw[col0 == key]
        if not hit.empty:
            meta[key.rstrip(":")] = hit.iloc[0, 3]

    try:
        start = col0[col0 == "Positions"].index[0] + 2  # skip label + header
    except IndexError:
        sys.exit("No 'Positions' block found — is this an MT5 history report?")
    end = col0[col0.isin(["Orders", "Deals"])]
    end = end.index[end.index > start][0]

    block = raw.iloc[start:end, :14].copy()
    block.columns = [
        "open_time", "position", "symbol", "type", "volume", "open_price",
        "sl", "tp", "close_time", "close_price", "commission", "swap",
        "profit", "_x",
    ]
    block = block.dropna(subset=["symbol"]).drop(columns="_x")

    for c in ("volume", "open_price", "sl", "tp", "close_price",
              "commission", "swap", "profit"):
        block[c] = pd.to_numeric(block[c], errors="coerce")
    for c in ("open_time", "close_time"):
        block[c] = pd.to_datetime(block[c], format="%Y.%m.%d %H:%M:%S",
                                  errors="coerce")

    block["type"] = block["type"].astype(str).str.lower().str.strip()
    block["root"] = block["symbol"].map(_strip_suffix)
    block["contract"] = block["root"].map(CONTRACT_SIZES)
    block = block.reset_index(drop=True)

    # signed price move captured, in points
    sign = np.where(block["type"] == "sell", -1.0, 1.0)
    block["points"] = (block["close_price"] - block["open_price"]) * sign
    block["duration_min"] = (
        block["close_time"] - block["open_time"]
    ).dt.total_seconds() / 60.0
    return block, meta


# ---------------------------------------------------------------- checks

def integrity_checks(df: pd.DataFrame) -> list[str]:
    """The failures that silently destroy a copier. Each returns a message."""
    issues = []

    unknown = df.loc[df["contract"].isna(), "root"].unique()
    if len(unknown):
        issues.append(
            f"CONTRACT SIZE UNKNOWN for {', '.join(unknown)} — add it to "
            "CONTRACT_SIZES from the broker's symbol spec before trusting any $ figure.")

    # direction must agree with the sign of the P&L
    known = df.dropna(subset=["contract"])
    bad = known[np.sign(known["points"]) != np.sign(known["profit"])]
    bad = bad[known["profit"] != 0]
    if len(bad):
        issues.append(
            f"DIRECTION MISMATCH on {len(bad)} trade(s): recorded direction "
            "disagrees with the sign of the realised profit. A copier reading "
            "this field would mirror those trades backwards.")

    # implied contract size vs assumed
    if len(known):
        implied = (known["profit"] - known["commission"] - known["swap"]) / (
            known["points"] * known["volume"])
        drift = (implied / known["contract"] - 1).abs()
        if (drift > 0.02).any():
            issues.append(
                f"CONTRACT SIZE DRIFT: implied size differs from assumed by up "
                f"to {drift.max():.1%}. Verify oz/lot with the broker.")

    n_nosl = int(df["sl"].isna().sum() + (df["sl"] == 0).sum())
    if n_nosl:
        issues.append(
            f"NO STOP LOSS on {n_nosl}/{len(df)} trade(s). Risk-based sizing "
            "from the master's SL is impossible; the copier must synthesise "
            "its own stop and enforce a hard per-trade loss cap.")

    if len(df) < 30:
        issues.append(
            f"SAMPLE TOO SMALL: {len(df)} trade(s). Any lot size derived from "
            "this is a curve fit, not a risk model. Target 100+ including losers.")

    if (df["profit"] > 0).all() and len(df) > 0:
        issues.append(
            "NO LOSING TRADES in sample. The drawdown distribution that "
            "determines position size is entirely unobserved.")

    return issues


def volume_behaviour(df: pd.DataFrame) -> list[str]:
    """Is the master risk-sizing, balance-scaling, or doubling into a wall?"""
    notes = []
    if len(df) < 2:
        return ["Not enough trades to characterise volume behaviour."]

    v = df["volume"].to_numpy()
    ratios = v[1:] / v[:-1]
    notes.append(f"Volume sequence: {', '.join(f'{x:g}' for x in v)}")
    notes.append(f"Step ratios:     {', '.join(f'{r:.3f}x' for r in ratios)}")

    if np.allclose(ratios, 2.0, atol=0.02):
        notes.append(
            "!! Every step is exactly 2x. Geometric progression — if this "
            "continues, volume passes 0.9 lots by trade 11. Confirm whether "
            "this is a deliberate scale-up, a martingale, or coincidence.")
    elif np.allclose(v, v[0], atol=1e-9):
        notes.append("Fixed volume — the master is not risk-sizing at all.")
    return notes


# ---------------------------------------------------------------- MAE

def attach_mae(df: pd.DataFrame, m1_path: str, tz_shift: float = 0.0) -> pd.DataFrame:
    """Join M1 OHLC to compute maximum adverse / favourable excursion.
    This is the number no MT5 report contains and every sizing model needs."""
    m1 = pd.read_csv(m1_path)
    m1.columns = [c.strip().lower() for c in m1.columns]
    m1["time"] = pd.to_datetime(m1["time"]) + pd.Timedelta(hours=tz_shift)
    m1 = m1.sort_values("time").set_index("time")

    mae, mfe = [], []
    for _, t in df.iterrows():
        win = m1.loc[t["open_time"]:t["close_time"]]
        if win.empty:
            mae.append(np.nan); mfe.append(np.nan); continue
        if t["type"] == "sell":
            mae.append(win["high"].max() - t["open_price"])
            mfe.append(t["open_price"] - win["low"].min())
        else:
            mae.append(t["open_price"] - win["low"].min())
            mfe.append(win["high"].max() - t["open_price"])
    df = df.copy()
    df["mae_points"] = mae
    df["mfe_points"] = mfe
    return df


# ---------------------------------------------------------------- sizing

def sizing_table(mae_points: float, target_points: float, contract: float,
                 daily_dd: float, buffer_mult: float, comm_per_lot: float,
                 lot_step: float = 0.01,
                 utilisations=(0.10, 0.15, 0.25, 0.50, 0.75)) -> pd.DataFrame:
    """Lot size such that (buffered stop distance x contract x lots) fits inside
    a chosen share of the daily loss allowance."""
    buffered = mae_points * buffer_mult
    risk_per_lot = buffered * contract
    rows = []
    for u in utilisations:
        budget = daily_dd * u
        lots = np.floor(budget / risk_per_lot / lot_step) * lot_step
        rows.append({
            "utilisation": f"{u:.0%}",
            "risk_budget": budget,
            "lots": lots,
            "actual_dd_at_observed_mae": mae_points * contract * lots,
            "loss_if_buffer_hit": buffered * contract * lots,
            "gross_profit": target_points * contract * lots,
            "commission": comm_per_lot * lots,
            "net_profit": target_points * contract * lots - comm_per_lot * lots,
        })
    out = pd.DataFrame(rows)
    out["reward_risk"] = out["net_profit"] / out["loss_if_buffer_hit"]
    return out


def breakeven_winrate(stop_points: float, target_points: float) -> float:
    return stop_points / (stop_points + target_points)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--m1", help="M1 OHLC CSV for MAE computation")
    ap.add_argument("--tz-shift", type=float, default=0.0)
    ap.add_argument("--account", type=float, default=50000)
    ap.add_argument("--daily-dd", type=float, default=2500)
    ap.add_argument("--max-dd", type=float, default=5000)
    ap.add_argument("--buffer", type=float, default=2.0)
    ap.add_argument("--comm-per-lot", type=float, default=10.0)
    ap.add_argument("--mae-override", type=float,
                    help="MAE in points, if measured by hand")
    a = ap.parse_args()

    df, meta = parse_mt5_report(a.report)

    print("=" * 72)
    for k, v in meta.items():
        print(f"{k:>10}: {v}")
    print(f"{'Trades':>10}: {len(df)}   "
          f"{df['open_time'].min()} -> {df['close_time'].max()}")
    print("=" * 72)

    show = df[["open_time", "symbol", "type", "volume", "open_price",
               "close_price", "sl", "tp", "points", "duration_min", "profit"]]
    print("\nPOSITIONS\n" + show.to_string(index=False))

    print("\nINTEGRITY CHECKS")
    issues = integrity_checks(df)
    print("\n".join(f"  [!] {m}" for m in issues) if issues
          else "  All checks passed.")

    print("\nVOLUME BEHAVIOUR")
    for n in volume_behaviour(df):
        print(f"  {n}")

    wins = (df["profit"] > 0).sum()
    print(f"\nPERFORMANCE\n  Win rate: {wins}/{len(df)} "
          f"({wins / max(len(df), 1):.0%})   "
          f"Net: ${df['profit'].sum():.2f}   "
          f"Mean hold: {df['duration_min'].mean():.0f} min")
    print("  Close times: " +
          ", ".join(df["close_time"].dt.strftime("%H:%M:%S")))

    if a.m1:
        df = attach_mae(df, a.m1, a.tz_shift)
        print("\nMAE / MFE (points)")
        print(df[["open_time", "type", "mae_points", "mfe_points",
                  "points"]].to_string(index=False))
        q = df["mae_points"].quantile([0.5, 0.75, 0.95, 1.0])
        print("\n  MAE percentiles: " +
              "  ".join(f"p{int(k*100)}={v:.2f}" for k, v in q.items()))
        mae = q.loc[0.95]
        print(f"  Sizing off p95 MAE = {mae:.2f} pts")
    elif a.mae_override:
        mae = a.mae_override
        print(f"\nMAE not computed (no --m1). Using override: {mae:.2f} pts")
    else:
        print("\nMAE unavailable. Supply --m1 or --mae-override to size.")
        return

    target = df["points"].mean()
    contract = df["contract"].dropna().iloc[0]
    stop = mae * a.buffer

    print(f"\nSIZING  (account ${a.account:,.0f} | daily DD ${a.daily_dd:,.0f} "
          f"| buffer {a.buffer}x | stop {stop:.2f} pts | target {target:.2f} pts)")
    tbl = sizing_table(mae, target, contract, a.daily_dd, a.buffer,
                       a.comm_per_lot)
    print(tbl.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    be = breakeven_winrate(stop, target)
    print(f"\n  Breakeven win rate at this stop/target: {be:.1%}")
    print(f"  Observed win rate: {wins / max(len(df), 1):.1%} on {len(df)} trades")
    for _, r in tbl.iterrows():
        n = a.max_dd / r["loss_if_buffer_hit"] if r["loss_if_buffer_hit"] else 0
        print(f"  At {r['utilisation']:>4} utilisation: "
              f"{n:.1f} full-buffer losses exhaust the ${a.max_dd:,.0f} max DD")


if __name__ == "__main__":
    main()
