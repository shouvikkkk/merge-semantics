"""End-to-end tests: build real synthetic git repos in temp dirs and
run the checker against them, exactly as a user would."""

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
    tmp = tempfile.mkdtemp(prefix="merge-semantics-test-")
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
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return proc


def findings_json(proc):
    return json.loads(proc.stdout)


def kinds(proc):
    return [f["kind"] for f in findings_json(proc)["findings"]]


class TestMergeSemantics(unittest.TestCase):
    def test_rename_vs_old_caller(self):
        repo = make_repo(self, {
            "utils.py": "def old_name():\n    return 1\n",
            "caller.py": "from utils import old_name\n\n\ndef run():\n    return old_name()\n",
        })
        branch_from_main(repo, "agent-a", {
            "utils.py": "def new_name():\n    return 1\n",
            "caller.py": "from utils import new_name\n\n\ndef run():\n    return new_name()\n",
        }, "rename old_name to new_name")
        branch_from_main(repo, "agent-b", {
            "feature.py": "from utils import old_name\n\n\ndef feature():\n    return old_name()\n",
        }, "add feature using old_name")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        found = findings_json(proc)["findings"]
        self.assertTrue(
            any(f["kind"] == "rename-vs-old-caller" and f["symbol"] == "old_name"
                for f in found),
            proc.stdout,
        )
        # The specific rename finding must win over the generic one.
        self.assertFalse(
            any(f["kind"] == "removed-symbol-still-referenced"
                and f["symbol"] == "old_name" for f in found),
            proc.stdout,
        )

    def test_removed_symbol_still_referenced(self):
        repo = make_repo(self, {
            "utils.py": "def helper():\n    return 1\n",
        })
        branch_from_main(repo, "agent-a", {
            "utils.py": "def other():\n    return 2\n",
        }, "drop helper")
        branch_from_main(repo, "agent-b", {
            "feature.py": "from utils import helper\n\n\ndef feature():\n    return helper()\n",
        }, "add feature using helper")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        found = findings_json(proc)["findings"]
        self.assertTrue(
            any(f["kind"] == "removed-symbol-still-referenced"
                and f["symbol"] == "helper" for f in found),
            proc.stdout,
        )

    def test_duplicate_definition(self):
        repo = make_repo(self, {"main.py": "def main():\n    return 0\n"})
        branch_from_main(repo, "agent-a", {
            "a.py": "def helper():\n    return 'a'\n",
        }, "add helper in a")
        branch_from_main(repo, "agent-b", {
            "b.py": "def helper():\n    return 'b'\n",
        }, "add helper in b")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        found = findings_json(proc)["findings"]
        self.assertTrue(
            any(f["kind"] == "duplicate-definition" and f["symbol"] == "helper"
                for f in found),
            proc.stdout,
        )

    def test_shared_registry_collision_lines(self):
        repo = make_repo(self, {"routes.txt": "/home\n"})
        branch_from_main(repo, "agent-a", {
            "routes.txt": "/home\n/dashboard\n",
        }, "add dashboard route")
        branch_from_main(repo, "agent-b", {
            "routes.txt": "/home\n/settings\n",
        }, "add settings route")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        self.assertTrue(
            any(k == "shared-registry-collision" for k in kinds(proc)),
            proc.stdout,
        )

    def test_shared_registry_collision_same_key(self):
        repo = make_repo(self, {".env.example": "BASE=1\n"})
        branch_from_main(repo, "agent-a", {
            ".env.example": "BASE=1\nSHARED_KEY=aaa\n",
        }, "add shared key a")
        branch_from_main(repo, "agent-b", {
            ".env.example": "BASE=1\nSHARED_KEY=bbb\n",
        }, "add shared key b")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        found = findings_json(proc)["findings"]
        self.assertTrue(
            any(f["kind"] == "shared-registry-collision"
                and f["symbol"] == "SHARED_KEY" for f in found),
            proc.stdout,
        )

    def test_migration_number_collision(self):
        repo = make_repo(self, {"README.md": "app\n"})
        branch_from_main(repo, "agent-a", {
            "migrations/0003_add_users.py": "def up():\n    pass\n",
        }, "add migration 0003 users")
        branch_from_main(repo, "agent-b", {
            "migrations/0003_add_posts.py": "def up():\n    pass\n",
        }, "add migration 0003 posts")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        self.assertTrue(
            any(k == "shared-registry-collision" for k in kinds(proc)),
            proc.stdout,
        )

    def test_contract_drift_env(self):
        repo = make_repo(self, {".env": "API_KEY=secret\nOTHER=1\n"})
        branch_from_main(repo, "agent-a", {
            ".env": "NEW_API_KEY=secret\nOTHER=1\n",
        }, "rename API_KEY")
        branch_from_main(repo, "agent-b", {
            "app.py": 'import os\n\n\ndef key():\n    return os.getenv("API_KEY")\n',
        }, "read API_KEY")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        found = findings_json(proc)["findings"]
        self.assertTrue(
            any(f["kind"] == "contract-drift" and f["symbol"] == "API_KEY"
                for f in found),
            proc.stdout,
        )

    def test_contract_drift_json(self):
        repo = make_repo(self, {
            "config.json": '{\n  "api_key": "secret",\n  "other": 1\n}\n',
        })
        branch_from_main(repo, "agent-a", {
            "config.json": '{\n  "other": 1\n}\n',
        }, "drop api_key")
        branch_from_main(repo, "agent-b", {
            "app.py": 'CONFIG = {}\n\n\ndef key():\n    return CONFIG["api_key"]\n',
        }, "read api_key")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        found = findings_json(proc)["findings"]
        self.assertTrue(
            any(f["kind"] == "contract-drift" and f["symbol"] == "api_key"
                for f in found),
            proc.stdout,
        )

    def test_clean_same_file_overlap_produces_no_findings(self):
        repo = make_repo(self, {
            "app.py": "def func_a():\n    return 1\n\n\ndef func_b():\n    return 2\n",
        })
        branch_from_main(repo, "agent-a", {
            "app.py": "def func_a():\n    return 10\n\n\ndef func_b():\n    return 2\n\n\ndef func_c():\n    return 3\n",
        }, "tweak a, add c")
        branch_from_main(repo, "agent-b", {
            "app.py": "def func_a():\n    return 1\n\n\ndef func_b():\n    return 20\n\n\ndef func_d():\n    return 4\n",
        }, "tweak b, add d")
        proc = run_cli(repo, "agent-a", "agent-b", ["--format", "json"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(findings_json(proc)["findings"], [], proc.stdout)

    def test_json_output_and_exit_codes(self):
        repo = make_repo(self, {"main.py": "def main():\n    return 0\n"})
        branch_from_main(repo, "agent-a", {
            "a.py": "def helper():\n    return 'a'\n",
        }, "add helper in a")
        branch_from_main(repo, "agent-b", {
            "b.py": "def helper():\n    return 'b'\n",
        }, "add helper in b")
        # Duplicate-definition is medium severity.
        proc = run_cli(repo, "agent-a", "agent-b",
                       ["--format", "json", "--fail-on", "high"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = findings_json(proc)
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["findings"][0]["kind"], "duplicate-definition")
        self.assertIn("branches", payload["findings"][0])
        self.assertIn("evidence", payload["findings"][0])
        proc = run_cli(repo, "agent-a", "agent-b",
                       ["--format", "json", "--fail-on", "medium"])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        proc = run_cli(repo, "agent-a", "agent-b",
                       ["--format", "json", "--fail-on", "never"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # Text output is human-readable and also exits 1 on "any".
        proc = run_cli(repo, "agent-a", "agent-b", ["--fail-on", "any"])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("duplicate-definition", proc.stdout)


if __name__ == "__main__":
    unittest.main()
