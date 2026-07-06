---
name: gh-cli
description: Patterns for invoking the GitHub CLI (gh) from agents. Use when the task mentions GitHub, gh, pull requests/PRs, issues, releases, gists, Actions/workflow runs, forks, repo cloning, reviews, or you need exact gh commands. Covers structured output, pagination, repo targeting, search vs list, issue types/sub-issues/discussions, projects, rulesets, and gh api fallback.
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
when not running interactively`).

- Set `GH_PROMPT_DISABLED=1` to force `gh` to fail instead of prompting.
- A few commands still need explicit flags non-interactively: `gh pr merge`
  (`--squash`/`--merge`/`--rebase`), `gh release create` (`--notes` or
  `--generate-notes`), `gh pr create` (`--fill` or explicit `--title`/`--body`).
- Exit codes: `0` success, `1` failure, `2` cancelled, `4` auth required. Check
  `gh auth status` first; missing auth returns `4`, not `1`.

## Parsing JSON

- `--json field1,field2,...` for structured output
- `--json` with no field list prints available fields — use this first
- `--jq '<expr>'` to filter without piping through `jq`
- `--template '<go-template>'` for shaped text output. Note: `-T`/`--template`
  collides with `gh pr create`/`gh issue create` body-template flag.
- Template helpers: `tablerow`, `tablerender`, `timeago`, `truncate`, `hyperlink`,
  `pluck`, `join`, `color`, `autocolor`, `regexMatch`, `contains`.

## Pagination

List commands cap results silently:

- `gh pr list`, `gh issue list`, `gh search ...`: use `-L N` (`--limit N`).
  Default is 30. No `totalCount` via `--json` — use `gh api graphql` for true
  totals.
- `gh api --paginate <path>` concatenates each page's JSON. For `[...]` responses
  that yields multiple arrays — add `--slurp` to wrap into one array.
- `gh api --cache 30m <path>` caches responses to avoid repeat hits.

## Repo targeting

`gh` infers the repo from cwd git remotes. Pass `--repo OWNER/REPO` (`-R`) to
override. Set `GH_REPO=OWNER/REPO` for session-wide default.

## Search vs list

- `gh search issues|prs|code|repos|commits` uses GitHub's search index with full
  syntax. Each qualifier is its own bare token — do NOT quote them as one string:
  `gh search issues repo:cli/cli is:open author:monalisa` works,
  `gh search issues "repo:cli/cli is:open"` fails. Quote only multi-word free text.
- `gh issue list --search "..."` / `gh pr list --search "..."` take one quoted
  string, scoped to one repo.
- Bots author as GitHub Apps: `--author dependabot` fails. Use `--app dependabot`
  (on `pr`/`issue list` and `search prs|issues`).
- Exclude qualifiers with `--` stop-parser: `gh search issues -- "error -label:bug"`.

## Issue types, sub-issues, and relationships

- `gh issue create`: `--type <name>`, `--parent <number|url>`,
  `--blocked-by <number|url,...>`, `--blocking <number|url,...>`.
- `gh issue edit`: `--type/--remove-type`, `--parent/--remove-parent`,
  `--add-sub-issue/--remove-sub-issue`, `--add-blocked-by/--remove-blocked-by`.
- `gh issue list --type <name>`.
- JSON fields: `issueType`, `parent`, `subIssues`, `subIssuesSummary`, `blockedBy`,
  `blocking`. Objects are `{"nodes": [...], "totalCount": N}` — compare node count
  vs `totalCount` to detect truncation (subIssues capped at 100, blocked/blocking
  at 50).

## Discussions (`gh discussion`)

- `gh discussion list`: `--state` (open/closed/all), `--category`, `--answered`
  (tri-state for Q&A), `--search`, `--sort`/`--order`, `--limit`, `--json`
- `gh discussion view {<n>|<url>}`: `--comments`, `--order`, `--limit`, `--json`
- `gh discussion create`: `--title`, `--body`, `--category` (required non-interactively)
- `gh discussion comment`: `--body`, `--edit`, `--delete` (with `--yes`)

## Projects V2 (`gh project`)

Full project management with 19 subcommands. Key patterns:

```bash
# List and view
gh project list --owner "@me"
gh project view <number> --json title,url,fields

# Items — add issues/PRs to projects
gh project item-add <number> --url <issue-or-pr-url>
gh project item-list <number> --owner "@me" --limit 50
gh project item-edit <item-id> --field-id <field-id> --text "value"
gh project item-edit <item-id> --field-id <field-id> --iteration-id <id>
gh project item-edit <item-id> --field-id <field-id> --single-select-option-id <id>
gh project item-archive <number> <item-id>

# Custom fields
gh project field-list <number> --json name,id,dataType
gh project field-create <number> --name "Status" --data-type SINGLE_SELECT \
  --single-select-options "Todo,In Progress,Done"

