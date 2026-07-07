"""Tests for the app factory: health endpoint, payload, isolation, URL guards."""

from __future__ import annotations

import numpy as np
import pytest

from flighttracker import AppConfig, load_flight_data
from flighttracker.app import _engine_payload, _fetch_log, _State, create_app
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


def test_engine_payload_optional_channels(klv_flight):
    st = _State(klv_flight, AppConfig())
    payload = _engine_payload(st)
    # KLV sample carries attitude + sensor pointing.
    assert payload["pitch"] is not None
    assert payload["slant"] is not None and payload["fc_lat"] is not None
    # Early frames may predate the first stare report -> JSON nulls, not NaN.
    for arr in (payload["slant"], payload["fc_lat"], payload["fc_lon"]):
        assert all(v is None or np.isfinite(v) for v in arr)
    # Map figure has the 6-trace layout the engine expects.
    from flighttracker.app import _map_figure
    assert [tr.name for tr in _map_figure(st).data] == [
        "track", "altitude", "halo", "aircraft", "stare-line", "stare-point"]


def test_upload_is_stateless(klv_flight, tmp_path):
    """A visitor's load must never mutate the boot flight other visitors get."""
    from flighttracker.sample import generate_sample_flight_data
    config = AppConfig()
    app = create_app(klv_flight, config)

    # Find the upload callback and feed it a staged CSV, as the browser would.
    upload_cb = next(e["callback"].__wrapped__ for e in app.callback_map.values()
                     if [i["id"] + "." + i["property"] for i in e["inputs"]]
                     == ["upload-csv.data"])
    csv_path = generate_sample_flight_data(tmp_path / "other.csv", 5, 1)
    staged = {"name": "other.csv", "csv": csv_path.read_text(),
              "orig_rows": 301, "kept_rows": 301}
    result = upload_cb(staged, [])
    assert result[4] == "other.csv"            # label reflects the new file

    # The boot layout served to a fresh visitor still holds the KLV flight.
    with app.server.test_request_context("/"):
        layout = app.layout
    assert "sample_uav" in str(layout["file-label"].children) or \
           "uav.txt" in str(layout["file-label"].children)


def test_hifi_budgets_and_label(klv_flight, tmp_path):
    """HI-FI keeps every sample in the display/playback arrays and says so."""
    from flighttracker.sample import generate_sample_flight_data
    config = AppConfig()
    fast = _State(klv_flight, config, hifi=False)
    hifi = _State(klv_flight, config, hifi=True)
    # 5-min KLV sample has 301 position rows: fast still decimates nothing
    # here, but the hi-fi budgets must be at least as large.
    assert hifi.eng_idx.size >= fast.eng_idx.size
    assert hifi.eng_idx.size == klv_flight.n          # all samples kept

    app = create_app(klv_flight, config)
    upload_cb = next(e["callback"].__wrapped__ for e in app.callback_map.values()
                     if [i["id"] + "." + i["property"] for i in e["inputs"]]
                     == ["upload-csv.data"])
    csv_path = generate_sample_flight_data(tmp_path / "big.csv", 10, 5)
    staged = {"name": "big.csv", "csv": csv_path.read_text(),
              "orig_rows": 3001, "kept_rows": 3001}
    result = upload_cb(staged, ["on"])
    assert "full fidelity · 3,001 rows" in result[4]
    assert len(result[9]["t"]) == 3001                # engine payload complete


def test_fetch_log_rejects_bad_urls():
    with pytest.raises(ValueError, match="https"):
        _fetch_log("http://example.com/a.csv", 1000)
    with pytest.raises(ValueError, match="non-public"):
        _fetch_log("https://127.0.0.1/a.csv", 1000)
    with pytest.raises(ValueError, match="non-public"):
        _fetch_log("https://localhost/a.csv", 1000)


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
    assert np.isnan(fd.slant_ft[0])                       # before first report
    assert fd.slant_ft[1] == pytest.approx(500 * 3.280839895, rel=1e-6)
    assert fd.fc_lat[2] == pytest.approx(33.8460)          # forward-filled
