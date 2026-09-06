# mac-sim: driving the host toolchain from the Linux container

## The constraint

Xcode, `xcrun`, and `simctl` are macOS-only. They cannot be installed in a Linux
guest, and no container configuration changes that. What the container *can* do
is reach the Mac: guest -> host TCP works.

So the fix is a channel, not an installation. `host/mac-sim-shim.py` runs on the
host and exposes a fixed allowlist of verbs over HTTP on `192.168.64.1:8890`.
The guest gets a `mac-sim` CLI that forwards to it. The host does the work;
results flow back through the bind-mounted workspace.

The same pattern works for anything host-only: Maestro, a device farm CLI, a
notarisation step. Declare it as a project command.

## Setup

```bash
cp config/mac-sim.example.json ~/.config/cc-container/mac-sim.json
$EDITOR ~/.config/cc-container/mac-sim.json
```

Read [SECURITY.md](../SECURITY.md) first, especially the part about why this file
must not live inside a project root.

Declare at minimum a project `name`, its `root`, and `defaults.device`. Add
`commands` for anything beyond the built-in simulator verbs.
`../examples/expo-monorepo.mac-sim.json` is a full worked example.

## Usage from inside the container

```
mac-sim list                              # available simulators + state
mac-sim boot                              # the project's default device
mac-sim install path/to/App.app
mac-sim launch                            # the project's default bundle id
mac-sim openurl "myapp://"
mac-sim screenshot /workspace/.mac-sim/x.png
mac-sim logs --last 2m --lines 300 [--predicate '...']
mac-sim build                             # xcodebuild the declared workspace
mac-sim terminate | uninstall | shutdown | gui
mac-sim -d "iPhone 16" boot               # target another device

mac-sim commands                          # what this project declares
mac-sim run                               # a declared project command
mac-sim run --profile e2e                 # ...in its "e2e" variant
mac-sim metro start --profile e2e         # `<cmd> start` is the command itself
mac-sim metro status                      # `<cmd> <sub>` -> the "metro-status" command
mac-sim maestro --tags functional         # a declared parameter, as a flag
mac-sim maestro .maestro/flows/x.yaml     # ...or its one path parameter, bare
mac-sim capture --help                    # the flags THIS command declares

mac-sim lock                              # which checkout holds the simulator
mac-sim release                           # hand it back
mac-sim run -f                            # take it over from another checkout
```

Screenshots must be written under `/workspace` so they land in the bind mount
and become readable in the container. That is how UI verification works: capture,
then read the PNG. Add `.mac-sim/` to the project's `.gitignore`.

Relative paths are absolutized guest-side before being sent, and `-d` is accepted
before or after the subcommand.

## Worktrees

Every command runs in **the checkout you call it from**, so a session started
with `ccx --worktree <name>` builds, tests and screenshots its own branch.

The CLI finds its checkout root by walking up to the nearest `.git` (a worktree's
is a file, not a directory) and sends it as `workdir`; the host resolves the
command's `cwd`, its `{root}` placeholder and every relative `path` param from
there. `-w <dir>` overrides it, `MAC_SIM_WORKDIR` sets a default. The path rules
do not loosen: a `workdir` is still resolved and then checked for containment in
the project root, and must itself be a checkout root, so neither an arbitrary
subdirectory nor anything outside the mount is accepted.

This works without extra configuration because a guest-created worktree lives at
`<root>/.claude/worktrees/<name>`, inside the bind mount and therefore inside the
allowlisted root.

**Write command paths relative, not absolute.** A `"default": "/workspace/.maestro"`
pins the command to the main checkout; `"default": ".maestro"` follows the caller.
`{root}` is the calling checkout; `{project_root}` is always the literal root.

### One simulator, many checkouts

Targeting the right worktree is necessary but not sufficient. The Mac has one
simulator, one installed copy of a bundle id, and one bundler port -- and a `run`
that declares `kill_port` frees that port before building. Two checkouts working
at once do not just race: the second replaces the first's build, and both sessions
then verify something neither of them wrote.

So a checkout **claims** the simulator: a non-blocking `flock` for the duration of
a command, plus a sticky owner record honoured for `lock.ttl` seconds afterwards
(default 4 h, `{"lock": {"enabled": true, "ttl": 14400}}`). A command from another
checkout is answered 409 with who holds it and why, not silently run. Switching is
legitimate, so this refuses rather than forbids: `-f` takes it over, `mac-sim
release` hands it back, `mac-sim lock` reports it.

Read-only verbs never claim: `list`, `booted`, `runtimes`, `screenshot`, `logs`.
A project command claims by default; declare `"exclusive": false` for one that
touches neither the simulator nor a shared port.

**A worktree's first native build is slow.** `apps/mobile/ios/` and friends are
gitignored, so a fresh checkout has no native project and `expo run:ios` prebuilds
and runs `pod install` from scratch. That is correct, just not quick.

## The command surface is generated, not hardcoded

The guest CLI fetches `/health` before parsing a project command and builds that
command's parser from the host's declarations. So a declared parameter is a real
flag (`--tags functional`, not only `--arg tags=functional`), `mac-sim <cmd>
--help` lists exactly what that command accepts, and an undeclared flag fails in
the guest with the declared names rather than on the Mac.

