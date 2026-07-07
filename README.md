# Flight Path Tracker

[![CI](https://github.com/prcahill/flight-path-tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/prcahill/flight-path-tracker/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An interactive dashboard for replaying an aircraft's flight path over a world map
from a text log of **latitude / longitude / altitude** data. It draws the full
track colored by altitude, animates the aircraft smoothly along the trajectory,
and shows live and summary flight information alongside altitude and speed
profiles — all in a clean, dark, enterprise-style web UI.

Built with a **minimal, mainstream** stack: `dash`, `plotly`, `pandas`, `numpy`.
Units are aviation-standard: **feet**, **knots**, **nautical miles**.

> Ported from an original MATLAB implementation; the numeric core (great-circle
> math, metric derivation, and the realistic sample-flight generator) is
> reproduced faithfully in NumPy.

## Features

- **Interactive world map** (Plotly / MapLibre) — pan/zoom, switchable token-free
  basemaps, flight path colored by altitude with a colorbar.
- **Multi-flight roster** — load up to 4 files (upload or URL); each becomes a
  header chip. The ACTIVE flight renders at full brightness with the altitude
  overlay, aircraft icon and playback; the others stay visible as dimmed
  altitude-colored previews. Switching is instant and fully client-side.
- **Smooth aircraft tracking** — the vehicle state (position, altitude, speed,
  heading) is interpolated between samples every frame, so the marker glides
  along the trajectory at any data rate; **Follow** mode turns the map into a
  chase camera that tracks the aircraft.
- **KPI cards** — distance, duration, samples, data rate, max/cruise altitude,
  max/avg speed, max climb/descent.
- **Live readout** — time, position, altitude, ground speed, heading (with
  compass point), vertical speed, distance flown.
- **Altitude & speed profiles** with a cursor synced to playback.
- **Playback controls** — play/pause, speed multiplier (1×–500×), and a scrubber.
- **Drag-and-drop CSV upload** to load a different log at runtime.
- **Scales to millions of points** — the path is decimated to two display
  budgets (a cheap ground-track polyline plus a sparse altitude-colored
  overlay; it does not draw every point).
- **HI-FI mode** — toggle in the header before loading a file: upload
  downsampling is disabled (every sample reaches the server, so all metrics
  are exact) and display/playback budgets rise ~40×. Verified with a
  720,001-row upload end-to-end in ~7 s locally. Response compression is
  Brotli and always lossless, in either mode.
- **Fully client-side playback** — a 60 fps requestAnimationFrame engine
  interpolates the flight locally and writes positions straight into the
  MapLibre GeoJSON sources. Zero network requests and zero Plotly/camera calls
  during playback: the map **stays fully draggable while the flight plays**,
  smoothness is independent of server latency, and every visitor gets an
  independent playback session (uploads are per-visitor too).
- **Sensor stare-point view** — for KLV data carrying frame-center tags
  (21/23/24), the map draws the sensor stare-point with a line from the
  aircraft, plus a slant-range readout; the aircraft renders as a
  heading-rotated plane icon.
- **Segment analytics** — drag-select a time range on the profiles to get
  distance, speed, altitude band and climb stats for that leg.
- **KML / GPX export** and **load-from-URL** (https, size-capped).
- **Production-tuned** — Brotli-compressed responses (~5-10x smaller first
  load), orjson serialization, `/healthz` for health checks and uptime pings.

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 1. Generate a realistic sample flight (KSFO -> KLAX)
flighttracker generate --out data/sample_flight_KSFO_KLAX.csv

# 2. Launch the dashboard (opens on http://127.0.0.1:8050)
flighttracker run --file data/sample_flight_KSFO_KLAX.csv
```

`flighttracker run` with no `--file` loads `data/sample_flight_KSFO_KLAX.csv` if
present, or you can upload a CSV from the header. You can also run the package
directly: `python -m flighttracker run`.

### CLI

```
flighttracker generate [--out PATH] [--minutes N] [--rate HZ]
flighttracker run      [--file PATH] [--host HOST] [--port PORT] [--debug]
```

## Input file formats

The loader sniffs the content, so both formats work everywhere a file can be
loaded (CLI `--file`, `FLIGHT_LOG`, and drag-and-drop upload).

### KLV frame-text telemetry (MISB ST 0601-style)

Decoded UAS metadata dumps stored as frame blocks:

```
========== FRAME ==========
(  2) Unix Time Stamp           : 2026-06-15 06:21:01.678
(  5) Platform Heading Angle    : 80.3 deg
( 13) Sensor Latitude           : 33.843407 deg
( 14) Sensor Longitude          : 131.031684 deg
( 15) Sensor True Altitude      : 15.2 m
========= END FRAME ========
```

Fields are matched by numeric tag (2 time, 5/6/7 heading/pitch/roll,
13/14/15 sensor lat/lon/altitude); unknown tags are ignored. The parser is
built for real dumps: sparse frames (a track point is emitted only for frames
carrying lat + lon), interleaved async streams with out-of-order and duplicate
timestamps, forward-filled altitude/attitude, and metric units converted to
aviation units (m → ft). Ground speed and vertical speed are derived from the
track; pitch and roll appear as extra LIVE readout rows. Generate a 2.6M-char
sample with `flighttracker generate --format klv`
(→ `data/sample_uav_telemetry.txt`).

### CSV

Comma-separated text, one header row, one sample per row. Canonical columns:

```
time, latitude, longitude, altitude_ft, groundspeed_kt, heading_deg
```

| Column | Notes |
|--------|-------|
| `time` | Elapsed **seconds** (numeric) **or** an ISO-8601 timestamp — auto-detected. |
| `latitude`, `longitude` | Decimal degrees. |
| `altitude_ft` | Feet MSL. |
| `groundspeed_kt`, `heading_deg` | **Optional** — derived from position/time if absent. |

The loader is tolerant: columns are matched by (case-insensitive) header name, so
`lat`, `Latitude`, `lon`, `lng`, `alt`, `gs`, `speed`, `track`, `hdg`, etc. all
work; with no recognizable header the first four columns are assumed to be
`time, latitude, longitude, altitude`. Invalid rows are dropped, rows are sorted
by time, and duplicate timestamps are removed.

Derived and displayed: total distance, duration, data rate, max/min/cruise
altitude, max & average ground speed, max climb & descent rate, and — live during
playback — position, altitude, ground speed, heading, vertical speed and distance
flown.

## Architecture

```
src/flighttracker/
├── metrics.py   # haversine/bearing, derive_metrics, FlightData/FlightSummary
├── data.py      # load_flight_data(): tolerant CSV reader
├── sample.py    # generate_sample_flight_data(): realistic KSFO->KLAX log
├── config.py    # AppConfig: budgets, colors, map styles, host/port
├── app.py       # create_app(): Dash layout + callbacks
├── cli.py       # `flighttracker` console entry point
└── assets/      # dark theme CSS
```

The full `FlightData` lives server-side; only lightweight playback state travels
to the browser. See module docstrings for details.

## Development

```bash
pip install -e ".[dev]"
pytest          # unit tests, incl. anti-spike regression guards
ruff check .    # lint
```

## Deploying to the web

The repo ships a [Render](https://render.com) blueprint (`render.yaml`) and a
production WSGI entry point (`flighttracker.wsgi:server`):

1. Sign in to Render with GitHub.
2. **New → Blueprint** and select this repository — Render reads `render.yaml`
   and deploys automatically. Every push to `main` redeploys.

Any other host works the same way:

```bash
pip install -r requirements.txt && pip install .
python -m gunicorn --workers 1 --threads 8 --bind 0.0.0.0:$PORT flighttracker.wsgi:server
```

Set `FLIGHT_LOG=/path/to/log.csv` to serve a specific file; otherwise the
bundled KSFO→KLAX sample is used.

**Deployment notes**

- Playback runs entirely in each visitor's browser, and uploads are stateless
  per-visitor — the server never mutates shared state, so any number of
  visitors can use the site independently.
- On the free Render plan the service sleeps when idle; the first visit after
  a quiet period takes ~30–60 s to wake. A free uptime pinger aimed at
  `/healthz` (e.g. UptimeRobot at a 10-minute interval) or Render's paid
  always-on tier eliminates the cold start.

## Basemaps

Default styles (`carto-darkmatter`, `carto-positron`, `open-street-map`,
`carto-voyager`) require **no access token**. Satellite imagery would require a
Mapbox token and is intentionally not enabled by default.

## License

[MIT](LICENSE) © Patrick Cahill
