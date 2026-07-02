"""Application configuration and shared constants.

Centralizes the tunables (performance budgets, theme colors, map styles, server
host/port) so behavior is easy to adjust in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Token-free basemap styles (MapLibre via Plotly). ``carto-*`` and
# ``open-street-map`` require no access token; satellite styles would.
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

    # Animation cadence (ms). Playback advances by interval * speed each tick.
    interval_ms: int = 100

    # Trailing tracer window as a fraction of total flight duration.
    tracer_fraction: float = 0.04
    tracer_min_seconds: float = 60.0

    # Theme colors.
    accent: str = "#FFD133"        # amber: tracer + cursors
    color_alt: str = "#4CC9F0"     # altitude trace
    color_speed: str = "#80FFA5"   # speed trace
    bg: str = "#0E1116"
    panel: str = "#161B22"
    muted: str = "#9FB0C0"
    text: str = "#E6EDF3"

    colorscale: str = "Turbo"      # altitude color map

    # Map.
    default_style: str = "carto-darkmatter"
    basemap_styles: tuple[str, ...] = field(default_factory=lambda: BASEMAP_STYLES)
    speed_multipliers: tuple[int, ...] = field(default_factory=lambda: SPEED_MULTIPLIERS)

    # Server.
    host: str = "127.0.0.1"
    port: int = 8050
