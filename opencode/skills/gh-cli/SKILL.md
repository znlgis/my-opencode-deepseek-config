---
name: gh-cli
description: Patterns for invoking the GitHub CLI (gh v2.100.0+) from agents. Use when the task mentions GitHub, gh, pull requests/PRs, issues, releases, gists, Actions/workflow runs, forks, repo cloning, reviews, or you need exact gh commands. Covers pagination, repo targeting, search vs list, discussions, projects, rulesets, skills, and gh api fallback.
---

# GitHub CLI (`gh`) agent patterns

Authoritative patterns for driving the official `gh` CLI (v2.100.0) from agents,
based on [cli/cli](https://github.com/cli/cli) trunk. Prefer `gh` over raw `curl`
or `gh api` — `gh` handles auth, pagination, and JSON output automatically.

## Security Advisory — escape-sequence injection (v2.97.0)

v2.97.0 fixed 4 escape-sequence injection vulnerabilities. These commands can
inject ANSI control sequences (cursor movement, screen clearing, clipboard
exfiltration) when output is rendered to a terminal:

| Affected command | Risk |
|---|---|
| `gh gist view` | Untrusted gist content → terminal |
| `gh api` | Untrusted API response → terminal |
| `gh pr diff` | Untrusted PR content → terminal |
| `gh release download --output -` | Untrusted release artifact → stdout/terminal |
| `gh codespace logs` | Untrusted container output → terminal |
| `gh agent-task view` / `create` | Untrusted task description/output → terminal |

**Agent rules:**
- **Never** pipe output to a terminal renderer — prefer `--json`, use `> file`
  for raw content; **never** `gh release download --output -` (stdout), always
  `--output <path>`.
- For `gh repo read-file`, binary content is auto-refused and ANSI is stripped
  by default since v2.97; use `--allow-escape-sequences` only when you need raw
  escapes and understand the risk.
- When fetching issues/PRs/comments from untrusted repos, prefer `--json` over
  human-readable output — JSON is not vulnerable to escape injection.

Other v2.96.0 / v2.97.0 fixes (advisory → impact → rule):

| Advisory | Impact | Agent rule |
|---|---|---|
| GHSA-cg6r-mpgc-h9mm | `gh auth status` (without `--show-token`) prints a token fragment for `github_pat_*` / `ghs_*` / `ghu_*` and the Actions `GITHUB_TOKEN`; classic `gho_*` / `ghp_*` are unaffected. | Upgrade ≥2.97.0; redact the token line before sharing output; never ship this output to CI logs. |
| GHSA-mm27-mwq9-fr5g | `gh attestation verify --signer-repo/--signer-workflow` interpolates the value into a regex without escaping metacharacters (`.` matches any char), so a lookalike name can satisfy a matcher intended for a trusted signer. | Only safe on ≥2.97.0; else use `--repo`/`--owner` (exact string match) or manually verify the SAN. |
| GHSA-4fjg-2h4q-fwg3 | Some REST request URLs built without escaping variable path segments → path traversal; a crafted name redirects `gh` to a different resource than intended. | Upgrade 2.97.0; be cautious with untrusted repo/input names. |
| GHSA-8cg3-r6g9-fpg2 | `gh codespace jupyter` opens the codespace-supplied URL unvalidated; a malicious codespace returns a `vscode://` link → command execution on the host (variant of GHSA-p2h2-3vg9-4p87 / CVE-2024-52308). | Upgrade ≥2.96.0; only use trusted codespaces. |

## Security Advisory — codespace port forwarding (v2.99.0)

GHSA-vfhh-p7hm-pxfh: `gh codespace ports` forwards container ports to the host
and, by default, binds them to **all interfaces** (0.0.0.0), not just localhost,
so a forwarded service is reachable from the local network. When forwarding a
port that serves sensitive or unauthenticated content, restrict exposure — do
not forward dev servers that hold secrets or unauthenticated endpoints.

## Interactivity policy

`gh` does the right thing in non-TTY contexts: skips the pager, strips ANSI
color, and errors fast instead of prompting.

- Set `GH_PROMPT_DISABLED=1` to force `gh` to fail instead of prompting.
- A few commands still need explicit flags non-interactively: `gh pr merge`
  (`--squash`/`--merge`/`--rebase`), `gh release create` (`--notes` or
  `--generate-notes`), `gh pr create` (`--fill` or explicit `--title`/`--body`).
- Exit codes: `0` success, `1` failure, `2` cancelled, `4` auth required.

| Variable | Effect |
|---|---|
| `GH_PROMPT_DISABLED=1` | Fail instead of prompting interactively |
| `GH_PAGER=cat` | Disable pager (already auto in non-TTY) |
| `GH_NO_UPDATE_NOTIFIER=1` | Skip version check (saves a request) |
| `NO_COLOR=1` | Strip ANSI color from output |
| `GH_DEBUG=api` | Log HTTP request/response for debugging |
| `GH_FORCE_TTY=1` | Force TTY output even when piped (debug color/rendering) |
| `GH_TELEMETRY=false` | Disable telemetry (opt-out; default enabled since v2.91.0) |

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

- `gh pr list`, `gh issue list`, `gh search ...`: use `-L N` (`--limit N`), default
  30. No `totalCount` via `--json` — use `gh api graphql` for true totals.
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
- Full qualifier syntax (v2.79.0+): `author:`, `label:`, `milestone:`, `assignee:`,
  `review:`, `status:`, `base:`, `head:`, `merged:`, `created:`, `updated:`,
  `closed:`, `comments:`, `interactions:`, `reactions:`.
- `gh search issues --search-type <lexical|semantic|hybrid>` (v2.98.0+): semantic
  relevance-ranked search, default `lexical`. `semantic`/`hybrid` are issue-only
  (reject `--include-prs`, `--sort`/`--order`, `--web`), bound to one page
  (separate 10/min bucket), github.com/ghe.com only — not single-tenant GHES.

## Reviewing PRs (`gh pr review` vs inline comments)

`gh pr review <n>` submits **only** a top-level review — one verdict + one body:

```bash
gh pr review <n> --approve  --body "LGTM"
gh pr review <n> --comment  --body "notes…"        # -c
gh pr review <n> --request-changes --body "…"       # -r (body required)
gh pr review <n> --approve --body-file review.md    # -F, use - for stdin
```

It has **no** flag for per-line comments — a common agent mistake. To attach
findings to specific lines, post one pending review via the REST API with a
`comments[]` array (new-side line numbers, inside changed hunks):

```bash
gh api repos/{owner}/{repo}/pulls/<n>/reviews --method POST \
  -f event=COMMENT -f body="overall summary" \
  -F 'comments[][path]=src/app.go' -F 'comments[][line]=42' \
  -F 'comments[][body]=this needs a nil check' \
  -F 'comments[][path]=src/app.go' -F 'comments[][line]=88' \
  -F 'comments[][body]=off-by-one here'
```

`event`: `APPROVE` | `REQUEST_CHANGES` | `COMMENT`, or omit for a `PENDING`
draft. Never auto-`APPROVE` from an agent — leave the verdict to a human.

Place each finding at the tightest scope its location allows: line comment
(inside a changed hunk), file-level comment (file in the diff, line outside a
hunk), or review body (file not in the diff). Line numbers must be the new-side
line inside a changed hunk.

## `gh pr diff` escape sequences (v2.97.0)

In non-TTY output, `gh pr diff` REJECTS (errors, does not strip) when the diff
contains escape sequences. To write a raw diff to a file, pass
`--allow-escape-sequences` explicitly. `-e <pattern>` is the short flag for
`--exclude` (matches path AND basename, e.g. `gh pr diff -e '*.generated.*'`).

## `gh pr checks`

gh pr checks <n> [--watch] [--fail-fast] [-i/--interval <sec>] [--required]
               [--json <fields>]

- --watch: poll until checks finish; --interval sets poll cadence.
- --fail-fast: exit on first failure.
- --required: only show required checks.
- JSON `bucket` field groups checks by state (pending/pass/fail).
- Exit code 8 = checks PENDING (distinct from 1 = command error).
- --json and --watch are mutually exclusive.

## `gh api` — the universal fallback

When no porcelain command covers what you need:

### GraphQL (preferred for complex queries)

```bash
gh api graphql -f query='query { viewer { login } }'
gh api graphql -F owner='cli' -F name='cli' -f query='query($name:String!,$owner:String!){repository(owner:$owner,name:$name){releases(last:3){nodes{tagName}}}}'
gh api graphql --paginate -f query='query($endCursor:String){search(query:"is:pr is:merged",type:ISSUE,first:100,after:$endCursor){nodes{...on PullRequest{number title}}pageInfo{hasNextPage endCursor}}}'
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

Other flags: `-i/--input <file>` reads the request body from a file;
`--verbose` / `--silent` control logging; `-p/--preview <name>` opts into an
OpenAPI preview; `--allow-escape-sequences` preserves raw escapes. There is
**no** `--preauthorize` flag — do not invent one.

## Authentication

- `gh auth status --json` — active host, user, auth source
- `GH_TOKEN` / `GITHUB_TOKEN` env vars for non-interactive/CI use
- `GH_ENTERPRISE_TOKEN` for GHES, `GH_HOST` for enterprise instances
- v2.100.0+ experimental `api_host` config (`gh config set api_host <host>`) overrides
  the API base host independently of `GH_HOST`; experimental — do not depend on it.
- `@me` resolves to the authenticated user (`--assignee @me`, `--author @me`)
- Never paste tokens on the command line; use `--with-token < file` or env vars

## Rate limits

- `gho_*` OAuth tokens (from `gh auth login`) get 5,000 GraphQL points/hr.
- `GITHUB_TOKEN` (Actions) is capped at 1,000 requests/hr/repo.
- Rely on response headers `x-ratelimit-remaining` / `x-ratelimit-reset`, not
  polling `/rate_limit` — cheaper and always accurate.
- On `429`/`403`, read `retry-after` and back off; don't retry blindly.
- Secondary limits: match `\bsecondary rate\b` in the response body — `retry-after`
  and `gh-limited-by` headers are often absent (github.com obfuscation). Wait
  ≥60s, then exponential backoff.

## Agent Skills (`gh skill`) — v2.94.0+

gh skill search <query> [--owner <org>]        # no --agent flag on search
gh skill list --json skillName,description,installed
gh skill preview <skill-id>

gh skill install owner/repo --agent opencode            # required non-interactively
gh skill install owner/repo --agent opencode --pin v1.2
gh skill install owner/repo --agent opencode --upstream # pull from upstream remote
gh skill install cli/cli gh --scope user                # self-install gh's own skill

gh skill update <skill-id> [--all] [--dry-run]
gh skill publish [<dir>] [--dry-run] [--tag <v>] [--fix]  # validate skills + cut a release

Default scope is `project`; default agent is `github-copilot` — always pass
`--agent opencode` (installs to ~/.config/opencode/skills/). Supported --agent
hosts: github-copilot, opencode, Devin, Grok, Cursor, antigravity-cli
(alias antigravity2.0), Windsurf (legacy), and more as the ecosystem adds
them. --scope user|project picks the install location.

## Agentic CI (`gh aw`) — GitHub's agentic CI framework (extension)

gh aw init            # scaffold the gh-aw workflow into the repo
gh aw compile         # compile the agent workflow (main flow)
gh aw run / logs / audit / status / checks / fix / upgrade / deploy

Still an extension (`gh extension install github/gh-aw`), NOT merged into the
main `gh` command. WARNING: the billing bug affecting versions 0.68.4–0.71.3
is retired — upgrade past 0.71.3 before using.

## Release verification (`gh release verify`) — v2.75.0+

Verify release artifact attestations (Sigstore supply-chain). No auth needed for
public repos.

```bash
gh release verify -R cli/cli                  # verify attestation for latest release
gh release verify v2.96.0 -R cli/cli          # verify a specific release
gh release verify-asset cli.zip -R cli/cli    # verify a specific asset (v2.81.0+)
```

Note: Releases created with v2.93.0+ are immutable — JSON output includes an
`isImmutable` field. Use `gh release download <tag>` (no auth for public repos,
v2.96.0+) to fetch artifacts.

## Attachments (`--attach`)

`--attach <path>` works on `gh issue`/`gh pr` `create`/`edit`/`comment`, repeatable
up to 50 per item. Formats: png/jpg/gif/webp/svg (images) and mp4/mov/webm
(video). Image alt text follows a `#` suffix: `--attach 'shot.png#crash screenshot'`.
GitHub.com + GHEC only — not supported on GHES.

gh issue create --title "crash" --attach shot.png#crash --attach repro.mp4

## Cold subcommands (one-liners)

- `gh project` — V2 projects: `item-add`/`item-list`/`item-edit`/`field-list`; by-name field editing `--field "Status" --value "Done"` (v2.97.0+).
- `gh ruleset` — `list`/`view`/`check -b <branch>` (compliance).
- `gh cache` — `list`/`delete`; `delete --succeed-on-no-caches` exits 0 when none match.
- `gh repo read-file <path>`/`read-dir [<path>]` — read repo contents without cloning (`--ref`, `--output`, `--clobber`); piped output is raw bytes, binary auto-refused.
- `gh discussion` — `list`/`view`/`create`/`comment`/`edit` (v2.94.0 preview).
- `gh issue create/edit` — `--type Bug|Feature|Task`, `--parent`, `--blocked-by`, `--blocking`; edit adds `--add-sub-issue`/`--add-blocked-by`/`--add-blocking` and `--remove-*` (types GHES 3.17+, relationships 3.19+).
- `gh gist` — `create`/`list`/`view --raw`/`edit`/`delete`/`clone`.
- `gh secret` / `gh variable` — set/list/remove; scoped repo/org/env. `gh codespace` — `list`/`create`/`stop`/`delete`/`logs`/`ssh`/`ports`.
- `gh config` — `set`/`get`/`list`/`clear-cache` (editor, git_protocol, prompt). `gh extension` — `install`/`list`/`upgrade`/`remove`/`search`/`create` (no auth since v2.90.0). `gh alias` — `set`/`list`/`delete`; `--shell` pipes to editors.
- `gh copilot` — native built-in (v2.86.0+), agent-driven + human-in-the-loop; not for unattended scripts. `gh agent-task` (alias `gh agent`/`gh agents`) — delegates coding tasks; requires `gho_` OAuth token.
- `gh pr create --reviewer @copilot` / `gh issue edit --add-assignee @copilot` — request Copilot review/assignment. `gh pr revert <n>`, `gh pr update-branch <n>`, `gh pr checkout` (alias `gh co`; `--worktree <path>` checks out the PR in an isolated git worktree, v2.99.0+), `gh pr create --fill-first/--dry-run/--recover <token>`.
- `gh run watch <id> --exit-status`, `gh run cancel <id> --force`, `gh run rerun <id> --failed`. `gh attestation verify|download -R owner/repo` — Sigstore supply-chain.
- `gh issue develop <n>` — linked branches; `--checkout` checks out the branch, `--worktree <path>` checks it out in an isolated git worktree (leaves cwd unchanged); `gh org list`; `gh label clone`; `gh browse --blame/--actions`; `gh status`. `gh preview prompter` — experimental; do not depend on it.

## Quick reference

```bash
# Issues (types v2.94.0+)
gh issue create --type Bug --title "..." --body "..."
gh issue list --type Bug --assignee @me -L 20 --json number,title,state,issueType
gh issue close <n> --duplicate-of <n>

# PRs
gh pr list --state open --label bug -L 20 --json number,title,state,headRefName
gh pr view <n> --json state,mergeable,reviewDecision,statusCheckRollup
gh pr create --fill --base main
gh pr diff <n> --exclude '*.generated.*'
gh pr merge <n> --squash --delete-branch
gh pr checks <n> --watch --required

# Actions
gh run list --workflow ci.yml --branch main --limit 20
gh run view <id> --log-failed
gh run watch <id> --exit-status --compact

# Releases
gh release create v1.2.0 --generate-notes
gh release download <tag>              # no auth for public repos
gh release verify -R owner/repo        # verify latest release attestation

# Search
gh search prs --author @me --state open --label bug --repo OWNER/REPO
gh search issues --repo OWNER/REPO --search "error in:title"

# Skills & agents
gh skill search <query>
gh skill install owner/repo --agent opencode

# Status & auth
gh status
gh config set git_protocol ssh
gh auth status --json
```

## Common workflows

### Create a PR from the current branch

```bash
# 1. Confirm the branch is pushed and see what will be included
git status --short
git log --oneline origin/main..HEAD

# 2. Create the PR. --fill derives title/body from commits; override when needed.
gh pr create --base main --fill
# Explicit form when the commit messages are not a good title/body:
gh pr create --base main --title "feat(api): add pagination" --body "Closes #42"

# 3. Verify and request review
gh pr view --json number,url,reviewDecision,statusCheckRollup
gh pr edit --add-reviewer @teammate
```

`--fill` fails if the branch has no commits ahead of base; use `--fill-first`
(first commit only) or explicit `--title`/`--body`. Never pass `--body` and
`--fill` together — `--fill` is ignored when `--body` is set.

### List and filter issues

```bash
# My open bugs, newest first, machine-readable
gh issue list --assignee @me --state open --label bug -L 20 \
  --json number,title,state,labels,updatedAt

# Full-text search within titles, scoped to a repo
gh issue list --search "timeout in:title" --state open -L 20

# Triage view: unassigned, no milestone
gh issue list --state open --no-assignee --no-milestone -L 30 \
  --json number,title,labels
```

`gh issue list` defaults to open issues and `-L 30`; always pass `-L` explicitly
when scripting so the result count is deterministic. Use `--json` + a field list
for parsing — never scrape the human-readable table.
