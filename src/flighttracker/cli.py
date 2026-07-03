"""Command-line interface: ``flighttracker run`` and ``flighttracker generate``."""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import AppConfig
from .sample import generate_sample_flight_data, generate_sample_klv_text

_DEFAULT_SAMPLE = "data/sample_flight_KSFO_KLAX.csv"
_DEFAULT_KLV_SAMPLE = "data/sample_uav_telemetry.txt"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flighttracker",
        description="Replay aircraft flight paths from lat/lon/altitude logs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Launch the interactive dashboard.")
    run.add_argument("-f", "--file", help="Flight log CSV to open on startup.")
    run.add_argument("--host", default=AppConfig.host, help="Bind host (default: 127.0.0.1).")
    run.add_argument("--port", type=int, default=AppConfig.port, help="Bind port (default: 8050).")
    run.add_argument("--debug", action="store_true", help="Run Dash in debug mode.")

    gen = sub.add_parser("generate", help="Write a realistic sample flight log.")
    gen.add_argument("-o", "--out", default=None, help="Output path.")
    gen.add_argument("--format", choices=("csv", "klv"), default="csv",
                     help="csv: airliner track CSV; klv: frame-text KLV "
                          "telemetry dump (MISB ST 0601-style).")
    gen.add_argument("--minutes", type=float, default=None,
                     help="Flight duration in minutes (default 60 csv / 25 klv).")
    gen.add_argument("--rate", type=float, default=None,
                     help="Sample rate in Hz (default 5 csv / 10 klv).")

    return parser


def _cmd_generate(args: argparse.Namespace) -> int:
    if args.format == "klv":
        generate_sample_klv_text(args.out or _DEFAULT_KLV_SAMPLE,
                                 args.minutes or 25.0, args.rate or 10.0)
    else:
        generate_sample_flight_data(args.out or _DEFAULT_SAMPLE,
                                    args.minutes or 60.0, args.rate or 5.0)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    # Imported lazily so ``generate`` does not require Dash/Plotly.
    from .app import run
    from .data import load_flight_data

    flight = None
    path = args.file
    if path is None and Path(_DEFAULT_SAMPLE).is_file():
        path = _DEFAULT_SAMPLE
        print(f"No --file given; using {path}")
    if path:
        flight = load_flight_data(path)

    config = AppConfig(host=args.host, port=args.port)
    print(f"Serving dashboard at http://{args.host}:{args.port}  (Ctrl+C to stop)")
    run(flight, config, debug=args.debug)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "generate":
        return _cmd_generate(args)
    if args.command == "run":
        return _cmd_run(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
