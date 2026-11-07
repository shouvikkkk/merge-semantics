"""Unit tests for the JavaScript/TypeScript symbol extractor."""

import unittest

from merge_semantics.jssymbols import parse_js_file


def defs(source):
    return {d.name: d.kind for d in parse_js_file("x.js", source).definitions}


def refs(source):
    return {r.name for r in parse_js_file("x.js", source).references}


class TestJsDefinitions(unittest.TestCase):
    def test_function_declarations(self):
        found = defs(
            "function foo() {}\n"
            "async function bar() {}\n"
            "export function baz() {}\n"
            "export default function qux() {}\n"
            "export async function quux() {}\n"
            "function *gen() {}\n"
        )
        self.assertEqual(
            found,
            {"foo": "function", "bar": "function", "baz": "function",
             "qux": "function", "quux": "function", "gen": "function"},
        )

    def test_class_declarations(self):
        found = defs(
            "class Widget {}\n"
            "export class Gadget extends Widget {}\n"
            "export default class Thing {}\n"
            "abstract class Base {}\n"
        )
        self.assertEqual(
            found,
            {"Widget": "class", "Gadget": "class", "Thing": "class",
             "Base": "class"},
        )
        # "extends" operands are references.
        self.assertIn("Widget", refs("class Gadget extends Widget {}"))

    def test_const_let_var_bindings(self):
        found = defs(
            "const arrow = (x) => x + 1;\n"
            "const single = x => x;\n"
            "const asyncArrow = async (x) => x;\n"
            "const fn = function (x) { return x; };\n"
            "const asyncFn = async function (x) { return x; };\n"
            "const Klass = class {};\n"
            "const value = 42;\n"
            "let mutable = 's';\n"
            "var old = {};\n"
            "const obj = { a: 1 };\n"
            "const called = foo(1);\n"
        )
        self.assertEqual(found["arrow"], "function")
        self.assertEqual(found["single"], "function")
        self.assertEqual(found["asyncArrow"], "function")
        self.assertEqual(found["fn"], "function")
        self.assertEqual(found["asyncFn"], "function")
        self.assertEqual(found["Klass"], "class")
        self.assertEqual(found["value"], "assignment")
        self.assertEqual(found["mutable"], "assignment")
        self.assertEqual(found["old"], "assignment")
        self.assertEqual(found["obj"], "assignment")
        self.assertEqual(found["called"], "assignment")

    def test_destructuring(self):
        found = defs(
            "const { a, b: c, ...rest } = obj;\n"
            "const [x, y] = arr;\n"
            "const { nested: { deep } } = obj;\n"
        )
        self.assertEqual(
            found,
            {"a": "assignment", "c": "assignment",
             "rest": "assignment", "x": "assignment", "y": "assignment",
             "deep": "assignment"},
        )
        # The renamed key "b" and the nested key "nested" are not bindings.
        self.assertNotIn("b", found)
        self.assertNotIn("nested", found)

    def test_locals_are_not_definitions(self):
        found = defs(
            "function outer() {\n"
            "  const inner = () => 1;\n"
            "  let local = 2;\n"
            "  function nested() {}\n"
            "}\n"
        )
        # Nested function declarations are recorded (mirroring the
        # Python extractor); const/let inside a function are locals.
        self.assertIn("nested", found)
        self.assertNotIn("inner", found)
        self.assertNotIn("local", found)

    def test_class_methods_and_fields(self):
        found = defs(
            "class Widget {\n"
            "  render() {}\n"
            "  async load() {}\n"
            "  static create() {}\n"
            "  get size() { return 1; }\n"
            "  set size(v) {}\n"
            "  *items() {}\n"
            "  constructor() {}\n"
            "  count = 0;\n"
            "  label = 'x';\n"
            "  static kind = 'w';\n"
            "  #secret = 1;\n"
            "}\n"
        )
        self.assertEqual(
            found,
            {
                "Widget": "class",
                "Widget.render": "method",
                "Widget.load": "method",
                "Widget.create": "method",
                "Widget.size": "method",
                "Widget.items": "method",
                "Widget.constructor": "method",
                "Widget.count": "assignment",
                "Widget.label": "assignment",
                "Widget.kind": "assignment",
                "Widget.#secret": "assignment",
            },
        )

    def test_field_initializer_values_are_references_not_fields(self):
        # "x = helper;" must not invent a field called "helper".
        found = defs(
            "class Widget {\n"
            "  x = helper;\n"
            "  y = this.render;\n"
            "  z = () => helper;\n"
            "}\n"
        )
        self.assertNotIn("Widget.helper", found)
        self.assertEqual(found.get("Widget.x"), "assignment")
        self.assertEqual(found.get("Widget.y"), "assignment")
        self.assertEqual(found.get("Widget.z"), "assignment")
        self.assertIn("helper", refs(
            "class Widget {\n  x = helper;\n}\n"))
        self.assertIn("render", refs(
            "class Widget {\n  y = this.render;\n}\n"))

    def test_object_literal_methods_are_not_symbols(self):
        found = defs("const o = { m() {}, n: 1 };\n")
        self.assertEqual(found, {"o": "assignment"})
        self.assertNotIn("m", found)

    def test_typescript_declarations(self):
        found = defs(
            "interface Shape {\n"
            "  area(): number;\n"
            "  name: string;\n"
            "}\n"
            "type ID = string | number;\n"
            "enum Colour { Red, Green = 2, Blue }\n"
            "namespace Util {\n"
            "  export function help() {}\n"
            "}\n"
        )
        self.assertEqual(found["Shape"], "interface")
        self.assertEqual(found["ID"], "type")
        self.assertEqual(found["Colour"], "enum")
        self.assertEqual(found["Colour.Red"], "assignment")
        self.assertEqual(found["Colour.Green"], "assignment")
        self.assertEqual(found["Colour.Blue"], "assignment")
        self.assertEqual(found["Util"], "namespace")
        self.assertEqual(found["help"], "function")
        # Interface members are declarations, not references.
        self.assertNotIn("area", refs(
            "interface Shape {\n  area(): number;\n}\n"))

    def test_typescript_class_fields(self):
        found = defs(
            "class Widget {\n"
            "  private count: number;\n"
            "  readonly label!: string;\n"
            "  name: string = 'w';\n"
            "}\n"
        )
        self.assertEqual(found["Widget.count"], "assignment")
        self.assertEqual(found["Widget.label"], "assignment")
        self.assertEqual(found["Widget.name"], "assignment")


