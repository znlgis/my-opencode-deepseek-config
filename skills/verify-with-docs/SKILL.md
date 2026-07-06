---
name: verify-with-docs
description: Verify a specific or fast-moving library, framework, or API against its current documentation before writing code — retrieval-first, never from memory. Use when implementing against a named dependency (especially one that changes fast or whose exact signatures matter), when unsure of an API's current shape, or when the task mentions "which version", "latest API", "did this change", "check the docs", "SDK", or a specific library/framework by name. Prevents hallucinated signatures and version drift.
license: MIT
compatibility: opencode
metadata:
  audience: developers
  workflow: research
---

# Verify With Docs (Retrieval-First)

Your training data has a cutoff and libraries move fast. Guessing an API from
memory is cheap to write and expensive to debug — a single hallucinated
signature can cost more than a whole afternoon of lookups. So for any code that
touches a specific dependency, **retrieve before you write.**

Adapted (pure prompt, no external tooling) from the retrieval-first pattern in
anomalyco/opencode's `effect` and `agents-sdk` skills: *"Your knowledge of this
library may be outdated. Prefer retrieval over pre-training."*

## When this applies

Reach for this skill when the task:

- implements against a named library/framework/SDK, especially a fast-moving one
  (web frameworks, cloud SDKs, AI/LLM SDKs, build tools, ORMs);
- depends on exact signatures, option names, return shapes, or config keys;
- says "latest", "current", "did X change in version Y", or names a version;
- is failing with an error that looks like a wrong/renamed API.

Skip it for stable stdlib usage you are certain of, or pure logic with no
external API surface.

## The loop

1. **Pin the exact version.** Read the version actually in use before reading any
   docs — the docs must match it.
   - JS/TS: `package.json` + the lockfile (`node_modules/<pkg>/package.json` is
     the ground truth for the installed version).
   - Python: `pyproject.toml` / `requirements.txt` / `pip show <pkg>`.
   - Go: `go.mod`. Rust: `Cargo.toml` / `Cargo.lock`.
2. **Go to the primary source, matched to that version.** Prefer, in order:
   official docs for that version → the library's own repo (tag/branch matching
   the version) → typed definitions shipped with the package (`.d.ts`, stubs,
   godoc) → reputable references. Skip blog posts and Q&A for signatures.
3. **Extract only what you need.** The exact signature, required vs optional
   params, return type, error modes, and any breaking-change note between the
   installed version and what memory assumes. Don't dump whole pages into
   context — quote the minimal relevant lines.
4. **Report verified facts with citations.** Every signature you hand back must
   carry a source URL or file path so the implementing agent can trust it.
5. **Only then implement** — against the retrieved API, not the remembered one.

## Prefer mounted references over repeated fetches

For a library you consult often, mount it once as an OpenCode **reference** so
agents search a local, version-pinned copy instead of re-fetching docs every
session (cheaper and offline-safe):

```jsonc
// opencode.json
"references": {
  "somelib": {
    "repository": "https://github.com/org/somelib",
    "branch": "v4"          // pin to the version you actually use
  },
  "internal-lib": {
    "path": "./vendor/internal-lib",
    "description": "Our vendored copy — read-only context"
  }
}
```

Then search the mounted reference for the exact API before answering. If the
reference is missing or stale, refresh it rather than falling back to memory.

## Output format

```
## Verified against docs
- <API/signature> — source: <URL or path> (version <x.y.z>)
- <option/return shape> — source: <URL or path>

## Changed / gotchas (if any)
- <what differs from a naive memory-based guess, and why it matters>

## Ready to implement
<one line: the caller can now code against the above without guessing>
```

## Anti-patterns

- Writing the call first and "checking later" — verify before you write.
- Citing a doc version that doesn't match the installed version.
- Pasting entire doc pages into context instead of the few relevant lines.
- Trusting a blog/Q&A snippet over the official docs or the shipped type defs.
