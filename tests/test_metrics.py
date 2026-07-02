"""Tests for great-circle math and derived metrics."""

from __future__ import annotations

import numpy as np

from flighttracker.metrics import (
    bearing_deg,
    compass_point,
    derive_metrics,
    format_duration,
    haversine_nm,
)


def test_haversine_sfo_lax():
    # KSFO -> KLAX great-circle distance is ~293 nm.
    d = float(haversine_nm(37.6188, -122.3750, 33.9425, -118.4081))
    assert 285 < d < 300


def test_haversine_zero():
    assert float(haversine_nm(10.0, 20.0, 10.0, 20.0)) == 0.0


def test_bearing_cardinals():
    assert abs(float(bearing_deg(0, 0, 1, 0)) - 0.0) < 1e-6      # due north
    assert abs(float(bearing_deg(0, 0, 0, 1)) - 90.0) < 1e-6     # due east


def test_compass_point():
    assert compass_point(0) == "N"
    assert compass_point(90) == "E"
    assert compass_point(180) == "S"
    assert compass_point(270) == "W"


def test_format_duration():
    assert format_duration(0) == "00:00:00"
    assert format_duration(3661) == "01:01:01"


def test_derive_metrics_constant_speed():
    # A straight eastward leg at the equator, 1 sample/sec.
    t = np.arange(0, 101, dtype=float)
    lat = np.zeros_like(t)
    lon = np.linspace(0, 0.1, t.size)
    alt = np.full_like(t, 10000.0)
    fd = derive_metrics(t, lat, lon, alt)

    assert fd.n == t.size
    assert fd.summary.distance_nm > 0
    # ground speed should be ~constant and positive
    assert np.all(fd.gs[1:] > 0)
    assert fd.summary.max_speed_kt < 2 * fd.summary.avg_speed_kt
    # heading due east
    assert abs(fd.hdg[-1] - 90.0) < 1.0
    # level flight -> ~zero vertical speed
    assert abs(fd.summary.max_climb_fpm) < 1e-6
