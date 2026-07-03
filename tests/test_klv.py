"""Tests for the KLV frame-text parser and sample generator."""

from __future__ import annotations

import numpy as np
import pytest

from flighttracker.data import load_flight_data
from flighttracker.klv import M_TO_FT, load_klv_text
from flighttracker.sample import generate_sample_klv_text

# A literal snippet reproducing the real-world quirks: sparse frames,
# an out-of-order stale airspeed stream, and duplicate timestamps.
SNIPPET = """\
========== FRAME ==========
(  2) Unix Time Stamp           : 2026-06-15 06:21:01.678
(  5) Platform Heading Angle    : 80.3 deg
(  6) Platform Pitch Angle      : -7.8 deg
(  7) Platform Roll Angle       : -2.6 deg
( 13) Sensor Latitude           : 33.843407 deg
( 14) Sensor Longitude          : 131.031684 deg
( 15) Sensor True Altitude      : 15.2 m
( 16) Sensor Horizontal FOV     : 31.53 deg
( 65) Platform Ground Speed     : 0f
========= END FRAME ========

========== FRAME ==========
(  2) Unix Time Stamp           : 2026-06-15 06:21:02.678
========= END FRAME ========

========== FRAME ==========
(  2) Unix Time Stamp           : 2026-06-15 06:20:57.501
(  8) Platform True Airspeed    : 18.0 km/h
========= END FRAME ========

========== FRAME ==========
(  2) Unix Time Stamp           : 2026-06-15 06:21:02.678
========= END FRAME ========

========== FRAME ==========
(  2) Unix Time Stamp           : 2026-06-15 06:21:02.781
( 13) Sensor Latitude           : 33.843400 deg
( 14) Sensor Longitude          : 131.031734 deg
( 15) Sensor True Altitude      : 12.8 m
( 25) Frame Center Elevation    : 4.6 m
========= END FRAME ========

========== FRAME ==========
(  2) Unix Time Stamp           : 2026-06-15 06:21:03.781
(  5) Platform Heading Angle    : 78.5 deg
(  6) Platform Pitch Angle      : -8.2 deg
(  7) Platform Roll Angle       : -0.6 deg
( 13) Sensor Latitude           : 33.843392 deg
( 14) Sensor Longitude          : 131.031791 deg
( 15) Sensor True Altitude      : 13.4 m
( 21) Slant Range               : 593.4 m
( 22) Target Width              : 331.0 m
========= END FRAME ========
"""


@pytest.fixture
def snippet_file(tmp_path):
    p = tmp_path / "telemetry.txt"
    p.write_text(SNIPPET)
    return p


def test_parses_position_frames_only(snippet_file):
    fd = load_klv_text(snippet_file)
    # 3 frames contain lat/lon; timestamp-only and airspeed frames are skipped.
    assert fd.n == 3


def test_units_converted_and_time_base(snippet_file):
    fd = load_klv_text(snippet_file)
    assert fd.alt[0] == pytest.approx(15.2 * M_TO_FT, abs=0.1)   # meters -> feet
    assert fd.t[0] == 0.0
    assert fd.t[-1] == pytest.approx(2.103, abs=0.01)
    assert fd.t0 is not None and fd.t0.year == 2026
    assert np.all(np.diff(fd.t) > 0)                              # sorted, unique


def test_attitude_forward_filled(snippet_file):
    fd = load_klv_text(snippet_file)
    assert fd.pitch is not None and fd.roll is not None
    # Second position frame had no attitude; carries the first frame's values.
    assert fd.pitch[1] == pytest.approx(-7.8)
    # Third frame carries its own.
    assert fd.pitch[2] == pytest.approx(-8.2)
    # Heading came from the dump (tag 5), not derived.
    assert fd.hdg[0] == pytest.approx(80.3)


def test_load_flight_data_sniffs_klv(snippet_file):
    fd = load_flight_data(snippet_file)    # same entry point as CSV
    assert fd.n == 3
    assert fd.pitch is not None


def test_generator_size_and_roundtrip(tmp_path):
    out = generate_sample_klv_text(tmp_path / "uav.txt", duration_min=25, rate_hz=10)
    text = out.read_text()
    assert len(text) >= 2_000_000                    # required scale
    fd = load_flight_data(out)
    assert fd.n > 1000                               # ~1 Hz position frames
    assert fd.summary.duration_s == pytest.approx(25 * 60, rel=0.02)
    assert fd.pitch is not None and fd.roll is not None
    # UAV mission: low altitude (~120 m AGL), modest speeds.
    assert 300 < fd.summary.max_alt_ft < 600
    assert fd.summary.max_speed_kt < 80
    # Orbits: roll should show sustained bank in both directions.
    assert fd.roll.max() > 5 and fd.roll.min() < -5
