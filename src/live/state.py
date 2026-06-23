"""Crash-safe persistence of the open position.

The live engine writes its position to disk after every change so a restart
(or an ephemeral cloud container coming back) resumes with an accurate view of
what is held — never blindly re-buying.
"""
from __future__ import annotations

import json
import dataclasses
from dataclasses import asdict, dataclass
from pathlib import Path

from src.config import PROJECT_ROOT

STATE_DIR = PROJECT_ROOT / "state"


@dataclass
class Position:
    """An open long position and the live state of its exit bracket.

    `qty` is the base currently held; `initial_qty` is the size at entry (so the
    scaled take-profit fractions are measured against the original position). The
    bracket fields (`stop_price`, `levels_hit`, `high_water`, `trailing_active`)
    are persisted so trailing / scale-out progress survives a restart.
    """

    symbol: str
    qty: float
    entry_price: float
    initial_stop: float
    stop_price: float
    entry_time: str
    initial_qty: float = 0.0
    risk_per_unit: float = 0.0
    high_water: float = 0.0
    levels_hit: int = 0
    trailing_active: bool = False

    def __post_init__(self) -> None:
        # Backfill derived fields for freshly constructed / legacy state files.
        if self.initial_qty <= 0:
            self.initial_qty = self.qty
        if self.risk_per_unit <= 0:
            self.risk_per_unit = self.entry_price - self.initial_stop
        if self.high_water <= 0:
            self.high_water = self.entry_price


def _path(symbol: str) -> Path:
    return STATE_DIR / f"position_{symbol}.json"


def load_position(symbol: str) -> Position | None:
    path = _path(symbol)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    # Tolerate legacy/extra keys (e.g. an older "take_profit" field).
    fields = {f.name for f in dataclasses.fields(Position)}
    return Position(**{k: v for k, v in data.items() if k in fields})


def save_position(pos: Position) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    _path(pos.symbol).write_text(json.dumps(asdict(pos), indent=2))


def clear_position(symbol: str) -> None:
    _path(symbol).unlink(missing_ok=True)
