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

    # Level-of-detail budgets (max points actually drawn). The flight path is
    # rendered as a cheap polyline plus a much sparser altitude-colored marker
    # overlay -- large logs are decimated to these budgets, never re-sent.
    path_line_budget: int = 20_000    # ground-track polyline
    path_marker_budget: int = 4_000   # altitude-colored marker overlay
    engine_budget: int = 6_000        # profiles + client-side playback arrays
    upload_max_rows: int = 50_000     # larger uploads are decimated in-browser

    # Theme colors (General Atomics-style navy / blue).
    bg: str = "#0A1526"            # app background
    panel: str = "#101E38"         # panels / cards / figure backgrounds
    panel_2: str = "#16294A"       # inputs / dropdowns
    border: str = "#26385C"
    text: str = "#EAF1FB"
    muted: str = "#8FA3C2"
    accent: str = "#2C7BE5"        # GA blue: buttons, highlights
    halo: str = "rgba(44,123,229,0.38)"  # soft ring under the aircraft marker
    cursor: str = "#FFFFFF"        # chart cursor dots + aircraft marker
    color_alt: str = "#5AA9FF"     # altitude trace
    color_speed: str = "#8FE388"   # speed trace
    grid: str = "#1C2E52"          # chart grid lines

    colorscale: str = "Turbo"      # altitude color map

    # Map.
    default_style: str = "carto-darkmatter"
    follow_zoom: float = 8.5       # zoom level used when "Follow" is on
    basemap_styles: tuple[str, ...] = field(default_factory=lambda: BASEMAP_STYLES)
    speed_multipliers: tuple[int, ...] = field(default_factory=lambda: SPEED_MULTIPLIERS)

    # Server.
    host: str = "127.0.0.1"
    port: int = 8050
