# github-remote — is the project on GitHub?

Shared check for every `/wise-pr-*` skill. A PR skill only makes sense
when `origin` points at a GitHub repository. This fragment classifies
`origin` once and tells the skill whether to continue or stop.

Run it **first**, before any `gh pr ...` call and before any `--on`
dispatch (so no child is spawned for a no-op, and a dispatched child
runs the same check as its first step).

The engine applies the identical rules in
`engine/wise_engine/phases/remote.py`. Keep the two in sync.

## Procedure

Run from the repo working tree.

### 1. Read the origin URL

```bash
git remote get-url origin 2>/dev/null
```

Never echo this URL back to the user: it can embed credentials
(`https://user:token@host/...`). Print only the host.

- Command fails or prints nothing -> `REMOTE=none`.
- Otherwise extract the host (step 2) and classify (step 3).

### 2. Extract the host

Strip the URL down to its host with POSIX parameter expansion:

```sh
rest=${origin#*://}     # drop any scheme://
rest=${rest%%/*}        # cut the authority at the first /
rest=${rest##*@}        # drop any user[:token]@
host=${rest%%:*}        # drop any :port or scp-style :path
```

This handles the scp-like form too: `git@github.com:a/b.git` has no
`://`, so `%%/*` leaves `git@github.com:a`, `##*@` leaves `github.com:a`,
and `%%:*` leaves `github.com`. A local path (`/srv/r.git`) yields an
empty host.

### 3. Classify

- `host` is `github.com`, `www.github.com`, `ssh.github.com`, or ends
  with `.ghe.com` -> `REMOTE=github`.
- else `gh auth status --hostname "$host"` exits 0 (a GitHub Enterprise
  Server this user is logged into) -> `REMOTE=github`.
- empty host -> `REMOTE=other` (a local-path remote).
- any other host -> `REMOTE=other`.

An SSH host alias (a host with no dot, from an scp-style or `ssh://`
URL) resolves with `ssh -G <alias>` (its `hostname` line), but only if
the skill allows `ssh`. When it does not, treat the alias host through
the `gh auth status --hostname` rule above.

## Outcomes

- `REMOTE=github` -> continue with the skill.
- `REMOTE=none` / `REMOTE=other` -> print **exactly one** line from the
  table below and stop, successfully. Never ask a question; never call
  `gh pr`.

`<no 'origin' remote | origin is <host>>` below means: print
`no 'origin' remote` for `none`, or `origin is <host>` for `other`.

| Skill | Line |
|---|---|
| create | `No GitHub remote (<no 'origin' remote \| origin is <host>>): no PR opened. Commits stay on <branch>.` (for `other`, append ` Push with git and open the merge request on <host>.`) |
| reviewers | `No GitHub remote (<no 'origin' remote \| origin is <host>>): no PR to add reviewers to.` |
| request review | `No GitHub remote (<no 'origin' remote \| origin is <host>>): no PR to request a bot review on.` |
| watch | `No GitHub remote (<no 'origin' remote \| origin is <host>>): no PR or GitHub checks to watch.` |
