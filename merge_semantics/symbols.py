"""Static symbol extraction for Python files, using only ``ast``.

For each file we collect:

* definitions -- functions, classes, methods (qualified as
  ``ClassName.method``) and module/class-level assignments;
* references -- every loaded ``Name`` and every ``Attribute`` name
  (attribute names cover ``obj.method()`` call sites, where the
  callee is an attribute rather than a plain name);
* imports -- treated as references, because importing a symbol that
  another branch removed still breaks the merged tree.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field


@dataclass
class Definition:
    name: str       # qualified name, e.g. "Widget.render" or "helper"
    kind: str       # "function", "class", "method", "assignment"
    file: str
    line: int


@dataclass
class Reference:
    name: str
    file: str
    line: int


@dataclass
class FileSymbols:
    path: str
    definitions: list = field(default_factory=list)
    references: list = field(default_factory=list)

    @property
    def def_names(self) -> set:
        return {d.name for d in self.definitions}

    @property
    def ref_names(self) -> set:
        return {r.name for r in self.references}


class _DefinitionVisitor(ast.NodeVisitor):
    """Walk a module tracking class nesting and function depth."""

    def __init__(self, path: str, definitions: list):
        self.path = path
        self.definitions = definitions
        self.class_stack: list = []
        self.function_depth = 0

    def _qualified(self, name: str) -> str:
        return ".".join(self.class_stack + [name])

    def visit_ClassDef(self, node: ast.ClassDef):
        self.definitions.append(
            Definition(self._qualified(node.name), "class", self.path, node.lineno)
        )
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        kind = "method" if self.class_stack else "function"
        self.definitions.append(
            Definition(self._qualified(node.name), kind, self.path, node.lineno)
        )
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def _record_target(self, target, line: int):
        if isinstance(target, ast.Name):
            self.definitions.append(
                Definition(self._qualified(target.id), "assignment", self.path, line)
            )
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._record_target(elt, line)

    def visit_Assign(self, node: ast.Assign):
        # Only module/class-level assignments are symbols; assignments
        # inside a function body are locals, not shared surface.
        if self.function_depth == 0:
            for target in node.targets:
                self._record_target(target, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        if self.function_depth == 0 and node.target is not None:
            self._record_target(node.target, node.lineno)
        self.generic_visit(node)


def parse_file(path: str, content: str) -> FileSymbols:
    """Parse one Python source file; unparseable files yield no symbols."""
    result = FileSymbols(path=path)
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return result
    _DefinitionVisitor(path, result.definitions).visit(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            result.references.append(Reference(node.id, path, node.lineno))
        elif isinstance(node, ast.Attribute):
            result.references.append(Reference(node.attr, path, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = (alias.asname or alias.name).split(".")[0]
                result.references.append(Reference(name, path, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                result.references.append(Reference(name, path, node.lineno))
    return result


class TreeSymbols:
    """Symbol tables for a whole git tree (one ref)."""

    def __init__(self):
        self.defs: dict = {}   # name -> list[Definition]
        self.refs: list = []   # list[Reference]

    def add_file(self, file_symbols: FileSymbols):
        for d in file_symbols.definitions:
            self.defs.setdefault(d.name, []).append(d)
        self.refs.extend(file_symbols.references)

    @property
    def def_names(self) -> set:
        return set(self.defs)

    def refs_named(self, name: str) -> list:
        return [r for r in self.refs if r.name == name]
