"""Dash + Plotly dashboard for replaying a flight path.

Builds an enterprise-style dark dashboard: KPI cards, an interactive world map
(altitude-colored path + animated marker and trailing tracer), synced
altitude/speed profiles, and playback controls (play/pause, speed, scrubber).

Performance: the full :class:`FlightData` is held server-side; the static path is
decimated to a display budget; each animation tick advances a master clock (the
scrubber) and updates only the marker/tracer/cursor/readouts via ``dash.Patch``
— the full path is never re-sent. This mirrors the real-time, frame-skipping
playback of the original MATLAB app so dense logs stay smooth.
"""

from __future__ import annotations

import base64
import tempfile
import time
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
from dash import Dash, Input, Output, Patch, State, dcc, html, no_update
from plotly.subplots import make_subplots

from .config import AppConfig
from .data import load_flight_data
from .metrics import FlightData, compass_point, format_duration

# Map trace indices (order matters for Patch updates).
_BASE, _ALT, _TRACER, _MARKER = 0, 1, 2, 3


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
    """Server-side holder for the loaded flight and precomputed display arrays."""

    def __init__(self, flight: FlightData | None, config: AppConfig):
        self.config = config
        self.style = config.default_style
        self.follow = False
        # Playback clock (wall-clock anchored so speed is accurate and jitter-proof).
        self.cur_time = 0.0
        self.anchor_wall = 0.0
        self.anchor_data = 0.0
        self.last_speed: float | None = None
        self.set_flight(flight)

    def set_flight(self, flight: FlightData | None) -> None:
        self.flight = flight
        self.cur_time = 0.0
        if flight is None:
            return
        c = self.config
        d = _decimate(flight.n, c.display_budget)
        self.disp_lat = flight.lat[d]
        self.disp_lon = flight.lon[d]
        self.disp_alt = flight.alt[d]
        p = _decimate(flight.n, c.profile_budget)
        self.prof_t = flight.t[p] / 60.0
        self.prof_alt = flight.alt[p]
        self.prof_gs = flight.gs[p]
        self.tracer_sec = max(c.tracer_min_seconds, c.tracer_fraction * flight.summary.duration_s)

    def index_at(self, t: float) -> int:
        """Sample index at data-time ``t`` seconds (clamped)."""
        f = self.flight
        idx = int(np.searchsorted(f.t, t, side="right")) - 1
        return max(0, min(f.n - 1, idx))

    def tracer_slice(self, idx: int) -> tuple[np.ndarray, np.ndarray]:
        f = self.flight
        start = int(np.searchsorted(f.t, f.t[idx] - self.tracer_sec, side="left"))
        sl = slice(start, idx + 1)
        lat = f.lat[sl]
        lon = f.lon[sl]
        if lat.size > self.config.tracer_budget:
            k = np.linspace(0, lat.size - 1, self.config.tracer_budget).astype(int)
            lat, lon = lat[k], lon[k]
        return lat, lon


