"""Compliance guard (ARCHITECTURE.md §10.1): this process must never contain
order-placement code. Grep the source for the MetaApi trading surface and fail
if any of it appears. This is a hard boundary, not a style check.
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "copier"

# MetaApi RpcConnection / trade helpers that write to an account. If any of these
# tokens shows up in source, the read-only guarantee is broken.
FORBIDDEN = [
    "create_market_buy_order",
    "create_market_sell_order",
    "create_limit_buy_order",
    "create_limit_sell_order",
    "create_stop_buy_order",
    "create_stop_sell_order",
    "create_stop_limit_buy_order",
    "create_stop_limit_sell_order",
    "modify_position",
    "close_position",
    "close_position_partially",
    "close_positions_by_symbol",
    "modify_order",
    "cancel_order",
    ".trade(",
]


def test_source_contains_no_order_placement_calls():
    offenders: list[str] = []
    for py in SRC.rglob("*.py"):
        text = py.read_text()
        for token in FORBIDDEN:
            if token in text:
                offenders.append(f"{py.relative_to(SRC.parent.parent)}: {token!r}")
    assert not offenders, "Order-placement API found in source:\n" + "\n".join(offenders)
