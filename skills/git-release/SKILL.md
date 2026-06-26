---
name: git-release
description: Prepare a tagged release with consistent release notes, a SemVer version bump, and a copy-pasteable gh release command. Use when cutting a release, drafting a changelog, tagging a version, or the task mentions "release", "changelog", "version bump", "tag", or "publish a new version".
license: MIT
compatibility: opencode
metadata:
  audience: maintainers
  workflow: github
---

# Git Release

Turn a set of merged changes into a clean, tagged release: collect what
changed, pick the right version, and produce the exact commands to cut it.
Pairs well with the `gh-cli` and `conventional-commits` skills.

## What I do

- Draft release notes from the commits/PRs since the last tag.
- Recommend a SemVer bump (major / minor / patch) from the change types.
- Provide copy-pasteable `git tag` and `gh release create` commands.

## When to use me

Use this when you are preparing a tagged release, drafting a changelog, or the
user asks to "cut a release", "bump the version", or "publish a new version".

## Step 1: Establish the baseline

```bash
git fetch --tags --force
git describe --tags --abbrev=0        # most recent tag, e.g. v1.3.0
git log <last-tag>..HEAD --oneline    # commits since that tag
```

If there is no prior tag, use the repository's first commit as the baseline
(`git log --oneline`) and start at `v0.1.0` (or `v1.0.0` for a first stable
release).

## Step 2: Decide the version (SemVer)

Inspect the Conventional Commit types since the last tag and pick the highest
applicable bump:

| Found in the range                                  | Bump   |
| --------------------------------------------------- | ------ |
| any `BREAKING CHANGE` / `!` marker                  | major  |
| any `feat:`                                         | minor  |
| only `fix:` / `perf:` / `refactor:` / `docs:` / ... | patch  |

`MAJOR.MINOR.PATCH` — incrementing major resets minor and patch to 0;
incrementing minor resets patch to 0. Pre-1.0 projects may treat `feat` as
patch and `BREAKING CHANGE` as minor — match the project's existing cadence.

## Step 3: Draft the release notes

Group changes by type, newest first, and link PRs/issues:

```markdown
## v1.4.0 — 2026-01-15

### Features
- add OAuth2 device-code login (#123)

### Fixes
- guard against null user before serializing (#130)

### Docs
- clarify skill auto-discovery paths (#128)
```

Prefer letting GitHub generate a first draft, then edit it:

```bash
gh release create v1.4.0 --generate-notes --draft
```

`--generate-notes` builds notes from merged PRs and the configured
`.github/release.yml` categories; `--draft` lets you review before publishing.

## Step 4: Tag and publish

```bash
# Annotated tag on the release commit
git tag -a v1.4.0 -m "v1.4.0"

# Push the tag to the remote
git push origin v1.4.0

# Create the GitHub release (the tag is created if it does not exist yet)
gh release create v1.4.0 --title "v1.4.0" --notes-file RELEASE_NOTES.md
# or attach build artifacts:
gh release create v1.4.0 ./dist/*.tar.gz --title "v1.4.0" --generate-notes
```

### Pre-releases

For alpha/beta/rc releases, use `--prerelease` and SemVer pre-release labels:

```bash
gh release create v2.0.0-beta.1 --prerelease --title "v2.0.0-beta.1" --generate-notes
```

### Pre-flight (dry run)

Before publishing, verify the release will look right:

```bash
gh release create v1.4.0 --generate-notes --draft   # draft first, review in browser
gh release view v1.4.0 --json tagName,name,body      # inspect before publishing
```

Note: some sandboxed environments cannot run `git push`. In that case push the
tag/commits via the available PR / report-progress workflow rather than
`git push --tags` directly.

## Step 5: Verify

```bash
gh release view v1.4.0
gh release list --limit 5
```

## Safety

- Never reuse or move an existing tag — pick the next unused version.
- Treat `gh release create` / `gh release delete` as mutating; confirm the
  target repo (`-R OWNER/REPO`) and tag before running.
- Keep the tag, the changelog heading, and any manifest version (e.g.
  `package.json`) in agreement.
