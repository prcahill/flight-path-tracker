"""Dash + Plotly dashboard for replaying a flight path.

Builds an enterprise-style dark dashboard: KPI cards, an interactive world map
(altitude-colored path + animated aircraft marker), synced altitude/speed
profiles, and playback controls (play/pause, speed, scrubber).

Architecture
------------
Playback runs **entirely client-side** (``assets/engine.js``): the decimated
flight arrays are shipped once in a ``dcc.Store`` and a requestAnimationFrame
loop interpolates the aircraft state at 60 fps, writing positions straight
into the MapLibre GeoJSON sources and the readout DOM. No network requests
occur during playback, so the app behaves identically on localhost and on a
remote host regardless of latency, the map stays fully draggable while the
flight plays, and each visitor gets an independent playback session. The
server's responsibilities are reduced to building the initial figures and
parsing uploaded CSVs.

The flight path itself is decimated to two display budgets (a cheap
ground-track polyline plus a sparse altitude-colored marker overlay) so very
large logs render instantly.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, dcc, html, no_update
from plotly.subplots import make_subplots

from .config import AppConfig
from .data import load_flight_data
from .metrics import FlightData, compass_point, format_duration

# Map trace indices (order matters: engine.js updates traces 2..5).
_LINE, _COLOR, _HALO, _DOT, _STARE_LINE, _STARE_PT = 0, 1, 2, 3, 4, 5


def _decimate(n: int, budget: int) -> np.ndarray:
    """Return sorted indices covering ``0..n-1`` with at most ``budget`` points."""
    if n <= budget:
        return np.arange(n)
    idx = np.arange(0, n, int(np.ceil(n / budget)))
    if idx[-1] != n - 1:
        idx = np.append(idx, n - 1)
    return idx


def _fmt_int(x: float) -> str:
    return f"{int(round(x)):,}"


def _fetch_log(url: str, max_bytes: int) -> str:
    """Fetch a remote log with SSRF and size guards (https-only, public hosts)."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    import requests

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("only https:// URLs are allowed")
    host = parsed.hostname or ""
    for info in socket.getaddrinfo(host, None):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("URL resolves to a non-public address")
    resp = requests.get(url, timeout=15, stream=True,
                        headers={"User-Agent": "flight-path-tracker"})
    resp.raise_for_status()
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=1 << 16):
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"remote file exceeds the {max_bytes // 1_000_000} MB limit")
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


class _State:
    """Server-side holder for the loaded flight and precomputed display arrays."""

    def __init__(self, flight: FlightData | None, config: AppConfig):
        self.config = config
        self.set_flight(flight)

    def set_flight(self, flight: FlightData | None) -> None:
        self.flight = flight
        if flight is None:
            return
        c = self.config
        li = _decimate(flight.n, c.path_line_budget)
        self.line_lat = flight.lat[li]
        self.line_lon = flight.lon[li]
        mi = _decimate(flight.n, c.path_marker_budget)
        self.mark_lat = flight.lat[mi]
        self.mark_lon = flight.lon[mi]
        self.mark_alt = flight.alt[mi]
        self.eng_idx = _decimate(flight.n, c.engine_budget)


def _engine_payload(state: _State) -> dict:
    """Decimated flight arrays + metadata consumed by ``assets/engine.js``."""
    f = state.flight
    c = state.config
    i = state.eng_idx
    lat_lim, lon_lim = f.summary.lat_lim, f.summary.lon_lim
    rnd = lambda a, p: np.round(a[i], p).tolist()  # noqa: E731

    def opt(a: np.ndarray | None, p: int) -> list | None:
        """Optional channel: None if absent; NaN entries become JSON nulls."""
        if a is None:
            return None
        vals = np.round(a[i], p)
        return [float(v) if np.isfinite(v) else None for v in vals]

    return {
        "t": rnd(f.t, 2),
        "lat": rnd(f.lat, 5),
        "lon": rnd(f.lon, 5),
        "alt": rnd(f.alt, 1),
        "gs": rnd(f.gs, 1),
        "hdg": rnd(f.hdg, 1),
        "vs": rnd(f.vs, 1),
        "dist": rnd(f.cum_nm, 2),
        "pitch": opt(f.pitch, 1),
        "roll": opt(f.roll, 1),
        "slant": opt(f.slant_ft, 0),
        "fc_lat": opt(f.fc_lat, 5),
        "fc_lon": opt(f.fc_lon, 5),
        "meta": {
            "duration_s": float(f.t[-1]),
            "t0_ms": int(f.t0.timestamp() * 1000) if f.t0 else None,
            "follow_zoom": c.follow_zoom,
            "fit_zoom": _zoom_for(lat_lim, lon_lim),
            "center_lat": float(np.mean(lat_lim)),
            "center_lon": float(np.mean(lon_lim)),
        },
    }


