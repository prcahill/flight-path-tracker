"""WSGI entry point for production servers.

Run with, e.g.:

    gunicorn --workers 1 --threads 8 --bind 0.0.0.0:$PORT flighttracker.wsgi:server

The flight log is resolved in this order:

1. ``FLIGHT_LOG`` environment variable, if set;
2. ``data/sample_flight_KSFO_KLAX.csv`` relative to the working directory
   (present when serving from a checkout of the repo, e.g. on Render);
3. a sample flight generated into the system temp directory on first boot.

Note: the app keeps flight and playback state server-side in a single
session, so a deployment of this entry point is a single-user demo -- run it
with ONE worker process (threads are fine).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .app import create_app
from .config import AppConfig
from .data import load_flight_data
from .sample import generate_sample_flight_data


def _resolve_flight_log() -> Path:
    env = os.environ.get("FLIGHT_LOG")
    if env:
        return Path(env)
    repo_sample = Path("data/sample_flight_KSFO_KLAX.csv")
    if repo_sample.is_file():
        return repo_sample
    tmp_sample = Path(tempfile.gettempdir()) / "sample_flight_KSFO_KLAX.csv"
    if not tmp_sample.is_file():
        generate_sample_flight_data(tmp_sample)
    return tmp_sample


app = create_app(load_flight_data(_resolve_flight_log()), AppConfig())
server = app.server  # the Flask WSGI application gunicorn serves
