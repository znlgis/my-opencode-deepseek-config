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

### CI / non-interactive mode

Essential environment variables for agent-driven scripting:

| Variable | Effect |
|---|---|
| `GH_PROMPT_DISABLED=1` | Fail instead of prompting interactively |
| `GH_PAGER=cat` | Disable pager (already auto in non-TTY) |
| `GH_NO_UPDATE_NOTIFIER=1` | Skip version check (saves a request) |
| `GH_FORCE_TTY=1` | Force colored/formatted output even when piped |
| `NO_COLOR=1` | Strip ANSI color from output |
| `GH_DEBUG=api` | Log HTTP request/response for debugging |
| `GH_TELEMETRY=0` | Disable telemetry logging |

Token/auth: `GH_TOKEN` or `GITHUB_TOKEN` for non-interactive use. `@me` resolves to the authenticated user: `--assignee @me`, `--author @me`, `--owner @me`. Use `gh auth status --json` to verify the active session.

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
- `gh issue close <n> --duplicate-of <number|url>` (v2.88+) — closes as a
  duplicate (sets `--reason duplicate` and links the canonical issue).
- JSON fields: `issueType`, `parent`, `subIssues`, `subIssuesSummary`, `blockedBy`,
  `blocking`. Objects are `{"nodes": [...], "totalCount": N}` — compare node count
  vs `totalCount` to detect truncation (subIssues capped at 100, blocked/blocking
  at 50).
- GHES availability: issue **types** require GHES 3.17+, **blocked-by/blocking**
  require GHES 3.19+. On older hosts these flags error — fall back to labels.

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

## Gists (`gh gist`)

```bash
gh gist create file.txt -d "description"               # public by default
gh gist create file.txt -d "desc" --public              # explicit public
gh gist create file.txt -d "desc" --secret              # unlisted
gh gist create -f "name=content" -d "snippet"           # inline content (-f)
gh gist list -L 20 --public                              # your public gists
gh gist list -L 20 --secret                              # your secret gists
gh gist view <id> --raw                                  # raw content
gh gist view <id> --files                                # list files
gh gist edit <id> -a "new content" -f file.txt           # append content
gh gist edit <id> -f "file.txt=new content"               # replace content
gh gist delete <id>                                       # delete (needs confirmation)
gh gist clone <id> [dir]                                  # clone to local dir
```

## Secrets and Variables (`gh secret`, `gh variable`)

Actions secrets and variables — distinct commands, both scoped to repo/org/env:

```bash
# Secrets (encrypted)
gh secret set SECRET_NAME -b "value" -R owner/repo         # from string (-b body)
gh secret set SECRET_NAME < secret.txt -R owner/repo       # from file
gh secret set SECRET_NAME -b "$(cmd)" -R owner/repo        # from command output
gh secret set SECRET_NAME -b "val" --org org                # org-level
gh secret set SECRET_NAME -b "val" --env production -R owner/repo  # env-level
gh secret list -R owner/repo                               # list names (not values)
gh secret remove SECRET_NAME -R owner/repo

# Variables (plaintext)
gh variable set VAR_NAME -b "value" -R owner/repo
gh variable set VAR_NAME -b "val" --org org
gh variable set VAR_NAME -b "val" --env staging -R owner/repo
gh variable list -R owner/repo
gh variable remove VAR_NAME -R owner/repo
```

## Codespaces (`gh codespace`)

```bash
gh codespace list --json name,state,machine
gh codespace create -R owner/repo -b main -m basicLinux32gb
gh codespace create -R owner/repo -b main --devcontainer-path .devcontainer/dev.json
gh codespace stop -c <name>
gh codespace delete -c <name>
gh codespace logs -c <name>
gh codespace ssh -c <name>                                # SSH in
gh codespace ports visibility 3000:public -c <name>       # port forwarding
```

## Config (`gh config`)

```bash
gh config set editor "code --wait"
gh config set git_protocol ssh
gh config set prompt disabled                             # disable interactivity
gh config get git_protocol
gh config list
gh config set browser ""                                   # disable browser opening
```

## Extensions (`gh extension`)

```bash
gh extension install owner/repo                           # install from GitHub
gh extension install /path/to/local                       # install from local dir
gh extension list                                          # list installed
gh extension upgrade owner/repo                           # upgrade one
gh extension upgrade --all                                # upgrade all
gh extension remove owner/repo                            # uninstall
gh extension exec <name> [args]                           # run by name
```

