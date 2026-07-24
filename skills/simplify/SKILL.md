---
name: simplify
description: Behavior-preserving code simplification — reduce complexity without changing what the code does. Use when the task mentions "simplify", "refactor", "clean up", "reduce complexity", "too clever", "hard to read", or after a feature lands and the code needs polishing. Focused on improving readability, reducing nesting, and removing unnecessary abstraction. Assigned to the oracle agent for deep analysis before editing.
license: MIT
compatibility: opencode
metadata:
  audience: developers
  workflow: refactoring
---

# Simplify

Improve code readability and reduce cognitive load without altering behavior.
This is not a general refactor — it's a focused, safety-first simplification
discipline.

## Core rule

**The code must do exactly the same thing after simplification.** No behavior
changes, no "while I'm here" improvements, no API surface modifications. If a
test breaks after simplification, the simplification was wrong — revert it.

## When to use me

- Code is "too clever" — over-abstracted, deeply nested, hard to follow
- After a feature lands and code has accumulated cruft
- Review feedback says "this is hard to understand"
- You spot unnecessary indirection: wrappers that don't add value, interfaces
  with only one implementation, functions called exactly once

## Simplification checklist

Apply each pattern only if it reduces complexity without changing behavior:

### Reduce nesting

- **Early returns** — replace `if (x) { ... entire function ... }` with
  `if (!x) return`. Flattens 3+ levels of nesting into a straight line.
- **Guard clauses** — check preconditions at the top and bail early.
- **Extract deeply nested blocks** into well-named functions only when the
  extraction clarifies intent (not reflexively).

### Remove unnecessary abstraction

- **Single-caller functions** — inline them. A function called from exactly one
  place is just an indirection tax.
- **Single-implementation interfaces** — remove the interface and use the
  concrete type directly.
- **Thin wrappers** — if a function just calls another function with the same
  arguments plus one default, ask: is this wrapper pulling its weight?
- **Overly generic utilities** — a `createMapWithDefaultValue(key, defaultVal,
  strategy, cachePolicy)` called only as `createMapWithDefaultValue(x, {}, 'lru',
  'none')` should just be the concrete code.

### Reduce variable count

- **Inline single-use variables** — if a variable is used exactly once, inline
  its value at the use site. This is the single highest-leverage simplification.
- **Eliminate temporary "explanation" variables** — if the variable name just
  restates what the RHS already says, delete it.
  - Bad: `const isActive = user.status === 'active'; if (isActive) ...`
  - Better: `if (user.status === 'active') ...` (the condition is self-documenting)

### Simplify conditionals

- **Ternary over if/else for assignments** — `const x = cond ? a : b` beats
  5 lines of if/else.
- **Remove redundant else** — after a return/throw/break, the `else` is dead
  weight.
- **Boolean expressions over if/else returning booleans**:
  - Bad: `if (x > 0) { return true; } return false;`
  - Good: `return x > 0;`

### Reduce surface area

- **Unexport what isn't used externally.** If a symbol is only used within its
  own module, don't export it. Smaller public API = less to understand.
- **Remove dead parameters.** If a function accepts a parameter it never uses
  (and no caller passes it meaningfully), remove it.

## Safety protocol

Before every simplification:

1. **Run existing tests** — confirm they pass before you touch anything.
2. **Make one simplification at a time** — never batch them. One edit, verify,
   commit.
3. **Run tests after each simplification** — if anything fails, revert
   immediately.
4. **Use LSP "find all references"** — confirm a function truly has one caller
   before inlining; confirm a parameter is truly unused before removing.

## What NOT to simplify

- Performance-critical code where the "cleverness" is intentional and benchmarked.
- Framework boilerplate (route definitions, DI registrations, schema
  declarations) — these follow framework conventions, not logic.
- Error handling paths — never remove error checks to "simplify."
- Public API signatures — changing them is a breaking change, not a
  simplification.
- Test code that intentionally exercises edge cases with verbose setup.

## Report format

After simplifying, report:

```
## Simplified: <file>:<function or section>
- Before: <pattern that was complex, e.g. "4 levels of nested if">
- After: <what changed, e.g. "early returns, flattened to 1 level">
- Tests: <all passing / N tests unchanged>
```

If you examined code and found no valid simplifications, say so explicitly rather
than forcing changes.
