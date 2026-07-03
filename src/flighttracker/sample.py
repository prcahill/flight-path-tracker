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


# ======================================================================= #
#  KLV frame-text sample (MISB ST 0601-style decoded metadata dump)       #
# ======================================================================= #
_KLV_START = "========== FRAME =========="
_KLV_END = "========= END FRAME ========"


def _klv_line(tag: int, name: str, value: str) -> str:
    return f"({tag:3d}) {name:<25s} : {value}"


def _klv_ts(epoch: float) -> str:
    from datetime import datetime
    dt = datetime.fromtimestamp(epoch)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{int(round(epoch % 1 * 1000)) % 1000:03d}"


def generate_sample_klv_text(
    out_path: str | Path,
    duration_min: float = 25.0,
    rate_hz: float = 10.0,
) -> Path:
    """Write a realistic KLV frame-text telemetry dump (>= 2M characters).

    Simulates a small-UAS surveillance mission (takeoff, transit, two orbit
    loiters, return, landing) and reproduces how real decoded KLV dumps
    behave: a video-rate frame stream in which most frames carry only a
    timestamp, full sensor frames arrive at ~1 Hz, an async airspeed stream
    is interleaved with ~5 s stale timestamps, and occasional duplicate
    timestamps appear.
    """
    out_path = Path(out_path)
    rng = np.random.default_rng(7)

    # ---- 1 Hz mission kinematics ------------------------------------------
    dur_s = int(duration_min * 60)
    t1 = np.arange(dur_s + 1, dtype=float)
    lat0, lon0, ground_m = 33.8434, 131.0317, 15.0
    m_lat = 1.0 / 111_320.0                                  # deg per meter
    m_lon = 1.0 / (111_320.0 * np.cos(np.radians(lat0)))

    x = np.zeros_like(t1)   # meters east
    y = np.zeros_like(t1)   # meters north
    alt_m = np.full_like(t1, ground_m)

    def orbit(tt, t_in, cx, cy, r, omega, phi0):
        ang = phi0 + omega * (tt - t_in)
        return cx + r * np.cos(ang), cy + r * np.sin(ang)

    for i, tt in enumerate(t1):
        if tt < 60:                                   # holding on the strip
            x[i], y[i] = 0.0, 0.0
        elif tt < 300:                                # climb-out + transit NE
            f = (tt - 60) / 240.0
            s = f * f * (3 - 2 * f)                   # smoothstep
            x[i], y[i] = 2100 * s, 1700 * s
        elif tt < 800:                                # orbit A
            x[i], y[i] = orbit(tt, 300, 2100 - 400, 1700, 400, 0.05, 0.0)
        elif tt < 900:                                # transit to orbit B
            f = (tt - 800) / 100.0
            xa, ya = orbit(800, 300, 1700, 1700, 400, 0.05, 0.0)
            x[i], y[i] = xa + (3600 - xa) * f, ya + (1200 - ya) * f
        elif tt < 1300:                               # orbit B
            x[i], y[i] = orbit(tt, 900, 3600 - 350, 1200, 350, -0.06, 0.0)
        elif tt < dur_s - 60:                         # return transit
            f = (tt - 1300) / max(dur_s - 60 - 1300, 1)
            xb, yb = orbit(1300, 900, 3250, 1200, 350, -0.06, 0.0)
            x[i], y[i] = xb * (1 - f), yb * (1 - f)
        else:                                         # approach + landing
            x[i], y[i] = 0.0, 0.0
        # altitude profile
        if tt < 90:
            alt_m[i] = ground_m
        elif tt < 300:
            alt_m[i] = ground_m + 105 * min(1.0, (tt - 90) / 180.0)
        elif tt < dur_s - 120:
            alt_m[i] = ground_m + 105
        else:
            alt_m[i] = ground_m + 105 * max(0.0, (dur_s - 60 - tt) / 60.0)

    x = _box_smooth(x, 9)
    y = _box_smooth(y, 9)
    alt_m = _box_smooth(alt_m, 15) + 0.4 * rng.standard_normal(t1.size)
    lat1 = lat0 + y * m_lat
    lon1 = lon0 + x * m_lon

    # attitude + speeds from the kinematics
    vx, vy = np.gradient(x, t1), np.gradient(y, t1)
    spd_ms = np.hypot(vx, vy)
    hdg1 = np.degrees(np.arctan2(vx, vy)) % 360.0
    hdg_rate = np.gradient(np.unwrap(np.radians(hdg1)), t1)
    roll1 = np.degrees(np.arctan2(spd_ms * hdg_rate, 9.81))
    roll1 = np.clip(_box_smooth(roll1, 7), -35, 35) + 0.3 * rng.standard_normal(t1.size)
    pitch1 = np.clip(np.degrees(np.arctan2(np.gradient(alt_m, t1),
                                           np.maximum(spd_ms, 1.0))), -15, 15)
    pitch1 = _box_smooth(pitch1, 7) + 0.3 * rng.standard_normal(t1.size)

    # ---- emit the 10 Hz frame stream --------------------------------------
    from datetime import datetime
    start_epoch = datetime(2026, 6, 15, 6, 20, 0).timestamp()
    dt_tick = 1.0 / rate_hz
    n_ticks = int(dur_s * rate_hz)
    out: list[str] = []

    def frame(lines: list[str]) -> None:
        out.append(_KLV_START)
        out.extend(lines)
        out.append(_KLV_END)
        out.append("")

    for k in range(n_ticks + 1):
        epoch = start_epoch + k * dt_tick
        ts_line = _klv_line(2, "Unix Time Stamp", _klv_ts(epoch))
        sub = k % int(rate_hz)
        i = min(k // int(rate_hz), dur_s)             # 1 Hz sample index

        if sub == 0:                                   # full sensor frame
            lines = [
                ts_line,
                _klv_line(5, "Platform Heading Angle", f"{hdg1[i]:.1f} deg"),
                _klv_line(6, "Platform Pitch Angle", f"{pitch1[i]:.1f} deg"),
                _klv_line(7, "Platform Roll Angle", f"{roll1[i]:.1f} deg"),
                _klv_line(13, "Sensor Latitude", f"{lat1[i]:.6f} deg"),
                _klv_line(14, "Sensor Longitude", f"{lon1[i]:.6f} deg"),
                _klv_line(15, "Sensor True Altitude", f"{alt_m[i]:.1f} m"),
                _klv_line(16, "Sensor Horizontal FOV", "31.53 deg"),
                _klv_line(17, "Sensor Vertical FOV", "17.74 deg"),
                _klv_line(18, "Sensor Relative Azimuth",
                          f"{(360 - 0.05 * (i % 100)) % 360:.2f} deg"),
                _klv_line(19, "Sensor Relative Elevation", f"{0.05 * (i % 3):.2f} deg"),
                _klv_line(20, "Sensor Relative Roll", f"{-0.01 * (i % 4):.2f} deg"),
            ]
            if i % 10 == 0:                            # extended frame
                slant = 550 + 50 * np.sin(i / 30)
                lines += [
                    _klv_line(21, "Slant Range", f"{slant:.1f} m"),
                    _klv_line(22, "Target Width", f"{slant * 0.558:.1f} m"),
                    _klv_line(23, "Frame Center Latitude", f"{lat1[i] + 0.0011:.6f} deg"),
                    _klv_line(24, "Frame Center Longitude", f"{lon1[i] + 0.0063:.6f} deg"),
                    _klv_line(25, "Frame Center Elevation", "4.6 m"),
                    _klv_line(40, "Platform Roll Rate", "67.72 deg/s"),
                    _klv_line(41, "Platform Yaw Rate", "131.04 deg/s"),
                ]
            lines.append(_klv_line(65, "Platform Ground Speed", "0f"))
            frame(lines)
        elif sub == 7:                                 # async airspeed stream
            lag_epoch = epoch - 5.1
            j = min(max(int(lag_epoch - start_epoch), 0), dur_s)
            frame([
                _klv_line(2, "Unix Time Stamp", _klv_ts(lag_epoch)),
                _klv_line(8, "Platform True Airspeed", f"{spd_ms[j] * 3.6:.1f} km/h"),
            ])
        else:                                          # timestamp-only frame
            frame([ts_line])
            if k % 37 == 5:                            # duplicate-ts quirk
                frame([ts_line])

    text = "\n".join(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")

    resolved = out_path.resolve()
    print(f"Wrote {resolved}")
    print(f"  frames: ~{n_ticks:,}   |   duration: {duration_min:g} min   |   "
          f"characters: {len(text):,}")
    print(f"  position frames (1 Hz): {dur_s + 1:,}   |   file size: "
          f"{resolved.stat().st_size / 1e6:.2f} MB")
    return resolved
