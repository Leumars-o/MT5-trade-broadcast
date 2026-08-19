"""Generate a synthetic MT5 "Trade History Report" .xlsx in the exact block
layout that ``ReplayFeed`` / ``mt5_risk_audit.py`` parse.

The real ``ReportHistory-51104542.xlsx`` is a private account export and is not
committed. This helper produces an equivalent fixture so the suite is
self-contained. Tests can also call ``write_report`` with custom rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from openpyxl import Workbook

# Column order of the MT5 Positions block (14 columns, index 0..13).
_HEADER = [
    "Time", "Position", "Symbol", "Type", "Volume", "Price",
    "S / L", "T / P", "Time", "Price", "Commission", "Swap", "Profit", "",
]

# Each row: (open_time, position_id, symbol, type, volume, open_price,
#            sl, tp, close_time, close_price, commission, swap, profit)
PositionRow = Sequence[object]

_DEFAULT_ROWS: list[PositionRow] = [
    # A small doubling pair, then a big scalp, then a still-open position.
    ("2025.08.18 09:00:00", 96484060, "XAUUSD.f", "sell", 0.000899, 4399.36,
     0, 0, "2025.08.18 09:30:00", 4392.34, -0.09, 0.0, 6.31),
    ("2025.08.18 09:15:00", 96484061, "XAUUSD.f", "sell", 0.001798, 4401.10,
     0, 0, "2025.08.18 10:00:00", 4396.00, -0.18, 0.0, 9.17),
    ("2025.08.18 10:05:00", 96484066, "XAUUSD.f", "sell", 0.40, 4399.36,
     0, 0, "2025.08.18 12:19:00", 4392.34, -4.00, 0.0, 280.80),
    # No close_time / close_price → still open at end of report.
    ("2025.08.18 11:00:00", 96484070, "XAUUSD.f", "buy", 0.01, 4390.00,
     0, 0, "", "", -0.10, 0.0, ""),
]


def write_report(path: str | Path, rows: Sequence[PositionRow] = _DEFAULT_ROWS) -> Path:
    """Write an MT5-style report workbook with a Positions block and an Orders
    terminator, returning the path."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"

    # Report metadata block (parsed by the audit script; ignored by ReplayFeed).
    ws.append(["Name:", "", "", "Demo Master"])
    ws.append(["Account:", "", "", "51104542"])
    ws.append(["Company:", "", "", "Example Broker"])
    ws.append([])

    ws.append(["Positions"])
    ws.append(_HEADER)
    for r in rows:
        ws.append(list(r) + [""] * (14 - len(list(r))))

    # Block terminator: the parser stops at the next 'Orders'/'Deals' label.
    ws.append([])
    ws.append(["Orders"])
    ws.append(_HEADER)

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


if __name__ == "__main__":
    dest = Path(__file__).with_name("ReportHistory-51104542.xlsx")
    write_report(dest)
    print(f"wrote {dest}")
