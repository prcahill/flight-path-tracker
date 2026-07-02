# Flight Path Tracker

[![CI](https://github.com/prcahill/flight-path-tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/prcahill/flight-path-tracker/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An interactive dashboard for replaying an aircraft's flight path over a world map
from a text log of **latitude / longitude / altitude** data. It draws the full
track colored by altitude, animates a moving aircraft marker with a trailing
**tracer**, and shows live and summary flight information alongside altitude and
speed profiles — all in a clean, dark, enterprise-style web UI.

Built with a **minimal, mainstream** stack: `dash`, `plotly`, `pandas`, `numpy`.
Units are aviation-standard: **feet**, **knots**, **nautical miles**.

> Ported from an original MATLAB implementation; the numeric core (great-circle
> math, metric derivation, and the realistic sample-flight generator) is
> reproduced faithfully in NumPy.

## Features

- **Interactive world map** (Plotly / MapLibre) — pan/zoom, switchable token-free
  basemaps, flight path colored by altitude with a colorbar.
- **Animated marker + amber tracer** driven by a master playback clock.
- **KPI cards** — distance, duration, samples, data rate, max/cruise altitude,
  max/avg speed, max climb/descent.
- **Live readout** — time, position, altitude, ground speed, heading (with
  compass point), vertical speed, distance flown.
- **Altitude & speed profiles** with a cursor synced to playback.
- **Playback controls** — play/pause, speed multiplier (1×–500×), and a scrubber.
- **Drag-and-drop CSV upload** to load a different log at runtime.
- **Scales to millions of points** — the static path is decimated for display,
  and playback updates only the marker/tracer/cursor via `dash.Patch`, advancing
  by real elapsed time so dense logs skip frames instead of crawling.

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

## Input file format

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

## Basemaps

Default styles (`carto-darkmatter`, `carto-positron`, `open-street-map`,
`carto-voyager`) require **no access token**. Satellite imagery would require a
Mapbox token and is intentionally not enabled by default.

## License

[MIT](LICENSE) © Patrick Cahill