# --------------------------------------------------------------------------- #
#  Figure builders
# --------------------------------------------------------------------------- #
def _map_figure(state: _State) -> go.Figure:
    f = state.flight
    c = state.config
    fig = go.Figure()
    fig.add_trace(go.Scattermap(
        lat=state.line_lat, lon=state.line_lon, mode="lines",
        line=dict(width=1.5, color="rgba(255,255,255,0.20)"),
        hoverinfo="skip", name="track",
    ))
    fig.add_trace(go.Scattermap(
        lat=state.mark_lat, lon=state.mark_lon, mode="markers",
        marker=dict(size=5, color=state.mark_alt, colorscale=c.colorscale,
                    showscale=True,
                    colorbar=dict(
                        title=dict(text="ALT (FT)", font=dict(color=c.muted, size=10)),
                        tickfont=dict(color=c.muted, size=9),
                        thickness=8, len=0.7, x=0.99, xanchor="right",
                        y=0.98, yanchor="top", outlinewidth=0, ticklen=3,
                        bgcolor="rgba(10,21,38,0.55)",
                    )),
        customdata=state.mark_alt,
        hovertemplate="%{customdata:,.0f} ft<extra></extra>",
        name="altitude",
    ))
    fig.add_trace(go.Scattermap(
        lat=[float(f.lat[0])], lon=[float(f.lon[0])], mode="markers",
        marker=dict(size=24, color=c.halo), hoverinfo="skip", name="halo",
    ))
    fig.add_trace(go.Scattermap(
        lat=[float(f.lat[0])], lon=[float(f.lon[0])], mode="markers",
        marker=dict(size=11, color=c.cursor), hoverinfo="skip", name="aircraft",
    ))
    # Sensor stare-point traces (populated by the engine for KLV data with
    # frame-center tags; empty otherwise).
    fig.add_trace(go.Scattermap(
        lat=[], lon=[], mode="lines",
        line=dict(width=1.5, color=c.stare), hoverinfo="skip", name="stare-line",
    ))
    fig.add_trace(go.Scattermap(
        lat=[], lon=[], mode="markers",
        marker=dict(size=9, color=c.stare), hoverinfo="skip", name="stare-point",
    ))
    lat_lim, lon_lim = f.summary.lat_lim, f.summary.lon_lim
    fig.update_layout(
        map=dict(
            style=c.default_style,
            center=dict(lat=float(np.mean(lat_lim)), lon=float(np.mean(lon_lim))),
            zoom=_zoom_for(lat_lim, lon_lim),
        ),
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor=c.panel, uirevision="keep", showlegend=False,
    )
    return fig


def _zoom_for(lat_lim, lon_lim) -> float:
    import math
    lat_span = max(lat_lim[1] - lat_lim[0], 1e-3)
    lon_span = max(lon_lim[1] - lon_lim[0], 1e-3)
    z = min(math.log2(360.0 / lon_span), math.log2(180.0 / lat_span)) - 0.6
    return float(max(1.0, min(16.0, z)))


def _profiles_figure(state: _State) -> go.Figure:
    f = state.flight
    c = state.config
    i = state.eng_idx
    prof_t = f.t[i] / 60.0
    cm = dict(color=c.cursor, size=8, line=dict(color=c.accent, width=2))
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.16,
                        subplot_titles=("ALTITUDE (FT)", "GROUND SPEED (KT)"))
    fig.add_trace(go.Scatter(x=prof_t, y=f.alt[i], mode="lines",
                             line=dict(color=c.color_alt, width=1.5), hoverinfo="skip"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=[0.0], y=[float(f.alt[0])], mode="markers",
                             marker=cm, hoverinfo="skip"), row=1, col=1)
    fig.add_trace(go.Scatter(x=prof_t, y=f.gs[i], mode="lines",
                             line=dict(color=c.color_speed, width=1.5), hoverinfo="skip"),
                  row=2, col=1)
    fig.add_trace(go.Scatter(x=[0.0], y=[float(f.gs[0])], mode="markers",
                             marker=cm, hoverinfo="skip"), row=2, col=1)
    fig.update_xaxes(gridcolor=c.grid, zeroline=False, tickfont=dict(size=10))
    fig.update_yaxes(gridcolor=c.grid, zeroline=False, tickfont=dict(size=10))
    fig.update_xaxes(title_text="TIME (MIN)", title_font=dict(size=10, color=c.muted),
                     row=2, col=1)
    for ann in fig.layout.annotations:
        ann.font = dict(size=10, color=c.muted)
        ann.x = 0.0
        ann.xanchor = "left"
    fig.update_layout(
        template="plotly_dark", showlegend=False,
        margin=dict(l=46, r=14, t=24, b=34),
        paper_bgcolor=c.panel, plot_bgcolor=c.panel, font=dict(color=c.muted),
        height=300, uirevision="keep",
        dragmode="select", selectdirection="h",   # drag = segment analytics
    )
    return fig


