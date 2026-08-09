"""A stand-in for a harness CLI, driven through the adapter's own ``launcher``.

Structured completion spawns a process, so testing it without a paid turn needs
a fake binary. The trick is that both adapters build argv as
``[*launcher, CLI_COMMAND, *flags]`` — so a launcher of
``("python3", "-c", script)`` makes the real CLI name a plain argument of a
script we control. Nothing in the adapter is monkey-patched, and the launcher
being honored (invariant 8) is exercised rather than asserted.

The fake records what it was given: its argv, everything it read on stdin, and
the contents of any file the adapter pointed it at. That is what lets a test
check the three §6 rules at once — the prompt is on stdin, the prompt is not in
``ps``, and the temp files are gone by the time the call returns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Flags whose value is a path the adapter writes just before launching and
# deletes just after. The fake reads them while they still exist.
FILE_FLAGS = ("--output-schema", "--system-prompt-file")

_BODY = """
argv = sys.argv[1:]
stdin = sys.stdin.read()
record = CONFIG["record"]
if record:
    files = {}
    for flag in CONFIG["file_flags"]:
        if flag in argv:
            path = pathlib.Path(argv[argv.index(flag) + 1])
            files[flag] = path.read_text() if path.exists() else None
    pathlib.Path(record).write_text(
        json.dumps({"argv": argv, "stdin": stdin, "files": files})
    )
sys.stdout.write(CONFIG["stdout"])
sys.stderr.write(CONFIG["stderr"])
sys.exit(CONFIG["exit_code"])
"""


def fake_cli(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    record: Path | None = None,
) -> tuple[str, ...]:
    """A ``launcher`` prefix that answers instead of the real CLI."""
    config = {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "record": str(record) if record is not None else None,
        "file_flags": list(FILE_FLAGS),
    }
    script = (
        "import json, pathlib, sys\n"
        f"CONFIG = json.loads({json.dumps(config)!r})\n" + _BODY
    )
    return ("python3", "-c", script)


@dataclass(frozen=True)
class Probe:
    """What the fake CLI saw."""

    argv: list[str]
    stdin: str
    files: dict[str, str | None]

    def value_of(self, flag: str) -> str:
        """The single value following ``flag``. Fails loudly if absent."""
        assert flag in self.argv, f"{flag} missing from {self.argv}"
        return self.argv[self.argv.index(flag) + 1]

    def file_content(self, flag: str) -> str | None:
        return self.files.get(flag)

    def path_of(self, flag: str) -> Path:
        return Path(self.value_of(flag))


def read_probe(record: Path) -> Probe:
    assert record.exists(), "the fake CLI was never launched"
    payload = json.loads(record.read_text())
    return Probe(
        argv=list(payload["argv"]),
        stdin=str(payload["stdin"]),
        files=dict(payload["files"]),
    )
