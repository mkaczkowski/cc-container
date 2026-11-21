# Agent instructions

**Do not add a `CLAUDE.md`** anywhere in the repo (root, `.claude/` or a
subdirectory). Claude Code loads `AGENTS.md` only for a project with no
`CLAUDE.md` of its own, so adding one silently hides this file from it. CI
fails if one is tracked.

Read `CONTRIBUTING.md` (ground rules, local checks, scope) before changing
anything, and `SECURITY.md` before anything that touches the VM boundary.

## How the pieces fit

- `bin/cc-container.sh` runs on the Mac and is sourced from both bash and zsh,
  so it must parse in both.
- `guest/` is mounted read-only at `/opt/cc-tools` and is on the guest `PATH`:
  an edit there applies to the next session without a rebuild. A change to
  `image/Dockerfile` needs `cc-container-build`.
- Session startup happens in two places that must stay in step:
  `guest/cc-entry` (one-off sessions, as the container entrypoint) and
  `cc-attach` in `bin/cc-container.sh` (the shared session, through
  `container exec`). A new startup step usually belongs in both. Codex has the
  same pair, `guest/codex-entry` and `codex-attach`, but none of the Claude Code
  steps (MCP sync, subscription guard) apply to it.

## Claude Code

- Behaviour that must hold whatever the model does (auth, policy, config sync)
  goes in a guest script that runs before Claude Code starts. Never rely on a
  skill, prompt template or plugin alone: the model may not load a skill, and
  the user can remove a plugin. `guest/cc-subscription-guard` is the pattern.
- A capability lives in one place (a command on the guest `PATH`, the shared
  `mcp-servers.json`), with at most thin wiring for Claude Code. `mac-sim` and
  `cc-mcp-sync` are the pattern.
- Do not reimplement Claude Code's own features (worktrees, subagents); see
  Scope in `CONTRIBUTING.md`.

## Sync scripts

`cc-mcp-sync` merges declarative config from the Mac into Claude Code state
that the user also edits by hand. Keep its contract:

- Change only what the sidecar records as owned by the script; keep everything
  else.
- Never overwrite a file the user created. Fail with a message that names the
  file and how to fix it.
- A sync error fails the session start. A session that silently lacks its
  config is worse than none.

## Before finishing

- Run the local checks in `CONTRIBUTING.md`. A new guest script goes into those
  checks and into `.github/workflows/ci.yml`.
- CI cannot run the VM, so say in the PR what you ran by hand.
- Add an entry under `## Unreleased` in `CHANGELOG.md` for user-visible
  changes, and update `README.md` when a command or a config file changes.
- Anything new the wrapper reads or writes outside the repo (a config file, a
  state file, something kept in the `cc-home` volume) goes into the lists under
  Configuration in `README.md`, so a fresh setup knows it exists.
