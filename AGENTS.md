# Agent instructions

Both Claude Code and pi read this file. Keep it agent-neutral: plain Markdown,
no `@imports` (pi does not resolve them), and name the agent whenever a rule
applies to only one of them.

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
- Session startup happens in four places that must stay in step:
  `guest/cc-entry` and `guest/pi-entry` (one-off sessions, as the container
  entrypoint) and `cc-attach` and `pi-attach` in `bin/cc-container.sh` (the
  shared session, through `container exec`). A new startup step usually belongs
  in more than one of them.

## Claude Code and pi

- Behaviour that must hold whatever the model does (auth, policy, config sync)
  goes in a guest script that runs before the agent starts. Never rely on a
  skill, prompt template or extension alone: the model may not load a skill,
  and the user can remove an extension. `guest/cc-subscription-guard` is the
  pattern.
- A capability both agents need lives in one agent-neutral place (a command on
  the guest `PATH`, the shared `mcp-servers.json`), with at most thin wiring for
  each agent. `mac-sim` and `cc-mcp-sync` are the pattern. Use the `pi-` prefix
  only for scripts that are pi-only.
- Do not reimplement one agent's features in the other (worktrees, subagents);
  see Scope in `CONTRIBUTING.md`. Users add pi extensions, skills, prompts and
  packages through `~/.config/cc-container/pi/`, not through this repo.

## Sync scripts

`cc-mcp-sync` and `pi-config-sync` merge declarative config from the Mac into
agent state that the user also edits by hand. Keep their contract:

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
