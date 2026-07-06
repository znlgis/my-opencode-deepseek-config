---
name: codemap
description: Generate a structured, hierarchical map of a repository or directory for quick orientation. Use when starting work in an unfamiliar codebase, when the task mentions "project structure", "codebase overview", "directory layout", "what does this repo contain", or when you need a bird's-eye view before diving into specific files. Produces a concise tree with annotations, saving repeated exploration tokens.
license: MIT
compatibility: opencode
metadata:
  audience: developers
  workflow: exploration
---

# Codemap

Generate a compact, annotated directory tree that helps any agent quickly
understand what a codebase contains and where key things live. One well-crafted
codemap can replace dozens of exploratory globs and reads across a session.

Inspired by oh-my-opencode-slim's `codemap` skill, adapted for pure prompt use.

## When to use me

- First time entering a new repository or unfamiliar subdirectory
- The user asks "what's in this project?" or "show me the project structure"
- You need to understand the lay of the land before deciding where to search
- An agent keeps globbing the same directories — a codemap saves those tokens

## How to generate

### Step 1: Build the raw tree

Use the appropriate file listing for the platform:

```bash
# Windows (PowerShell)
Get-ChildItem -Recurse -Depth 3 -Name | Where-Object { $_ -notmatch 'node_modules|\.git|dist|build|\.next|coverage|__pycache__|\.venv' }

# Unix
find . -maxdepth 3 -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/dist/*' -not -path '*/build/*' | sort
```

Adjust the depth and exclusions based on repo size. For very large repos, start
at depth 2 and expand specific directories on demand.

### Step 2: Annotate the tree

After generating the raw tree, annotate key directories with **one-line** purpose
descriptions. Only annotate directories whose purpose isn't obvious from the name.

```text
project-root/
├── src/                       # main source
│   ├── app/                   # Next.js App Router pages + layouts
│   ├── components/            # shared React components
│   ├── lib/                   # utilities, API clients, shared logic
│   └── server/                # server-only code (DB, auth)
├── prisma/
│   └── schema.prisma          # database schema
├── public/                    # static assets
├── tests/                     # integration + e2e tests
├── package.json               # Node dependencies + scripts
└── tsconfig.json              # TypeScript config
```

### Step 3: Add quick-reference metadata

At the bottom, add a few lines that capture the project's essence:

```text
## Quick Reference
- Language: TypeScript 5.x
- Runtime: Node.js 20+
- Framework: Next.js 14 (App Router)
- DB: PostgreSQL via Prisma
- Package manager: pnpm
- Test runner: vitest
- Lint/format: biome
- Build: `pnpm build`
- Test: `pnpm test`
```

### Step 4: Store (optional)

For projects you'll return to, write the codemap to `.opencode/codemap.md` so
future sessions can read it directly instead of regenerating:

```bash
# One-time write
New-Item -ItemType Directory -Force -Path .opencode
# ... write the annotated tree ...
```

Then in subsequent sessions, the orchestrator can check for `.opencode/codemap.md`
before exploring blindly.

## Output discipline

- **Keep it concise.** Annotations are one-liners. The whole map should fit in
  well under 200 lines for most projects.
- **Skip noise.** Don't annotate entries whose purpose is obvious from the name
  (e.g., `tests/`, `docs/`, `public/`).
- **Highlight surprises.** Call out non-obvious patterns: "src/server/ contains
  route handlers, not under src/app/" or "config lives in src/lib/config.ts, not
  at the root."
- **Mention key config files.** `package.json`, `tsconfig.json`, `docker-compose.yml`,
  CI configs — these are the first files agents typically need to read.

## Anti-patterns

- Generating a 500-line exhaustive tree for a 200-file repo — compress it.
- Annotating every single file — only directories and key config files.
- Re-generating the same codemap every session when `.opencode/codemap.md` exists
  and is fresh.
