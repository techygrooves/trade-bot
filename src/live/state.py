"""Crash-safe persistence of the open position.

The live engine writes its position to disk after every change so a restart
(or an ephemeral cloud container coming back) resumes with an accurate view of
what is held — never blindly re-buying.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from src.config import PROJECT_ROOT

STATE_DIR = PROJECT_ROOT / "state"


@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float
    stop_price: float
    take_profit: float
    entry_time: str


def _path(symbol: str) -> Path:
    return STATE_DIR / f"position_{symbol}.json"


def load_position(symbol: str) -> Position | None:
    path = _path(symbol)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return Position(**data)


def save_position(pos: Position) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    _path(pos.symbol).write_text(json.dumps(asdict(pos), indent=2))


def clear_position(symbol: str) -> None:
    _path(symbol).unlink(missing_ok=True)
