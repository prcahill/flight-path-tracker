"""Flight Path Tracker — replay aircraft flight paths from lat/lon/altitude logs.

Public API:
    load_flight_data          -- read a log text file into a FlightData
    generate_sample_flight_data -- write a realistic sample log
    create_app                -- build the Dash dashboard
    FlightData, FlightSummary -- data containers
    AppConfig                 -- dashboard configuration
"""

from __future__ import annotations

from .config import AppConfig
from .data import load_flight_data
from .metrics import FlightData, FlightSummary
from .sample import generate_sample_flight_data

__version__ = "0.2.1"

__all__ = [
    "AppConfig",
    "FlightData",
    "FlightSummary",
    "__version__",
    "create_app",
    "generate_sample_flight_data",
    "load_flight_data",
]


def create_app(*args, **kwargs):
    """Lazy proxy for :func:`flighttracker.app.create_app`.

    Imported lazily so that ``load_flight_data`` / ``generate_sample_flight_data``
    can be used without Dash/Plotly being imported.
    """
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)
