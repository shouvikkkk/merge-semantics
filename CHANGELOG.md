# Changelog

## 0.2.0 — 2026-11-07

### Added
- JavaScript/TypeScript support: `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`,
  `.cjs` files are now parsed with a built-in tokenizer (stdlib-only,
  no third-party parser) and take part in every finding kind, not just
  the registry/contract checks. Extracted surface mirrors the Python
  extractor: functions, classes, methods qualified as
  `ClassName.method`, top-level `const`/`let`/`var` bindings (arrow
  and function expressions count as functions), destructured bindings,
  and — for TypeScript — `interface`, `type`, `enum` and `namespace`
  declarations. References cover call callees, `new X`,
  `extends`/`implements` operands, import/export specifiers, member
  names after `.`, and JSX component names (capitalised tags only;
  JSX text between tags is never recorded as a reference).
- Method renames are now detected for both languages: a qualified
  method name (`Widget.render`) is also matched on its unqualified
  tail at call sites (`w.render()`), so the canonical
  rename-vs-old-caller case works for class methods, not just
  top-level functions.

### Changed
- The definition-line filter in the rename check now recognises
  JavaScript/TypeScript definitions (`function`/`class`/`interface`/
  `type`/`enum`, `const`/`let`/`var` bindings, `name(...) {` method
  definitions) so a branch's own added definitions are never mistaken
  for call sites.

### Fixed
- `branch_added_call` no longer misses calls written as member
  access (`obj.method()`): the word match for a method tail allows a
  preceding `.`, where top-level names still require a non-`.` boundary.

## 0.1.0 — 2026-10-25

- Initial release: post-merge semantic conflict checker for Python
  files — `removed-symbol-still-referenced`, `rename-vs-old-caller`,
  `duplicate-definition`, `shared-registry-collision`,
  `contract-drift`.
