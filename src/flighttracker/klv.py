"""Parser for frame-delimited KLV metadata dumps (MISB ST 0601-style).

Handles telemetry stored as human-readable decoded KLV frames::

    ========== FRAME ==========
    (  2) Unix Time Stamp          : 2026-06-15 06:21:01.678
    (  5) Platform Heading Angle   : 80.3 deg
    ( 13) Sensor Latitude          : 33.843407 deg
    ( 14) Sensor Longitude         : 131.031684 deg
    ( 15) Sensor True Altitude     : 15.2 m
    ========= END FRAME ========

Real dumps of this kind are sparse and messy, and the parser is built for
that reality:

- most frames carry only a timestamp (video-rate KLV); full sensor frames
  arrive at a lower rate -- a track point is emitted only for frames that
  contain both latitude and longitude;
- interleaved async streams mean timestamps arrive out of order and
  duplicated -- records are sorted and de-duplicated afterwards;
- fields are identified by their numeric tag (robust against label
  variations); unknown tags are ignored;
- values missing from a frame (altitude, attitude) are forward-filled from
  the most recent frame that carried them, in file order;
- metric units are converted to aviation units (meters -> feet).

Ground speed and track are derived from positions by the shared metrics
pipeline rather than trusted from the sparse (and often stale) airspeed
fields.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .metrics import FlightData, derive_metrics

M_TO_FT = 3.280839895

# MISB ST 0601 tags used.
_TAG_TIME = 2
_TAG_HEADING = 5
_TAG_PITCH = 6
_TAG_ROLL = 7
_TAG_LAT = 13
_TAG_LON = 14
_TAG_ALT = 15

_FRAME_SPLIT_RE = re.compile(r"^=+\s*FRAME\s*=+\s*$", re.MULTILINE)
_LINE_RE = re.compile(r"^\(\s*(\d+)\)\s*[^:]*:\s*(.+?)\s*$")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

# Used (on a small head sample) to decide whether a text file is this format.
SNIFF_RE = re.compile(r"=+\s*FRAME\s*=+")


def _parse_time(value: str) -> float | None:
    """Parse ``2026-06-15 06:21:01.678`` into epoch seconds (UTC per MISB)."""
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _num(fields: dict[int, str], tag: int) -> float:
    """First numeric token of a tag's value, or NaN if absent."""
    raw = fields.get(tag)
    if raw is None:
        return np.nan
    m = _NUM_RE.search(raw)
    return float(m.group(0)) if m else np.nan


def load_klv_text(path: str | Path) -> FlightData:
    """Parse a KLV frame-text dump into a :class:`FlightData`."""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")

    records: list[tuple[float, float, float, float, float, float, float]] = []
    # Forward-filled state (file order): alt_ft, heading, pitch, roll.
    last = {"alt": np.nan, "hdg": np.nan, "pitch": np.nan, "roll": np.nan}

    for block in _FRAME_SPLIT_RE.split(text):
        fields: dict[int, str] = {}
        for line in block.splitlines():
            m = _LINE_RE.match(line.strip())
            if m:
                fields[int(m.group(1))] = m.group(2)

        if (alt_m := _num(fields, _TAG_ALT)) == alt_m:  # not NaN
            last["alt"] = alt_m * M_TO_FT
        if (hdg := _num(fields, _TAG_HEADING)) == hdg:
            last["hdg"] = hdg
        if (pitch := _num(fields, _TAG_PITCH)) == pitch:
            last["pitch"] = pitch
        if (roll := _num(fields, _TAG_ROLL)) == roll:
            last["roll"] = roll

        lat, lon = _num(fields, _TAG_LAT), _num(fields, _TAG_LON)
        if np.isnan(lat) or np.isnan(lon):
            continue
        t_raw = fields.get(_TAG_TIME)
        epoch = _parse_time(t_raw) if t_raw else None
        if epoch is None:
            continue
        records.append((epoch, lat, lon, last["alt"],
                        last["hdg"], last["pitch"], last["roll"]))

    if len(records) < 2:
        raise ValueError(
            f"Found {len(records)} position frame(s) in {path.name}; "
            "need at least two frames containing latitude and longitude.")

    arr = np.array(records, dtype=float)
    epoch, lat, lon, alt = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3]
    hdg, pitch, roll = arr[:, 4], arr[:, 5], arr[:, 6]

    # Clean: valid positions, sort by time, drop non-increasing timestamps.
    good = (np.isfinite(lat) & np.isfinite(lon) & np.isfinite(alt)
            & (np.abs(lat) <= 90) & (np.abs(lon) <= 180))
    epoch, lat, lon, alt = epoch[good], lat[good], lon[good], alt[good]
    hdg, pitch, roll = hdg[good], pitch[good], roll[good]

    order = np.argsort(epoch, kind="stable")
    epoch, lat, lon, alt = epoch[order], lat[order], lon[order], alt[order]
    hdg, pitch, roll = hdg[order], pitch[order], roll[order]

    keep = np.concatenate([[True], np.diff(epoch) > 0])
    epoch, lat, lon, alt = epoch[keep], lat[keep], lon[keep], alt[keep]
    hdg, pitch, roll = hdg[keep], pitch[keep], roll[keep]

    if epoch.size < 2:
        raise ValueError("Need at least two valid samples after cleaning.")

    t = epoch - epoch[0]
    t0 = datetime.fromtimestamp(epoch[0], tz=timezone.utc)
    # Heading only if the dump actually carried it; speed is always derived.
    hdg_in = hdg if np.isfinite(hdg).any() else None
    if hdg_in is not None:
        hdg_in = np.nan_to_num(hdg_in, nan=0.0)

    fd = derive_metrics(t, lat, lon, alt, gs=None, hdg=hdg_in,
                        t0=t0, file=str(path.resolve()))
    if np.isfinite(pitch).any():
        fd.pitch = np.nan_to_num(pitch, nan=0.0)
    if np.isfinite(roll).any():
        fd.roll = np.nan_to_num(roll, nan=0.0)
    return fd
