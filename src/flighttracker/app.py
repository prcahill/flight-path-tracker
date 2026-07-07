"""Dash + Plotly dashboard for replaying aircraft flight paths.

Builds an enterprise-style dark dashboard: KPI cards, an interactive world map,
synced altitude/speed profiles, and playback controls — with a multi-flight
roster (up to 4 loaded files).

Architecture
------------
The server is **stateless per visitor** and its responses are small: loading a
file returns one *flight package* (decimated arrays + display geometry + KPI
strings). Everything on the map is rendered by the client engine
(``assets/engine.js``) as native MapLibre layers — the active flight at full
brightness with the altitude-colored overlay, aircraft icon and sensor
stare-point; other roster flights as dimmed altitude-colored previews.
Switching the active flight, playback, exports and segment analytics are all
client-side and instant. Response compression (Brotli) is lossless.
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
from .metrics import FlightData

_KPI_LABELS = ("Distance", "Duration", "Samples", "Data rate", "Max altitude",
               "Cruise", "Max / avg speed", "Max climb / descent")

# Readout rows: (wrapper id, value id, label, optional).
_READOUT_ROWS = (
    ("rrow-time", "rd-time", "Time", False),
    ("rrow-pos", "rd-pos", "Position", False),
    ("rrow-alt", "rd-alt", "Altitude", False),
    ("rrow-gs", "rd-gs", "Ground speed", False),
    ("rrow-hdg", "rd-hdg", "Heading", False),
    ("rrow-vs", "rd-vs", "Vertical speed", False),
    ("rrow-dist", "rd-dist", "Distance flown", False),
    ("rrow-pitch", "rd-pitch", "Pitch", True),
    ("rrow-roll", "rd-roll", "Roll", True),
    ("rrow-slant", "rd-slant", "Slant range", True),
)

# Google Turbo colormap polynomial (ascending coefficients).
_TURBO = {
    "r": (0.13572138, 4.61539260, -42.66032258, 132.13108234, -152.94239396, 59.28637943),
    "g": (0.09140261, 2.19418839, 4.84296658, -14.18503333, 4.27729857, 2.82956604),
    "b": (0.10667330, 12.64194608, -60.58204836, 110.36276771, -89.90310912, 27.34824973),
}


def _turbo_hex(x: np.ndarray) -> list[str]:
    """Vectorized Turbo colormap: normalized values -> hex colors."""
    x = np.clip(np.asarray(x, dtype=float), 0.0, 1.0)
    chans = []
    for key in ("r", "g", "b"):
        val = np.clip(np.polyval(_TURBO[key][::-1], x), 0.0, 1.0)
        chans.append((val * 255).astype(int))
    return [f"#{r:02x}{g:02x}{b:02x}"
            for r, g, b in zip(chans[0], chans[1], chans[2], strict=True)]


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


class _State:
    """Precomputed decimations for one flight at the chosen fidelity."""

    def __init__(self, flight: FlightData | None, config: AppConfig,
                 hifi: bool = False):
        self.config = config
        self.hifi = hifi
        self.set_flight(flight)

    def set_flight(self, flight: FlightData | None) -> None:
        self.flight = flight
        if flight is None:
            return
        c = self.config
        b = 1 if self.hifi else 0
        self.line_idx = _decimate(flight.n, c.path_line_budget[b])
        self.mark_idx = _decimate(flight.n, c.path_marker_budget[b])
        self.eng_idx = _decimate(flight.n, c.engine_budget[b])


def _kpi_values(f: FlightData) -> list[str]:
    s = f.summary
    return [
        f"{s.distance_nm:.1f} nm",
        s.duration,
        _fmt_int(s.num_points),
        f"{s.data_rate_hz:.1f} Hz",
        f"{_fmt_int(s.max_alt_ft)} ft",
        f"{_fmt_int(s.cruise_alt_ft)} ft",
        f"{s.max_speed_kt:.0f} / {s.avg_speed_kt:.0f} kt",
        f"{_fmt_int(s.max_climb_fpm)} / {_fmt_int(s.max_descent_fpm)} fpm",
    ]


def _zoom_for(lat_lim, lon_lim) -> float:
    import math
    lat_span = max(lat_lim[1] - lat_lim[0], 1e-3)
    lon_span = max(lon_lim[1] - lon_lim[0], 1e-3)
    z = min(math.log2(360.0 / lon_span), math.log2(180.0 / lat_span)) - 0.6
    return float(max(1.0, min(16.0, z)))


def _flight_package(state: _State, label: str | None = None) -> dict:
    """One flight, fully described for the client engine.

    Contains playback arrays (engine budget), map display geometry (line +
    altitude-colored markers at the display budgets), KPI strings and metadata.
    """
    f = state.flight
    c = state.config
    i = state.eng_idx
    xp = 1 if state.hifi else 0
    lat_lim, lon_lim = f.summary.lat_lim, f.summary.lon_lim
    rnd = lambda a, p: np.round(a[i], p + xp).tolist()  # noqa: E731

    def opt(a: np.ndarray | None, p: int) -> list | None:
        if a is None:
            return None
        vals = np.round(a[i], p)
        return [float(v) if np.isfinite(v) else None for v in vals]

    li, mi = state.line_idx, state.mark_idx
    alt_min, alt_max = float(f.alt.min()), float(f.alt.max())
    span = max(alt_max - alt_min, 1.0)
    name = Path(f.file).name if f.file else "flight"
    return {
        "name": name,
        "label": label or name,
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
        "map": {
            "line_lat": np.round(f.lat[li], 5).tolist(),
            "line_lon": np.round(f.lon[li], 5).tolist(),
            "mk_lat": np.round(f.lat[mi], 5).tolist(),
            "mk_lon": np.round(f.lon[mi], 5).tolist(),
            "mk_color": _turbo_hex((f.alt[mi] - alt_min) / span),
        },
        "kpis": _kpi_values(f),
        "meta": {
            "duration_s": float(f.t[-1]),
            "t0_ms": int(f.t0.timestamp() * 1000) if f.t0 else None,
            "follow_zoom": c.follow_zoom,
            "fit_zoom": _zoom_for(lat_lim, lon_lim),
            "center_lat": float(np.mean(lat_lim)),
            "center_lon": float(np.mean(lon_lim)),
            "alt_min": alt_min,
            "alt_max": alt_max,
        },
    }


# --------------------------------------------------------------------------- #
#  Layout shells (all flight content is drawn by the client engine)
# --------------------------------------------------------------------------- #
def _map_shell(config: AppConfig) -> go.Figure:
    fig = go.Figure()
    # One empty map-type trace is required for plotly to instantiate the
    # MapLibre subplot at all; the engine draws everything as native layers.
    fig.add_trace(go.Scattermap(lat=[], lon=[], mode="markers", hoverinfo="skip"))
    fig.update_layout(
        map=dict(style=config.default_style, center=dict(lat=30, lon=-40), zoom=1.4),
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor=config.panel, uirevision="keep", showlegend=False,
    )
    return fig


def _profiles_shell(config: AppConfig) -> go.Figure:
    c = config
    cm = dict(color=c.cursor, size=8, line=dict(color=c.accent, width=2))
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.16,
                        subplot_titles=("ALTITUDE (FT)", "GROUND SPEED (KT)"))
    fig.add_trace(go.Scatter(x=[], y=[], mode="lines",
                             line=dict(color=c.color_alt, width=1.5), hoverinfo="skip"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=[], y=[], mode="markers", marker=cm,
                             hoverinfo="skip"), row=1, col=1)
    fig.add_trace(go.Scatter(x=[], y=[], mode="lines",
                             line=dict(color=c.color_speed, width=1.5), hoverinfo="skip"),
                  row=2, col=1)
    fig.add_trace(go.Scatter(x=[], y=[], mode="markers", marker=cm,
                             hoverinfo="skip"), row=2, col=1)
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
        dragmode="select", selectdirection="h",
    )
    return fig


def _kpi_skeleton() -> list:
    return [html.Div(className="kpi", children=[
        html.Div(label, className="kpi-label"),
        html.Div("—", className="kpi-value", id=f"kpi-v-{i}"),
    ]) for i, label in enumerate(_KPI_LABELS)]


def _readout_skeleton() -> list:
    rows = []
    for wrap_id, val_id, label, optional in _READOUT_ROWS:
        style = {"display": "none"} if optional else None
        rows.append(html.Div(className="rd-row", id=wrap_id, style=style, children=[
            html.Span(label, className="rd-label"),
            html.Span("—", className="rd-value", id=val_id),
        ]))
    return rows


# --------------------------------------------------------------------------- #
#  App factory
# --------------------------------------------------------------------------- #
def create_app(flight: FlightData | None = None, config: AppConfig | None = None) -> Dash:
    config = config or AppConfig()

    app = Dash(
        __name__,
        assets_folder=str(Path(__file__).parent / "assets"),
        title="Flight Path Tracker",
        update_title=None,
        compress=True,
    )
    app.server.config["MAX_CONTENT_LENGTH"] = 160 * 1024 * 1024

    @app.server.route("/healthz")
    def _healthz():  # pragma: no cover - trivial route, exercised in tests
        from . import __version__
        return {"status": "ok", "version": __version__}

    # Use the logo as the favicon (placed after {%favicon%} so it wins).
    app.index_string = """<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        <link rel="icon" type="image/svg+xml" href="/assets/logo.svg">
        {%css%}
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>"""

    boot_package = None
    if flight is not None:
        boot_package = _flight_package(_State(flight, config))

    app.layout = html.Div(className="app", children=[
        # Header
        html.Div(className="header", children=[
            html.Div(className="brand", children=[
                html.Img(src="/assets/logo.svg", className="brand-logo",
                         alt="Flight Path Tracker"),
                html.Div("FLIGHT PATH TRACKER", className="title"),
            ]),
            html.Div(id="roster", className="roster"),
            html.Div(id="file-label", className="chip status"),
            html.Div(className="header-controls", children=[
                dcc.Dropdown(id="basemap", className="dd",
                             options=[{"label": s, "value": s} for s in config.basemap_styles],
                             value=config.default_style, clearable=False),
                dcc.Checklist(id="follow", className="follow",
                              options=[{"label": "FOLLOW", "value": "on"}], value=[]),
                html.Div(title="Full fidelity for the next file load: no upload "
                               "downsampling (exact metrics from every sample) and "
                               "much higher display/playback detail. Slower with "
                               "very large files.",
                         children=dcc.Checklist(
                             id="hifi", className="follow",
                             options=[{"label": "HI-FI", "value": "on"}], value=[])),
                dcc.Upload(id="upload", className="upload",
                           children=html.Div("⬆ ADD FILE"), multiple=False),
            ]),
        ]),

        # KPI cards (engine fills the values for the active flight)
        html.Div(id="kpis", className="kpis", children=_kpi_skeleton()),

        # Main: map | side panel
        html.Div(className="main", children=[
            html.Div(className="map-wrap", children=[
                dcc.Graph(id="map", className="map",
                          config={"displayModeBar": False, "scrollZoom": True},
                          figure=_map_shell(config)),
                html.Div(className="alt-legend", id="alt-legend",
                         style={"display": "none"}, children=[
                    html.Span("—", id="leg-max", className="leg-lab"),
                    html.Div(className="leg-bar"),
                    html.Span("—", id="leg-min", className="leg-lab"),
                    html.Span("ALT FT", className="leg-title"),
                ]),
            ]),
            html.Div(className="side", children=[
                html.Div(className="panel", children=[
                    html.Div("LIVE FLIGHT DATA", className="panel-title"),
                    html.Div(id="readout", className="readout",
                             children=_readout_skeleton()),
                ]),
                dcc.Graph(id="profiles", className="profiles",
                          config={"displayModeBar": False},
                          figure=_profiles_shell(config)),
                html.Div(id="seg-stats", className="seg-stats",
                         title="Drag horizontally across a profile to analyze a segment"),
            ]),
        ]),

        # Controls
        html.Div(className="controls", children=[
            html.Button("▶  PLAY", id="play", className="play"),
            dcc.Dropdown(id="speed", className="dd speed", clearable=False,
                         options=[{"label": f"{m}×", "value": m} for m in config.speed_multipliers],
                         value=25),
            dcc.Slider(id="scrub", min=0, max=1, value=0, step=0.1,
                       marks=None, updatemode="drag", included=True),
            html.Button("KML", id="export-kml", className="ghost"),
            html.Button("GPX", id="export-gpx", className="ghost"),
            html.Div(id="clock", className="clock", children="00:00:00 / 00:00:00"),
        ]),

        # Flight packages travel through this store; the engine keeps a roster.
        dcc.Store(id="engine-data", data=boot_package),
        dcc.Store(id="cs-ack"),
        dcc.Store(id="upload-csv"),
    ])

    _register_callbacks(app, config)
    return app


def _register_callbacks(app: Dash, config: AppConfig) -> None:
    # ---- clientside: engine wiring -----------------------------------------
    app.clientside_callback(
        "function(pkg){ if (window.FT) { window.FT.load(pkg); } return null; }",
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
    app.clientside_callback(
        "function(c, f, h){ var lossless = h && h.length > 0; "
        "return window.FT ? window.FT.prepUpload(c, f, "
        f"lossless ? 1e12 : {config.upload_max_rows}) : null; }}",
        Output("upload-csv", "data"),
        Input("upload", "contents"),
        State("upload", "filename"),
        State("hifi", "value"),
        prevent_initial_call=True,
    )

    # ---- server: parse a flight and return its package ---------------------
    load_outputs = (
        Output("engine-data", "data"),
        Output("scrub", "max"),
        Output("scrub", "step"),
        Output("scrub", "value"),
        Output("play", "children", allow_duplicate=True),
        Output("file-label", "children"),
    )

    def _error(name: str, err: object) -> tuple:
        return (no_update, no_update, no_update, no_update, no_update,
                f"⚠ {name}: {err}")

    def _render_text(text: str, name: str, label: str, hifi: bool) -> tuple:
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
        local = _State(flight, config, hifi=hifi)
        t_max = float(flight.t[-1])
        return (_flight_package(local, label), t_max,
                max(0.1, round(t_max / 2000.0, 2)), 0.0, "▶  PLAY", "")

    @app.callback(*load_outputs, Input("upload-csv", "data"),
                  State("hifi", "value"), prevent_initial_call=True)
    def _upload(staged, hifi_value):
        if not staged:
            return (no_update,) * 6
        hifi = bool(hifi_value)
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
        elif hifi and orig:
            label = f"{name} · full fidelity · {orig:,} rows"
        return _render_text(staged["csv"], name, label, hifi)


def run(flight: FlightData | None = None, config: AppConfig | None = None,
        debug: bool = False) -> None:
    """Build and serve the dashboard (blocking)."""
    config = config or AppConfig()
    app = create_app(flight, config)
    app.run(host=config.host, port=config.port, debug=debug)
