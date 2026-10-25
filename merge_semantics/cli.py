"""Command line interface: ``merge-semantics check ...``."""

from __future__ import annotations

import argparse
import sys

from .detector import SEVERITY_ORDER, detect
from .gitutil import GitError
from .report import render_json, render_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merge-semantics",
        description="Post-merge semantic conflict checker for parallel "
        "coding agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser(
        "check",
        description="Check two branches against their merge-base for "
        "semantic conflicts git cannot see.",
    )
    check.add_argument("--repo", default=".", help="path to the git repo")
    check.add_argument("--branch-a", required=True, help="first branch/ref")
    check.add_argument("--branch-b", required=True, help="second branch/ref")
    check.add_argument(
        "--format", choices=["text", "json"], default="text",
        help="output format (default: text)",
    )
    check.add_argument(
        "--fail-on",
        choices=["high", "medium", "any", "never"],
        default="high",
        help="fail (exit 1) when a finding at or above this severity "
        "exists (default: high)",
    )
    return parser


def _should_fail(findings: list, fail_on: str) -> bool:
    if fail_on == "never":
        return False
    if fail_on == "any":
        return bool(findings)
    threshold = SEVERITY_ORDER[fail_on]
    return any(SEVERITY_ORDER[f["severity"]] >= threshold for f in findings)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        try:
            findings = detect(args.repo, args.branch_a, args.branch_b)
        except GitError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2
        if args.format == "json":
            print(render_json(findings), end="")
        else:
            print(render_text(findings), end="")
        return 1 if _should_fail(findings, args.fail_on) else 0
    return 2


def entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entry()

















