# --------------------------------------------------------------------------- #
#  Figure builders
# --------------------------------------------------------------------------- #
def _map_figure(state: _State, idx: int = 0) -> go.Figure:
    f = state.flight
    c = state.config
    tr_lat, tr_lon = state.tracer_slice(idx)
    fig = go.Figure()
    fig.add_trace(go.Scattermap(
        lat=state.disp_lat, lon=state.disp_lon, mode="lines",
        line=dict(width=1, color="rgba(255,255,255,0.25)"),
        hoverinfo="skip", name="path",
    ))
    fig.add_trace(go.Scattermap(
        lat=state.disp_lat, lon=state.disp_lon, mode="markers",
        marker=dict(size=6, color=state.disp_alt, colorscale=c.colorscale,
                    showscale=True,
                    colorbar=dict(title="Alt (ft)", x=0.99, thickness=12,
                                  tickfont=dict(color=c.text), title_font=dict(color=c.text))),
        hoverinfo="skip", name="altitude",
    ))
    fig.add_trace(go.Scattermap(
        lat=tr_lat, lon=tr_lon, mode="lines",
        line=dict(width=4, color=c.tracer_color), hoverinfo="skip", name="tracer",
    ))
    fig.add_trace(go.Scattermap(
        lat=[f.lat[idx]], lon=[f.lon[idx]], mode="markers",
        marker=dict(size=14, color=c.cursor), hoverinfo="skip", name="aircraft",
    ))
    lat_lim, lon_lim = f.summary.lat_lim, f.summary.lon_lim
    if state.follow:
        center = dict(lat=float(f.lat[idx]), lon=float(f.lon[idx]))
        zoom = c.follow_zoom
    else:
        center = dict(lat=float(np.mean(lat_lim)), lon=float(np.mean(lon_lim)))
        zoom = _zoom_for(lat_lim, lon_lim)
    fig.update_layout(
        map=dict(style=state.style, center=center, zoom=zoom),
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


def _profiles_figure(state: _State, idx: int = 0) -> go.Figure:
    f = state.flight
    c = state.config
    cm = dict(color=c.cursor, size=9, line=dict(color=c.accent, width=2))
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.12,
                        subplot_titles=("Altitude (ft)", "Ground speed (kt)"))
    fig.add_trace(go.Scatter(x=state.prof_t, y=state.prof_alt, mode="lines",
                             line=dict(color=c.color_alt, width=1.6), hoverinfo="skip"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=[f.t[idx] / 60.0], y=[f.alt[idx]], mode="markers",
                             marker=cm, hoverinfo="skip"), row=1, col=1)
    fig.add_trace(go.Scatter(x=state.prof_t, y=state.prof_gs, mode="lines",
                             line=dict(color=c.color_speed, width=1.6), hoverinfo="skip"),
                  row=2, col=1)
    fig.add_trace(go.Scatter(x=[f.t[idx] / 60.0], y=[f.gs[idx]], mode="markers",
                             marker=cm, hoverinfo="skip"), row=2, col=1)
    fig.update_xaxes(title_text="Time (min)", row=2, col=1)
    fig.update_layout(
        template="plotly_dark", showlegend=False,
        margin=dict(l=48, r=16, t=28, b=36),
        paper_bgcolor=c.panel, plot_bgcolor=c.panel, font=dict(color=c.muted),
        height=300, uirevision="keep",
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


def _time_str(f: FlightData, idx: int) -> str:
    if f.t0 is None:
        return format_duration(f.t[idx])
    from datetime import timedelta
    return (f.t0 + timedelta(seconds=float(f.t[idx]))).strftime("%Y-%m-%d %H:%M:%S")


def _readout_children(f: FlightData, idx: int) -> list:
    rows = [
        ("Time", _time_str(f, idx)),
        ("Position", f"{f.lat[idx]:.4f}°, {f.lon[idx]:.4f}°"),
        ("Altitude", f"{_fmt_int(f.alt[idx])} ft"),
        ("Ground speed", f"{f.gs[idx]:.0f} kt"),
        ("Heading", f"{f.hdg[idx]:03.0f}°  {compass_point(f.hdg[idx])}"),
        ("Vertical speed", f"{_fmt_int(f.vs[idx])} ft/min"),
        ("Distance flown", f"{f.cum_nm[idx]:.1f} nm"),
    ]
    return [html.Div(className="rd-row", children=[
        html.Span(label, className="rd-label"),
        html.Span(value, className="rd-value"),
    ]) for label, value in rows]


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
    )

    file_label = Path(flight.file).name if flight and flight.file else "no file loaded"
    t_max = float(flight.t[-1]) if flight else 1.0

    app.layout = html.Div(className="app", children=[
        # Header
        html.Div(className="header", children=[
            html.Div("✈  FLIGHT PATH TRACKER", className="title"),
            html.Div(id="file-label", className="file-label", children=file_label),
            html.Div(className="header-controls", children=[
                dcc.Dropdown(id="basemap", className="dd",
                             options=[{"label": s, "value": s} for s in config.basemap_styles],
                             value=config.default_style, clearable=False),
                dcc.Checklist(id="follow", className="follow",
                              options=[{"label": " Follow", "value": "on"}], value=[]),
                dcc.Upload(id="upload", className="upload",
                           children=html.Div("⬆ Load CSV"), multiple=False),
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
                    html.Div("LIVE", className="panel-title"),
                    html.Div(id="readout", className="readout",
                             children=_readout_children(flight, 0) if flight else []),
                ]),
                dcc.Graph(id="profiles", className="profiles", config={"displayModeBar": False},
                          figure=_profiles_figure(state) if flight else go.Figure()),
            ]),
        ]),

        # Controls
        html.Div(className="controls", children=[
            html.Button("▶  Play", id="play", className="play"),
            dcc.Dropdown(id="speed", className="dd speed", clearable=False,
                         options=[{"label": f"{m}×", "value": m} for m in config.speed_multipliers],
                         value=25),
            dcc.Slider(id="scrub", min=0, max=t_max, value=0,
                       step=max(0.1, round(t_max / 1000.0, 2)),
                       marks=None, updatemode="drag", included=True),
            html.Div(id="clock", className="clock", children="00:00:00 / 00:00:00"),
        ]),

        dcc.Interval(id="tick", interval=config.interval_ms, disabled=True, n_intervals=0),
    ])

    _register_callbacks(app, state, config)
    return app