def _kpi_children(f: FlightData) -> list:
    s = f.summary
    cards = [
        ("Distance", f"{s.distance_nm:.1f} nm"),
        ("Duration", s.duration),
        ("Samples", _fmt_int(s.num_points)),
        ("Data rate", f"{s.data_rate_hz:.1f} Hz"),
        ("Max altitude", f"{_fmt_int(s.max_alt_ft)} ft"),
        ("Cruise", f"{_fmt_int(s.cruise_alt_ft)} ft"),
        ("Max / avg speed", f"{s.max_speed_kt:.0f} / {s.avg_speed_kt:.0f} kt"),
        ("Max climb / descent", f"{_fmt_int(s.max_climb_fpm)} / {_fmt_int(s.max_descent_fpm)} fpm"),
    ]
    return [html.Div(className="kpi", children=[
        html.Div(label, className="kpi-label"),
        html.Div(value, className="kpi-value"),
    ]) for label, value in cards]


def _readout_children(f: FlightData) -> list:
    """Initial readout rows; the value spans carry ids engine.js writes into."""
    rows = [
        ("rd-time", "Time", format_duration(0)),
        ("rd-pos", "Position", f"{f.lat[0]:.4f}°, {f.lon[0]:.4f}°"),
        ("rd-alt", "Altitude", f"{_fmt_int(f.alt[0])} ft"),
        ("rd-gs", "Ground speed", f"{f.gs[0]:.0f} kt"),
        ("rd-hdg", "Heading", f"{f.hdg[0]:03.0f}°  {compass_point(f.hdg[0])}"),
        ("rd-vs", "Vertical speed", f"{_fmt_int(f.vs[0])} ft/min"),
        ("rd-dist", "Distance flown", f"{f.cum_nm[0]:.1f} nm"),
    ]
    # Attitude / sensor rows only when the source format carries them (KLV).
    if f.pitch is not None:
        rows.append(("rd-pitch", "Pitch", f"{f.pitch[0]:+.1f}°"))
    if f.roll is not None:
        rows.append(("rd-roll", "Roll", f"{f.roll[0]:+.1f}°"))
    if f.slant_ft is not None:
        first = f.slant_ft[0]
        rows.append(("rd-slant", "Slant range",
                     f"{_fmt_int(first)} ft" if np.isfinite(first) else "—"))
    return [html.Div(className="rd-row", children=[
        html.Span(label, className="rd-label"),
        html.Span(value, className="rd-value", id=rid),
    ]) for rid, label, value in rows]


