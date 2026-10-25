"""Small wrappers around git plumbing commands.

All commands are run with an argument list (never ``shell=True``), so
branch names, paths and other caller-supplied values cannot inject
extra shell commands.  Any git failure raises ``GitError`` with the
command's stderr attached.
"""

from __future__ import annotations

import subprocess
from collections import Counter


class GitError(RuntimeError):
    """Raised when a git plumbing command fails."""


def run_git(repo: str, args: list) -> str:
    """Run ``git -C <repo> <args>`` and return stdout as text."""
    proc = subprocess.run(
        ["git", "-C", repo] + list(args),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitError(
            "git %s failed: %s" % (" ".join(args), proc.stderr.strip())
        )
    return proc.stdout


def try_git(repo: str, args: list) -> str | None:
    """Like :func:`run_git`, but return ``None`` instead of raising."""
    proc = subprocess.run(
        ["git", "-C", repo] + list(args),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def merge_base(repo: str, ref_a: str, ref_b: str) -> str:
    """Return the merge-base commit of two refs."""
    return run_git(repo, ["merge-base", ref_a, ref_b]).strip()


def changed_files(repo: str, base: str, ref: str) -> list:
    """Return paths changed between ``base`` and ``ref`` (sorted)."""
    out = run_git(repo, ["diff", "--name-only", base, ref])
    return sorted(p for p in out.splitlines() if p.strip())


def list_files(repo: str, ref: str) -> list:
    """Return every tracked path at ``ref`` (sorted)."""
    out = run_git(repo, ["ls-tree", "-r", "--name-only", ref])
    return sorted(p for p in out.splitlines() if p.strip())


def file_content(repo: str, ref: str, path: str) -> str | None:
    """Return the content of ``path`` at ``ref``, or ``None`` if absent."""
    return try_git(repo, ["show", "%s:%s" % (ref, path)])


def added_lines(base_content: str | None, branch_content: str | None) -> list:
    """Return lines present in the branch version but not in the base.

    Uses a multiset difference so that a line added twice is reported
    twice, and lines merely moved around are not mistaken for additions
    when they already existed in the base version.
    """
    base_lines = (base_content or "").splitlines()
    branch_lines = (branch_content or "").splitlines()
    counts = Counter(base_lines)
    added = []
    for line in branch_lines:
        if counts[line] > 0:
            counts[line] -= 1
        else:
            added.append(line)
    return added

















































