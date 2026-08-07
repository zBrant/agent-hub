"""Local AgentHub command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from app.config import Settings
from app.models.pricing import PricingError, load_price_history
from app.storage.db import Database
from app.storage.meta import MetaError
from app.storage.replay import ReplayError, ReplayResult, replay_run
from app.storage.repository import RepositoryError, repository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agenthub")
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve = subcommands.add_parser("serve", help="run the local web application")
    serve.add_argument("--port", type=int, default=8000)

    replay = subcommands.add_parser(
        "replay",
        help="rebuild a run's SQLite projection from its NDJSON log",
        description=(
            "Discards the run's derived rows and rebuilds them from "
            "runs/<run_id>/events.ndjson. Sessions and nodes are authored input "
            "and are never touched. Usage is repriced with the price table "
            "version pinned in the run's meta.json, and the command refuses if "
            "that version is no longer in pricing.yaml."
        ),
    )
    replay.add_argument("run_id")
    replay.add_argument(
        "--root", type=Path, default=None, help="AgentHub root (default ~/.agenthub)"
    )
    replay.add_argument(
        "--pricing",
        type=Path,
        default=None,
        help=(
            "price history (default: AGENTHUB_PRICING_PATH or repository pricing.yaml)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        # Local-only is a product boundary, not a user-configurable default.
        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port)
        return 0
    if args.command == "replay":
        return asyncio.run(
            _replay(run_id=args.run_id, root=args.root, pricing=args.pricing)
        )
    raise AssertionError(f"unhandled command {args.command!r}")


async def _replay(*, run_id: str, root: Path | None, pricing: Path | None) -> int:
    settings = Settings() if root is None else Settings(root=root)
    if not settings.db_path.exists():
        print(f"no database at {settings.db_path}", file=sys.stderr)
        return 1

    try:
        prices = load_price_history(pricing or settings.pricing_path)
    except PricingError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    database = Database.from_settings(settings)
    try:
        async with repository(database) as repo:
            result = await replay_run(
                repository=repo,
                runs_root=settings.runs_root,
                run_id=run_id,
                prices=prices,
            )
    except (ReplayError, MetaError, PricingError, RepositoryError) as exc:
        # A refusal is the designed outcome, not a crash: a missing price table
        # or a missing node means the rebuild would have invented something.
        print(f"replay refused: {exc}", file=sys.stderr)
        return 1
    finally:
        await database.dispose()

    _report(result)
    return 0


def _report(result: ReplayResult) -> None:
    counts = result.totals.counts
    print(f"run {result.run_id} → node {result.node_id}")
    print(f"  events        {result.events}")
    print(
        f"  usage rows    {result.usage_events} "
        f"(priced with table v{result.price_table_version})"
    )
    print(f"  status        {result.run_status.value}")
    print(
        f"  tokens        in={counts.input_tokens} out={counts.output_tokens} "
        f"cache_read={counts.cache_read_tokens} "
        f"cache_write={counts.cache_write_tokens}"
    )
    if result.totals.cost_usd is None:
        print("  cost          unknown")
    else:
        # Invariant 7: under a subscription there is no per-token billing.
        print(f"  cost          ${result.totals.cost_usd:.4f} estimated equivalent")
    if not result.totals.complete:
        print(f"  {result.totals.unpriced_events} usage row(s) had no known price")
    if result.truncated:
        print(
            f"  log truncated at line {result.truncated_line}; run marked interrupted"
        )
    if result.permission_denials:
        print(f"  {result.permission_denials} permission denial(s): do not merge")
    if not result.trusted:
        print("  parser did not fully trust this run's stream: do not merge")