# --------------------------------------------------------------------------- #
#  App factory
# --------------------------------------------------------------------------- #
def create_app(flight: FlightData | None = None, config: AppConfig | None = None) -> Dash:
    config = config or AppConfig()
    state = _State(flight, config)

    app = Dash(
        __name__,
        assets_folder=str(Path(__file__).parent / "assets"),
        title="Flight Path Tracker",
        update_title=None,
        compress=True,   # Brotli/gzip via flask-compress: ~5-10x smaller payloads
    )
    app.server.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

    @app.server.route("/healthz")
    def _healthz():  # pragma: no cover - trivial route, exercised in tests
        from . import __version__
        return {"status": "ok", "version": __version__}

    file_label = Path(flight.file).name if flight and flight.file else "no file loaded"
    t_max = float(flight.t[-1]) if flight else 1.0

    app.layout = html.Div(className="app", children=[
        # Header
        html.Div(className="header", children=[
            html.Div(className="brand", children=[
                html.Div("✈", className="brand-mark"),
                html.Div(children=[
                    html.Div("FLIGHT PATH TRACKER", className="title"),
                    html.Div("TELEMETRY REPLAY CONSOLE", className="subtitle"),
                ]),
            ]),
            html.Div(id="file-label", className="chip", children=file_label),
            html.Div(className="header-controls", children=[
                dcc.Input(id="url-in", type="url", className="url-in", debounce=True,
                          placeholder="https://…  log URL"),
                html.Button("GO", id="url-go", className="ghost go"),
                dcc.Dropdown(id="basemap", className="dd",
                             options=[{"label": s, "value": s} for s in config.basemap_styles],
                             value=config.default_style, clearable=False),
                dcc.Checklist(id="follow", className="follow",
                              options=[{"label": "FOLLOW", "value": "on"}], value=[]),
                dcc.Upload(id="upload", className="upload",
                           children=html.Div("⬆ LOAD FILE"), multiple=False),
            ]),
        ]),

        # KPI cards
        html.Div(id="kpis", className="kpis",
                 children=_kpi_children(flight) if flight else []),

        # Main: map | side panel
        html.Div(className="main", children=[
            html.Div(className="map-wrap", children=[
                dcc.Graph(id="map", className="map",
                          config={"displayModeBar": False, "scrollZoom": True},
                          figure=_map_figure(state) if flight else go.Figure()),
            ]),
            html.Div(className="side", children=[
                html.Div(className="panel", children=[
                    html.Div("LIVE TELEMETRY", className="panel-title"),
                    html.Div(id="readout", className="readout",
                             children=_readout_children(flight) if flight else []),
                ]),
                dcc.Graph(id="profiles", className="profiles",
                          config={"displayModeBar": False},
                          figure=_profiles_figure(state) if flight else go.Figure()),
                html.Div(id="seg-stats", className="seg-stats"),
            ]),
        ]),

        # Controls
        html.Div(className="controls", children=[
            html.Button("▶  PLAY", id="play", className="play"),
            dcc.Dropdown(id="speed", className="dd speed", clearable=False,
                         options=[{"label": f"{m}×", "value": m} for m in config.speed_multipliers],
                         value=25),
            dcc.Slider(id="scrub", min=0, max=t_max, value=0,
                       step=max(0.1, round(t_max / 2000.0, 2)),
                       marks=None, updatemode="drag", included=True),
            html.Button("KML", id="export-kml", className="ghost"),
            html.Button("GPX", id="export-gpx", className="ghost"),
            html.Div(id="clock", className="clock", children="00:00:00 / 00:00:00"),
        ]),

        # Client-side playback engine data + dummy ack target.
        dcc.Store(id="engine-data", data=_engine_payload(state) if flight else None),
        dcc.Store(id="cs-ack"),
        # Upload staging: the browser decodes (and, for very large files,
        # decimates) the CSV before it is sent to the server for parsing.
        dcc.Store(id="upload-csv"),
    ])

    _register_callbacks(app, state, config)
    return app


