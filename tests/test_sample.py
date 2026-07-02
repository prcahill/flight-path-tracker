"""Tests for the sample-data generator, including anti-spike regression guards."""

from __future__ import annotations

from flighttracker.data import load_flight_data
from flighttracker.sample import generate_sample_flight_data


def test_generate_shape_and_route(tmp_path):
    out = generate_sample_flight_data(tmp_path / "s.csv", duration_min=60, rate_hz=5)
    assert out.is_file()
    fd = load_flight_data(out)
    assert fd.n == int(60 * 60 * 5) + 1
    # Total distance should track the ~297 nm KSFO->KLAX reference route.
    assert 285 < fd.summary.distance_nm < 310
    # Cruise near FL350.
    assert 34000 < fd.summary.max_alt_ft < 36000
    # Realistic cruise ground speed at a 60-minute duration.
    assert fd.summary.max_speed_kt < 500


def test_no_speed_or_climb_spikes(tmp_path):
    """Regression guard: the smooth-noise design must keep derived rates sane.

    An earlier version added per-sample white noise, producing point-to-point
    velocity spikes (max ~7200 kt vs ~300 kt average, a 24x ratio) at high
    sample rates. The invariant that catches this — independent of the chosen
    duration — is that the max is not wildly above the average.
    """
    out = generate_sample_flight_data(tmp_path / "hi.csv", duration_min=60, rate_hz=50)
    fd = load_flight_data(out)
    s = fd.summary
    assert s.max_speed_kt < 1.8 * s.avg_speed_kt   # no isolated speed spikes
    assert s.max_climb_fpm < 8000                  # bounded climb
    assert s.max_descent_fpm > -8000               # bounded descent


def test_reproducible(tmp_path):
    a = load_flight_data(generate_sample_flight_data(tmp_path / "a.csv", 10, 5))
    b = load_flight_data(generate_sample_flight_data(tmp_path / "b.csv", 10, 5))
    assert a.summary.distance_nm == b.summary.distance_nm
    assert a.summary.max_alt_ft == b.summary.max_alt_ft