def _frame_updates(state: _State, idx: int):
    """Build the per-frame Patch updates (map, profiles) + readout + clock.

    Only trace data is patched (and the map center, when Follow is on), so the
    user's pan/zoom is preserved and the map stays interactive during playback.
    """
    f = state.flight
    mp = Patch()
    tr_lat, tr_lon = state.tracer_slice(idx)
    mp["data"][_TRACER]["lat"] = tr_lat.tolist()
    mp["data"][_TRACER]["lon"] = tr_lon.tolist()
    mp["data"][_MARKER]["lat"] = [float(f.lat[idx])]
    mp["data"][_MARKER]["lon"] = [float(f.lon[idx])]
    if state.follow:
        mp["layout"]["map"]["center"] = {"lat": float(f.lat[idx]), "lon": float(f.lon[idx])}

    pp = Patch()
    tmin = float(f.t[idx] / 60.0)
    pp["data"][1]["x"] = [tmin]
    pp["data"][1]["y"] = [float(f.alt[idx])]
    pp["data"][3]["x"] = [tmin]
    pp["data"][3]["y"] = [float(f.gs[idx])]

    clock = f"{format_duration(f.t[idx])} / {format_duration(f.t[-1])}"
    return mp, pp, _readout_children(f, idx), clock


def _register_callbacks(app: Dash, state: _State, config: AppConfig) -> None:
    # Playback tick: advance the wall-clock-anchored clock and update the
    # scrubber AND all visuals in a single round-trip (so the map cannot lag
    # behind the scrubber).
    @app.callback(
        Output("scrub", "value", allow_duplicate=True),
        Output("map", "figure", allow_duplicate=True),
        Output("profiles", "figure", allow_duplicate=True),
        Output("readout", "children", allow_duplicate=True),
        Output("clock", "children", allow_duplicate=True),
        Output("tick", "disabled", allow_duplicate=True),
        Output("play", "children", allow_duplicate=True),
        Input("tick", "n_intervals"),
        State("speed", "value"),
        prevent_initial_call=True,
    )
    def _play_tick(_n, speed):
        f = state.flight
        if f is None:
            return (no_update,) * 7
        speed = speed or 1
        if speed != state.last_speed:  # re-anchor on speed change (no time jump)
            state.anchor_data = state.cur_time
            state.anchor_wall = time.monotonic()
            state.last_speed = speed
        data_t = state.anchor_data + (time.monotonic() - state.anchor_wall) * speed
        ended = data_t >= f.t[-1]
        if ended:
            data_t = float(f.t[-1])
        state.cur_time = data_t
        idx = state.index_at(data_t)
        mp, pp, ro, clock = _frame_updates(state, idx)
        disabled = True if ended else no_update
        label = "▶  Play" if ended else no_update
        return data_t, mp, pp, ro, clock, disabled, label

    # Play / pause. Starting (re)anchors the clock; restarts from 0 when at end.
    @app.callback(
        Output("tick", "disabled"),
        Output("play", "children"),
        Output("scrub", "value", allow_duplicate=True),
        Input("play", "n_clicks"),
        State("tick", "disabled"), State("scrub", "value"),
        prevent_initial_call=True,
    )
    def _play(_clicks, disabled, value):
        f = state.flight
        if f is None:
            return no_update, no_update, no_update
        if not disabled:  # currently playing -> pause
            state.cur_time = value or 0.0
            return True, "▶  Play", no_update
        at_end = (value or 0.0) >= f.t[-1]
        start_t = 0.0 if at_end else float(value or 0.0)
        state.cur_time = start_t
        state.anchor_data = start_t
        state.anchor_wall = time.monotonic()
        state.last_speed = None
        return False, "❚❚  Pause", (0.0 if at_end else no_update)

    # Manual scrub (only while paused; during playback the tick owns updates).
    @app.callback(
        Output("map", "figure", allow_duplicate=True),
        Output("profiles", "figure", allow_duplicate=True),
        Output("readout", "children", allow_duplicate=True),
        Output("clock", "children", allow_duplicate=True),
        Input("scrub", "value"),
        State("tick", "disabled"),
        prevent_initial_call=True,
    )
    def _seek(value, disabled):
        if state.flight is None or not disabled:
            return (no_update,) * 4
        state.cur_time = float(value or 0.0)
        return _frame_updates(state, state.index_at(state.cur_time))

    # Follow toggle: on -> zoom in and track the aircraft; off -> leave the map
    # entirely to the user (fully interactive) and refit to the whole flight.
    @app.callback(
        Output("map", "figure", allow_duplicate=True),
        Input("follow", "value"),
        prevent_initial_call=True,
    )
    def _follow(value):
        f = state.flight
        if f is None:
            return no_update
        state.follow = bool(value) and "on" in value
        idx = state.index_at(state.cur_time)
        p = Patch()
        if state.follow:
            p["layout"]["map"]["center"] = {"lat": float(f.lat[idx]), "lon": float(f.lon[idx])}
            p["layout"]["map"]["zoom"] = config.follow_zoom
        else:
            lat_lim, lon_lim = f.summary.lat_lim, f.summary.lon_lim
            p["layout"]["map"]["center"] = {
                "lat": float(np.mean(lat_lim)), "lon": float(np.mean(lon_lim))}
            p["layout"]["map"]["zoom"] = _zoom_for(lat_lim, lon_lim)
        return p

    # Basemap style change: patch just the style so the current view is kept.
    @app.callback(
        Output("map", "figure", allow_duplicate=True),
        Input("basemap", "value"),
        prevent_initial_call=True,
    )
    def _basemap(style):
        if state.flight is None:
            return no_update
        state.style = style
        p = Patch()
        p["layout"]["map"]["style"] = style
        return p

    # Upload a new CSV -> reload everything.
    @app.callback(
        Output("map", "figure", allow_duplicate=True),
        Output("profiles", "figure", allow_duplicate=True),
        Output("kpis", "children"),
        Output("file-label", "children"),
        Output("scrub", "max"),
        Output("scrub", "value", allow_duplicate=True),
        Output("tick", "disabled", allow_duplicate=True),
        Output("play", "children", allow_duplicate=True),
        Input("upload", "contents"),
        State("upload", "filename"),
        prevent_initial_call=True,
    )
    def _upload(contents, filename):
        if not contents:
            return (no_update,) * 8
        _, b64 = contents.split(",", 1)
        raw = base64.b64decode(b64)
        with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        try:
            flight = load_flight_data(tmp_path)
        except Exception as exc:  # noqa: BLE001 - surface parse errors to the UI
            return (no_update, no_update, no_update, f"⚠ {filename}: {exc}",
                    no_update, no_update, no_update, no_update)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        flight.file = filename or "uploaded.csv"
        state.set_flight(flight)
        return (_map_figure(state), _profiles_figure(state), _kpi_children(flight),
                filename, float(flight.t[-1]), 0.0, True, "▶  Play")


def run(flight: FlightData | None = None, config: AppConfig | None = None,
        debug: bool = False) -> None:
    """Build and serve the dashboard (blocking)."""
    config = config or AppConfig()
    app = create_app(flight, config)
    app.run(host=config.host, port=config.port, debug=debug)
