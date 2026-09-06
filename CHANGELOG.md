# Changelog

## Unreleased

First public release.

- Claude Code in an `apple/container` VM: one-off (`ccrun`) and shared
  multi-session (`ccup` / `ccx`) modes, persistent `cc-home` volume,
  `--dangerously-skip-permissions` by default.
- All user-specific settings moved to `~/.config/cc-container/`; the repo ships
  only examples.
- Resource defaults derived from the host (half its cores and RAM) instead of
  being hardcoded.
- Host proxy is now conditional: `CC_PROXY=auto|on|off`, with `cc-doctor`
  running the egress probe once and recording the verdict, rather than starting
  a proxy on every invocation.
- `cc-doctor`: prerequisite and egress diagnostics.
- Host-side tooling is not bundled. `CC_EXTRA_RUN_ARGS`, `CC_PRE_RUN_HOOK` and
  `CC_POST_DOWN_HOOK`, plus a `/opt/cc-local` directory on the guest `PATH`, are
  the seams for wiring up your own; `SECURITY.md` covers how to do it safely.
- `cc-proxy-down` kills only the tinyproxy this tool started; logs and state
  moved out of `/tmp` into `~/.local/state/cc-container/`.
- Container lookups parse JSON instead of prefix-matching `container list`.
- `install.sh` / `install.sh --uninstall`.
- Optional `guest/statusline.sh`: a container-aware Claude Code statusline, off
  until you point Claude Code at it.
- Corrected several claims that turned out to be wrong when checked against a
  real setup: the install command (`brew install container`, a formula not a
  cask), the disk a long-lived session occupies, the cold-start figure, and the
  fact that `ccrun`/`ccsh` cannot run while a shared session holds the volume.
- Troubleshooting for the two failure modes a full disk actually produces: the
  build erroring on space, and buildkit wedging read-only afterwards.
- `cc-update`: pull the repo, then do the parts a pull cannot -- rebuild when
  image inputs changed, report new config settings, and say when a running
  session needs recycling.
- Optional `~/.config/cc-container/Dockerfile.local`, built on top of the repo
  image, so adding tools never means forking the Dockerfile.
- Documented `ccx --worktree` for isolating parallel sessions. It needs no
  configuration, because Claude Code places worktrees under the repo root, which
  is already the mount. The caveat is that worktrees record absolute paths, so
  one created in the guest reads as `prunable` on the host and one created on the
  host is refused in the guest; `git worktree lock` is the fix for the first.
- Documented `worktree.sparsePaths` for large repos, where a worktree's full
  checkout is written through the bind mount onto host disk. Includes how it
  composes with `.worktreeinclude`, which copies gitignored files in regardless
  of the sparse set.
- Measured where worktree bootstrap time goes: writing a dependency tree through
  the bind mount is metadata-bound, 94x slower than the container filesystem for
  the same 3,000 files, and hardlinking saves disk rather than time. Documented
  the store placement and the bootstrap hook that follow from it.
- Corrected that bootstrap hook to run asynchronously. A `SessionStart` hook blocks
  session initialisation, so installing from one directly presents as a hung
  session. The pattern is now `async` + `asyncRewake` for the worker, a
  millisecond-cheap synchronous hook emitting `additionalContext` so Claude does
  not build against an empty tree, and a marker file the statusline renders so the
  wait is visible to the user.
