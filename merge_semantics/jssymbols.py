"""Static symbol extraction for JavaScript/TypeScript files.

No parser is available in the standard library, so this module is a
hand-rolled tokenizer plus a brace-depth tracker.  It is deliberately
conservative: it extracts the module's shared surface (top-level and
class-level definitions) and the references another branch can break,
and it says nothing it cannot see.

What it extracts
----------------
* definitions -- ``function``/``class``/``interface``/``type``/``enum``
  declarations (including ``export``/``export default``/``async`` forms),
  top-level ``const``/``let``/``var`` bindings (arrow/function/class
  right-hand sides are kind ``"function"``/``"class"``, anything else
  ``"assignment"``), destructured bindings, and class methods/fields
  qualified as ``ClassName.method`` (exactly like the Python extractor
  qualifies ``ClassName.method``);
* references -- every other identifier, including call callees,
  ``new X``, ``extends``/``implements`` operands, import/export
  specifiers, member names after ``.`` (``obj.method()``), and JSX
  component names (capitalised tags only; lowercase HTML tags are not
  symbols).  JSX *text* between tags is not code and is never recorded;
  ``{...}`` expressions inside JSX are scanned as code.

What it deliberately does NOT do
---------------------------------
* Type-only references are not separated from value references:
  a type name in an annotation (``let x: Widget``) is recorded as a
  reference, so a removed value symbol sharing a type's name can
  produce a false positive.  Rare in practice; documented in README.
* Object-literal methods (``const o = { m() {} }``) are not symbols.
* Dynamic ``import()`` targets are not followed.
* Parameter properties (``constructor(private x)``) are not fields.
* Anonymous class expressions contribute no qualified method names.

Strings, comments, regex literals and template-literal text are
blanked before analysis, so they can never invent a symbol.  The
``${...}`` expressions inside template literals ARE scanned as code.
A file that defeats the tokenizer yields no symbols rather than a
crash -- exactly like the Python extractor on a syntax error.
"""

from __future__ import annotations

import re

from .symbols import Definition, FileSymbols, Reference

_IDENT_START = re.compile(r"[A-Za-z_$]")
_IDENT_PART = re.compile(r"[A-Za-z0-9_$]")

_KEYWORDS = {
    # JS reserved + contextual words that are never symbol names here.
    "await", "break", "case", "catch", "class", "const", "continue",
    "debugger", "default", "delete", "do", "else", "export", "extends",
    "false", "finally", "for", "function", "if", "import", "in",
    "instanceof", "let", "new", "null", "return", "super", "switch",
    "this", "throw", "true", "try", "typeof", "var", "void", "while",
    "with", "yield", "static", "async", "of", "from", "as", "get",
    "set", "declare", "namespace", "interface", "type", "enum",
    "implements", "readonly", "abstract", "private", "protected",
    "public", "keyof", "infer", "is", "satisfies", "override",
    "undefined", "NaN", "Infinity",
}

# A ``/`` starts a regex literal (rather than division) when the
# previous significant token is one of these, or there is none.
_REGEX_OK_PREV = {
    "(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";",
    "return", "=>", "typeof", "instanceof", "in", "of", "new",
    "delete", "void", "throw", "case", "do", "else", "&&", "||",
    "??", "+", "-", "*", "%", "~", "^", "<<", ">>", ">>>",
}

_MULTI_PUNCT = (
    "...", "===", "!==", "**=", "<<=", ">>=", ">>>=", "&&=", "||=",
    "??=", "?.", "++", "--", "+=", "-=", "*=", "/=", "%=", "&=",
    "|=", "^=", "==", "!=", "<=", ">=", "&&", "||", "??", "<<",
    ">>", "=>",
)


# Tokens that end JSX children text and resume ordinary code.  A
# comma is included even though JSX text can contain commas: code
# after a comma (``f(<div/>, helper)``) carries real references, while
# a text word after a comma is rarely a symbol name.
_JSX_CHILDREN_END = {
    ";", ")", "]", "}", "=", "=>", "+", "-", "*", "/", "%", "?",
    ":", "&&", "||", "??", "!", "~", "&", "|", "^", "<<", ">>",
    ">>>", ".", "?.", "...", ",", "==", "!=", "===", "!==",
    "<=", ">=", "return", "throw", "case", "typeof", "in",
    "instanceof", "new", "delete", "void", "of",
}


