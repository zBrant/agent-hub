#!/usr/bin/env python
"""Export deterministic OpenAPI and AgentEvent schemas for frontend generation."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.harnesses.events import agent_event_adapter
from app.main import create_app

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_ROOT = BACKEND_ROOT / "schemas"
OPENAPI_PATH = SCHEMAS_ROOT / "openapi.json"
AGENT_EVENT_PATH = SCHEMAS_ROOT / "agent-event.schema.json"


def render(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def documents() -> tuple[tuple[Path, str], ...]:
    return (
        (OPENAPI_PATH, render(create_app().openapi())),
        (AGENT_EVENT_PATH, render(agent_event_adapter.json_schema())),
    )


def export(*, check: bool = False) -> bool:
    clean = True
    for path, expected in documents():
        if check:
            actual = path.read_text(encoding="utf-8") if path.exists() else None
            if actual != expected:
                print(f"schema out of date: {path}", file=sys.stderr)
                clean = False
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8")
        print(f"wrote {path}")
    return clean


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    return 0 if export(check=args.check) else 1


if __name__ == "__main__":
    raise SystemExit(main())
