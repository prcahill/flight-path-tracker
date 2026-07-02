"""Tests for the tolerant flight-log loader."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from flighttracker.data import load_flight_data


def _write(path, df):
    df.to_csv(path, index=False)
    return path


def test_canonical_columns(tmp_path):
    p = _write(tmp_path / "f.csv", pd.DataFrame({
        "time": [0, 1, 2, 3],
        "latitude": [37.0, 37.1, 37.2, 37.3],
        "longitude": [-122.0, -122.1, -122.2, -122.3],
        "altitude_ft": [0, 1000, 2000, 3000],
        "groundspeed_kt": [0, 300, 320, 310],
        "heading_deg": [0, 45, 46, 47],
    }))
    fd = load_flight_data(p)
    assert fd.n == 4
    # provided speed/heading are used as-is
    assert fd.gs[1] == 300
    assert fd.hdg[1] == 45


def test_fuzzy_headers_and_derivation(tmp_path):
    # Odd header names, and no speed/heading -> must be matched and derived.
    p = _write(tmp_path / "f.csv", pd.DataFrame({
        "T": [0, 1, 2],
        "Lat": [0.0, 0.0, 0.0],
        "LONG": [0.0, 0.05, 0.10],
        "Altitude": [10000, 10000, 10000],
    }))
    fd = load_flight_data(p)
    assert fd.n == 3
    assert np.all(fd.gs[1:] > 0)              # derived ground speed
    assert abs(fd.hdg[-1] - 90.0) < 1.0        # derived heading (due east)


def test_positional_fallback(tmp_path):
    p = _write(tmp_path / "f.csv", pd.DataFrame({
        "a": [0, 1, 2],
        "b": [37.0, 37.1, 37.2],
        "c": [-122.0, -122.1, -122.2],
        "d": [0, 1000, 2000],
    }))
    fd = load_flight_data(p)
    assert fd.n == 3
    assert fd.summary.max_alt_ft == 2000


def test_cleaning_sorts_and_drops(tmp_path):
    p = _write(tmp_path / "f.csv", pd.DataFrame({
        "time": [2, 0, 1, 1, 3],           # unsorted + duplicate timestamp
        "latitude": [37.2, 37.0, 37.1, 99.0, 37.3],  # one out-of-range lat
        "longitude": [-122.2, -122.0, -122.1, -122.15, -122.3],
        "altitude_ft": [2000, 0, 1000, 1500, 3000],
    }))
    fd = load_flight_data(p)
    assert np.all(np.diff(fd.t) > 0)          # strictly increasing
    assert fd.summary.lat_lim[1] <= 90        # bad latitude row dropped


def test_iso_timestamps(tmp_path):
    p = _write(tmp_path / "f.csv", pd.DataFrame({
        "timestamp": ["2026-07-01T00:00:00", "2026-07-01T00:00:10", "2026-07-01T00:00:20"],
        "lat": [0.0, 0.0, 0.0],
        "lon": [0.0, 0.01, 0.02],
        "alt": [5000, 5000, 5000],
    }))
    fd = load_flight_data(p)
    assert fd.t0 is not None
    assert fd.t[-1] == pytest.approx(20.0)


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        load_flight_data("does_not_exist_12345.csv")
