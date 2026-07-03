"""Great-circle math, derived flight metrics, and flight-data containers.

This is a direct port of the numeric core of the original MATLAB implementation
(``loadFlightData.m``). All math is vectorized with NumPy and free of any
toolbox / heavy dependency. Units are aviation-standard: feet, knots, nautical
miles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

EARTH_RADIUS_NM = 3440.065  # mean Earth radius in nautical miles

_COMPASS_16 = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


def haversine_nm(
    lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray
) -> np.ndarray:
    """Great-circle distance in nautical miles (vectorized)."""
    p1 = np.radians(lat1)
    p2 = np.radians(lat2)
    dphi = np.radians(np.asarray(lat2) - np.asarray(lat1))
    dlam = np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlam / 2) ** 2
    return EARTH_RADIUS_NM * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


def bearing_deg(
    lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray
) -> np.ndarray:
    """Initial great-circle bearing in degrees ``[0, 360)`` (vectorized)."""
    p1 = np.radians(lat1)
    p2 = np.radians(lat2)
    dlam = np.radians(np.asarray(lon2) - np.asarray(lon1))
    y = np.sin(dlam) * np.cos(p2)
    x = np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dlam)
    return np.mod(np.degrees(np.arctan2(y, x)), 360.0)


def format_duration(seconds: float) -> str:
    """Format a number of seconds as ``HH:MM:SS``."""
    s = max(0, int(round(seconds)))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def compass_point(deg: float) -> str:
    """16-point compass label for a heading in degrees."""
    return _COMPASS_16[int(round(deg / 22.5)) % 16]


@dataclass
class FlightSummary:
    """Aggregate statistics describing a flight."""

    num_points: int
    duration_s: float
    duration: str
    data_rate_hz: float
    distance_nm: float
    max_alt_ft: float
    min_alt_ft: float
    cruise_alt_ft: float
    max_speed_kt: float
    avg_speed_kt: float
    max_climb_fpm: float
    max_descent_fpm: float
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    lat_lim: tuple[float, float]
    lon_lim: tuple[float, float]
    start_time: datetime | None = None


@dataclass
class FlightData:
    """Per-sample flight track plus its summary.

    All arrays are aligned float64 vectors of length ``n``.
    """

    t: np.ndarray            # elapsed seconds from start
    lat: np.ndarray          # decimal degrees
    lon: np.ndarray          # decimal degrees
    alt: np.ndarray          # feet MSL
    gs: np.ndarray           # ground speed, knots
    hdg: np.ndarray          # track/heading, degrees
    vs: np.ndarray           # vertical speed, feet/minute
    cum_nm: np.ndarray       # cumulative great-circle distance, nautical miles
    summary: FlightSummary
    t0: datetime | None = None
    file: str = ""
    # Optional attitude channels (present in e.g. KLV telemetry dumps).
    pitch: np.ndarray | None = None   # degrees, nose up positive
    roll: np.ndarray | None = None    # degrees, right wing down positive
    # Optional sensor-pointing channels (KLV tags 21/23/24). May contain NaN
    # where the dump had not yet reported them.
    slant_ft: np.ndarray | None = None   # slant range to frame center, feet
    fc_lat: np.ndarray | None = None     # frame center latitude, degrees
    fc_lon: np.ndarray | None = None     # frame center longitude, degrees
    n: int = field(init=False)

    def __post_init__(self) -> None:
        self.n = int(self.t.size)


def derive_metrics(
    t: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    alt: np.ndarray,
    gs: np.ndarray | None = None,
    hdg: np.ndarray | None = None,
    *,
    t0: datetime | None = None,
    file: str = "",
) -> FlightData:
    """Compute derived per-sample quantities and the summary.

    Ground speed and heading are derived from position/time when not supplied.
    Assumes ``t`` is strictly increasing and inputs are already cleaned.
    """
    t = np.asarray(t, dtype=float)
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    alt = np.asarray(alt, dtype=float)
    n = t.size
    if n < 2:
        raise ValueError("Need at least two valid samples.")

    step_nm = haversine_nm(lat[:-1], lon[:-1], lat[1:], lon[1:])
    cum_nm = np.concatenate([[0.0], np.cumsum(step_nm)])
    dt_hr = np.diff(t) / 3600.0
    dt_min = np.diff(t) / 60.0

    if gs is None:
        gs = np.empty(n)
        gs[1:] = step_nm / np.maximum(dt_hr, np.finfo(float).eps)
        gs[0] = gs[1]
    else:
        gs = np.asarray(gs, dtype=float)

    if hdg is None:
        hdg = np.empty(n)
        hdg[1:] = bearing_deg(lat[:-1], lon[:-1], lat[1:], lon[1:])
        hdg[0] = hdg[1]
    else:
        hdg = np.asarray(hdg, dtype=float)

    vs = np.empty(n)
    vs[1:] = np.diff(alt) / np.maximum(dt_min, np.finfo(float).eps)
    vs[0] = 0.0

    dur = float(t[-1] - t[0])
    high = alt > 0.8 * alt.max()
    cruise = round(float(np.median(alt[high])) / 100.0) * 100.0 if high.any() else float(alt.max())

    summary = FlightSummary(
        num_points=n,
        duration_s=dur,
        duration=format_duration(dur),
        data_rate_hz=(n - 1) / max(dur, np.finfo(float).eps),
        distance_nm=float(cum_nm[-1]),
        max_alt_ft=float(alt.max()),
        min_alt_ft=float(alt.min()),
        cruise_alt_ft=cruise,
        max_speed_kt=float(gs.max()),
        avg_speed_kt=float(gs.mean()),
        max_climb_fpm=float(vs.max()),
        max_descent_fpm=float(vs.min()),
        start_lat=float(lat[0]),
        start_lon=float(lon[0]),
        end_lat=float(lat[-1]),
        end_lon=float(lon[-1]),
        lat_lim=(float(lat.min()), float(lat.max())),
        lon_lim=(float(lon.min()), float(lon.max())),
        start_time=t0,
    )

    return FlightData(
        t=t, lat=lat, lon=lon, alt=alt, gs=gs, hdg=hdg, vs=vs,
        cum_nm=cum_nm, summary=summary, t0=t0, file=file,
    )
