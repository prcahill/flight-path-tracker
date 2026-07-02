"""Tolerant flight-log reader.

Ports ``loadFlightData.m``: reads a comma-separated text log, matches columns by
fuzzy header name (with a positional fallback), auto-detects numeric-seconds vs.
ISO datetime timestamps, cleans the data, and returns a :class:`FlightData`.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from .metrics import FlightData, derive_metrics

# Ordered regex alternatives used to locate each logical column by header name.
_PATTERNS: dict[str, list[str]] = {
    "time": [r"^time$", r"timestamp", r"^t$", r"datetime", r"utc"],
    "lat": [r"^latitude$", r"^lat$", r"lat_deg", r"\blat\b"],
    "lon": [r"^longitude$", r"^lon$", r"^lng$", r"^long$", r"lon_deg", r"\blon\b"],
    "alt": [r"altitude_ft", r"^altitude$", r"^alt$", r"alt_ft", r"height", r"elevation"],
    "gs": [r"groundspeed_kt", r"groundspeed", r"ground_speed", r"^gs$", r"^speed$", r"velocity"],
    "hdg": [r"heading_deg", r"^heading$", r"^track$", r"^hdg$", r"course", r"bearing"],
}


def _find_col(columns: list[str], patterns: list[str]) -> int | None:
    """Return the index of the first column whose (lowercased) name matches."""
    low = [c.lower() for c in columns]
    for pat in patterns:
        rx = re.compile(pat)
        for i, name in enumerate(low):
            if rx.search(name):
                return i
    return None


def load_flight_data(path: str | Path) -> FlightData:
    """Load a flight log and return a :class:`FlightData` with derived metrics.

    Only latitude, longitude and altitude are required; ground speed and heading
    are derived if absent.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    df = pd.read_csv(path)
    if df.empty:
        raise ValueError(f"No data rows in {path}")

    cols = list(df.columns.astype(str))
    idx = {key: _find_col(cols, pats) for key, pats in _PATTERNS.items()}

    # Positional fallback when the essentials are not recognizable by name.
    if idx["lat"] is None and idx["lon"] is None and idx["alt"] is None and len(cols) >= 4:
        idx.update(time=0, lat=1, lon=2, alt=3)

    if idx["lat"] is None or idx["lon"] is None or idx["alt"] is None:
        raise ValueError("Could not find latitude, longitude and altitude columns.")

    lat = df.iloc[:, idx["lat"]].to_numpy(dtype=float)
    lon = df.iloc[:, idx["lon"]].to_numpy(dtype=float)
    alt = df.iloc[:, idx["alt"]].to_numpy(dtype=float)

    # Time column: numeric elapsed seconds or an ISO-8601 datetime.
    t0 = None
    if idx["time"] is None:
        t = np.arange(len(df), dtype=float)  # assume 1 Hz
    else:
        col = df.iloc[:, idx["time"]]
        if pd.api.types.is_numeric_dtype(col):
            t = col.to_numpy(dtype=float)
            t = t - t[0]
        else:
            dt = pd.to_datetime(col, errors="coerce", utc=False)
            if dt.isna().all():
                raise ValueError("Could not parse the time column.")
            t0 = dt.iloc[0].to_pydatetime()
            t = (dt - dt.iloc[0]).dt.total_seconds().to_numpy(dtype=float)

    gs = df.iloc[:, idx["gs"]].to_numpy(dtype=float) if idx["gs"] is not None else None
    hdg = df.iloc[:, idx["hdg"]].to_numpy(dtype=float) if idx["hdg"] is not None else None

    # Clean: drop invalid rows, sort by time, drop non-increasing timestamps.
    good = (
        np.isfinite(t) & np.isfinite(lat) & np.isfinite(lon) & np.isfinite(alt)
        & (np.abs(lat) <= 90) & (np.abs(lon) <= 180)
    )
    t, lat, lon, alt = t[good], lat[good], lon[good], alt[good]
    if gs is not None:
        gs = gs[good]
    if hdg is not None:
        hdg = hdg[good]

    order = np.argsort(t, kind="stable")
    t, lat, lon, alt = t[order], lat[order], lon[order], alt[order]
    if gs is not None:
        gs = gs[order]
    if hdg is not None:
        hdg = hdg[order]

    keep = np.concatenate([[True], np.diff(t) > 0])
    t, lat, lon, alt = t[keep], lat[keep], lon[keep], alt[keep]
    if gs is not None:
        gs = gs[keep]
    if hdg is not None:
        hdg = hdg[keep]

    if t.size < 2:
        raise ValueError("Need at least two valid samples after cleaning.")

    return derive_metrics(t, lat, lon, alt, gs, hdg, t0=t0, file=str(path.resolve()))
