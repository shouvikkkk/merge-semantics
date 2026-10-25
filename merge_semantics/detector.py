"""Finding logic: compare two branches against their merge-base.

Everything here works from already-extracted symbol tables and file
contents, so the detectors are pure functions of three git trees:
``base``, ``branch_a`` and ``branch_b``.  A finding is only emitted
when two branches genuinely contradict each other; two branches
touching the same file without a symbol-level contradiction produce
nothing (see the negative-control test).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from . import gitutil
from .symbols import TreeSymbols, parse_file

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}

# Path fragments that mark a non-Python file as a shared registry:
# route tables, URL maps, registries, feature-flag lists, blueprints.
_REGISTRY_MARKERS = (
    "route",
    "urls",
    "registry",
    "feature_flag",
    "feature-flag",
    "flags",
    "blueprint",
)
_REGISTRY_BASENAMES = {".env.example", ".env.sample", ".env.template"}


def _word_in(text: str, name: str) -> bool:
    """True if ``name`` appears in ``text`` as a standalone token."""
    pattern = r"(?<![A-Za-z0-9_.])" + re.escape(name) + r"(?![A-Za-z0-9_])"
    return re.search(pattern, text) is not None


def _flatten_json_keys(value, prefix: str = "") -> set:
    keys = set()
    if isinstance(value, dict):
        for key, child in value.items():
            dotted = "%s.%s" % (prefix, key) if prefix else str(key)
            keys.add(dotted)
            keys.update(_flatten_json_keys(child, dotted))
    elif isinstance(value, list):
        for item in value:
            keys.update(_flatten_json_keys(item, prefix))
    return keys


def config_keys(content: str | None, path: str) -> set | None:
    """Return the set of config keys in a file, or ``None`` if it is
    not a recognised config file (.env-style, JSON, YAML or TOML)."""
    if content is None:
        return set()
    lower = path.lower()
    base = lower.rsplit("/", 1)[-1]
    if ".env" in base:
        keys = set()
        for line in content.splitlines():
            match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if match:
                keys.add(match.group(1))
        return keys
    if lower.endswith(".json"):
        try:
            return _flatten_json_keys(json.loads(content))
        except (ValueError, TypeError):
            return set()
    if lower.endswith((".yml", ".yaml", ".toml")):
        keys = set()
        for line in content.splitlines():
            if line.strip().startswith("#"):
                continue
            match = re.match(r"\s*([A-Za-z0-9_][A-Za-z0-9_-]*)\s*[:=]", line)
            if match:
                keys.add(match.group(1))
        return keys
    return None


def _is_registry_path(path: str) -> bool:
    lower = path.lower()
    base = lower.rsplit("/", 1)[-1]
    if base in _REGISTRY_BASENAMES:
        return True
    return any(marker in lower for marker in _REGISTRY_MARKERS)


def _finding(kind, severity, symbol, branches, evidence, explanation):
    return {
        "kind": kind,
        "severity": severity,
        "symbol": symbol,
        "branches": list(branches),
        "evidence": evidence,
        "explanation": explanation,
    }


def _ref_evidence(branch, refs):
    return [
        {
            "branch": branch,
            "file": r.file,
            "line": r.line,
            "detail": "references %s" % r.name,
        }
        for r in refs[:20]
    ]


class Snapshot:
    """Trees, changed files and added lines for one check run."""

    def __init__(self, repo: str, branch_a: str, branch_b: str):
        self.repo = repo
        self.branch_a = branch_a
        self.branch_b = branch_b
        self.base = gitutil.merge_base(repo, branch_a, branch_b)
        self.changed = {
            branch_a: gitutil.changed_files(repo, self.base, branch_a),
            branch_b: gitutil.changed_files(repo, self.base, branch_b),
        }
        self.trees = {}
        for ref in (self.base, branch_a, branch_b):
            tree = TreeSymbols()
            for path in gitutil.list_files(repo, ref):
                if path.endswith(".py"):
                    content = gitutil.file_content(repo, ref, path)
                    if content is not None:
                        tree.add_file(parse_file(path, content))
            self.trees[ref] = tree
        # Contents of every file either branch touched, at all three refs.
        touched = sorted(set(self.changed[branch_a]) | set(self.changed[branch_b]))
        self.contents = {}
        for ref in (self.base, branch_a, branch_b):
            for path in touched:
                self.contents[(ref, path)] = gitutil.file_content(repo, ref, path)
        # Added-line text per branch per file, for "added a reference"
        # checks (a reference only counts as new if the branch added it).
        self.added_text = {}
        for branch in (branch_a, branch_b):
            for path in self.changed[branch]:
                added = gitutil.added_lines(
                    self.contents[(self.base, path)],
                    self.contents[(branch, path)],
                )
                self.added_text[(branch, path)] = "\n".join(added)

    def branch_added_text(self, branch: str, name: str) -> bool:
        """True if ``branch`` added a reference to ``name`` anywhere."""
        for path in self.changed[branch]:
            if _word_in(self.added_text[(branch, path)], name):
                return True
        return False

    def branch_added_call(self, branch: str, name: str) -> bool:
        """True if ``branch`` added a genuine call/reference to ``name``.

        Lines that merely *define* ``name`` do not count: without this
        filter, any branch that drops one function and adds another
        would look like a rename, because its added ``def`` line
        contains the new name.
        """
        def_pattern = re.compile(
            r"\s*(def|class)\s+" + re.escape(name) + r"\b"
            r"|\s*" + re.escape(name) + r"\s*[:=]"
        )
        for path in self.changed[branch]:
            for line in self.added_text[(branch, path)].splitlines():
                if def_pattern.match(line):
                    continue
                if _word_in(line, name):
                    return True
        return False

    def added_config_keys(self, branch: str, path: str) -> set:
        base_keys = config_keys(self.contents[(self.base, path)], path) or set()
        branch_keys = config_keys(self.contents[(branch, path)], path) or set()
        return branch_keys - base_keys


def detect(repo: str, branch_a: str, branch_b: str) -> list:
    """Return the list of findings for merging ``branch_a`` and ``branch_b``."""
    snap = Snapshot(repo, branch_a, branch_b)
    base_tree = snap.trees[snap.base]
    findings = []
    covered_renames = set()  # (removing branch, old name) already reported

    # --- 2. rename-vs-old-caller (checked before removals so the more
    # specific finding wins over the generic removed-symbol finding).
    # A rename is only inferred when ALL of these hold in the renaming
    # branch: the old name is gone, a new name is defined in the same
    # file the old name lived in, and the branch's own added lines
    # call the new name (call sites updated).  Anything weaker falls
    # through to the removed-symbol check below.
    for branch, other in ((branch_a, branch_b), (branch_b, branch_a)):
        branch_tree = snap.trees[branch]
        removed = base_tree.def_names - branch_tree.def_names
        added = branch_tree.def_names - base_tree.def_names
        for old in sorted(removed):
            old_files = {d.file for d in base_tree.defs[old]}
            for new in sorted(added):
                new_files = {d.file for d in branch_tree.defs[new]}
                if not (old_files & new_files):
                    continue
                if not snap.branch_added_call(branch, new):
                    continue
                if not snap.branch_added_call(other, old):
                    continue
                covered_renames.add((branch, old))
                # Evidence should show the call the other branch *added*,
                # not pre-existing references in files it never touched.
                added_refs = [
                    r for r in snap.trees[other].refs_named(old)
                    if r.file in snap.changed[other]
                ]
                evidence = _ref_evidence(
                    other, added_refs or snap.trees[other].refs_named(old)
                )
                findings.append(
                    _finding(
                        "rename-vs-old-caller",
                        "high",
                        old,
                        (branch_a, branch_b),
                        evidence,
                        "Branch %s renamed %s to %s (old name gone, new "
                        "name defined, its own call sites updated), but "
                        "branch %s added a call to the old name %s. The "
                        "merge is clean in git and broken at runtime."
                        % (branch, old, new, other, old),
                    )
                )

    # --- 1. removed-symbol-still-referenced.
    for branch, other in ((branch_a, branch_b), (branch_b, branch_a)):
        branch_tree = snap.trees[branch]
        other_tree = snap.trees[other]
        for old in sorted(base_tree.def_names - branch_tree.def_names):
            if (branch, old) in covered_renames:
                continue
            refs = other_tree.refs_named(old)
            if not refs:
                continue
            findings.append(
                _finding(
                    "removed-symbol-still-referenced",
                    "high",
                    old,
                    (branch_a, branch_b),
                    _ref_evidence(other, refs),
                    "Branch %s removed %s, but branch %s still "
                    "references it. The merged tree has the reference "
                    "and no definition, so it fails at runtime."
                    % (branch, old, other),
                )
            )

    # --- 3. duplicate-definition: both branches add the same symbol.
    added_a = snap.trees[branch_a].def_names - base_tree.def_names
    added_b = snap.trees[branch_b].def_names - base_tree.def_names
    for name in sorted(added_a & added_b):
        evidence = []
        for branch in (branch_a, branch_b):
            for d in snap.trees[branch].defs[name]:
                evidence.append(
                    {
                        "branch": branch,
                        "file": d.file,
                        "line": d.line,
                        "detail": "defines %s" % name,
                    }
                )
        findings.append(
            _finding(
                "duplicate-definition",
                "medium",
                name,
                (branch_a, branch_b),
                evidence,
                "Both branches add a definition of %s. Whichever "
                "definition the merge keeps, the other branch's "
                "behaviour is silently lost or shadowed." % name,
            )
        )

    # --- 4. shared-registry-collision.
    common = sorted(
        p
        for p in set(snap.changed[branch_a]) & set(snap.changed[branch_b])
        if not p.endswith(".py")
    )
    for path in common:
        added_a_lines = gitutil.added_lines(
            snap.contents[(snap.base, path)], snap.contents[(branch_a, path)]
        )
        added_b_lines = gitutil.added_lines(
            snap.contents[(snap.base, path)], snap.contents[(branch_b, path)]
        )
        keys_a = snap.added_config_keys(branch_a, path)
        keys_b = snap.added_config_keys(branch_b, path)
        shared_keys = sorted(keys_a & keys_b)
        if shared_keys:
            findings.append(
                _finding(
                    "shared-registry-collision",
                    "medium",
                    shared_keys[0],
                    (branch_a, branch_b),
                    [
                        {
                            "branch": branch_a,
                            "file": path,
                            "line": None,
                            "detail": "adds key %s" % shared_keys[0],
                        },
                        {
                            "branch": branch_b,
                            "file": path,
                            "line": None,
                            "detail": "adds key %s" % shared_keys[0],
                        },
                    ],
                    "Both branches add the same key %s to %s. "
                    "Git merges the lines cleanly, but the two "
                    "entries collide at runtime." % (shared_keys[0], path),
                )
            )
        elif (
            _is_registry_path(path)
            and added_a_lines
            and added_b_lines
        ):
            findings.append(
                _finding(
                    "shared-registry-collision",
                    "medium",
                    path,
                    (branch_a, branch_b),
                    [
                        {
                            "branch": branch_a,
                            "file": path,
                            "line": None,
                            "detail": "adds: %s" % added_a_lines[0].strip(),
                        },
                        {
                            "branch": branch_b,
                            "file": path,
                            "line": None,
                            "detail": "adds: %s" % added_b_lines[0].strip(),
                        },
                    ],
                    "Both branches add entries to the shared registry "
                    "file %s. Neither branch saw the other's entry, so "
                    "numbering, ordering or uniqueness assumptions in "
                    "the file can silently break." % path,
                )
            )

    # Migration-numbered files: both branches add a new file with the
    # same migration number (e.g. migrations/0003_...).
    def _migration_numbers(branch):
        numbers = defaultdict(list)
        for path in snap.changed[branch]:
            if snap.contents[(snap.base, path)] is not None:
                continue  # not a newly added file
            if "migration" not in path.lower():
                continue
            base_name = path.rsplit("/", 1)[-1]
            match = re.match(r"(\d+)[-_]", base_name)
            if match:
                numbers[(path.rsplit("/", 1)[0], match.group(1))].append(path)
        return numbers

    mig_a = _migration_numbers(branch_a)
    mig_b = _migration_numbers(branch_b)
    for key in sorted(set(mig_a) & set(mig_b)):
        findings.append(
            _finding(
                "shared-registry-collision",
                "medium",
                "migration %s" % key[1],
                (branch_a, branch_b),
                [
                    {"branch": branch_a, "file": mig_a[key][0], "line": None,
                     "detail": "adds migration %s" % key[1]},
                    {"branch": branch_b, "file": mig_b[key][0], "line": None,
                     "detail": "adds migration %s" % key[1]},
                ],
                "Both branches add migration number %s in %s. Only "
                "one of them can own that number." % (key[1], key[0]),
            )
        )

    # --- 5. contract-drift: a config/schema key removed (or renamed)
    # in one branch while the other branch adds a reference to it.
    config_paths = sorted(
        p
        for p in set(snap.changed[branch_a]) | set(snap.changed[branch_b])
        if config_keys(snap.contents[(snap.base, p)], p) is not None
    )
    for path in config_paths:
        base_keys = config_keys(snap.contents[(snap.base, path)], path) or set()
        for branch, other in ((branch_a, branch_b), (branch_b, branch_a)):
            branch_keys = config_keys(snap.contents[(branch, path)], path) or set()
            for key in sorted(base_keys - branch_keys):
                candidates = {key, key.rsplit(".", 1)[-1]}
                if any(snap.branch_added_text(other, cand) for cand in candidates):
                    findings.append(
                        _finding(
                            "contract-drift",
                            "high",
                            key,
                            (branch_a, branch_b),
                            [
                                {
                                    "branch": branch,
                                    "file": path,
                                    "line": None,
                                    "detail": "removes key %s" % key,
                                },
                                {
                                    "branch": other,
                                    "file": path,
                                    "line": None,
                                    "detail": "adds a reference to %s" % key,
                                },
                            ],
                            "Branch %s removed config key %s from %s, "
                            "but branch %s added code that reads it. "
                            "The merged tree reads a key that no "
                            "longer exists." % (branch, key, path, other),
                        )
                    )

    return findings
