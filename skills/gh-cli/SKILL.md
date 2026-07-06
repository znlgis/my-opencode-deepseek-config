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
gh repo rename NEW-NAME --repo OWNER/REPO
gh repo archive --repo OWNER/REPO --yes
gh repo unarchive --repo OWNER/REPO --yes

# Read files/dirs from a repo (no local clone needed)
gh repo read-dir path/to/dir --repo OWNER/REPO
gh repo read-file path/to/file --repo OWNER/REPO

# Autolink references (e.g. JIRA tickets)
gh repo autolink create --key-prefix JIRA- --url-template "https://jira.example.com/browse/JIRA-<num>"
gh repo autolink list
gh repo autolink delete <id>

# Deploy keys
gh repo deploy-key add key.pub --title "CI deploy" --allow-write
gh repo deploy-key list
gh repo deploy-key delete <id>

# Generate .gitignore / LICENSE from templates
gh repo gitignore list
gh repo gitignore view Go
gh repo license list
gh repo license view MIT
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
gh pr edit 42 --title "New title" --body "Updated body" --add-label triaged
gh pr lock 42 --reason resolved           # prevent further comments
gh pr unlock 42
gh pr revert 42 --yes                     # revert a merged PR
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
gh issue delete 7                         # permanently delete (use with caution)
gh issue lock 7 --reason resolved         # prevent further comments
gh issue unlock 7
gh issue pin 7                            # pin to top of issue list
gh issue unpin 7
gh issue transfer 7 https://github.com/OTHER/REPO   # move to another repo
gh issue status                           # dashboard of your open issues
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

# Actions cache
gh cache list                             # caches saved for the repo
gh cache delete <id>                      # or --all to clear every cache

# `gh actions` is a help hub that points at run/workflow/cache subcommands:
gh help actions
```

To debug a red PR: `gh pr checks <n>` → grab the failing run → `gh run view
<id> --log-failed`.

## Releases, gists, and more

```bash
# Releases
gh release create v1.2.0 --generate-notes
gh release create v1.2.0 ./dist/*.tar.gz --title "v1.2.0" --notes-file CHANGELOG.md
gh release list / gh release view v1.2.0 / gh release download v1.2.0
gh release edit v1.2.0 --title "v1.2.0 (hotfix)" --notes-file CHANGELOG.md
gh release upload v1.2.0 ./extra/*.zip    # add assets to an existing release
gh release delete-asset v1.2.0 asset.zip  # remove an asset
gh release verify v1.2.0                  # verify release assets against checksums
gh release verify-asset asset.zip --repo OWNER/REPO

# Gists
gh gist create file.txt --public --desc "snippet"
gh gist list
gh gist view <id> --files                 # list files in a gist
gh gist edit <id> --add extra.txt --desc "updated"
gh gist clone <id>                        # clone a gist locally
gh gist rename <id>                       # rename a gist file
```

## Search

```bash
gh search prs --author @me --state open --label bug
gh search issues "memory leak" --repo OWNER/REPO --state open
gh search code "func main" --language go --owner OWNER
gh search repos "cli tool" --language rust --sort stars --limit 10
gh search commits "fix memory leak" --repo OWNER/REPO --author @me
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
gh variable get REGION               # read a single variable's value
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

## Discussions

```bash
gh discussion create --title "RFC: ..." --body "Proposal details..." --repo OWNER/REPO --category Announcements
gh discussion list --repo OWNER/REPO
gh discussion view 3 --repo OWNER/REPO --comments
gh discussion comment 3 --body "+1" --repo OWNER/REPO
gh discussion edit 3 --title "Better title" --repo OWNER/REPO
```

## Organizations

```bash
gh org list                                # list orgs you belong to
```

## Rulesets

```bash
gh ruleset list --repo OWNER/REPO          # list repository-level rulesets
gh ruleset view 1 --repo OWNER/REPO        # inspect a ruleset
gh ruleset check main --repo OWNER/REPO    # check if a branch satisfies a ruleset
```

## Attestation (supply-chain security)

```bash
gh attestation download OWNER/REPO         # download build attestations
gh attestation verify <artifact> --repo OWNER/REPO --digest-alg sha256
gh attestation trusted-root                # inspect trusted root metadata
```

## Copilot

`gh copilot` is now a **built-in** command (preview) that launches the standalone
GitHub Copilot CLI — it downloads the Copilot CLI on first run, then execs it.
Because a built-in command takes precedence over the old `github/gh-copilot`
extension, the former `gh copilot suggest` / `gh copilot explain` subcommands are
no longer available this way; `gh copilot` now launches the full Copilot CLI.

```bash
gh copilot                                 # launch the interactive Copilot CLI
gh copilot -p "Summarize this week's commits" --allow-tool 'shell(git)'
gh copilot --remove                        # remove the gh-managed Copilot CLI
gh copilot -- --help                       # pass flags straight through to Copilot CLI
```

## Agent tasks

`gh agent-task` (preview) drives Copilot coding-agent tasks. `create` takes the
task prompt as a positional argument; requires an OAuth login (`gh auth login`).

```bash
gh agent-task create "Add unit tests for the auth module"   # start a task on the current repo
gh agent-task list                                          # your most recent tasks
gh agent-task view 123                                      # by PR number, or by session ID
```

## Skills

`gh skill` (alias `gh skills`, preview) installs and manages agent skills from
GitHub repos. `install`/`preview` take `OWNER/REPO` plus the skill name.

```bash
gh skill search terraform                          # search the skill marketplace
gh skill install github/awesome-copilot documentation-writer
gh skill preview github/awesome-copilot documentation-writer
gh skill list                                      # list installed skills
gh skill update --all                              # update all installed skills
gh skill publish --dry-run                         # validate skills for publishing
```

## Preview features

```bash
gh preview                                 # unstable, testing/demo-only feature previews
gh preview prompter                        # try the experimental prompter
```

## Aliases, extensions, and config

```bash
# Aliases — shorten common invocations
gh alias set prc 'pr create --fill'
gh alias set bugs 'issue list --label bug'
gh alias list
gh alias import aliases.yml --clobber        # bulk-import aliases from a YAML file

# Extensions — community subcommands
gh extension list
gh extension install dlvhdr/gh-dash
gh extension upgrade --all

# Config & status
gh config set editor "code --wait"
gh config set git_protocol ssh
gh config get editor                        # read a config value
gh config clear-cache                       # clear the API cache
gh status                    # cross-repo summary of your assigned work
```

## Account keys, templates & shell

```bash
# SSH keys (authentication + commit signing)
gh ssh-key list --type signing
gh ssh-key add ~/.ssh/id_ed25519.pub --title "laptop" --type authentication
gh ssh-key delete <id> --yes

# GPG keys (commit/tag signing)
gh gpg-key list
gh gpg-key add key.gpg --title "signing key"
gh gpg-key delete <id> --yes

# License & .gitignore templates (standalone `gh licenses`, plus repo-scoped forms)
gh licenses list                     # list SPDX license templates
gh licenses view mit                 # view a specific license's text
gh repo gitignore list               # .gitignore templates
gh repo license view MIT             # (repo-scoped alias of the license view)

# Shell completion (add to your shell profile)
gh completion -s bash                # or zsh | fish | powershell
```


## Environment variables

| Variable                  | Effect                                              |
| ------------------------- | --------------------------------------------------- |
| `GH_TOKEN` / `GITHUB_TOKEN` | auth token for non-interactive / CI use           |
| `GH_ENTERPRISE_TOKEN`     | auth token for a GitHub Enterprise Server instance |
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