# CRUD
gh project create --title "My Project" --owner "@me"
gh project copy <number> --source-owner <org> --target-owner "@me" --title "Copy"
gh project close <number>
gh project delete <number>
```

All `gh project` commands accept `--owner` (user or org). Use `--format json` +
`--jq` for structured output on `item-list`, `field-list`.

## Rulesets (`gh ruleset`)

```bash
gh ruleset list -R owner/repo             # --json for structured
gh ruleset view <id> -R owner/repo
gh ruleset check -b main -R owner/repo    # check branch compliance
```

## Actions cache (`gh cache`)

```bash
gh cache list -R owner/repo -L 50 --json key,sizeInBytes,ref
gh cache delete <key> -R owner/repo
gh cache delete --all -R owner/repo       # requires confirmation
```

## Reading files and directories (`gh repo read-file` / `read-dir`)

Read repo contents over API without cloning. Honor `-R` and `--ref <branch|tag|commit>`.

```bash
gh repo read-file <path> [--ref <ref>] [--output <path> [--clobber]]
gh repo read-dir [<path>] [--ref <ref>] [--json <fields>]
```

## `gh api` — the universal fallback

When no porcelain command covers what you need:

### GraphQL (preferred for complex queries)

```bash
# Basic
gh api graphql -f query='query { viewer { login } }'

# With variables (use -F for typed: numbers, booleans, @file)
gh api graphql -F owner='cli' -F name='cli' -f query='
  query($name: String!, $owner: String!) {
    repository(owner: $owner, name: $name) {
      releases(last: 3) { nodes { tagName } }
    }
  }'

# Paginated GraphQL ($endCursor is auto-managed by --paginate)
gh api graphql --paginate -f query='
  query($endCursor: String) {
    search(query: "is:pr is:merged", type: ISSUE, first: 100, after: $endCursor) {
      nodes { ... on PullRequest { number title } }
      pageInfo { hasNextPage endCursor }
    }
  }'
```

### REST

```bash
gh api repos/{owner}/{repo}/releases
gh api repos/{owner}/{repo}/issues/123/comments -f body='Hello'
gh api --paginate --slurp repos/{owner}/{repo}/issues --jq 'map(.number)'
gh api --cache 30m repos/{owner}/{repo}
```

`{owner}/{repo}` placeholders auto-fill from detected remotes.
`-f key=value` sends strings; `-F key=value` parses numbers/booleans/`@file`.

## Authentication

- `gh auth status --json` — active host, user, auth source
- `GH_TOKEN` / `GITHUB_TOKEN` env vars for non-interactive/CI use
- `GH_ENTERPRISE_TOKEN` for GHES, `GH_HOST` for enterprise instances
- Never paste tokens on the command line; use `--with-token < file` or env vars

## Recent commands worth knowing

- `gh pr revert <n>` — open a revert PR (`--draft`, `--title`, `--body-file`).
- `gh pr update-branch <n>` — sync PR branch from base (`--rebase`).
- `gh variable set|get|list|delete` — Actions **variables** (distinct from secrets).
- `gh attestation verify|download file.bin -R owner/repo` — Sigstore supply-chain.
- `gh run watch <id> --exit-status` — block until run finishes, exit non-zero on
  failure. `--compact` shows only failed/relevant steps. Fine-grained PATs lack
  `checks:read` — use `GITHUB_TOKEN` in Actions or a classic PAT.
- `gh run rerun <id> --failed` — rerun only failed jobs.
- `gh agent-task create "Fix login bug" --base main` (preview).

## Quick reference

Most common commands agents need:

```bash
# PRs
gh pr list --state open --label bug -L 20 --json number,title,state,headRefName
gh pr view <n> --json state,mergeable,reviewDecision,statusCheckRollup
gh pr create --fill --base main
gh pr create --title "Fix X" --body "Closes #12" --base main
gh pr merge <n> --squash --delete-branch
gh pr diff <n>
gh pr checks <n>

# Issues
gh issue list --state open --assignee @me -L 20 --json number,title,state,labels
gh issue create --title "Bug: ..." --body "Steps..." --label bug
gh issue close <n> --reason completed

# Actions
gh run list --workflow ci.yml --branch main --limit 20
gh run view <id> --log --log-failed
gh run watch <id> --exit-status
gh pr checks <n>

# Releases
gh release create v1.2.0 --generate-notes
gh release list -L 5

# Search
gh search prs --author @me --state open --label bug --repo OWNER/REPO
gh search issues --repo OWNER/REPO --search "error in:title"
gh search repos --language go --stars ">5000"

# Projects
gh project item-add <number> --url <issue-url>
gh project field-create <number> --name "Priority" --data-type SINGLE_SELECT \
  --single-select-options "High,Medium,Low"

# Repo / Config
gh repo set-default OWNER/REPO
gh repo view OWNER/REPO --json description,stargazerCount
gh auth status
```