Two spellings exist because the project's docs and agent definitions were written
against them, and they read better: `<cmd> <sub>` resolves to the declared
`<cmd>-<sub>` (`metro status` -> `metro-status`) with `start` meaning the base
command, and a bare positional becomes the command's path parameter when it
declares exactly one (`maestro .maestro/flows/x.yaml`). `-V` and `--arg key=value`
still work.

## Declaring a project command

A command is an argv template plus its environment:

```json
"run": {
  "argv": ["xcodebuild", "-scheme", "{scheme}", "-destination", "id={udid}", "build"],
  "cwd": "ios",
  "timeout": 2400,
  "path_prepend": ["~/.gem/ruby/4.0.0/bin"],
  "env": { "SENTRY_DISABLE_AUTO_UPLOAD": "true" },
  "variants": { "release": { "env": { "CONFIGURATION": "Release" } } },
  "params": { "filter": { "pattern": "^[A-Za-z0-9._-]{0,64}$", "default": "" } },
  "optional_argv": [ { "when": "filter", "argv": ["--only", "{filter}"] } ]
}
```

- **Placeholders**: `{root}` (the calling checkout), `{project_root}` (always the
  literal root), `{udid}`, `{device}`, `{bundle_id}`, `{scheme}`, plus any name in
  `params`. A rendered value is always one argv element; it is never re-split and
  never reaches a shell.
- **`params`** are the only caller-supplied values, each validated by its own
  regex, or by `"path": true` (relative to the calling checkout, and still checked
  for containment in the project root). An argument the command did not declare is
  rejected, not ignored.
- **`cwd`** is relative to the calling checkout, so it follows a worktree.
- **`exclusive`** (default `true`) makes the command claim the simulator. Set it
  `false` for one that reads state without touching the device or a shared port.
- **`optional_argv`** appends only when a param is non-empty, so an unset
  optional flag leaves no stray empty argument.
- **`variants`** overlay the base spec: `argv` is replaced, `env` is merged.
  Use them for build profiles. `--profile` selects one, unless the command
  declares a parameter of that name.
- **`kill_port`** frees a TCP port first. This matters for bundlers whose env
  flags are inlined at build time: a stale server left from another profile
  silently serves the wrong bundle, and the failure looks like a selector bug
  rather than a wrong-build error.
- **`detach` + `ready_url` + `ready_match`** run a server that must outlive the
  request. It is started in its own session, logged to `log`, and the shim polls
  `ready_url` until `ready_match` appears before replying.

## Lifecycle: nothing to run by hand

`ccup` and `ccrun`/`ccsh` start the shim automatically **when the directory you
launch from is inside a declared project root**, and `ccdown` stops it (with the
proxy) once no containers remain. So the normal flow is just `ccup`.

Outside those roots the shim is deliberately not started: it could not act on
that project anyway, so running it would add host attack surface for nothing.
`CC_SIM_AUTO=0` disables auto-start; `cc-sim-up` starts it manually, `cc-sim-down`
stops it, `cc-sim-log` tails it, and `ccst` shows whether it is listening.

It cannot be started from inside the container: the guest has no way to spawn a
host process, so something host-side has to launch it first. Startup is ~0.3 s
(polled, not a fixed sleep) and it idles at ~24 MB.

## Teaching the agent it exists

An agent in the container reads the repo's `CLAUDE.md`, sees `xcodebuild`, finds
no `xcrun`, and correctly concludes it cannot build the app. It has no way to
discover `mac-sim` on its own. **Add a section to the project's `CLAUDE.md` /
`AGENTS.md`** listing the verbs, or the sandbox will keep declining that work:

```markdown
## If you are running in a Linux container (no `xcrun`)

If `uname -s` is `Linux`, you are in the container, not on the Mac. Xcode
cannot exist here. This does not mean you cannot run the app: a `mac-sim` CLI
forwards a scoped set of verbs to the macOS host.

    mac-sim commands                  # what is available
    mac-sim list | boot | run
    mac-sim screenshot .mac-sim/x.png # then read the PNG to see the UI

Screenshots must be written under the repo so they land in the bind mount.
Everything runs in the checkout you call from, worktrees included; `mac-sim lock`
says which checkout currently holds the simulator.
```

## Why the CLI is mounted as a directory

`mac-sim` ships in this repo's `guest/`, which is bind-mounted at `/opt/cc-tools`
and put on the guest `PATH` by the image. Nothing is baked in and there is no
launcher: `PATH` resolves the script at exec time, from the repo working tree, so
a `git pull` reaches a running session on its next command.

The mount is a **directory**, never a single file. A single-file bind mount pins
an inode, so editing the host file while a container runs left the guest reading
a stale or half-written copy: one session reported the CLI as "truncated
mid-statement" while the host file was intact. The same applies to the MCP config
directory.

If you add another guest-side tool of your own, either drop it in `guest/` or
mount your own directory at `/opt/cc-local`, which is on `PATH` too. `/opt/cc-tools`
comes first, so a file of the same name in `guest/` wins.
