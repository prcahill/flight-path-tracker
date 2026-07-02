"""Realistic sample flight-log generator (KSFO -> KLAX).

Ports ``generateSampleFlightData.m``. Simulates a full flight profile (taxi,
climb, cruise at FL350, descent, approach, landing) along a great-circle route
and writes it to a CSV text file. Uses NumPy only (no SciPy): profiles are
piecewise-linear with light box smoothing, and noise is low-frequency and
ramped in by a continuous altitude weight so that derived speed and vertical
rates stay physically sensible even at high sample rates.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .metrics import bearing_deg, haversine_nm

_EPS = np.finfo(float).eps

# San Francisco (KSFO) -> Los Angeles (KLAX) with a gentle coastal dog-leg.
WAYPOINTS = np.array([
    [37.6188, -122.3750],   # KSFO
    [37.3600, -121.9300],   # south of the Bay
    [35.6000, -120.6000],   # central coast
    [34.9000, -119.8000],   # approaching the LA basin
    [33.9425, -118.4081],   # KLAX
])


def _ll2vec(lat: float, lon: float) -> np.ndarray:
    lat_r, lon_r = np.radians(lat), np.radians(lon)
    return np.array([
        np.cos(lat_r) * np.cos(lon_r),
        np.cos(lat_r) * np.sin(lon_r),
        np.sin(lat_r),
    ])


def _vec2ll(v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    v = v / np.linalg.norm(v, axis=1, keepdims=True)
    return np.degrees(np.arcsin(v[:, 2])), np.degrees(np.arctan2(v[:, 1], v[:, 0]))


def _gc_interp(lat1, lon1, lat2, lon2, n):
    """``n`` points along the great circle from p1 to p2 (endpoints included)."""
    v1, v2 = _ll2vec(lat1, lon1), _ll2vec(lat2, lon2)
    omega = np.arccos(np.clip(np.dot(v1, v2), -1.0, 1.0))
    f = np.linspace(0.0, 1.0, n)
    if omega < 1e-9:
        v = np.tile(v1, (n, 1))
    else:
        s1 = np.sin((1 - f) * omega) / np.sin(omega)
        s2 = np.sin(f * omega) / np.sin(omega)
        v = np.outer(s1, v1) + np.outer(s2, v2)
    return _vec2ll(v)


def _box_smooth(x: np.ndarray, win: int) -> np.ndarray:
    """O(n) moving-average smoothing with edge padding."""
    x = np.asarray(x, dtype=float)
    win = max(1, int(win))
    if win == 1 or x.size <= 1:
        return x
    pad = win // 2
    xp = np.pad(x, pad, mode="edge")
    csum = np.cumsum(np.insert(xp, 0, 0.0))
    ma = (csum[win:] - csum[:-win]) / win
    return ma[: x.size]


def _cumtrapz(y: np.ndarray, x: np.ndarray) -> np.ndarray:
    out = np.zeros_like(y, dtype=float)
    out[1:] = np.cumsum((y[1:] + y[:-1]) / 2.0 * np.diff(x))
    return out


def _smooth_noise(t: np.ndarray, corr_sec: float, amp: float, rng) -> np.ndarray:
    """Low-frequency noise of amplitude ``amp``, correlated over ``corr_sec``.

    Random values on a coarse grid are linearly interpolated to ``t`` so the
    result is continuous with a bounded time-derivative (no per-sample spikes).
    """
    span = max(t[-1] - t[0], _EPS)
    nk = max(2, int(np.ceil(span / corr_sec)) + 1)
    tk = np.linspace(t[0], t[-1], nk)
    yk = amp * rng.standard_normal(nk)
    return np.interp(t, tk, yk)


def generate_sample_flight_data(
    out_path: str | Path,
    duration_min: float = 60.0,
    rate_hz: float = 5.0,
) -> Path:
    """Write a realistic KSFO->KLAX flight log to ``out_path`` (CSV).

    Crank ``rate_hz`` / ``duration_min`` up to produce millions of rows for
    stress testing, e.g. ``generate_sample_flight_data("big.csv", 120, 100)``.
    Returns the resolved output path.
    """
    out_path = Path(out_path)
    wp = WAYPOINTS

    # Dense great-circle reference path through the waypoints.
    path_lat = [wp[0, 0]]
    path_lon = [wp[0, 1]]
    for k in range(len(wp) - 1):
        seg = float(haversine_nm(wp[k, 0], wp[k, 1], wp[k + 1, 0], wp[k + 1, 1]))
        npts = max(2, int(round(seg * 2)))  # ~2 points per nm
        la, lo = _gc_interp(wp[k, 0], wp[k, 1], wp[k + 1, 0], wp[k + 1, 1], npts)
        path_lat.extend(la[1:])
        path_lon.extend(lo[1:])
    path_lat = np.array(path_lat)
    path_lon = np.array(path_lon)

    seg_nm = haversine_nm(path_lat[:-1], path_lon[:-1], path_lat[1:], path_lon[1:])
    path_dist = np.concatenate([[0.0], np.cumsum(seg_nm)])
    total_nm = float(path_dist[-1])

    # Time base.
    n = int(round(duration_min * 60 * rate_hz)) + 1
    t = np.arange(n) / rate_hz
    frac = t / t[-1]

    # Relative ground-speed profile -> progress along the path.
    f_spd = np.array([0.00, 0.02, 0.05, 0.20, 0.25, 0.70, 0.80, 0.93, 0.98, 1.00])
    v_spd = np.array([0.00, 0.35, 0.55, 0.75, 1.00, 1.00, 0.80, 0.45, 0.20, 0.00])
    vrel = _box_smooth(np.clip(np.interp(frac, f_spd, v_spd), 0.0, None), int(rate_hz * 3))
    prog = _cumtrapz(vrel, frac)
    prog = prog / prog[-1]
    s_along = prog * total_nm
    lat = np.interp(s_along, path_dist, path_lat)
    lon = np.interp(s_along, path_dist, path_lon)

    # Altitude profile (ft) with realistic phases.
    f_alt = np.array([0.00, 0.03, 0.06, 0.22, 0.25, 0.70, 0.80, 0.92, 0.98, 1.00])
    v_alt = np.array([13, 13, 3000, 35000, 35000, 35000, 24000, 8000, 500, 126], dtype=float)
    alt = _box_smooth(np.interp(frac, f_alt, v_alt), int(rate_hz * 5))

    # Low-frequency noise, ramped in with a continuous altitude weight so it
    # never introduces a discontinuity (and thus a velocity spike) at
    # takeoff / landing.
    rng = np.random.default_rng(42)
    w = np.clip((alt - 200.0) / 2000.0, 0.0, 1.0)  # 0 on ground -> 1 above ~2200 ft
    alt = alt + w * _smooth_noise(t, 20.0, 40.0, rng)
    lat = lat + w * _smooth_noise(t, 60.0, 0.002, rng)
    lon = lon + w * _smooth_noise(t, 60.0, 0.002, rng)
    alt = np.maximum(alt, 0.0)

    # Derived ground speed (kt) and heading (deg) from the final positions.
    step_nm = haversine_nm(lat[:-1], lon[:-1], lat[1:], lon[1:])
    dt_hr = np.diff(t) / 3600.0
    gs = np.empty(n)
    gs[1:] = step_nm / np.maximum(dt_hr, _EPS)
    gs[0] = gs[1]
    hdg = np.empty(n)
    hdg[1:] = bearing_deg(lat[:-1], lon[:-1], lat[1:], lon[1:])
    hdg[0] = hdg[1]

    df = pd.DataFrame({
        "time": t,
        "latitude": lat,
        "longitude": lon,
        "altitude_ft": alt,
        "groundspeed_kt": gs,
        "heading_deg": hdg,
    })
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    resolved = out_path.resolve()
    size_mb = resolved.stat().st_size / 1e6
    print(f"Wrote {resolved}")
    print(f"  rows: {n}   |   duration: {duration_min:g} min   |   rate: {rate_hz:g} Hz")
    print(f"  route length: {total_nm:.1f} nm   |   file size: {size_mb:.2f} MB")
    return resolved
