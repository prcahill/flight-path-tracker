"""Dash + Plotly dashboard for replaying a flight path.

Builds an enterprise-style dark dashboard: KPI cards, an interactive world map
(altitude-colored path + animated aircraft marker), synced altitude/speed
profiles, and playback controls (play/pause, speed, scrubber).

Performance model
-----------------
The full :class:`FlightData` is held server-side. The static path is decimated
once to two level-of-detail budgets -- a cheap polyline for the ground track and
a much sparser altitude-colored marker overlay -- so even multi-million-point
logs render instantly and pan/zoom stays fluid. Each animation tick advances a
wall-clock-anchored playback clock, **interpolates the aircraft state between
samples** (position, altitude, speed, heading), and ships the frame to the
browser through a ``dcc.Store``; a clientside callback applies it with
``Plotly.restyle`` so the map is never re-rendered on the hot path -- the user
can pan and zoom freely while playback is running. Profile cursors and readouts
update via ``dash.Patch``; the path itself is never re-sent. With Follow
enabled the map center glides along the interpolated trajectory.
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
_LINE, _COLOR, _HALO, _DOT = 0, 1, 2, 3


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
        li = _decimate(flight.n, c.path_line_budget)
        self.line_lat = flight.lat[li]
        self.line_lon = flight.lon[li]
        mi = _decimate(flight.n, c.path_marker_budget)
        self.mark_lat = flight.lat[mi]
        self.mark_lon = flight.lon[mi]
        self.mark_alt = flight.alt[mi]
        pi = _decimate(flight.n, c.profile_budget)
        self.prof_t = flight.t[pi] / 60.0
        self.prof_alt = flight.alt[pi]
        self.prof_gs = flight.gs[pi]

    def sample_at(self, t: float) -> dict:
        """Aircraft state at data-time ``t``, interpolated between samples.

        Sub-sample interpolation keeps the marker (and Follow camera) moving
        smoothly along the trajectory regardless of the log's data rate.
        """
        f = self.flight
        tt = float(np.clip(t, f.t[0], f.t[-1]))
        i = int(np.searchsorted(f.t, tt, side="right")) - 1
        i = max(0, min(f.n - 2, i))
        j = i + 1
        dt = float(f.t[j] - f.t[i])
        w = 0.0 if dt <= 0 else (tt - float(f.t[i])) / dt

        def lerp(a: np.ndarray) -> float:
            return float(a[i] + (a[j] - a[i]) * w)

        # Heading interpolates along the shortest angular arc (359 -> 1 != 180).
        dh = ((float(f.hdg[j]) - float(f.hdg[i]) + 180.0) % 360.0) - 180.0
        return {
            "t": tt,
            "lat": lerp(f.lat),
            "lon": lerp(f.lon),
            "alt": lerp(f.alt),
            "gs": lerp(f.gs),
            "vs": lerp(f.vs),
            "dist": lerp(f.cum_nm),
            "hdg": (float(f.hdg[i]) + dh * w) % 360.0,
        }


# --------------------------------------------------------------------------- #
#  Figure builders
# --------------------------------------------------------------------------- #
def _map_figure(state: _State, s: dict | None = None) -> go.Figure:
    f = state.flight
    c = state.config
    s = s or state.sample_at(0.0)
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
        lat=[s["lat"]], lon=[s["lon"]], mode="markers",
        marker=dict(size=24, color=c.halo), hoverinfo="skip", name="halo",
    ))
    fig.add_trace(go.Scattermap(
        lat=[s["lat"]], lon=[s["lon"]], mode="markers",
        marker=dict(size=11, color=c.cursor), hoverinfo="skip", name="aircraft",
    ))
    lat_lim, lon_lim = f.summary.lat_lim, f.summary.lon_lim
    if state.follow:
        center = dict(lat=s["lat"], lon=s["lon"])
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


def _profiles_figure(state: _State, s: dict | None = None) -> go.Figure:
    c = state.config
    s = s or state.sample_at(0.0)
    cm = dict(color=c.cursor, size=8, line=dict(color=c.accent, width=2))
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.16,
                        subplot_titles=("ALTITUDE (FT)", "GROUND SPEED (KT)"))
    fig.add_trace(go.Scatter(x=state.prof_t, y=state.prof_alt, mode="lines",
                             line=dict(color=c.color_alt, width=1.5), hoverinfo="skip"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=[s["t"] / 60.0], y=[s["alt"]], mode="markers",
                             marker=cm, hoverinfo="skip"), row=1, col=1)
    fig.add_trace(go.Scatter(x=state.prof_t, y=state.prof_gs, mode="lines",
                             line=dict(color=c.color_speed, width=1.5), hoverinfo="skip"),
                  row=2, col=1)
    fig.add_trace(go.Scatter(x=[s["t"] / 60.0], y=[s["gs"]], mode="markers",
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


def _time_str(f: FlightData, t: float) -> str:
    if f.t0 is None:
        return format_duration(t)
    from datetime import timedelta
    return (f.t0 + timedelta(seconds=float(t))).strftime("%Y-%m-%d %H:%M:%S")


def _readout_children(f: FlightData, s: dict) -> list:
    rows = [
        ("Time", _time_str(f, s["t"])),
        ("Position", f"{s['lat']:.4f}°, {s['lon']:.4f}°"),
        ("Altitude", f"{_fmt_int(s['alt'])} ft"),
        ("Ground speed", f"{s['gs']:.0f} kt"),
        ("Heading", f"{s['hdg']:03.0f}°  {compass_point(s['hdg'])}"),
        ("Vertical speed", f"{_fmt_int(s['vs'])} ft/min"),
        ("Distance flown", f"{s['dist']:.1f} nm"),
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
    s0 = state.sample_at(0.0) if flight else None

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
                dcc.Dropdown(id="basemap", className="dd",
                             options=[{"label": s, "value": s} for s in config.basemap_styles],
                             value=config.default_style, clearable=False),
                dcc.Checklist(id="follow", className="follow",
                              options=[{"label": "FOLLOW", "value": "on"}], value=[]),
                dcc.Upload(id="upload", className="upload",
                           children=html.Div("⬆ LOAD CSV"), multiple=False),
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
                          figure=_map_figure(state, s0) if flight else go.Figure()),
            ]),
            html.Div(className="side", children=[
                html.Div(className="panel", children=[
                    html.Div("LIVE TELEMETRY", className="panel-title"),
                    html.Div(id="readout", className="readout",
                             children=_readout_children(flight, s0) if flight else []),
                ]),
                dcc.Graph(id="profiles", className="profiles",
                          config={"displayModeBar": False},
                          figure=_profiles_figure(state, s0) if flight else go.Figure()),
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
            html.Div(id="clock", className="clock", children="00:00:00 / 00:00:00"),
        ]),

        dcc.Interval(id="tick", interval=config.interval_ms, disabled=True, n_intervals=0),
        # Per-tick aircraft sample, applied client-side via Plotly.restyle so
        # the map never re-renders during playback (keeps drag/pan alive).
        dcc.Store(id="frame"),
        dcc.Store(id="frame-ack"),
    ])

    _register_callbacks(app, state, config)
    return app


def _frame_updates(state: _State, s: dict):
    """Build the per-frame outputs: frame store, profiles Patch, readout, clock.

    The aircraft marker is NOT patched into the map figure here -- the frame
    dict goes to a ``dcc.Store`` and a clientside callback applies it with
    ``Plotly.restyle``, which updates trace data without re-rendering the map.
    That keeps an in-progress drag alive, so the user can pan/zoom freely
    while playback is running.
    """
    f = state.flight
    frame = {"lat": s["lat"], "lon": s["lon"], "follow": state.follow}

    pp = Patch()
    tmin = s["t"] / 60.0
    pp["data"][1]["x"] = [tmin]
    pp["data"][1]["y"] = [s["alt"]]
    pp["data"][3]["x"] = [tmin]
    pp["data"][3]["y"] = [s["gs"]]

    clock = f"{format_duration(s['t'])} / {format_duration(f.t[-1])}"
    return frame, pp, _readout_children(f, s), clock


def _patch_marker(p: Patch, s: dict) -> Patch:
    """Refresh the aircraft marker inside a server-side map figure Patch.

    Server patches (basemap / follow) re-render the map from Dash's stored
    figure, which no longer tracks the marker (the hot path bypasses it via
    ``Plotly.restyle``). Including the current position prevents a snap-back.
    """
    p["data"][_HALO]["lat"] = [s["lat"]]
    p["data"][_HALO]["lon"] = [s["lon"]]
    p["data"][_DOT]["lat"] = [s["lat"]]
    p["data"][_DOT]["lon"] = [s["lon"]]
    return p


def _register_callbacks(app: Dash, state: _State, config: AppConfig) -> None:
    # Apply each frame client-side: restyle only the halo/dot traces (no map
    # re-render, so an in-progress drag survives), and in Follow mode glide
    # the camera along with the aircraft.
    app.clientside_callback(
        """
        function(frame) {
            if (!frame) { return window.dash_clientside.no_update; }
            var el = document.getElementById('map');
            var gd = el && el.querySelector('.js-plotly-plot');
            if (!gd || !gd.data || gd.data.length < 4) {
                return window.dash_clientside.no_update;
            }
            Plotly.restyle(gd, {
                lat: [[frame.lat], [frame.lat]],
                lon: [[frame.lon], [frame.lon]],
            }, [2, 3]);
            if (frame.follow) {
                Plotly.relayout(gd, {'map.center': {lat: frame.lat, lon: frame.lon}});
            }
            return window.dash_clientside.no_update;
        }
        """,
        Output("frame-ack", "data"),
        Input("frame", "data"),
        prevent_initial_call=True,
    )

    # Playback tick: advance the wall-clock-anchored clock and update the
    # scrubber AND all visuals in a single round-trip (so the map cannot lag
    # behind the scrubber).
    @app.callback(
        Output("scrub", "value", allow_duplicate=True),
        Output("frame", "data", allow_duplicate=True),
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
        s = state.sample_at(data_t)
        frame, pp, ro, clock = _frame_updates(state, s)
        disabled = True if ended else no_update
        label = "▶  PLAY" if ended else no_update
        return data_t, frame, pp, ro, clock, disabled, label

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
            return True, "▶  PLAY", no_update
        at_end = (value or 0.0) >= f.t[-1]
        start_t = 0.0 if at_end else float(value or 0.0)
        state.cur_time = start_t
        state.anchor_data = start_t
        state.anchor_wall = time.monotonic()
        state.last_speed = None
        return False, "❚❚  PAUSE", (0.0 if at_end else no_update)

    # Manual scrub (only while paused; during playback the tick owns updates).
    @app.callback(
        Output("frame", "data", allow_duplicate=True),
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
        return _frame_updates(state, state.sample_at(state.cur_time))

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
        s = state.sample_at(state.cur_time)
        p = _patch_marker(Patch(), s)  # keep marker in sync (see _patch_marker)
        if state.follow:
            p["layout"]["map"]["center"] = {"lat": s["lat"], "lon": s["lon"]}
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
        p = _patch_marker(Patch(), state.sample_at(state.cur_time))
        p["layout"]["map"]["style"] = style
        return p

    # Upload a new CSV -> reload everything.
    @app.callback(
        Output("map", "figure", allow_duplicate=True),
        Output("profiles", "figure", allow_duplicate=True),
        Output("kpis", "children"),
        Output("file-label", "children"),
        Output("scrub", "max"),
        Output("scrub", "step"),
        Output("scrub", "value", allow_duplicate=True),
        Output("tick", "disabled", allow_duplicate=True),
        Output("play", "children", allow_duplicate=True),
        Input("upload", "contents"),
        State("upload", "filename"),
        prevent_initial_call=True,
    )
    def _upload(contents, filename):
        if not contents:
            return (no_update,) * 9
        _, b64 = contents.split(",", 1)
        raw = base64.b64decode(b64)
        with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        try:
            flight = load_flight_data(tmp_path)
        except Exception as exc:  # noqa: BLE001 - surface parse errors to the UI
            return (no_update, no_update, no_update, f"⚠ {filename}: {exc}",
                    no_update, no_update, no_update, no_update, no_update)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        flight.file = filename or "uploaded.csv"
        state.set_flight(flight)
        t_max = float(flight.t[-1])
        return (_map_figure(state), _profiles_figure(state), _kpi_children(flight),
                filename, t_max, max(0.1, round(t_max / 2000.0, 2)), 0.0,
                True, "▶  PLAY")


def run(flight: FlightData | None = None, config: AppConfig | None = None,
        debug: bool = False) -> None:
    """Build and serve the dashboard (blocking)."""
    config = config or AppConfig()
    app = create_app(flight, config)
    app.run(host=config.host, port=config.port, debug=debug)
