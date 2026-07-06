---
name: gh-skill
description: Manage agent skills with gh skill. Use this skill to discover, preview, install, update, and publish Agent Skills so an agent can self-manage the skills available in its environment.
license: MIT
compatibility: opencode
metadata:
  audience: developers
  workflow: github
---

# Managing skills with `gh skill`

`gh skill` installs, previews, searches, updates, and publishes
[Agent Skills](https://agentskills.io). An agent can use it to keep its
own skill set in sync with one or more GitHub repositories.

The command is also aliased as `gh skills`. Prefer the canonical singular
`gh skill` in scripts and docs.

## Search

```bash
gh skill search <query>                                  # free-text search
gh skill search <query> --owner <org>                    # restrict to one owner
gh skill search <query> --limit 20 --page 2
gh skill search <query> --json skillName,repo,description
```

## Preview before installing

```bash
gh skill preview <owner>/<repo> <skill-name>
gh skill preview <owner>/<repo> <skill-name>@v1.2.0     # pin a version
```

## Install

```bash
gh skill install <owner>/<repo> <skill-name>
gh skill install <owner>/<repo> <skill-name>@v1.2.0
gh skill install <owner>/<repo> skills/<scope>/<skill-name>   # exact path, fastest
gh skill install ./local-skills-repo --from-local
```

Both `<owner>/<repo>` and `<skill-name>` are required for non-local installs.

Useful flags:
- `--agent <id>` — target host (e.g. `opencode`). Repeat for multiple. When
  non-interactive, defaults to `github-copilot` — you must set this explicitly
  for your agent.
- `--scope project|user` — `project` (default) writes inside the current git
  repo; `user` writes to the home directory and applies everywhere.
- `--pin <ref>` — pin to a tag, branch, or commit SHA. Mutually exclusive with
  `--from-local` and inline `@version` syntax.
- `--allow-hidden-dirs` — also discover skills under dot-directories such as
  `.claude/skills/`. Avoid unless necessary.
- `--force` — overwrite an existing install.

## List installed skills

```bash
gh skill list                   # list installed skills
```

## Update

```bash
gh skill update --all           # update every installed skill
gh skill update <skill>         # update one
gh skill update <skill> --force
gh skill update --unpin         # drop the pin and move to latest
```

## Publish

Publishing turns a repo into a discoverable skill source. Skills are discovered
with these conventions:

- `skills/<name>/SKILL.md`
- `skills/<scope>/<name>/SKILL.md`
- `<name>/SKILL.md` (root-level)
- `plugins/<scope>/skills/<name>/SKILL.md`

Each `SKILL.md` needs YAML frontmatter with `name` (matching the directory name)
and `description` (≤1024 chars recommended). `license` is optional but recommended.

### Validate, then publish

```bash
gh skill publish --dry-run                 # validate only, no release
gh skill publish --dry-run ./path/to/repo  # validate a specific dir
gh skill publish --fix                     # auto-strip install metadata (does not publish)
gh skill publish --tag v1.0.0              # non-interactive publish
gh skill publish                           # interactive publish flow
```

`--fix` and `--dry-run` are mutually exclusive. `--fix` only rewrites
install-injected `metadata.github-*` keys; commit the result and re-run `publish`.

The publish flow:
1. Adds the `agent-skills` topic to the repo (so search can find it).
2. Uses `--tag` (or prompts for one in a TTY).
3. Auto-pushes any unpushed commits.
4. Creates a GitHub release with auto-generated notes.

Always pass `--tag` so it doesn't fall through to the interactive flow.

## Self-management pattern for agents

A reasonable loop for an agent managing its own skills:

1. `gh skill search <topic> --json skillName,repo,namespace`
2. `gh skill preview <repo> <skill>` to inspect the `SKILL.md`.
3. `gh skill install <repo> <skill> --agent opencode --pin <ref>` for a
   reproducible install.
4. Periodically `gh skill update --all` to refresh.
