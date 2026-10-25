"""Render findings as human-readable text or machine-readable JSON."""

from __future__ import annotations

import json


def render_text(findings: list) -> str:
    if not findings:
        return "No semantic conflicts found.\n"
    lines = ["Found %d semantic conflict(s):" % len(findings), ""]
    for finding in findings:
        lines.append(
            "[%s] %s: %s"
            % (finding["severity"].upper(), finding["kind"], finding["symbol"])
        )
        lines.append("  branches: %s" % ", ".join(finding["branches"]))
        lines.append("  %s" % finding["explanation"])
        for ev in finding["evidence"]:
            where = ev["file"]
            if ev.get("line") is not None:
                where += ":%s" % ev["line"]
            lines.append("    - %s %s (%s)" % (ev["branch"], where, ev["detail"]))
        lines.append("")
    return "\n".join(lines)


def render_json(findings: list) -> str:
    counts = {}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    payload = {
        "findings": findings,
        "summary": {
            "total": len(findings),
            "by_severity": counts,
        },
    }
    return json.dumps(payload, indent=2) + "\n"
