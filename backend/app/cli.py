"""Local AgentHub command line entry point."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agenthub")
    subcommands = parser.add_subparsers(dest="command", required=True)
    serve = subcommands.add_parser("serve", help="run the local web application")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        # Local-only is a product boundary, not a user-configurable default.
        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port)
        return 0
    raise AssertionError(f"unhandled command {args.command!r}")