class _Token:
    __slots__ = ("kind", "value", "line")

    def __init__(self, kind, value, line):
        self.kind = kind  # "ident" | "number" | "string" | "punct"
        self.value = value
        self.line = line

    def __repr__(self):  # pragma: no cover - debugging aid
        return "_Token(%r, %r, %d)" % (self.kind, self.value, self.line)


def _tokenize(content: str) -> list:
    """Split ``content`` into tokens, blanking strings/comments/regexes.

    Template-literal ``${...}`` expressions are tokenized as code;
    every other literal becomes one ``"string"`` token whose value is
    discarded, so literal text can never look like a symbol.
    """
    tokens: list = []
    i = 0
    n = len(content)
    line = 1

    def prev_significant():
        return tokens[-1].value if tokens else None

    while i < n:
        ch = content[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch in " \t\r\f\v":
            i += 1
            continue
        # Comments.
        if ch == "/" and i + 1 < n and content[i + 1] == "/":
            while i < n and content[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and content[i + 1] == "*":
            i += 2
            while i + 1 < n and not (content[i] == "*" and content[i + 1] == "/"):
                if content[i] == "\n":
                    line += 1
                i += 1
            i += 2
            continue
        # Quoted strings.
        if ch in "'\"":
            quote = ch
            i += 1
            while i < n and content[i] != quote:
                if content[i] == "\\":
                    i += 2
                    continue
                if content[i] == "\n":
                    break  # unterminated string: stop at the newline
                i += 1
            i += 1  # closing quote (or EOF)
            tokens.append(_Token("string", "", line))
            continue
        # Template literals: blank the text, tokenize ${...} as code.
        if ch == "`":
            i += 1
            start_line = line
            while i < n and content[i] != "`":
                if content[i] == "\\":
                    i += 2
                    continue
                if content[i] == "\n":
                    line += 1
                    i += 1
                    continue
                if content[i] == "$" and i + 1 < n and content[i + 1] == "{":
                    i += 2
                    depth = 1
                    expr_start = i
                    expr_line = line
                    while i < n and depth > 0:
                        c = content[i]
                        if c == "\n":
                            line += 1
                        elif c in "'\"`":
                            # String/template nested in the expression:
                            # skip it wholesale (literal text is never
                            # a symbol); nested ${...} inside a nested
                            # template is skipped balanced.
                            q = c
                            i += 1
                            while i < n and content[i] != q:
                                if content[i] == "\\":
                                    i += 2
                                    continue
                                if content[i] == "\n":
                                    line += 1
                                    i += 1
                                    continue
                                if q == "`" and content[i] == "$" \
                                        and i + 1 < n \
                                        and content[i + 1] == "{":
                                    d = 1
                                    i += 2
                                    while i < n and d > 0:
                                        if content[i] == "{":
                                            d += 1
                                        elif content[i] == "}":
                                            d -= 1
                                        if content[i] == "\n":
                                            line += 1
                                        i += 1
                                    continue
                                i += 1
                            i += 1
                            continue
                        elif c == "{":
                            depth += 1
                        elif c == "}":
                            depth -= 1
                            if depth == 0:
                                break
                        i += 1
                    expr = content[expr_start:i]
                    sub = _tokenize(expr)
                    for tok in sub:
                        tok.line = expr_line + tok.line - 1
                        tokens.append(tok)
                    i += 1  # closing }
                    continue
                i += 1
            i += 1  # closing backtick (or EOF)
            tokens.append(_Token("string", "", start_line))
            continue
        # Regex literals (heuristic: previous token decides).
        if ch == "/" and (prev_significant() in _REGEX_OK_PREV
                          or not tokens):
            j = i + 1
            in_class = False
            closed = False
            while j < n:
                c = content[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "\n":
                    break
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    closed = True
                    j += 1
                    break
                j += 1
            if closed:
                while j < n and content[j].isalpha():
                    j += 1
                tokens.append(_Token("string", "", line))
                i = j
                continue
            # Not a regex after all: fall through to division.
        # Numbers.
        if ch.isdigit() or (ch == "." and i + 1 < n and content[i + 1].isdigit()):
            j = i
            while j < n and (content[j].isalnum() or content[j] in "._"):
                j += 1
            tokens.append(_Token("number", content[i:j], line))
            i = j
            continue
        # Identifiers / keywords.
        if _IDENT_START.match(ch):
            j = i + 1
            while j < n and _IDENT_PART.match(content[j]):
                j += 1
            word = content[i:j]
            kind = "ident" if word not in _KEYWORDS else "keyword"
            tokens.append(_Token(kind, word, line))
            i = j
            continue
        # Punctuation (longest match first).
        matched = None
        for cand in _MULTI_PUNCT:
            if content.startswith(cand, i):
                matched = cand
                break
        if matched is None:
            matched = ch
        tokens.append(_Token("punct", matched, line))
        i += len(matched)
    return tokens


def _is_ident(tok, word=None):
    return tok is not None and tok.kind == "ident" and (
        word is None or tok.value == word
    )


def _is(tok, value):
    return tok is not None and tok.value == value


class _Scope:
    """One open ``{...}`` region: a class body, an enum, or a block."""

    __slots__ = ("kind", "name")

    def __init__(self, kind, name=None):
        self.kind = kind  # "class" | "interface" | "enum" | "block"
        self.name = name


def _index_of_arrow(tokens, start):
    """Index of the ``=>`` ending an arrow-function head, or None."""
    depth = 0
    i = start
    while i < len(tokens):
        tok = tokens[i]
        if tok.value in ("(", "["):
            depth += 1
        elif tok.value in (")", "]"):
            if depth == 0:
                return None
            depth -= 1
        elif depth == 0 and tok.value == "=>":
            return i
        elif depth == 0 and tok.value in (";", ",", "{"):
            return None
        i += 1
    return None


def _after_paren(tokens, i):
    """Index just past the ``)`` matching ``tokens[i] == "("``."""
    depth = 0
    while i < len(tokens):
        if tokens[i].value == "(":
            depth += 1
        elif tokens[i].value == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(tokens)


_MEMBER_MODIFIERS = (
    "static", "async", "get", "set", "public", "private", "protected",
    "readonly", "declare", "abstract", "override",
)


def _is_member_position(tokens, m):
    """True if the identifier at ``m`` sits where a class member starts.

    Without this, a value identifier inside a field initializer
    (``x = helper;``) would be misread as a field called ``helper``.
    A member follows ``{``, ``;`` or ``}``, a member modifier, or a
    decorator (``@dec`` / ``@dec(...)``); anything else (``=``, ``(``,
    operators, ...) means the identifier is a value, not a member.
    Semicolon-less (ASI) members after a field initializer are missed
    by design -- the previous token is then a value, not a boundary.
    """
    if m <= 0:
        return False
    prev = tokens[m - 1]
    if prev.value in ("{", ";", "}"):
        return True
    if prev.value in _MEMBER_MODIFIERS or prev.value == "*":
        return True
    if prev.kind == "ident" and m >= 2 and tokens[m - 2].value == "@":
        return True  # @decorator name
    if prev.value == ")":
        # @decorator(...) name -- walk back to the matching "(".
        depth = 0
        k = m - 1
        while k >= 0:
            if tokens[k].value == ")":
                depth += 1
            elif tokens[k].value == "(":
                depth -= 1
                if depth == 0:
                    return k >= 2 and tokens[k - 1].kind == "ident" \
                        and tokens[k - 2].value == "@"
            k -= 1
    return False


def parse_js_file(path: str, content: str) -> FileSymbols:
    """Parse one JavaScript/TypeScript file; never raises.

    Unparseable input yields no symbols, matching ``symbols.parse_file``.
    """
    result = FileSymbols(path=path)
    try:
        tokens = _tokenize(content)
    except Exception:
        return result
    try:
        _analyse(tokens, path, result)
    except Exception:
        # A tokenizer/analyser bug must never break a whole check run.
        return FileSymbols(path=path)
    return result


def _analyse(tokens, path, result):
    definitions: list = result.definitions
    references: list = result.references
    consumed: set = set()  # token indexes used as definition names
    scopes: list = []  # stack of _Scope, one per open "{"
    pending_class = None  # class name whose "{" has not opened yet
    pending_kind = None  # "class" | "interface" | "enum"
    brace_depth = 0
    # JSX state is a stack of frames so nested elements and {…}
    # expressions inside them cannot corrupt each other's bookkeeping:
    #   ["tag"]  -- inside <Tag …> (attribute names are not refs)
    #   ["expr", depth] -- inside a {…} expression in JSX
    #   ["text"] -- between tags (identifiers are JSX text, not code)
    jsx: list = []

    def current_class():
        for scope in reversed(scopes):
            if scope.kind == "class":
                return scope
        return None

    def in_class_body():
        scope = scopes[-1] if scopes else None
        return scope is not None and scope.kind == "class"

    def in_interface():
        return bool(scopes) and scopes[-1].kind == "interface"

    def add_def(name, kind, line):
        definitions.append(Definition(name, kind, path, line))

    def add_ref(name, line):
        references.append(Reference(name, path, line))

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        prev = tokens[i - 1] if i > 0 else None
        nxt = tokens[i + 1] if i + 1 < n else None

        # --- JSX tracking -------------------------------------
        # A tag only opens outside a tag frame: "<" whose previous
        # token is an identifier/number/string/) /] is a comparison,
        # not a tag.  The tag name is a reference only when capitalised
        # (a component, not an HTML tag).
        _jsx_top = jsx[-1] if jsx else None
        _in_tag = _jsx_top is not None and _jsx_top[0] == "tag"
        if tok.value == "<" and not _in_tag and (
            _is_ident(nxt) and not (
                prev is not None
                and prev.kind in ("ident", "number", "string")
                or prev is not None and prev.value in (")", "]", "++", "--")
            )
            or _is(nxt, ">")
            or _is(nxt, "/")
        ):
            if _is_ident(nxt):
                jsx.append(["tag"])
                if nxt.value[0].isupper():
                    add_ref(nxt.value, nxt.line)
                consumed.add(i + 1)
                i += 2
                continue
            if _is(nxt, ">"):
                jsx.append(["tag"])  # fragment <>
                i += 2
                continue
            # Closing tag </Foo> or </>: a reference only when
            # capitalised; no frame change (the parent's text frame,
            # if any, stays).
            name_tok = tokens[i + 2] if i + 2 < n else None
            if _is_ident(name_tok):
                if name_tok.value[0].isupper():
                    add_ref(name_tok.value, name_tok.line)
                consumed.add(i + 2)
                i += 4 if _is(tokens[i + 3], ">") else 3
            elif _is(tokens[i + 2], ">"):
                i += 3
            else:
                i += 2
            continue
        if tok.value == "{":
            if _jsx_top is not None and _jsx_top[0] in ("tag", "text"):
                # Attribute expression or children expression.
                jsx.append(["expr", 1])
                i += 1
                continue
            if _jsx_top is not None and _jsx_top[0] == "expr":
                _jsx_top[1] += 1  # nested object/block inside the expr
                i += 1
                continue
            # Otherwise a real scope brace (fall through).
        if tok.value == "}":
            # A "}" ends children text first, then the expression.
            while jsx and jsx[-1][0] == "text":
                jsx.pop()
            _jsx_top = jsx[-1] if jsx else None
            if _jsx_top is not None and _jsx_top[0] == "expr":
                _jsx_top[1] -= 1
                if _jsx_top[1] == 0:
                    jsx.pop()
                i += 1
                continue
            # Otherwise a real scope brace (fall through).
        if _in_tag and tok.value in (">", "/>"):
            jsx.pop()  # close the tag
            if not jsx or jsx[-1][0] == "text":
                if not jsx:
                    jsx.append(["text"])
                # Inside an expression there is no text frame: the
                # expression simply continues.
            i += 1
            continue
        _jsx_top = jsx[-1] if jsx else None
        if _jsx_top is not None and _jsx_top[0] == "text":
            if tok.value in _JSX_CHILDREN_END:
                jsx.pop()  # resume ordinary code; fall through
            else:
                # JSX text: never a symbol.
                i += 1
                continue

        # --- Braces and scopes ------------------------------------
        if tok.value == "{":
            if pending_kind is not None:
                scopes.append(_Scope(pending_kind, pending_class))
                pending_kind = None
                pending_class = None
            else:
                scopes.append(_Scope("block"))
            brace_depth += 1
            i += 1
            continue
        if tok.value == "}":
            if scopes:
                scopes.pop()
            brace_depth = max(0, brace_depth - 1)
            i += 1
            continue

        # --- Keywords that start definitions ----------------------
        # Optional "export" / "export default" / "declare" prefix.
        j = i
        exported = False
        if _is(tokens[j], "export"):
            exported = True
            j += 1
            if _is(tokens[j], "default"):
                j += 1
        if _is(tokens[j], "declare"):
            j += 1

        # export { a, b as c } [from ...]: specifiers are references
        # to local bindings, not definitions.
        if exported and _is(tokens[j], "{"):
            k = j + 1
            while k < n and not _is(tokens[k], "}"):
                if _is_ident(tokens[k]):
                    add_ref(tokens[k].value, tokens[k].line)
                    consumed.add(k)
                    if _is(tokens[k + 1], "as") and _is_ident(tokens[k + 2]):
                        consumed.add(k + 1)
                        consumed.add(k + 2)
                        k += 3
                        continue
                    k += 1
                    continue
                k += 1
            i = k + 1
            continue
        if exported and _is(tokens[j], "*"):
            i = j + 1
            continue

        # [async] function [*] name( ... ) -- parameter lists are
        # skipped so parameter names are never recorded as references.
        k = j
        if _is(tokens[k], "async"):
            k += 1
        if _is(tokens[k], "function"):
            k += 1
            if _is(tokens[k], "*"):
                k += 1
            if _is_ident(tokens[k]) and _is(tokens[k + 1], "("):
                add_def(tokens[k].value, "function", tokens[k].line)
                consumed.add(k)
                i = _after_paren(tokens, k + 1)
                continue
            if _is(tokens[k], "("):
                i = _after_paren(tokens, k)
                continue
            i = k
            continue

        # class Name [extends ...] { ... } / abstract class
        if _is(tokens[j], "abstract") and _is(tokens[j + 1], "class"):
            j += 1
        if _is(tokens[j], "class"):
            if _is_ident(tokens[j + 1]):
                add_def(tokens[j + 1].value, "class", tokens[j + 1].line)
                consumed.add(j + 1)
                pending_class = tokens[j + 1].value
                pending_kind = "class"
                i = j + 2
                continue
            pending_class = None
            pending_kind = "class"
            i = j + 1
            continue

        # interface Name { ... } (TypeScript)
        if _is(tokens[j], "interface"):
            if _is_ident(tokens[j + 1]):
                add_def(tokens[j + 1].value, "interface", tokens[j + 1].line)
                consumed.add(j + 1)
                pending_class = tokens[j + 1].value
                pending_kind = "interface"
                i = j + 2
                continue
            i = j + 1
            continue

        # type Name = ... (TypeScript)
        if _is(tokens[j], "type"):
            if _is_ident(tokens[j + 1]):
                add_def(tokens[j + 1].value, "type", tokens[j + 1].line)
                consumed.add(j + 1)
                i = j + 2
                continue
            i = j + 1
            continue

        # enum Name { ... } (TypeScript)
        if _is(tokens[j], "enum"):
            if _is_ident(tokens[j + 1]):
                add_def(tokens[j + 1].value, "enum", tokens[j + 1].line)
                consumed.add(j + 1)
                pending_class = tokens[j + 1].value
                pending_kind = "enum"
                i = j + 2
                continue
            i = j + 1
            continue

        # namespace Name { ... } (TypeScript)
        if _is(tokens[j], "namespace"):
            if _is_ident(tokens[j + 1]):
                add_def(tokens[j + 1].value, "namespace", tokens[j + 1].line)
                consumed.add(j + 1)
                i = j + 2
                continue
            i = j + 1
            continue

        # const/let/var bindings at module top level (locals inside
        # functions are not module surface, mirroring the Python
        # extractor's module/class-level rule).
        if _is(tokens[j], "const") or _is(tokens[j], "let") \
                or _is(tokens[j], "var"):
            if brace_depth == 0:
                i = _handle_binding(tokens, j, path, result, consumed)
                continue
            i = j + 1
            continue

        # import ... from ... : local binding names are references.
        if _is(tokens[i], "import") and not _is(nxt, "("):
            i = _handle_import(tokens, i, add_ref, consumed)
            continue

        # --- Class-body members -----------------------------------
        if in_class_body():
            cls = current_class()
            # Optional modifiers, then a member name.
            m = i
            while _is(tokens[m], "static") or _is(tokens[m], "async") \
                    or _is(tokens[m], "get") or _is(tokens[m], "set") \
                    or _is(tokens[m], "public") or _is(tokens[m], "private") \
                    or _is(tokens[m], "protected") or _is(tokens[m], "*") \
                    or _is(tokens[m], "readonly") \
                    or _is(tokens[m], "declare") \
                    or _is(tokens[m], "abstract") \
                    or _is(tokens[m], "override"):
                m += 1
            if _is_ident(tokens[m]) and cls.name is not None \
                    and _is_member_position(tokens, m):
                name = tokens[m].value
                after = tokens[m + 1] if m + 1 < n else None
                if _is(after, "("):
                    add_def("%s.%s" % (cls.name, name), "method",
                            tokens[m].line)
                    consumed.add(m)
                    i = _after_paren(tokens, m + 1)
                    continue
                if _is(after, "=") or _is(after, ";") or _is(after, ":") \
                        or _is(after, "!"):
                    # Field declaration (TS "name: Type" included).
                    add_def("%s.%s" % (cls.name, name), "assignment",
                            tokens[m].line)
                    consumed.add(m)
                    i = m + 1
                    continue
            # #private field
            if _is(tokens[i], "#") and _is_ident(nxt) \
                    and cls.name is not None \
                    and _is_member_position(tokens, i):
                add_def("%s.#%s" % (cls.name, nxt.value), "assignment",
                        nxt.line)
                consumed.add(i + 1)
                i += 2
                continue
            # static { ... } blocks: the "{" arm above pushes a block
            # scope, so nothing special is needed here.

        # --- Enum members -----------------------------------------
        if bool(scopes) and scopes[-1].kind == "enum":
            if _is_ident(tok) and scopes[-1].name \
                    and prev is not None and prev.value in ("{", ","):
                add_def("%s.%s" % (scopes[-1].name, tok.value),
                        "assignment", tok.line)
                consumed.add(i)
                i += 1
                continue

        # --- General reference rule -------------------------------
        if tok.kind == "ident" and i not in consumed:
            _top = jsx[-1] if jsx else None
            if _top is not None and _top[0] == "tag" and _is(nxt, "="):
                # JSX attribute name, not a reference.
                i += 1
                continue
            if _is(nxt, ":") and prev is not None \
                    and prev.value in ("{", ","):
                # Object-literal key, not a reference.
                i += 1
                continue
            if in_interface() and (_is(nxt, ":") or _is(nxt, "(")):
                # Interface member declaration, not a reference.
                i += 1
                continue
            add_ref(tok.value, tok.line)
        i += 1


def _handle_binding(tokens, j, path, result, consumed):
    """Handle ``const|let|var ...`` at module top level.

    Returns the index to continue scanning from.  Records definitions
    for plain bindings, destructured bindings, and class/function/
    arrow right-hand sides.  For arrows, scanning resumes after the
    ``=>`` so parameter names are never recorded as references.
    """
    definitions = result.definitions
    n = len(tokens)
    k = j + 1  # token after const/let/var
    if _is(tokens[k], "{") or _is(tokens[k], "["):
        # Destructuring: const { a, b: c } = ... / const [a, b] = ...
        depth = 0
        p = k
        while p < n:
            tok = tokens[p]
            if tok.value in ("{", "["):
                depth += 1
            elif tok.value in ("}", "]"):
                depth -= 1
                if depth == 0:
                    break
            elif tok.kind == "ident" and tok.value not in _KEYWORDS:
                prev = tokens[p - 1] if p > 0 else None
                nxt = tokens[p + 1] if p + 1 < n else None
                if _is(prev, ":"):
                    # Renamed binding: b: c binds c.
                    definitions.append(
                        Definition(tok.value, "assignment", path, tok.line))
                    consumed.add(p)
                elif _is(prev, "..."):
                    definitions.append(
                        Definition(tok.value, "assignment", path, tok.line))
                    consumed.add(p)
                elif not _is(nxt, ":"):
                    # Shorthand binding, unless it is a key whose
                    # value is a nested pattern ({a: {b}} -- "a" is a
                    # key, not a binding).
                    if not (_is(nxt, "{") or _is(nxt, "[")):
                        definitions.append(
                            Definition(tok.value, "assignment", path,
                                       tok.line))
                        consumed.add(p)
            p += 1
        return p + 1
    if not _is_ident(tokens[k]):
        return j + 1
    name = tokens[k].value
    line = tokens[k].line
    consumed.add(k)
    if not _is(tokens[k + 1], "="):
        # Declaration without an initializer: const x; (TS) / let x;
        definitions.append(Definition(name, "assignment", path, line))
        return k + 1
    rhs = k + 2
    if _is(tokens[rhs], "class"):
        definitions.append(Definition(name, "class", path, line))
        return rhs  # class arm records the pending class on next pass
    if _is(tokens[rhs], "async") and _is(tokens[rhs + 1], "function"):
        definitions.append(Definition(name, "function", path, line))
        return rhs
    if _is(tokens[rhs], "function"):
        definitions.append(Definition(name, "function", path, line))
        return rhs
    arrow = _index_of_arrow(tokens, rhs)
    if arrow is not None:
        definitions.append(Definition(name, "function", path, line))
        return arrow + 1
    definitions.append(Definition(name, "assignment", path, line))
    return rhs


def _handle_import(tokens, i, add_ref, consumed):
    """Record the local binding names of a static import statement.

    Returns the index to continue scanning from.  Handles default
    imports, namespace imports and named imports, including the
    ``import Foo, { a } from '...'`` combination.
    """
    n = len(tokens)
    j = i + 1
    if _is(tokens[j], "type"):
        j += 1  # import type {...} -- bindings are still references
    if tokens[j] is not None and tokens[j].kind == "string":
        return j + 1  # side-effect import: import './x'
    if _is(tokens[j], "*"):
        # import * as ns from '...'
        if _is(tokens[j + 1], "as") and _is_ident(tokens[j + 2]):
            add_ref(tokens[j + 2].value, tokens[j + 2].line)
            consumed.add(j + 2)
            return j + 3
        return j + 1
    if _is(tokens[j], "{"):
        return _handle_named_import(tokens, j, add_ref, consumed)
    if _is_ident(tokens[j]):
        # Default import, optionally followed by , {...} or , * as ns.
        add_ref(tokens[j].value, tokens[j].line)
        consumed.add(j)
        j += 1
        if _is(tokens[j], ","):
            j += 1
            if _is(tokens[j], "{"):
                return _handle_named_import(tokens, j, add_ref, consumed)
            if _is(tokens[j], "*"):
                if _is(tokens[j + 1], "as") and _is_ident(tokens[j + 2]):
                    add_ref(tokens[j + 2].value, tokens[j + 2].line)
                    consumed.add(j + 2)
                    return j + 3
        return j
    return i + 1


def _handle_named_import(tokens, j, add_ref, consumed):
    """Record bindings of ``import { a, b as c }`` starting at ``{``."""
    n = len(tokens)
    k = j + 1
    while k < n and not _is(tokens[k], "}"):
        if _is_ident(tokens[k]):
            if _is(tokens[k + 1], "as") and _is_ident(tokens[k + 2]):
                add_ref(tokens[k + 2].value, tokens[k + 2].line)
                consumed.add(k + 2)
                k += 3
                continue
            add_ref(tokens[k].value, tokens[k].line)
            consumed.add(k)
            k += 1
            continue
        k += 1
    return k + 1
