---
name: gh-cli
description: Operate GitHub from the terminal with the official gh CLI. Use when the task mentions GitHub, gh, pull requests/PRs, issues, releases, gists, Actions/workflow runs, forks, repo cloning, reviews, or "create a PR / merge / check CI" and you need exact, copy-pasteable gh commands instead of guessing the web UI or raw API.
license: MIT
compatibility: opencode
metadata:
  audience: developers
  workflow: github
---

# GitHub CLI (`gh`)

Authoritative, copy-pasteable recipes for driving GitHub from the terminal with
the official `gh` CLI (https://github.com/cli/cli). Prefer `gh` over hand-rolled
`curl` calls or `git` push helpers: `gh` handles auth, pagination, and JSON
output for you. Drop down to `gh api` only when no porcelain command exists.

## When to use me

Use this skill when the user asks to:

- create / view / review / merge / check out a **pull request**
- create / list / comment / close an **issue**
- inspect **CI / GitHub Actions** runs and logs (`gh run`, `gh workflow`)
- create **releases**, **gists**, **forks**, or **clone** repos
- **search** issues, PRs, code, or repos
- script GitHub data with **`gh api`** + `--jq`

If the task can be done with a porcelain subcommand, never reach for raw API.

## First: verify auth and context

```bash
gh auth status                 # who am I, which host, what scopes
gh auth login                  # interactive login (browser or token)
gh auth login --with-token < token.txt    # non-interactive
gh auth refresh -s project,read:org       # add missing OAuth scopes
gh repo set-default OWNER/REPO            # pin the repo for ambiguous commands
```

Most commands infer the repo from the current git remote. Outside a clone, pass
`-R OWNER/REPO` (alias for `--repo`) explicitly.

## Repositories

```bash
gh repo view OWNER/REPO --web            # open in browser
gh repo clone OWNER/REPO                 # clone (sets up remotes)
gh repo create my-proj --private --source=. --remote=origin --push
gh repo fork OWNER/REPO --clone          # fork + clone + add upstream
gh repo list OWNER --limit 50            # list repos for a user/org
gh repo edit --default-branch main --enable-issues
```

## Pull requests

```bash
# Create — fill from commits, or pass title/body explicitly
gh pr create --fill
gh pr create --title "Fix X" --body "Closes #12" --base main --head feature
gh pr create --draft --reviewer alice,bob --label bug --assignee @me

# Inspect
gh pr list --state open --label bug --author @me
gh pr status                              # PRs relevant to you
gh pr view 42                             # summary
gh pr view 42 --json state,mergeable,reviewDecision --jq '.'
gh pr diff 42                             # unified diff
gh pr checks 42                           # CI check status for the PR

# Work with a PR locally
gh pr checkout 42                         # fetch + switch to the PR branch

# Review / approve / request changes
gh pr review 42 --approve
gh pr review 42 --request-changes --body "See comments"
gh pr comment 42 --body "Thanks!"

# Land it
gh pr merge 42 --squash --delete-branch   # also --merge or --rebase
gh pr merge 42 --auto --squash            # enable auto-merge when checks pass
gh pr merge 42 --admin                    # bypass required checks (use sparingly)
gh pr update-branch 42                    # update the PR branch with its base
gh pr ready 42                            # mark draft as ready
gh pr close 42 / gh pr reopen 42
```

Useful JSON fields for `--json`: `number,title,state,isDraft,mergeable,
mergeStateStatus,reviewDecision,headRefName,baseRefName,url,labels,reviews,
statusCheckRollup`. Combine with `--jq` to extract exactly what you need.

## Issues

```bash
gh issue create --title "Bug: crash on save" --body "Steps..." --label bug
gh issue list --state open --assignee @me --label "good first issue"
gh issue view 7 --comments
gh issue comment 7 --body "Reproduced on main"
gh issue close 7 --reason completed       # or "not planned"
gh issue edit 7 --add-label triaged --add-assignee @me
gh issue develop 7 --checkout             # create+checkout a branch for the issue
```

## GitHub Actions / CI

```bash
gh run list --workflow ci.yml --branch main --limit 20
gh run view <run-id>                      # job/step summary
gh run view <run-id> --log                # full logs
gh run view <run-id> --log-failed         # only failed steps (fast triage)
gh run watch <run-id>                     # live-follow until done
gh run rerun <run-id> --failed            # rerun only failed jobs
gh run download <run-id>                  # download artifacts

gh workflow list
gh workflow view ci.yml
gh workflow run deploy.yml -f environment=staging   # dispatch with inputs
gh workflow enable ci.yml / gh workflow disable ci.yml
```

To debug a red PR: `gh pr checks <n>` → grab the failing run → `gh run view
<id> --log-failed`.

## Releases, gists, and more

```bash
gh release create v1.2.0 --generate-notes
gh release create v1.2.0 ./dist/*.tar.gz --title "v1.2.0" --notes-file CHANGELOG.md
gh release list / gh release view v1.2.0 / gh release download v1.2.0

gh gist create file.txt --public --desc "snippet"
gh gist list
```

## Search

```bash
gh search prs --author @me --state open --label bug
gh search issues "memory leak" --repo OWNER/REPO --state open
gh search code "func main" --language go --owner OWNER
gh search repos "cli tool" --language rust --sort stars --limit 10
```

## Projects, labels, and repo metadata

```bash
# Projects (Projects v2) — plan/track work
gh project list --owner OWNER
gh project view 5 --owner OWNER --web
gh project item-list 5 --owner OWNER
gh project item-add 5 --owner OWNER --url https://github.com/OWNER/REPO/issues/7

# Labels
gh label list
gh label create bug --color B60205 --description "Something is broken"
gh label edit bug --name defect
gh label clone OWNER/REPO            # copy labels from another repo

# Variables & secrets (Actions / Dependabot / Codespaces)
gh secret set TOKEN < token.txt      # never echo secrets on the CLI
gh secret list
gh variable set REGION --body us-east-1
gh variable list

# Sync a fork with upstream
gh repo sync OWNER/fork --source OWNER/upstream
```

## Codespaces

```bash
gh codespace list
gh codespace create -R OWNER/REPO -b main
gh codespace code            # open current/selected codespace in VS Code
gh codespace ssh
gh codespace delete
```

## Aliases, extensions, and config

```bash
# Aliases — shorten common invocations
gh alias set prc 'pr create --fill'
gh alias set bugs 'issue list --label bug'
gh alias list

# Extensions — community subcommands
gh extension list
gh extension install dlvhdr/gh-dash
gh extension upgrade --all

# Config & status
gh config set editor "code --wait"
gh config set git_protocol ssh
gh status                    # cross-repo summary of your assigned work
```

## Environment variables

| Variable                  | Effect                                              |
| ------------------------- | --------------------------------------------------- |
| `GH_TOKEN` / `GITHUB_TOKEN` | auth token for non-interactive / CI use           |
| `GH_REPO`                 | default `OWNER/REPO` so you can omit `-R`            |
| `GH_HOST`                 | target a GitHub Enterprise host                     |
| `GH_PAGER` / `PAGER`      | pager for long output (`cat` to disable)            |
| `NO_COLOR`                | disable ANSI color (stable output for scripts)      |
| `GH_PROMPT_DISABLED`      | never prompt — fail instead (good for automation)   |

## Raw API (escape hatch)

Use only when no porcelain command exists. `gh api` adds auth, base URL, and
pagination automatically.

```bash
gh api repos/OWNER/REPO --jq '.default_branch'
gh api repos/OWNER/REPO/pulls --jq '.[].title'        # array of PR titles
gh api --paginate repos/OWNER/REPO/issues --jq '.[].number'
gh api -X POST repos/OWNER/REPO/issues -f title="Bug" -f body="..."
gh api graphql -f query='query{ viewer{ login } }' --jq '.data.viewer.login'
```

- `-f key=value` sends string fields; `-F key=value` parses numbers/booleans/`@file`.
- `--paginate` follows `Link` headers so you get every page.
- `--jq <expr>` runs a jq filter on the response without piping to `jq`.

## Scripting & output discipline

- Add `--json <fields>` to any list/view command for stable, parseable output,
  then filter with `--jq`. Never screen-scrape human output.
- `--limit N` controls how many results come back (default is small).
- Exit codes are meaningful: `0` success, non-zero on failure — check them in
  scripts instead of grepping stderr.
- `gh` respects `GH_TOKEN` / `GITHUB_TOKEN` env vars for non-interactive/CI use.
- For GitHub Enterprise, set `GH_HOST` or pass `--hostname`.

## Safety

- Treat `gh pr merge`, `gh release create`, `gh repo delete`, and any
  `gh api -X DELETE/POST/PATCH` as **mutating** — confirm the target repo and
  ref before running, especially with `-R`/`--repo` pointing elsewhere.
- Prefer `--squash --delete-branch` for a clean history unless the project
  convention says otherwise.
- Never paste tokens on the command line where they land in shell history; use
  `--with-token < file` or an env var.
