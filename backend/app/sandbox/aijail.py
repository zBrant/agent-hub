"""Build the ai-jail argv for one node run (design.md §2.1, invariant 8).

Pure argv construction: no subprocess, no I/O, no async. The caller passes the
result to ``asyncio.create_subprocess_exec``.

Verified against **ai-jail 1.16.0** on macOS (Darwin 24.6, ``sandbox-exec`` /
seatbelt). Everything below was read off real ``--dry-run`` output and real
sandboxed runs, not off the documentation:

* ``--mask`` and ``--deny-path`` both become ``(deny file-read* ...)`` rules in
  the SBPL profile, and are enforced: ``cat .env`` returns "Operation not
  permitted" inside the sandbox.
* Globs are expanded **at launch, against files that already exist**, and a
  single ``*`` does not cross directory boundaries: ``*.pem`` only matched the
  sandbox cwd, ``**/*.pem`` matched every subdirectory. Hence the ``**/``
  prefixes in :func:`default_policy`.
* A mask entry with no glob character is emitted as a ``(subpath ...)`` rule
  even when the file does not exist yet, so it also covers a file the agent
  creates mid-run. That is why the default policy keeps a literal ``.env``
  alongside ``**/.env``.
* ``--worktree`` (default on) adds write access to the *parent* repository's
  ``.git`` and to ``.git/worktrees/<name>``, which is what lets a node commit
  from inside a linked worktree (invariant 2).
* ``--no-gpu`` is a Linux-only no-op on macOS. We still pass it unconditionally:
  the sandbox policy should read the same on both platforms, and the flag being
  inert here is ai-jail's business, not the caller's.
* Harness arguments after the positional preset are **not** consumed by ai-jail
  — option parsing stops at the first positional, so ``claude --mask X`` reaches
  the harness untouched. A ``--`` separator is accepted and produces a byte
  identical profile, but it is not required, so we do not emit one.

Two flags are here for reasons that are not obvious:

* ``--clean`` ignores a ``.ai-jail`` file sitting in the target repository. That
  file can weaken the policy (``--mask-except``), and it lives in a directory the
  agent can write to. The policy must come from AgentHub, never from the
  repository being worked on (invariant 8).
* ``--no-save-config`` stops ai-jail from writing a ``.ai-jail`` file into the
  cwd on every run. Verified: without it, the file appears in the worktree and
  lands in the node's diff.

Nothing in this module accepts a token, key, or credential — argv is visible in
``ps`` (docs/conventions.md §6).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

AIJAIL_BIN = "ai-jail"

# Presets ai-jail 1.16.0 recognizes as the positional command. Any other string
# is passed through as a plain command, so this is documentation rather than a
# whitelist: the harness -> preset mapping belongs in app/harnesses/.
AIJAIL_PRESETS = frozenset(
    {
        "gemini",
        "claude",
        "codex",
        "opencode",
        "crush",
        "soulforge",
        "grok",
        "pi",
        "bash",
    }
)


class SandboxPolicyError(Exception):
    """A sandbox policy that would produce a permissive command line.

    Reparent under ``AgentHubError`` once that base exists
    (docs/conventions.md §2).
    """


@dataclass(frozen=True)
class SandboxPolicy:
    """Default-deny sandbox policy for a single node run.

    ``mask`` entries are glob patterns interpreted by ai-jail relative to the
    sandbox cwd, not filesystem paths — ``**/*.pem`` is a pattern that no
    ``Path`` should pretend to be. ``deny_paths`` are real locations and must be
    absolute: ai-jail does not expand ``~``, so ``Path("~/.aws")`` would silently
    protect nothing.
    """

    mask: tuple[str, ...] = ()
    deny_paths: tuple[Path, ...] = ()
    worktree: bool = True
    # False -> --exec: no PTY proxy and no status bar between the harness and our
    # stream-json parser (Channel A). Channel B wants the default proxy mode.
    pty: bool = False
    docker: bool = False
    gpu: bool = False

    def __post_init__(self) -> None:
        if not self.mask and not self.deny_paths:
            raise SandboxPolicyError(
                "empty sandbox policy: at least one --mask or --deny-path is "
                "required (invariant 8)"
            )
        for pattern in self.mask:
            if not pattern.strip():
                raise SandboxPolicyError("empty mask pattern in sandbox policy")
        for path in self.deny_paths:
            if not path.is_absolute():
                raise SandboxPolicyError(
                    f"deny path must be absolute, got {path!r}: ai-jail does not "
                    "expand '~', resolve it when the policy is built"
                )


DEFAULT_MASKS: tuple[str, ...] = (
    # Literal first: emitted as a rule even when the file does not exist yet.
    ".env",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa*",
)

# Relative to the home directory; resolved in default_policy().
DEFAULT_DENY_HOME_DIRS: tuple[str, ...] = (
    ".aws",
    ".ssh",
    ".gnupg",
    ".config/gh",
)


def default_policy(home: Path | None = None, *, pty: bool = False) -> SandboxPolicy:
    """The policy every node run starts from (docs/conventions.md §6).

    ``home`` defaults to the real home directory and exists so tests do not
    depend on the machine they run on.
    """
    root = home if home is not None else Path.home()
    return SandboxPolicy(
        mask=DEFAULT_MASKS,
        deny_paths=tuple(root / relative for relative in DEFAULT_DENY_HOME_DIRS),
        pty=pty,
    )


def build_argv(
    policy: SandboxPolicy,
    harness: str,
    harness_args: Sequence[str] = (),
) -> list[str]:
    """Return the full argv: ``ai-jail [options] <preset> [harness args...]``.

    ``harness`` is the ai-jail preset name (``claude``, ``codex``, ...), not the
    AgentHub harness id.
    """
    if not harness or harness.startswith("-"):
        raise ValueError(
            f"harness preset must be a positional name, got {harness!r}; "
            f"known presets: {sorted(AIJAIL_PRESETS)}"
        )

    argv = build_launcher(policy)
    argv.append(harness)
    argv += harness_args
    return argv


def build_launcher(policy: SandboxPolicy) -> list[str]:
    """Return the sandbox prefix before ai-jail's positional harness preset.

    A harness adapter appends its own CLI command to this prefix, and that
    command simultaneously becomes ai-jail's preset. Keeping this composition
    explicit avoids duplicating ``claude``/``codex`` between two argv builders.
    """
    argv = [
        AIJAIL_BIN,
        "--clean",
        "--no-save-config",
        "--worktree" if policy.worktree else "--no-worktree",
    ]
    for pattern in policy.mask:
        argv += ["--mask", pattern]
    for path in policy.deny_paths:
        argv += ["--deny-path", str(path)]
    argv.append("--docker" if policy.docker else "--no-docker")
    argv.append("--gpu" if policy.gpu else "--no-gpu")
    if not policy.pty:
        argv.append("--exec")
    return argv
