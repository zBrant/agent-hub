"""Golden tests for the ai-jail argv builder.

The exact-argv test is the point of the file: it is what will fail loudly when
someone reorders flags or quietly drops one from the default policy.
"""

import dataclasses
import shutil
import subprocess
from pathlib import Path

import pytest

from app.sandbox.aijail import (
    DEFAULT_DENY_HOME_DIRS,
    SandboxPolicy,
    SandboxPolicyError,
    build_argv,
    default_policy,
)

FAKE_HOME = Path("/home/tester")

EXPECTED_DEFAULT_ARGV = [
    "ai-jail",
    "--clean",
    "--no-save-config",
    "--worktree",
    "--mask",
    ".env",
    "--mask",
    "**/.env",
    "--mask",
    "**/.env.*",
    "--mask",
    "**/*.pem",
    "--mask",
    "**/*.key",
    "--mask",
    "**/id_rsa*",
    "--deny-path",
    "/home/tester/.aws",
    "--deny-path",
    "/home/tester/.ssh",
    "--deny-path",
    "/home/tester/.gnupg",
    "--deny-path",
    "/home/tester/.config/gh",
    "--no-docker",
    "--no-gpu",
    "--exec",
    "claude",
    "-p",
    "--output-format",
    "stream-json",
    "--input-format",
    "stream-json",
    "--verbose",
]

CHANNEL_A_ARGS = [
    "-p",
    "--output-format",
    "stream-json",
    "--input-format",
    "stream-json",
    "--verbose",
]


def test_default_policy_argv_is_exact() -> None:
    argv = build_argv(default_policy(FAKE_HOME), "claude", CHANNEL_A_ARGS)
    assert argv == EXPECTED_DEFAULT_ARGV


def test_default_policy_covers_the_mandatory_secrets() -> None:
    policy = default_policy(FAKE_HOME)
    assert ".env" in policy.mask
    for suffix in (".env.*", "*.pem", "*.key", "id_rsa*"):
        assert f"**/{suffix}" in policy.mask
    assert policy.deny_paths == tuple(FAKE_HOME / d for d in DEFAULT_DENY_HOME_DIRS)


def test_empty_policy_raises() -> None:
    # Invariant 8: no masks and no deny-paths must never become `ai-jail claude`.
    with pytest.raises(SandboxPolicyError, match="empty sandbox policy"):
        SandboxPolicy()


def test_dropping_every_rule_from_the_default_policy_raises() -> None:
    policy = default_policy(FAKE_HOME)
    with pytest.raises(SandboxPolicyError, match="empty sandbox policy"):
        dataclasses.replace(policy, mask=(), deny_paths=())


def test_a_single_rule_is_enough() -> None:
    argv = build_argv(SandboxPolicy(mask=(".env",)), "codex")
    assert argv[-1] == "codex"
    assert "--mask" in argv


def test_blank_mask_pattern_raises() -> None:
    with pytest.raises(SandboxPolicyError, match="empty mask pattern"):
        SandboxPolicy(mask=("  ",))


def test_relative_deny_path_raises() -> None:
    # ai-jail does not expand '~', so an unresolved path protects nothing.
    with pytest.raises(SandboxPolicyError, match="must be absolute"):
        SandboxPolicy(deny_paths=(Path("~/.aws"),))


def test_channel_a_gets_exec_and_channel_b_does_not() -> None:
    policy = default_policy(FAKE_HOME)

    channel_a = build_argv(policy, "claude")
    assert "--exec" in channel_a

    channel_b = build_argv(dataclasses.replace(policy, pty=True), "claude")
    assert "--exec" not in channel_b

    # --exec is the only difference between the two channels.
    assert [a for a in channel_a if a != "--exec"] == channel_b


