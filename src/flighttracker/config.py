"""Application configuration and shared constants.

Centralizes the tunables (performance budgets, theme colors, map styles, server
host/port) so behavior is easy to adjust in one place. The color palette follows
a General Atomics-style scheme: deep navy surfaces, blue accents, light text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Token-free basemap styles (MapLibre via Plotly). ``carto-*`` and
# ``open-street-map`` require no access token (they do fetch tiles online).
BASEMAP_STYLES: tuple[str, ...] = (
    "carto-darkmatter",
    "carto-positron",
    "open-street-map",
    "carto-voyager",
)

# Playback speed multipliers offered in the UI.
SPEED_MULTIPLIERS: tuple[int, ...] = (1, 5, 25, 100, 500)


@dataclass(frozen=True)
class AppConfig:
    """Immutable configuration for the dashboard."""

    # Performance / level-of-detail budgets (max points actually drawn).
    display_budget: int = 30_000   # static altitude-colored path
    tracer_budget: int = 1_500     # trailing tracer polyline
    profile_budget: int = 8_000    # altitude / speed mini-charts

    # Animation cadence (ms). Playback is timed off the wall clock.
    interval_ms: int = 100

    # Trailing tracer window as a fraction of total flight duration.
    tracer_fraction: float = 0.04
    tracer_min_seconds: float = 60.0

    # Theme colors (General Atomics-style navy / blue).
    bg: str = "#0A1526"            # app background
    panel: str = "#101E38"         # panels / cards / figure backgrounds
    panel_2: str = "#16294A"       # inputs / dropdowns
    border: str = "#26385C"
    text: str = "#EAF1FB"
    muted: str = "#8FA3C2"
    accent: str = "#2C7BE5"        # GA blue: buttons, highlights
    tracer_color: str = "#26C6FF"  # bright cyan tracer (pops on dark map)
    cursor: str = "#FFFFFF"        # chart cursor dots + aircraft marker
    color_alt: str = "#5AA9FF"     # altitude trace
    color_speed: str = "#8FE388"   # speed trace

    colorscale: str = "Turbo"      # altitude color map

    # Map.
    default_style: str = "carto-darkmatter"
    follow_zoom: float = 8.0       # zoom level used when "Follow" is on
    basemap_styles: tuple[str, ...] = field(default_factory=lambda: BASEMAP_STYLES)
    speed_multipliers: tuple[int, ...] = field(default_factory=lambda: SPEED_MULTIPLIERS)

    # Server.
    host: str = "127.0.0.1"
    port: int = 8050