def _register_callbacks(app: Dash, state: _State, config: AppConfig) -> None:
    # ---- clientside: wire controls to the playback engine (assets/engine.js)
    app.clientside_callback(
        "function(data){ if (window.FT) { window.FT.load(data); } return null; }",
        Output("cs-ack", "data"),
        Input("engine-data", "data"),
    )
    app.clientside_callback(
        "function(n){ return window.FT ? window.FT.togglePlay() : '▶  PLAY'; }",
        Output("play", "children"),
        Input("play", "n_clicks"),
        prevent_initial_call=True,
    )
    app.clientside_callback(
        "function(v){ if (window.FT) { window.FT.setSpeed(v); } return null; }",
        Output("cs-ack", "data", allow_duplicate=True),
        Input("speed", "value"),
        prevent_initial_call=True,
    )
    app.clientside_callback(
        "function(v){ if (window.FT) { window.FT.seek(v); } return null; }",
        Output("cs-ack", "data", allow_duplicate=True),
        Input("scrub", "value"),
        prevent_initial_call=True,
    )
    app.clientside_callback(
        "function(v){ if (window.FT) { window.FT.setFollow(v && v.length > 0); } return null; }",
        Output("cs-ack", "data", allow_duplicate=True),
        Input("follow", "value"),
        prevent_initial_call=True,
    )
    app.clientside_callback(
        "function(v){ if (window.FT) { window.FT.setBasemap(v); } return null; }",
        Output("cs-ack", "data", allow_duplicate=True),
        Input("basemap", "value"),
        prevent_initial_call=True,
    )
    app.clientside_callback(
        "function(c, f){ return window.FT ? window.FT.prepUpload(c, f, "
        f"{config.upload_max_rows}) : null; }}",
        Output("upload-csv", "data"),
        Input("upload", "contents"),
        State("upload", "filename"),
        prevent_initial_call=True,
    )

    # ---- clientside: segment analytics + track exports ---------------------
    app.clientside_callback(
        "function(sel){ return window.FT ? window.FT.segmentStats(sel) : ''; }",
        Output("seg-stats", "children"),
        Input("profiles", "selectedData"),
        prevent_initial_call=True,
    )
    app.clientside_callback(
        "function(n){ if (window.FT) { window.FT.exportTrack('kml'); } return null; }",
        Output("cs-ack", "data", allow_duplicate=True),
        Input("export-kml", "n_clicks"),
        prevent_initial_call=True,
    )
    app.clientside_callback(
        "function(n){ if (window.FT) { window.FT.exportTrack('gpx'); } return null; }",
        Output("cs-ack", "data", allow_duplicate=True),
        Input("export-gpx", "n_clicks"),
        prevent_initial_call=True,
    )

    # ---- server: parse a flight and render the full response --------------
    # Stateless on purpose: a local _State is built per request and the
    # module-level boot state is never mutated, so each visitor's upload is
    # isolated (new page loads always get the boot flight).
    upload_outputs = (
        Output("map", "figure"),
        Output("profiles", "figure"),
        Output("kpis", "children"),
        Output("readout", "children"),
        Output("file-label", "children"),
        Output("scrub", "max"),
        Output("scrub", "step"),
        Output("scrub", "value"),
        Output("play", "children", allow_duplicate=True),
        Output("engine-data", "data"),
    )

    def _error(name: str, err: object) -> tuple:
        return (no_update, no_update, no_update, no_update, f"⚠ {name}: {err}",
                no_update, no_update, no_update, no_update, no_update)

    def _render_text(text: str, name: str, label: str) -> tuple:
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         encoding="utf-8") as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        try:
            flight = load_flight_data(tmp_path)
        except Exception as exc:  # noqa: BLE001 - surface parse errors to the UI
            return _error(name, exc)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        flight.file = name
        local = _State(flight, config)
        t_max = float(flight.t[-1])
        return (_map_figure(local), _profiles_figure(local), _kpi_children(flight),
                _readout_children(flight), label, t_max,
                max(0.1, round(t_max / 2000.0, 2)), 0.0, "▶  PLAY",
                _engine_payload(local))

    @app.callback(*upload_outputs, Input("upload-csv", "data"),
                  prevent_initial_call=True)
    def _upload(staged):
        if not staged:
            return (no_update,) * 10
        name = staged.get("name", "uploaded.csv")
        if staged.get("err") or not staged.get("csv"):
            return _error(name, staged.get("err", "empty file"))
        if len(staged["csv"]) > config.upload_max_bytes:
            return _error(name, "file too large after staging "
                                f"(limit {config.upload_max_bytes // 1_000_000} MB)")
        label = name
        orig, kept = staged.get("orig_rows", 0), staged.get("kept_rows", 0)
        if orig and kept and kept < orig:
            label = f"{name} · downsampled {orig:,} → {kept:,} rows"
        return _render_text(staged["csv"], name, label)

    @app.callback(
        Output("map", "figure", allow_duplicate=True),
        Output("profiles", "figure", allow_duplicate=True),
        Output("kpis", "children", allow_duplicate=True),
        Output("readout", "children", allow_duplicate=True),
        Output("file-label", "children", allow_duplicate=True),
        Output("scrub", "max", allow_duplicate=True),
        Output("scrub", "step", allow_duplicate=True),
        Output("scrub", "value", allow_duplicate=True),
        Output("play", "children", allow_duplicate=True),
        Output("engine-data", "data", allow_duplicate=True),
        Input("url-go", "n_clicks"),
        Input("url-in", "n_submit"),
        State("url-in", "value"),
        prevent_initial_call=True,
    )
    def _load_url(_clicks, _submit, url):
        if not url:
            return (no_update,) * 10
        name = url.rsplit("/", 1)[-1] or "remote log"
        try:
            text = _fetch_log(url, config.upload_max_bytes)
        except Exception as exc:  # noqa: BLE001 - surface fetch errors to the UI
            return _error(name, exc)
        return _render_text(text, name, name)


def run(flight: FlightData | None = None, config: AppConfig | None = None,
        debug: bool = False) -> None:
    """Build and serve the dashboard (blocking)."""
    config = config or AppConfig()
    app = create_app(flight, config)
    app.run(host=config.host, port=config.port, debug=debug)