def test_masks_and_deny_paths_repeat_one_flag_per_entry_in_order() -> None:
    policy = SandboxPolicy(
        mask=("a.txt", "b.txt", "c.txt"),
        deny_paths=(Path("/srv/one"), Path("/srv/two")),
    )
    argv = build_argv(policy, "bash")

    assert argv.count("--mask") == 3
    assert argv.count("--deny-path") == 2

    masks = [argv[i + 1] for i, a in enumerate(argv) if a == "--mask"]
    denies = [argv[i + 1] for i, a in enumerate(argv) if a == "--deny-path"]
    assert masks == ["a.txt", "b.txt", "c.txt"]
    assert denies == ["/srv/one", "/srv/two"]


def test_worktree_flag_is_always_explicit() -> None:
    policy = default_policy(FAKE_HOME)
    assert "--worktree" in build_argv(policy, "claude")
    assert "--no-worktree" in build_argv(
        dataclasses.replace(policy, worktree=False), "claude"
    )


def test_docker_and_gpu_flags_are_always_explicit() -> None:
    policy = default_policy(FAKE_HOME)
    assert {"--no-docker", "--no-gpu"} <= set(build_argv(policy, "claude"))

    permissive = dataclasses.replace(policy, docker=True, gpu=True)
    assert {"--docker", "--gpu"} <= set(build_argv(permissive, "claude"))


def test_project_config_is_ignored_and_never_written() -> None:
    # A .ai-jail inside the target repo could weaken the policy, and ai-jail
    # otherwise writes one into the worktree, polluting the node's diff.
    argv = build_argv(default_policy(FAKE_HOME), "claude")
    assert "--clean" in argv
    assert "--no-save-config" in argv


def test_harness_args_follow_the_preset_without_a_separator() -> None:
    # Verified against ai-jail 1.16.0: option parsing stops at the first
    # positional, so a harness flag is never captured by the sandbox. A '--'
    # is accepted but redundant, so the builder does not emit one.
    argv = build_argv(default_policy(FAKE_HOME), "claude", ["--mask", "not-a-sandbox"])
    assert "--" not in argv
    assert argv[-3:] == ["claude", "--mask", "not-a-sandbox"]


def test_no_harness_args_ends_at_the_preset() -> None:
    assert build_argv(default_policy(FAKE_HOME), "opencode")[-1] == "opencode"


@pytest.mark.parametrize("harness", ["", "--exec", "-p"])
def test_a_harness_that_looks_like_a_flag_raises(harness: str) -> None:
    with pytest.raises(ValueError, match="positional name"):
        build_argv(default_policy(FAKE_HOME), harness)


@pytest.mark.harness
def test_real_aijail_accepts_the_argv_and_emits_the_deny_rules(
    tmp_path: Path,
) -> None:
    """Contract test against the installed binary (ai-jail 1.16.0, macOS).

    The SBPL profile is the only honest proof that our flags mean what we think.
    """
    if shutil.which("ai-jail") is None:
        pytest.skip("ai-jail is not installed")

    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / ".env").write_text("PLACEHOLDER=1\n")
    (repo / "sub" / "deploy.key").write_text("PLACEHOLDER\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    argv = build_argv(default_policy(home), "claude", CHANNEL_A_ARGS)

    result = subprocess.run(
        [argv[0], "--dry-run", *argv[1:]],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    profile = result.stdout

    assert "(deny default)" in profile
    # Masks are expanded against existing files, recursively thanks to '**/'.
    assert f'(deny file-read* (literal "{(repo / ".env").resolve()}"))' in profile
    assert (
        f'(deny file-read* (literal "{(repo / "sub" / "deploy.key").resolve()}"))'
        in profile
    )
    for relative in DEFAULT_DENY_HOME_DIRS:
        assert f'(deny file-read* (subpath "{home / relative}"))' in profile

    # The harness argv reaches the harness, not the sandbox.
    assert "claude -p --output-format stream-json" in profile
    # --no-save-config held: no config file dropped into the worktree.
    assert not (repo / ".ai-jail").exists()
