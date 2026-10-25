# merge-semantics

A post-merge semantic conflict checker for parallel coding agents.

## The problem

Run two coding agents in parallel worktrees and git will happily merge
their work — and the result can still be broken. Git merges text, not
meaning. As
[dev.to/naw103, "Running multiple Claude Code agents in parallel: what worktrees cannot see"](https://dev.to/naw103/running-multiple-claude-code-agents-in-parallel-what-worktrees-cannot-see)
puts it:

> "Two agents can each be correct and jointly wrong."

and:

> "Git merges by comparing changed lines against a common ancestor"

so it is blind to meaning. The canonical case: one agent renames a
function while another adds a call to the old name. Neither branch
conflicts, the merge is clean, and the build is broken.

The same article describes an existing tool, Foremerge, but it is
**declaration-based**: each agent must publish its intents and scopes
*before* editing. The article itself admits the hole:

> "An agent that declares `extend` and then rewrites the thing is not detected"

— under-declaration. A tool that trusts declarations misses exactly
the agents that do not declare honestly.

`merge-semantics` needs **no declarations**. It diffs what the
branches actually did, after the fact, against their merge-base, so it
catches undeclared changes too. And unlike CI, which only reports a
breakage after the merge, it names the two branches and the two
contradicting changes.

## What it checks

Given `--branch-a` and `--branch-b`, the tool computes their
merge-base, diffs each branch against it, parses the Python files with
`ast`, and reports:

| Finding | Meaning |
|---|---|
| `removed-symbol-still-referenced` (high) | A symbol defined at base is removed in one branch but still referenced by the other branch's version. |
| `rename-vs-old-caller` (high) | One branch renames a symbol (old name gone, new name defined in the same file, its own call sites updated) while the other branch adds a call to the old name. |
| `duplicate-definition` (medium) | Both branches add the same top-level symbol. |
| `shared-registry-collision` (medium) | Both branches add entries to the same registry-style file (route tables, `.env.example` keys, migration-numbered files, feature-flag lists), or add the same key. |
| `contract-drift` (high) | A config/schema key is removed or renamed in one branch while the other branch adds a reference to the old key (`.env`-style `KEY=` lines and JSON object keys covered; YAML/TOML keys too). |

Every finding carries its severity, the two branches involved,
`file:line` evidence, and a plain-language explanation.

## Setup

Requires Python 3.9+ and git. No third-party dependencies — the
standard library only.

```bash
pip install -e .
# or, without installing:
python -m merge_semantics check --repo <path> --branch-a <ref> --branch-b <ref>
```

## Usage

```bash
merge-semantics check --repo ./my-project --branch-a agent-a --branch-b agent-b
merge-semantics check --repo ./my-project --branch-a agent-a --branch-b agent-b \
    --format json --fail-on medium
```

Exit code is 1 when a finding at or above `--fail-on` exists
(`high` by default; also `medium`, `any`, `never`), otherwise 0.

### Worked example

Base has `utils.py` defining `old_name()`, and `caller.py` calling it.
Agent A renames it to `new_name()` and updates `caller.py`. Agent B,
unaware, adds `feature.py` calling `old_name()`. Git merges cleanly.
`merge-semantics` does not:

```
$ merge-semantics check --repo ./demo --branch-a agent-a --branch-b agent-b
Found 1 semantic conflict(s):

[HIGH] rename-vs-old-caller: old_name
  branches: agent-a, agent-b
  Branch agent-a renamed old_name to new_name (old name gone, new name
  defined, its own call sites updated), but branch agent-b added a call
  to the old name old_name. The merge is clean in git and broken at runtime.
    - agent-b feature.py:1 (references old_name)
    - agent-b feature.py:5 (references old_name)
```

With `--format json` the same result is machine-readable: a
`findings` list (each with `kind`, `severity`, `symbol`, `branches`,
`evidence`, `explanation`) plus a `summary` with totals by severity.

## How it differs

* **vs. git itself** — git resolves changed lines; this tool resolves
  changed meaning. A clean merge is not evidence of a working merge.
* **vs. Foremerge (declaration-based)** — Foremerge needs agents to
  declare intents up front and, by its own article's admission, misses
  under-declaration. This tool reads the finished diffs, so what an
  agent actually did is what gets checked, declared or not.
* **vs. CI** — CI tells you the merged tree is broken, after the
  merge. This tool tells you *before* the merge, and names which two
  branches contradicted each other and where.

## Limitations (honest)

* **Python only in v1.** Other languages are not parsed; their files
  only participate in the registry/contract checks.
* **Static AST analysis.** It reasons about names, not runtime
  behaviour: dynamic dispatch, `getattr` strings, monkey-patching and
  metaprogramming can hide a real conflict or suggest a spurious one.
* **Rename inference is heuristic.** A rename is only claimed when
  the old name is gone, a new name appears in the same file, and the
  renaming branch's own added lines call the new name. A genuine
  rename that does not meet all three conditions is reported as a
  removal instead, and an unrelated drop-plus-add in the same file
  can still be misread if the new symbol happens to be called from
  the branch's added lines.
* **Attribute references are conservative.** Any `obj.name` access
  counts as a reference to `name`, so an unrelated attribute with the
  same name as a removed symbol can produce a false positive.
* **Same-name symbols in different modules are flagged as duplicates.**
  In Python those are separate namespaces; the tool flags them
  because two agents independently inventing the same top-level name
  is the collision the check is designed to surface — treat that
  finding as advisory.
* **It does not perform the merge.** Findings describe the two
  branches against their merge-base, not the literal merged tree;
  pathological merge resolutions could differ.

## Development

```bash
python -m unittest discover -s tests -v
```

## License

MIT — see [LICENSE](LICENSE).
