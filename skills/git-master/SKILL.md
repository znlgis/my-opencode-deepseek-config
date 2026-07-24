---
name: git-master
description: Advanced Git operations beyond basic add/commit/push — atomic commits, rebase, squash, fixup, blame, bisect, reflog, code archaeology (git log -S/-G), and worktree management. Use when the task mentions rebase, squash, bisect, blame, reflog, fixup, finding deleted code, tracing commit history, cleaning up a branch, or recovering lost work.
license: MIT
compatibility: opencode
metadata:
  audience: developers
  workflow: git
---

# Git Master

Advanced Git patterns for when the basics aren't enough.

## When to use me

- Rebasing, squashing, or interactive history editing
- Finding when/where code was introduced or deleted (`git blame`, `git log -S/-G`)
- Recovering lost commits or branches (`git reflog`)
- Bisecting to find the commit that introduced a bug
- Cherry-picking or fixup workflows
- Managing stacked branches or worktrees

## Atomic commits

One logical change per commit. Before committing, review with:

```bash
git add -p              # stage hunks interactively — only what belongs together
git diff --cached       # review exactly what you're about to commit
```

If the diff mixes unrelated changes, split them into separate commits.

## Rewriting history (safety first)

Never rewrite shared/pushed history unless you own the branch and no one else
works on it. For personal branches:

```bash
# Interactive rebase — reorder, squash, or edit commits
git rebase -i HEAD~5     # last 5 commits
git rebase -i main        # rebase onto main interactively

# Squash the last N commits into one
git reset --soft HEAD~3   # uncommit last 3, keep changes staged
git commit -m "feat: combined feature work"

# Fixup — amend a past commit without a separate "fix" commit
git commit --fixup <sha>  # mark this as a fix for <sha>
git rebase -i --autosquash main  # automatically reorder+squash fixups

# Amend the last commit (not pushed yet)
git commit --amend --no-edit    # add staged changes to last commit
git commit --amend -m "better message"
```

## Code archaeology

Find when code was introduced, changed, or deleted:

```bash
# Who last touched each line?
git blame path/to/file -L 40,60    # blame lines 40-60

# When was this string added/removed? (-S = pickaxe)
git log -S "functionName" --oneline -- path/
git log -S "removed_feature" --patch       # show the diff that removed it

# When was a regex pattern changed? (-G = grep-based)
git log -G "TODO|FIXME" --oneline

# Find which commit deleted a file
git log --all --full-history -- "**/deleted-file.ts"

# Show a file at a specific point in time
git show <commit>:path/to/file.ts
git show <commit>~1:path/to/file.ts   # the version before that commit
```

## Recovering lost work

```bash
git reflog                        # history of all HEAD movements (30 days default)
git reflog show feature-branch    # reflog for a specific branch
git checkout HEAD@{2}             # jump back 2 HEAD moves ago
git branch recovered HEAD@{5}     # restore a "lost" commit as a new branch
```

`git reflog` captures everything — rebases, resets, checkouts, even deleted
branches (until garbage collection). It's the ultimate undo.

## Bisect: find the exact commit that broke things

```bash
git bisect start
git bisect bad HEAD               # current commit is broken
git bisect good v1.2.0            # last known-good tag
# Git checks out a midpoint; test it
git bisect good                   # this commit works
git bisect bad                    # this commit is broken
# ...repeat until the bad commit is isolated
git bisect reset                  # return to original HEAD
```

For automated bisect with a test script:

```bash
git bisect start HEAD v1.2.0
git bisect run npm test           # script exits 0 = good, 1-127 = bad
```

## Cherry-pick

```bash
git cherry-pick <sha>             # apply a single commit from another branch
git cherry-pick -x <sha>          # include "(cherry picked from <sha>)" in message
git cherry-pick <sha1>..<sha3>    # apply a range (exclusive of sha1)
```

## Stash work-in-progress

```bash
git stash push -m "WIP: half-done refactor"
git stash list
git stash pop                     # apply + drop top stash
git stash apply stash@{1}         # apply without dropping
git stash drop stash@{1}
```

## Worktrees: parallel branches without cloning

```bash
git worktree add ../feature-x feature-branch   # checkout a branch in a separate dir
git worktree list                               # list all worktrees
git worktree remove ../feature-x                # clean up when done
git worktree prune                              # remove stale entries
```

Use worktrees when you need to work on two branches simultaneously (e.g., a
hotfix while mid-feature) without stashing or cloning.

## Safety principles

- **Never force-push shared branches** — `git push --force-with-lease` is the
  safer alternative, but still confirm no one else is on the branch.
- **Check authorship before amending** — `git log -1 --format='%an %ae'` to see
  who wrote the last commit; don't amend commits authored by others.
- **Verify before destructive operations** — always run `git status` and
  `git log --oneline -5` before a rebase or reset to confirm you're on the
  right branch.
- **Reflog is your safety net** — if you mess up a rebase or reset, `git reflog`
  + `git checkout` can recover almost anything.
