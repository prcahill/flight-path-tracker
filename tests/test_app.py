"""Tests for the app factory: health endpoint, packages, isolation, URL guards."""

from __future__ import annotations

import numpy as np
import pytest

from flighttracker import AppConfig, load_flight_data
from flighttracker.app import _flight_package, _State, create_app
from flighttracker.sample import generate_sample_klv_text


@pytest.fixture(scope="module")
def klv_flight(tmp_path_factory):
    p = generate_sample_klv_text(tmp_path_factory.mktemp("klv") / "uav.txt",
                                 duration_min=5, rate_hz=10)
    return load_flight_data(p)


def test_healthz(klv_flight):
    app = create_app(klv_flight, AppConfig())
    client = app.server.test_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_compression_enabled(klv_flight):
    app = create_app(klv_flight, AppConfig())
    client = app.server.test_client()
    resp = client.get("/_dash-layout", headers={"Accept-Encoding": "br"})
    assert resp.headers.get("Content-Encoding") == "br"


def test_flight_package_contents(klv_flight):
    st = _State(klv_flight, AppConfig())
    pkg = _flight_package(st)
    # Playback arrays + optional channels from the KLV sample.
    assert len(pkg["t"]) == len(pkg["lat"]) == len(pkg["alt"])
    assert pkg["pitch"] is not None and pkg["slant"] is not None
    for arr in (pkg["slant"], pkg["fc_lat"], pkg["fc_lon"]):
        assert all(v is None or np.isfinite(v) for v in arr)
    # Display geometry: altitude-colored markers with one hex color per point.
    m = pkg["map"]
    assert len(m["mk_lat"]) == len(m["mk_lon"]) == len(m["mk_color"])
    assert all(c.startswith("#") and len(c) == 7 for c in m["mk_color"][:50])
    # KPI strings and metadata for the client legend.
    assert len(pkg["kpis"]) == 8
    assert pkg["meta"]["alt_max"] > pkg["meta"]["alt_min"]
    assert pkg["name"] == "uav.txt"


def test_upload_is_stateless(klv_flight, tmp_path):
    """A visitor's load must never mutate the boot flight other visitors get."""
    from flighttracker.sample import generate_sample_flight_data
    config = AppConfig()
    app = create_app(klv_flight, config)

    upload_cb = next(e["callback"].__wrapped__ for e in app.callback_map.values()
                     if [i["id"] + "." + i["property"] for i in e["inputs"]]
                     == ["upload-csv.data"])
    csv_path = generate_sample_flight_data(tmp_path / "other.csv", 5, 1)
    staged = {"name": "other.csv", "csv": csv_path.read_text(),
              "orig_rows": 301, "kept_rows": 301}
    package, t_max, _step, value, play, status = upload_cb(staged, [])
    assert package["name"] == "other.csv"
    assert value == 0.0 and play == "▶  PLAY" and status == ""
    assert t_max == pytest.approx(300.0, rel=0.01)

    # The boot layout served to a fresh visitor still holds the KLV flight.
    with app.server.test_request_context("/"):
        layout = app.layout
    assert layout["engine-data"].data["name"] == "uav.txt"


def test_hifi_budgets_and_label(klv_flight, tmp_path):
    """HI-FI keeps every sample in the playback arrays and says so."""
    from flighttracker.sample import generate_sample_flight_data
    config = AppConfig()
    fast = _State(klv_flight, config, hifi=False)
    hifi = _State(klv_flight, config, hifi=True)
    assert hifi.eng_idx.size >= fast.eng_idx.size
    assert hifi.eng_idx.size == klv_flight.n

    app = create_app(klv_flight, config)
    upload_cb = next(e["callback"].__wrapped__ for e in app.callback_map.values()
                     if [i["id"] + "." + i["property"] for i in e["inputs"]]
                     == ["upload-csv.data"])
    csv_path = generate_sample_flight_data(tmp_path / "big.csv", 10, 5)
    staged = {"name": "big.csv", "csv": csv_path.read_text(),
              "orig_rows": 3001, "kept_rows": 3001}
    result = upload_cb(staged, ["on"])
    assert "full fidelity · 3,001 rows" in result[0]["label"]
    assert len(result[0]["t"]) == 3001


def test_klv_sensor_tags(tmp_path):
    from flighttracker.klv import load_klv_text
    text = """\
========== FRAME ==========
(  2) Unix Time Stamp           : 2026-06-15 06:21:01.000
( 13) Sensor Latitude           : 33.8434 deg
( 14) Sensor Longitude          : 131.0317 deg
( 15) Sensor True Altitude      : 100.0 m
========= END FRAME ========
========== FRAME ==========
(  2) Unix Time Stamp           : 2026-06-15 06:21:02.000
( 13) Sensor Latitude           : 33.8444 deg
( 14) Sensor Longitude          : 131.0327 deg
( 15) Sensor True Altitude      : 100.0 m
( 21) Slant Range               : 500.0 m
( 23) Frame Center Latitude     : 33.8460 deg
( 24) Frame Center Longitude    : 131.0400 deg
========= END FRAME ========
========== FRAME ==========
(  2) Unix Time Stamp           : 2026-06-15 06:21:03.000
( 13) Sensor Latitude           : 33.8454 deg
( 14) Sensor Longitude          : 131.0337 deg
( 15) Sensor True Altitude      : 100.0 m
========= END FRAME ========
"""
    p = tmp_path / "stare.txt"
    p.write_text(text)
    fd = load_klv_text(p)
    assert fd.slant_ft is not None and fd.fc_lat is not None
    assert np.isnan(fd.slant_ft[0])
    assert fd.slant_ft[1] == pytest.approx(500 * 3.280839895, rel=1e-6)
    assert fd.fc_lat[2] == pytest.approx(33.8460)
