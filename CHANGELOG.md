# Changelog

## Unreleased

- Behind the proxy, Node's built-in `fetch`/`http`/`https` now reach the
  network: the container sets `NODE_USE_ENV_PROXY=1` (warning silenced), and
  `tinyproxy.conf` allows `CONNECT` to port 80, which Node uses for plain HTTP.
  Add `ConnectPort 80` to an existing `~/.config/cc-container/tinyproxy.conf`.
  `no_proxy`/`NO_PROXY` now also exempt the host bridge address.
- Declarative global pi config: `~/.config/cc-container/pi/` (`settings.json`,
  `keybindings.json`, `extensions/`, `prompts/`, `skills/`) is mounted at
  `/opt/cc-pi` and merged into the guest's `~/.pi/agent` by
  `guest/pi-config-sync` at every pi start. `pix -C <dir>` starts pi in a
  subdirectory of the project.
- The image ships the pi coding agent (`@earendil-works/pi-coding-agent`)
  beside Claude Code: `pix` attaches it to the shared session, `pirun` runs it
  one-off, `pi-container-upgrade` upgrades it. Its state lives in the same
  `cc-home` volume, and `mcp-servers.json` is synced into its `mcp.json` too.
  Rebuild the image (`cc-container-build`) to get it.
- `fd` (Debian's `fd-find`) is in the image.
- `CC_SUBSCRIPTION_ONLY=1`: only a Claude.ai subscription login is accepted.
  Claude Code enforces it from a read-only managed policy
  (`forceLoginMethod`, `allowedProviders`), which also covers
  pi-claude-bridge; `guest/cc-subscription-guard` refuses to start pi while it
  sees API keys, AWS credentials or a non-OAuth pi login.
- `cc-container-build` remembers the Claude Code and pi versions of the last
  build and reuses them for any it is not given, so upgrading one agent no
  longer drops the other's pin. `cc-doctor` reports both versions.
- A project command's output is written to `.mac-sim/logs/<command>.log` in the
  calling checkout while it runs, and the CLI prints it live (`-q` for the end
  only). Before, a long build was one silent request until the host finished.
- `ready_app`: a detached command is ready once the app has a live process on
  the device. Declare a build that then serves the bundle (`expo run:ios`) as
  `detach` + `ready_url` + `ready_app`: as a plain command it never exited, so
  it held the request and the claim (refusing `launch`, `openurl`, ...) until
  its 40 minute timeout. **Update your host config**; see
  `examples/expo-monorepo.mac-sim.json`.
- A detached command that exits before it is ready now fails at once with its
  output, and one that is not ready in time is stopped instead of left running.
- `mac-sim status`: device, claim, what is executing, the bundler and the build
  profile it was started with, and whether the app is running; exits 0 only
  when the app is ready to drive.
- `mac-sim logs` shows the app's own process by default; `--all` for the whole
  simulator log.
- `log` now defaults to the checkout rather than `/tmp` on the host.
- `mac-sim sim create|list|delete`: private simulators, so two checkouts verify
  at the same time. Each gets a copy of the app installed on the shared device,
  its own bundler port (`{port}`) and its own claim; `--sim <name>` targets one
  on any command. Opt-in per project through a `simulators` block.
- `mac-sim appearance [light|dark]`, `mac-sim launch --locale <lang>`, and
  `mac-sim pull <alias|path> [out]` (a file from the app's data container; a
  SQLite database arrives as one consistent snapshot).
- `kill_port`, `ready_url` and `log` are rendered like `argv`, and `kill_port`
  now kills the port's listener only, not its clients.
- Fixed: an option written before a built-in subcommand (`mac-sim -d "iPhone 16"
  boot`) was dropped, so the command ran against the default device.
- The CLI now says so when the running shim predates a verb or option, instead
  of the shim silently ignoring the field.

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
- `mac-sim`: an opt-in bridge that lets the guest drive the Mac's Xcode and
  simctl toolchain, which cannot exist in a Linux VM. A host listener
  (`host/mac-sim-shim.py`) exposes a fixed allowlist of verbs on the bridge
  address; `guest/mac-sim` forwards to it. Projects and their commands are argv
  templates declared in `~/.config/cc-container/mac-sim.json`: no shell string,
  no free-form argument, every path resolved inside a declared root. Inert until
  that file exists, so an install that does not want it gets no listener and no
  container env. `cc-sim-up` / `cc-sim-down` / `cc-sim-log`, auto-started by
  `ccup` and stopped by `ccdown`, and reported by `ccst`. See `docs/MAC-SIM.md`.
- Commands run in the checkout the guest calls from, so a `ccx --worktree`
  session builds and verifies its own branch instead of the main checkout. A
  checkout also claims the simulator while it uses it (one device, one bundle
  id, one bundler port are shared), and a command from another checkout is
  refused naming the holder rather than replacing its build.
- Other host-side tooling is still not bundled. `CC_EXTRA_RUN_ARGS`,
  `CC_PRE_RUN_HOOK` and `CC_POST_DOWN_HOOK`, plus a `/opt/cc-local` directory on
  the guest `PATH`, are the seams for wiring up your own; `SECURITY.md` covers
  how to do it safely.
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
  Warns that a directory only the package manager reads (pnpm `patches/`) must
  be in the sparse set too, or install fails with ENOENT on a tracked file.
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
- Statusline: show the mounted project, and a `deps installing...` indicator while a
  bootstrap marker exists. Every project is `/workspace` in the guest and a worktree
  shows its own name, so nothing on screen distinguished one repo from another after
  recycling the session elsewhere. The project name comes from the git remote and is
  shown only when it differs from the directory, so it stays quiet on the host.
- Documented that Node 22's native recursive `fs.cpSync`/`fs.rmSync` fail on the
  virtiofs bind mount (`EACCES`, then `ENOTEMPTY` over the debris they leave), which
  breaks any postinstall that rebuilds a directory and leaves it unable to repair
  itself. Covers the coreutils fallback preloaded via `NODE_OPTIONS`, why a green
  `pnpm install` is not evidence the tree is intact, and the side-effects cache that
  makes a "fresh worktree" test pass having run zero postinstalls.
- Statusline also reports `deps FAILED`, since a tree that is present but incomplete
  otherwise reads as healthy.
- Removed a duplicated copy of the worktree section that had drifted out of sync: it
  still carried the superseded advice to install synchronously from `SessionStart`.