## Aliases (`gh alias`)

```bash
gh alias set myprs 'pr list --author @me --state open -L 10'
gh alias set --shell review 'gh pr diff $1 | code -'     # pipe to editor
gh alias list
gh alias delete myprs
```

## AI-integrated commands (`gh agent-task`, `gh copilot`)

Newer command groups in `gh` v2.96+ for agent workflows:

```bash
# Agent tasks — delegate a coding task to a GitHub coding agent (preview)
gh agent-task create "Fix the login redirect bug" --base main
gh agent-task list --json id,state,title
gh agent-task view <id>

# Copilot in the CLI — built-in passthrough that downloads and runs the
# standalone Copilot CLI binary (~/.config/gh/copilot/); interactive, human-in-the-loop
gh copilot
```

`gh agent-task` needs an OAuth token (from `gh auth login`, `gho_` prefix) — a
plain PAT/`GH_TOKEN` is rejected. `gh copilot` is now a **built-in** binary
passthrough (since ~v2.80); it is **not** the old `gh-copilot` extension, which was
deprecated Oct 2025 — do not rely on `gh copilot explain/suggest` in scripts.
Both are interactive; prefer them for human-in-the-loop use, not scripted flows.

**Request a Copilot code review on a PR** (v2.88+, github.com + GHES 3.15+):

```bash
gh pr create --reviewer @copilot --fill      # request review on create
gh pr edit <n> --add-reviewer @copilot       # or add it afterward
```

## Recent commands worth knowing

- `gh pr revert <n>` — open a revert PR (`--draft`, `--title`, `--body-file`).
- `gh pr update-branch <n>` — sync PR branch from base (`--rebase`).
- `gh pr create --fill-first` — use only the first commit's message as body (vs `--fill` which uses all commits, `--fill-verbose` which uses all commit msg+bodies).
- `gh pr create --dry-run` — preview without creating.
- `gh pr create --recover <token>` — recover from a crashed create session.
- `gh run watch <id> --exit-status` — block until run finishes, exit non-zero on
  failure. `--compact` shows only failed/relevant steps. Fine-grained PATs lack
  `checks:read` — use `GITHUB_TOKEN` in Actions or a classic PAT.
- `gh run rerun <id> --failed` — rerun only failed jobs.
- `gh attestation verify|download file.bin -R owner/repo` — Sigstore supply-chain.
- `gh release download <tag>` — no auth needed on public repos (v2.96+).
- `gh skill` — first-class command group (v2.90+) for discovering/installing/publishing Agent Skills; see the `gh-skill` skill for the full workflow.
- `--json` with NO value: `gh pr list --json` prints all available JSON field names — use this to discover fields before querying. Works on all list/view commands.

## Quick reference

Most common commands agents need:

```bash
# PRs
gh pr list --state open --label bug -L 20 --json number,title,state,headRefName
gh pr view <n> --json state,mergeable,reviewDecision,statusCheckRollup
gh pr create --fill --base main
gh pr create --fill-first --base main                      # first commit msg only
gh pr create --title "Fix X" --body "Closes #12" --base main
gh pr diff <n>
gh pr merge <n> --squash --delete-branch
gh pr merge <n> --auto --squash                             # auto-merge when CI passes
gh pr revert <n>
gh pr checks <n>
gh pr ready <n>                                              # mark draft as ready

# Issues
gh issue list --state open --assignee @me -L 20 --json number,title,state,labels
gh issue create --title "Bug: ..." --body "Steps..." --label bug
gh issue close <n> --reason completed

# Actions
gh run list --workflow ci.yml --branch main --limit 20
gh run view <id> --log --log-failed
gh run watch <id> --exit-status
gh run rerun <id> --failed

# Releases
gh release create v1.2.0 --generate-notes
gh release list -L 5

# Search
gh search prs --author @me --state open --label bug --repo OWNER/REPO
gh search issues --repo OWNER/REPO --search "error in:title"
gh search repos --language go --stars ">5000"

# Gists
gh gist create file.txt -d "description"
gh gist list -L 20

# Secrets / Variables
gh secret set TOKEN -b "value" -R owner/repo
gh variable set URL -b "https://..." -R owner/repo

# Projects
gh project item-add <number> --url <issue-url>
gh project field-create <number> --name "Priority" --data-type SINGLE_SELECT \
  --single-select-options "High,Medium,Low"

# Config
gh config set git_protocol ssh
gh auth status --json
```
