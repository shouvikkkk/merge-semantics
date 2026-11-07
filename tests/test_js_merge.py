"""End-to-end tests for JavaScript/TypeScript support: build real
synthetic git repos in temp dirs and run the checker against them,
exactly as a user would.  (Harness copied from test_merge_semantics,
kept separate so the Python suite stays untouched.)"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        check=True,
        capture_output=True,
        text=True,
    )


def commit(repo, files, message):
    for name, content in files.items():
        path = Path(repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def make_repo(testcase, base_files):
    tmp = tempfile.mkdtemp(prefix="merge-semantics-js-test-")
    testcase.addCleanup(lambda: subprocess.run(["rm", "-rf", tmp]))
    repo = Path(tmp)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    commit(repo, base_files, "base")
    return repo


def branch_from_main(repo, name, files, message):
    git(repo, "checkout", "-q", "-b", name, "main")
    commit(repo, files, message)


def run_cli(repo, branch_a, branch_b, extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    cmd = [
        sys.executable, "-m", "merge_semantics", "check",
        "--repo", str(repo),
        "--branch-a", branch_a, "--branch-b", branch_b,
    ] + (extra or [])
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def findings_json(proc):
    return json.loads(proc.stdout)


class TestJsMergeSemantics(unittest.TestCase):
    def test_js_rename_vs_old_caller(self):
        repo = make_repo(self, {
            "utils.js": "export function oldName() {\n  return 1;\n}\n",
            "caller.js": "import { oldName } from './utils.js';\n\n"
                         "export function run() {\n  return oldName();\n}\n",
        })
        branch_from_main(repo, "agent-a", {
            "utils.js": "export function newName() {\n  return 1;\n}\n",
            "caller.js": "import { newName } from './utils.js';\n\n"
                         "export function run() {\n  return newName();\n}\n",
        }, "rename oldName to newName")
        branch_from_main(repo, "agent-b", {
            "feature.js": "import { oldName } from './utils.js';\n\n"
                          "export function feature() {\n  return oldName();\n}\n",
        }, "add feature using oldName")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        found = findings_json(proc)["findings"]
        self.assertTrue(
            any(f["kind"] == "rename-vs-old-caller"
                and f["symbol"] == "oldName" for f in found),
            proc.stdout,
        )
        self.assertFalse(
            any(f["kind"] == "removed-symbol-still-referenced"
                and f["symbol"] == "oldName" for f in found),
            proc.stdout,
        )

    def test_js_removed_symbol_still_referenced(self):
        repo = make_repo(self, {
            "utils.js": "export function helper() {\n  return 1;\n}\n",
        })
        branch_from_main(repo, "agent-a", {
            "utils.js": "export function other() {\n  return 2;\n}\n",
        }, "drop helper")
        branch_from_main(repo, "agent-b", {
            "feature.js": "import { helper } from './utils.js';\n\n"
                          "export function feature() {\n  return helper();\n}\n",
        }, "add feature using helper")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        found = findings_json(proc)["findings"]
        self.assertTrue(
            any(f["kind"] == "removed-symbol-still-referenced"
                and f["symbol"] == "helper" for f in found),
            proc.stdout,
        )

    def test_js_duplicate_definition(self):
        repo = make_repo(self, {"main.js": "export function main() {\n  return 0;\n}\n"})
        branch_from_main(repo, "agent-a", {
            "a.js": "export function helper() {\n  return 'a';\n}\n",
        }, "add helper in a")
        branch_from_main(repo, "agent-b", {
            "b.js": "export const helper = () => 'b';\n",
        }, "add helper in b")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        found = findings_json(proc)["findings"]
        self.assertTrue(
            any(f["kind"] == "duplicate-definition"
                and f["symbol"] == "helper" for f in found),
            proc.stdout,
        )

    def test_js_class_method_rename(self):
        # The canonical parallel-agent failure, in the language most
        # agent-built projects use: one agent renames a method, the
        # other adds a call to the old name.
        repo = make_repo(self, {
            "widget.js": "export class Widget {\n"
                         "  render() {\n    return 1;\n  }\n}\n",
            "app.js": "import { Widget } from './widget.js';\n\n"
                      "const w = new Widget();\n"
                      "export function run() {\n  return w.render();\n}\n",
        })
        branch_from_main(repo, "agent-a", {
            "widget.js": "export class Widget {\n"
                         "  paint() {\n    return 1;\n  }\n}\n",
            "app.js": "import { Widget } from './widget.js';\n\n"
                      "const w = new Widget();\n"
                      "export function run() {\n  return w.paint();\n}\n",
        }, "rename Widget.render to paint")
        branch_from_main(repo, "agent-b", {
            "feature.js": "import { Widget } from './widget.js';\n\n"
                          "const w = new Widget();\n"
                          "export function feature() {\n  return w.render();\n}\n",
        }, "add feature calling render")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        found = findings_json(proc)["findings"]
        self.assertTrue(
            any(f["kind"] == "rename-vs-old-caller"
                and f["symbol"] == "Widget.render" for f in found),
            proc.stdout,
        )

    def test_python_method_rename(self):
        # Same improvement, Python side: method renames are matched on
        # the unqualified method name at the call site.
        repo = make_repo(self, {
            "widget.py": "class Widget:\n    def render(self):\n        return 1\n",
            "app.py": "from widget import Widget\n\n\ndef run():\n    return Widget().render()\n",
        })
        branch_from_main(repo, "agent-a", {
            "widget.py": "class Widget:\n    def paint(self):\n        return 1\n",
            "app.py": "from widget import Widget\n\n\ndef run():\n    return Widget().paint()\n",
        }, "rename Widget.render to paint")
        branch_from_main(repo, "agent-b", {
            "feature.py": "from widget import Widget\n\n\ndef feature():\n    return Widget().render()\n",
        }, "add feature calling render")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        found = findings_json(proc)["findings"]
        self.assertTrue(
            any(f["kind"] == "rename-vs-old-caller"
                and f["symbol"] == "Widget.render" for f in found),
            proc.stdout,
        )

    def test_ts_type_rename(self):
        # Type names in annotations are recorded as references, so a
        # renamed type whose old name is still used is caught.
        repo = make_repo(self, {
            "types.ts": "export type OldId = string;\n",
        })
        branch_from_main(repo, "agent-a", {
            "types.ts": "export type NewId = string;\n",
        }, "rename OldId to NewId")
        branch_from_main(repo, "agent-b", {
            "user.ts": "import { OldId } from './types.js';\n\n"
                       "export function get(id: OldId): OldId {\n"
                       "  return id;\n}\n",
        }, "add user code using OldId")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        found = findings_json(proc)["findings"]
        self.assertTrue(
            any(f["kind"] == "removed-symbol-still-referenced"
                and f["symbol"] == "OldId" for f in found),
            proc.stdout,
        )

    def test_js_clean_same_file_overlap_produces_no_findings(self):
        repo = make_repo(self, {
            "app.js": "export function funcA() {\n  return 1;\n}\n\n"
                      "export function funcB() {\n  return 2;\n}\n",
        })
        branch_from_main(repo, "agent-a", {
            "app.js": "export function funcA() {\n  return 10;\n}\n\n"
                      "export function funcB() {\n  return 2;\n}\n\n"
                      "export function funcC() {\n  return 3;\n}\n",
        }, "tweak a, add c")
        branch_from_main(repo, "agent-b", {
            "app.js": "export function funcA() {\n  return 1;\n}\n\n"
                      "export function funcB() {\n  return 20;\n}\n\n"
                      "export function funcD() {\n  return 4;\n}\n",
        }, "tweak b, add d")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(findings_json(proc)["findings"], [], proc.stdout)

    def test_mixed_python_js_clean(self):
        # Both languages checked in one run; no false positives from
        # JSX text, strings, or cross-language name sharing.
        repo = make_repo(self, {
            "app.py": "def main():\n    return 0\n",
            "ui.jsx": "export function Panel() {\n"
                      "  return <div>Loading</div>;\n}\n",
        })
        branch_from_main(repo, "agent-a", {
            "app.py": "def main():\n    return 1\n",
        }, "tweak python")
        branch_from_main(repo, "agent-b", {
            "ui.jsx": "export function Panel() {\n"
                      "  return <div>Loading</div>;\n}\n\n"
                      "export function Footer() {\n"
                      "  return <div>Done</div>;\n}\n",
        }, "add footer")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(findings_json(proc)["findings"], [], proc.stdout)

    def test_js_registry_collision_still_works(self):
        # JS files must not be treated as registry files, and the
        # registry check must still fire for real registry files.
        repo = make_repo(self, {"routes.txt": "/home\n"})
        branch_from_main(repo, "agent-a", {
            "routes.txt": "/home\n/dashboard\n",
            "a.js": "export function helper() {\n  return 'a';\n}\n",
        }, "add dashboard route and helper")
        branch_from_main(repo, "agent-b", {
            "routes.txt": "/home\n/settings\n",
            "b.js": "export function helper() {\n  return 'b';\n}\n",
        }, "add settings route and helper")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        found = findings_json(proc)["findings"]
        self.assertTrue(
            any(f["kind"] == "shared-registry-collision" for f in found),
            proc.stdout,
        )
        self.assertTrue(
            any(f["kind"] == "duplicate-definition"
                and f["symbol"] == "helper" for f in found),
            proc.stdout,
        )


if __name__ == "__main__":
    unittest.main()
