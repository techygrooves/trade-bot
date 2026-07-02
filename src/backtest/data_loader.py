"""Historical OHLCV loading for backtests.

Two sources:
  * load_csv: any CSV with open_time/open/high/low/close/volume.
  * load_binance_vision: Binance's public monthly kline dumps from
    https://data.binance.vision (no API key, full history). This is the
    recommended source for backtesting from the cloud environment — it requires
    the host `data.binance.vision` to be on the network egress allowlist.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from urllib.request import urlopen

import pandas as pd

_TREND_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}

_VISION_URL = (
    "https://data.binance.vision/data/spot/monthly/klines/"
    "{symbol}/{interval}/{symbol}-{interval}-{month}.zip"
)

# Column layout of Binance kline CSVs (no header row).
_KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_base", "taker_quote", "ignore",
]


def _to_datetime(col: pd.Series) -> pd.Series:
    """Parse epoch timestamps, auto-detecting s / ms / us.

    Binance dumps have historically used milliseconds but newer ones use
    microseconds; detect by magnitude so either works.
    """
    if pd.api.types.is_numeric_dtype(col):
        magnitude = float(col.dropna().abs().median())
        if magnitude < 1e12:
            unit = "s"
        elif magnitude < 1e15:
            unit = "ms"
        else:
            unit = "us"
        return pd.to_datetime(col, unit=unit, utc=True)
    return pd.to_datetime(col, utc=True)


def _finalize(df: pd.DataFrame) -> pd.DataFrame:
    numeric = ["open", "high", "low", "close", "volume"]
    df[numeric] = df[numeric].astype(float)
    if "quote_volume" in df.columns:
        df["quote_volume"] = df["quote_volume"].astype(float)
    df["open_time"] = _to_datetime(df["open_time"])
    if "close_time" in df.columns:
        df["close_time"] = _to_datetime(df["close_time"])
    df = df.set_index("open_time").sort_index()
    keep = [c for c in ["open", "high", "low", "close", "volume", "quote_volume", "close_time"] if c in df.columns]
    return df[keep]


def resample_trend(signal_df: pd.DataFrame, trend_interval: str) -> pd.DataFrame:
    """Build the higher-timeframe trend frame by resampling the signal frame."""
    return signal_df.resample(trend_interval).agg(_TREND_AGG).dropna()


def save_csv(df: pd.DataFrame, path: str | Path) -> None:
    """Write OHLCV to a CSV that `load_csv` reads back losslessly."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.reset_index()
    out["open_time"] = out["open_time"].astype("int64") // 1_000_000  # ns -> ms
    cols = [c for c in ["open_time", "open", "high", "low", "close", "volume"] if c in out.columns]
    out[cols].to_csv(path, index=False)


def load_csv(path: str | Path) -> pd.DataFrame:
    """Load OHLCV from a CSV. Auto-detects whether a header is present."""
    path = Path(path)
    head = pd.read_csv(path, nrows=1, header=None)
    has_header = str(head.iloc[0, 0]).strip().lower() in {"open_time", "opentime", "time", "date"}
    if has_header:
        df = pd.read_csv(path)
        df = df.rename(columns={"time": "open_time", "date": "open_time"})
    else:
        df = pd.read_csv(path, header=None, names=_KLINE_COLUMNS)
    return _finalize(df)


def _months(start: str, end: str) -> list[str]:
    rng = pd.period_range(start=start, end=end, freq="M")
    return [p.strftime("%Y-%m") for p in rng]


def load_binance_vision(
    symbol: str, interval: str, start_month: str, end_month: str
) -> pd.DataFrame:
    """Download and concatenate monthly kline dumps from data.binance.vision.

    `start_month`/`end_month` are "YYYY-MM". Requires network access to
    data.binance.vision (allowlist the host in the environment's egress policy).
    """
    frames = []
    for month in _months(start_month, end_month):
        url = _VISION_URL.format(symbol=symbol.upper(), interval=interval, month=month)
        with urlopen(url, timeout=60) as resp:  # noqa: S310 - fixed, trusted host
            payload = resp.read()
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            name = zf.namelist()[0]
            with zf.open(name) as fh:
                first = fh.readline().decode().split(",")[0].strip().lower()
            with zf.open(name) as fh:
                if first in {"open_time", "opentime"}:
                    part = pd.read_csv(fh)
                else:
                    part = pd.read_csv(fh, header=None, names=_KLINE_COLUMNS)
        frames.append(part)
    combined = pd.concat(frames, ignore_index=True)
    return _finalize(combined)