class TestJsReferences(unittest.TestCase):
    def test_calls_and_members(self):
        found = refs(
            "import { helper } from './utils';\n"
            "const x = helper(1);\n"
            "obj.render();\n"
            "const w = new Widget();\n"
            "arr.map(cb);\n"
        )
        for name in ("helper", "render", "Widget", "cb"):
            self.assertIn(name, found)

    def test_import_forms(self):
        self.assertEqual(
            refs("import Foo from './x';\n"), {"Foo"})
        self.assertEqual(
            refs("import { a, b as c } from './x';\n"), {"a", "c"})
        self.assertEqual(
            refs("import * as ns from './x';\n"), {"ns"})
        self.assertEqual(
            refs("import Foo, { a } from './x';\n"), {"Foo", "a"})
        self.assertEqual(refs("import './x';\n"), set())
        self.assertEqual(
            refs("export { a, b as c };\n"), {"a", "b"})
        self.assertEqual(refs("export * from './x';\n"), set())

    def test_object_keys_are_not_references(self):
        found = refs("const o = { foo: 1, bar: baz };\n")
        self.assertNotIn("foo", found)
        self.assertNotIn("bar", found)
        self.assertIn("baz", found)  # value IS a reference
        # Shorthand properties ARE references.
        self.assertIn("foo", refs("const o = { foo };\n"))

    def test_ternary_references_kept(self):
        found = refs("const x = cond ? yes : no;\n")
        self.assertIn("yes", found)
        self.assertIn("no", found)

    def test_strings_comments_regex_blanked(self):
        found = refs(
            "// helper is mentioned here\n"
            "/* helper again */\n"
            "const s = 'helper';\n"
            "const t = \"helper\";\n"
            "const r = /helper/;\n"
            "const u = `helper ${real}`;\n"
        )
        self.assertNotIn("helper", found)
        self.assertIn("real", found)  # ${...} IS scanned as code
        self.assertEqual(
            defs("const s = 'function fake() {}';\n"), {"s": "assignment"})
        self.assertEqual(
            defs("// function fake() {}\n"), {})
        self.assertEqual(
            defs("const r = /function fake() {} /;\n"),
            {"r": "assignment"})

    def test_regex_with_braces_does_not_break_scopes(self):
        found = defs(
            "const r = /^\\d{2}$/;\n"
            "function after() {}\n"
        )
        self.assertEqual(found["after"], "function")

    def test_parameters_are_not_references(self):
        found = refs(
            "function f(helper) { return helper; }\n"
            "const g = (helper) => helper;\n"
            "class C { m(helper) { return helper; } }\n"
        )
        # Parameter declarations are skipped; uses inside the body are
        # (like locals in the Python extractor) still recorded.
        self.assertIn("helper", found)

    def test_jsx(self):
        found = refs(
            "const el = <Widget onClick={handler} title='x' />;\n"
            "const html = <div className='c'>text</div>;\n"
            "const frag = <><Widget /></>;\n"
        )
        self.assertIn("Widget", found)
        self.assertIn("handler", found)
        self.assertNotIn("div", found)
        self.assertNotIn("onClick", found)
        self.assertNotIn("title", found)
        self.assertNotIn("className", found)
        self.assertNotIn("text", found)

    def test_jsx_expression_operators(self):
        # ">" inside a JSX attribute expression must not close the tag
        # early (which would corrupt brace tracking for the rest of
        # the file).
        found = refs(
            "const el = <Widget show={a > b} />;\n"
            "function after() {}\n"
        )
        self.assertIn("a", found)
        self.assertIn("b", found)
        self.assertEqual(defs(
            "const el = <Widget show={a > b} />;\n"
            "function after() {}\n")["after"], "function")

    def test_jsx_nested_in_expression(self):
        # The common React pattern: an element inside map() inside
        # children.  Brace bookkeeping must survive the nesting, and
        # code after the JSX must still be read as code.
        source = (
            "const el = <ul>{items.map((x) => <li key={x.id}>{x.name}</li>)}</ul>;\n"
            "function after() {}\n"
        )
        found = refs(source)
        for name in ("items", "map", "x", "id", "name"):
            self.assertIn(name, found)
        self.assertNotIn("key", found)  # attribute name
        # Lowercase HTML tags are not symbols.
        self.assertNotIn("ul", found)
        self.assertNotIn("li", found)
        self.assertEqual(defs(source)["after"], "function")

    def test_jsx_fragment(self):
        found = refs("const el = <><Widget /></>;\n")
        self.assertIn("Widget", found)

    def test_no_crash_on_garbage(self):
        for source in (
            "function ((({\n",
            "class {\n  m( {\n",
            "`unterminated ${x\n",
            "const s = 'unterminated\n",
            "/* unterminated\n",
            "a = /unterminated\n",
            "export { \n",
        ):
            result = parse_js_file("x.js", source)
            self.assertIsInstance(result.definitions, list)
            self.assertIsInstance(result.references, list)


if __name__ == "__main__":
    unittest.main()
