---
name: gh-cli
description: Patterns for invoking the GitHub CLI (gh) from agents. Use when the task mentions GitHub, gh, pull requests/PRs, issues, releases, gists, Actions/workflow runs, forks, repo cloning, reviews, or you need exact gh commands. Covers structured output, pagination, repo targeting, search vs list, issue types/sub-issues/discussions, and gh api fallback.
license: MIT
compatibility: opencode
metadata:
  audience: developers
  workflow: github
---

# GitHub CLI (`gh`) agent patterns

Authoritative patterns for driving the official `gh` CLI from agents, based on
[cli/cli](https://github.com/cli/cli) trunk. Prefer `gh` over raw `curl` or
`gh api` — `gh` handles auth, pagination, and JSON output automatically.

## Interactivity policy

`gh` does the right thing in non-TTY contexts: skips the pager, strips ANSI
color, and errors fast instead of prompting (e.g. `must provide --title and --body
when not running interactively`). Do not defensively set `GH_PAGER` or
`--no-pager` (no such flag exists).

- Set `GH_PROMPT_DISABLED=1` to force `gh` to fail instead of prompting — the
  safest default for automation.
- A few commands still prompt in a TTY even when they could infer: `gh pr merge`
  (pass `--squash`/`--merge`/`--rebase`), `gh release create` (pass `--notes` or
  `--generate-notes`), `gh run watch`/`gh pr create` (pass the run id / `--fill`).
- Exit codes: `0` success, `1` failure, `2` cancelled/interrupted, `4` auth
  required. Check `gh auth status` first; missing auth returns `4`, not `1`.

## Parsing JSON

Human output from `gh` is column-formatted. For structured data:

- `--json field1,field2,...` for structured output
- `--json` with no field list prints available fields — use this first
- `--jq '<expr>'` to filter without piping through `jq`
- `--template '<go-template>'` (alongside `--json`) for shaped text output.
  Note: `--template`/`-T` collides with a body-template flag on `gh pr create`,
  `gh issue create` — always check `--help` before using `-T`.

## Pagination

List commands cap results silently:

- `gh issue list`, `gh pr list`, `gh search ...`: use `-L N` (`--limit N`). Default is 30.
- `gh issue list` / `gh pr list` do not expose `totalCount` via `--json`. For a
  true total use `gh api graphql` to query `totalCount`; otherwise treat `-L` as cap.
- For raw API calls: `gh api --paginate <path>`. `--paginate` concatenates each
  page's JSON — for `[...]` responses that yields multiple arrays, not one. Add
  `--slurp` to wrap them into a single array, then `--jq` to shape it.

## Repo targeting

`gh` infers the repo from the cwd's git remotes. Pass `--repo OWNER/REPO` (`-R`)
to override.

## Search vs list

- `gh search issues|prs|code|repos|commits|users` uses GitHub's search index
  with full search syntax (`is:open`, `author:`, `label:`, `repo:owner/name`,
  `in:title`, ...). Each qualifier is its own bare token — do NOT quote them as
  one string: `gh search issues repo:cli/cli is:open author:monalisa` works,
  `gh search issues "repo:cli/cli is:open"` fails. Quote only multi-word free text.
- `gh issue list --search "..."` / `gh pr list --search "..."` take the query as
  one quoted string and are scoped to one repo.
- Bots author as GitHub Apps: `--author dependabot` matches nothing. Use
  `--app dependabot` (on `pr`/`issue list` and `search prs|issues`).

## Issue types, sub-issues, and relationships

- `gh issue create`: `--type <name>`, `--parent <number|url>`,
  `--blocked-by <number|url,...>`, `--blocking <number|url,...>`.
- `gh issue edit`: `--type/--remove-type`, `--parent/--remove-parent`,
  `--add-sub-issue/--remove-sub-issue`, `--add-blocked-by/--remove-blocked-by`,
  `--add-blocking/--remove-blocking`.
- `gh issue list --type <name>`.
- `gh issue view`/`list` --json fields: `issueType`, `parent`, `subIssues`,
  `subIssuesSummary`, `blockedBy`, `blocking`. `subIssues`/`blockedBy`/`blocking`
  are `{"nodes": [...], "totalCount": N}`; compare node count vs `totalCount` to
  detect truncation (subIssues capped at 100, blocked/blocking at 50).
- GHES: issue types/sub-issues need 3.17+; blocked-by/blocking need 3.19+.

## Discussions (`gh discussion`)

- `gh discussion list`: `--state` (open/closed/all), `--category`, `--answered`
  (tri-state for Q&A), `--search`, `--sort`/`--order`, `--limit`, `--json`
- `gh discussion view {<n>|<url>|<comment-id>}`: `--comments`, `--order`, `--limit`, `--json`
- `gh discussion create`: `--title`, `--body/--body-file`, `--category` (required non-interactively)
- `gh discussion edit`: `--title`, `--body/--body-file`, `--category`, `--add-label/--remove-label`
- `gh discussion comment`: `--body/--body-file`, `--edit`, `--delete` (with `--yes`)
- `--json`/`--jq`/`--template` on `list` and `view` only

## Reading files and directories (`gh repo read-file` / `read-dir`)

Preview commands — read repo contents over API without cloning. Honor `-R` and `--ref <branch|tag|commit>`.

```bash
gh repo read-file <path> [--ref <ref>] [--output <path> [--clobber]]
gh repo read-dir [<path>] [--ref <ref>] [--json <fields>]
```

- `--output` and `--json` are mutually exclusive. Binary files piped to stdout
  on non-TTY, refused on TTY.
- `--allow-escape-sequences` allows files containing terminal escape sequences.

## Fall back to `gh api`

When no porcelain command covers what you need:

```bash
gh api repos/{owner}/{repo}/pulls/{n}/comments    # review-thread comments (pr view --comments covers issue-level only)
gh api graphql -f query='...' -F var=value        # arbitrary GraphQL
gh api --paginate repos/{owner}/{repo}/issues --jq '.[].number'
gh api --paginate --slurp repos/{owner}/{repo}/issues --jq 'map(.number)'
gh api --cache 30m repos/{owner}/{repo}           # cache response for 30 min
```

- `{owner}/{repo}` placeholders are filled in automatically when run from a repo with detected remotes.
- `-f key=value` sends strings; `-F key=value` parses numbers/booleans/`@file`.

## Authentication

- `gh auth status` / `gh auth status --json` — active host, user, auth source
- `GH_TOKEN` / `GITHUB_TOKEN` env vars for non-interactive/CI use
- `GH_ENTERPRISE_TOKEN` for GHES
- `GH_HOST` for enterprise instances

## Other agent-relevant notes

- `gh pr checkout <n>` switches branches. Use `gh pr diff <n>` or `gh pr view <n>` to read.
- `NO_COLOR`, `CLICOLOR_FORCE`, `GH_FORCE_TTY` are honored. Set `GH_FORCE_TTY=1` for TTY-style output in an agent harness.
- `GH_REPO` sets default `OWNER/REPO` so you can omit `-R`.
- `GH_PROMPT_DISABLED` makes `gh` fail instead of prompting (good for automation).
- Never paste tokens on the command line; use `--with-token < file` or env vars.

## Recent commands worth knowing

Stable additions an agent should reach for (all honor `-R`/`--json`):

- `gh pr revert <n>` — open a revert PR for a merged PR (`--draft`, `--title`, `--body-file`).
- `gh pr update-branch <n>` — sync a PR branch from its base (`--rebase` for rebase instead of merge).
- `gh variable set|get|list|delete` — Actions **variables** (distinct from `gh secret`).
- `gh attestation verify|inspect|download` and `gh release verify` — Sigstore supply-chain checks.
- `gh run watch <id> --exit-status` — block until the run finishes and exit non-zero on failure (ideal for CI gates); `--compact` shows only failed/relevant steps. Note: fine-grained PATs lack `checks:read` and return 403 on annotations — use `GITHUB_TOKEN` in Actions or a classic PAT.

## Quick reference

Most common commands agents need:

```bash
# PRs
gh pr list --state open --label bug -L 20 --json number,title,state,headRefName
gh pr view <n> --json state,mergeable,reviewDecision,statusCheckRollup
gh pr create --fill --base main          # title/body from commits, no prompt
gh pr create --title "Fix X" --body "Closes #12" --base main
gh pr merge <n> --squash --delete-branch

# Issues
gh issue list --state open --assignee @me -L 20 --json number,title,state,labels
gh issue create --title "Bug: ..." --body "Steps..." --label bug
gh issue close <n> --reason completed

# Actions
gh run list --workflow ci.yml --branch main --limit 20
gh run view <id> --log --log-failed
gh run watch <id> --exit-status   # block on run, fail script if it fails
gh pr checks <n>                  # CI status for a PR

# Releases
gh release create v1.2.0 --generate-notes
gh release list -L 5

# Search
gh search prs --author @me --state open --label bug --repo OWNER/REPO

# Config
gh repo set-default OWNER/REPO

# Debugging
gh auth status
```
