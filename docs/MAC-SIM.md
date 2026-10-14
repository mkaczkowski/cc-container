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
mac-sim launch --locale pl                # relaunch in that language (pl, pl-PL)
mac-sim openurl "myapp://"
mac-sim appearance dark                   # light|dark; no arg prints the current one
mac-sim screenshot /workspace/.mac-sim/x.png
mac-sim logs --last 2m --lines 300        # the app's own process only
mac-sim logs --all | --predicate '...'    # the whole simulator log, or your filter
mac-sim status                            # device, claim, bundler, app; exit 0 = ready
mac-sim pull db                           # copy a file out of the app's data
mac-sim pull Documents/x.json out.json    # container: a declared alias, or a
                                          # path relative to the container
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

mac-sim lock                              # who holds the simulator, and what
                                           # is executing right now, if anything
mac-sim release                           # hand back the claim (does not stop
                                           # anything still running)
mac-sim reset                             # force-stop a stuck command and
                                           # actually free the lock; also
                                           # `mac-sim release -f`
mac-sim reset --pid 12345                 # only reset if that's still the pid
                                           # `mac-sim lock` showed you
mac-sim run -f                            # take it over from another checkout

mac-sim sim create mine                   # a private simulator for this checkout
mac-sim --sim mine metro start            # every command takes --sim
mac-sim sim list
mac-sim sim delete mine                   # also stops its bundler
```

`pull` writes under `.mac-sim/` by default, keeping the file's own name. A SQLite
database is copied through the SQLite backup API, not as a file: a live database
keeps its recent writes in a `-wal` sibling, so a plain copy of the main file is
stale or torn. Declare aliases per project as `"pull": { "db": "Documents/app.db" }`.

`launch --locale` terminates the app first, because launch arguments only reach a
fresh process.

A refused command exits `75` (`EX_TEMPFAIL`), not the shim's normal failure
exit code, so a script can tell "busy, try again" apart from "actually broken."

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

These are two different things, and only telling one apart from the other matters
when something goes wrong. The owner record is what `lock` reads and `release`
clears -- a courtesy note, not an enforcement mechanism. The `flock` is what
actually refuses a second command; it is held by the host thread running your
command for exactly as long as that command runs, and it does **not** care
whether the client that asked for it is still there. Killing a guest-side
wrapper (or losing the container) does not touch it: the host-side subprocess it
is waiting on keeps running, and the lock stays held until that subprocess exits
or its own timeout does. `mac-sim release` in that situation clears the owner
record and looks like it worked, but the flock is untouched, so the next command
is still refused -- now unable to even name who holds it, because the record
that would have said so is gone. **Use `mac-sim reset`** (or `release -f`)
instead: it stops the tracked subprocess so the thread holding the flock
finishes and releases it for real, and `mac-sim lock` reports live execution
state (verb, pid, elapsed time) precisely so you can tell these two states
apart before deciding which one to reach for.

Read-only verbs never claim: `list`, `booted`, `runtimes`, `screenshot`, `logs`,
`pull`, `status`, and `appearance` without an argument.
A project command claims by default; declare `"exclusive": false` for one that
touches neither the simulator nor a shared port.

### Private simulators

The claim serialises checkouts. A private simulator removes the queue for the
common case, a change that is JS only:

```
mac-sim sim create mine            # about a minute
mac-sim --sim mine metro start     # this checkout's bundler, on its own port
mac-sim --sim mine launch
mac-sim --sim mine screenshot .mac-sim/x.png
mac-sim sim delete mine
```

`sim create` makes a device of the same type and runtime as the project's default
one, named `<project>-<name>`, and then does what the project's `simulators`
block declares:

```json
"simulators": {
  "max": 3,
  "port_base": 8090,
  "clone_app": true,
  "copy_prefs": ["com.apple.launchservices.schemeapproval.plist"],
  "global_defaults": { "RCT_jsLocation": "localhost:{port}" }
}
```

- **`clone_app`** installs a copy of `defaults.bundle_id` from the shared
  simulator, so a worktree with no native project still gets a binary. The shared
  simulator must be booted with the app installed. It is the shared build: a
  native change still has to be built, on either device.
- **`port_base`** is where bundler ports are allocated from, one per simulator.
  Commands reach it as `{port}`; `defaults.port` is the shared simulator's.
- **`copy_prefs`** are files from the shared device's `Library/Preferences`,
  copied before the first boot. The example carries the approval that stops iOS
  asking "Open in ...?" on every deep link.
- **`global_defaults`** are written to the device's global defaults domain. The
  example points a React Native debug build at the private bundler; the global
  domain survives an app-state reset, the app's own domain does not.
- **`max`** caps how many exist. Each booted simulator costs a few GB of RAM.

`--sim <name>` (or `MAC_SIM_SIM`) then targets it on any command, built-in or
declared. A private simulator has its own claim, so it never contends with the
shared one or with another private one, and the claim still stops a second
checkout from driving or deleting it without `-f`.

Without the block there are no private simulators: the verbs are refused. Only a
simulator created here can be deleted here; the registry lives in
`~/.local/state/cc-container/mac-sim-sims/`.

For this to work a project's commands must not hardcode the device or the port.
Use `{udid}` and `{port}`, including in `kill_port`, `ready_url` and `log`, which
are rendered like `argv`. Pin a test runner to `{udid}` as well: with two
simulators booted, "the booted one" is no longer one device.

## The Android emulator

The Android emulator needs a hypervisor the guest does not have, so it runs on
the Mac as well, next to the SDK that builds for it. A project that declares an
`android` block gets the same verbs against an emulator there:

```json
"android": {
  "sdk": "/opt/homebrew/share/android-commandlinetools",
  "java_home": "/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home",
  "avd": "Pixel_8_API_35",
  "console_port": 5560,
  "port": 8097,
  "emulator_args": ["-no-window", "-no-audio", "-read-only", "-no-boot-anim"]
}
```

```
mac-sim android list                      # AVDs, and what adb has attached
mac-sim android boot                      # start the AVD headless; returns once booted
mac-sim android install path/to/app.apk
mac-sim android launch [--locale pl]      # a per-app locale, Android 13+
mac-sim android terminate | uninstall
mac-sim android openurl "myapp://"
mac-sim android appearance [light|dark]
mac-sim android screenshot .mac-sim/a.png
mac-sim android logs --lines 300          # logcat of the app's pid; --all for everything
mac-sim android status                    # emulator, claim, bundler, app; exit 0 = ready
mac-sim android lock | release | reset
mac-sim android shutdown
mac-sim android run --profile e2e         # any other word: the "android-run" command
```

- **One emulator per project, fixed by `console_port`.** Its adb serial is
  `emulator-<console_port>`, and every adb call names it, so another device on
  the Mac is never the one acted on. Pick a port other than 5554, which an
  emulator started by hand takes, and add `-read-only` to `emulator_args` if
  both may run the same AVD at once (it also means nothing installed survives a
  shutdown). The port must be even, in 5554..5682.
- **Its own claim**, `<project>@android`, with the same rules as the simulator's.
  An Android run never waits on an iOS one, or the other way around.
- **`package`** defaults to `defaults.bundle_id`; set it when the two differ.
  **`port`** is the bundler port the Android commands get as `{port}`; give it
  its own, so an iOS bundler and an Android one can both run.
- **`status`** reports whether the installed build is debuggable: a debug build
  is not ready without its bundler, a release build embeds its bundle.
- `logs` takes `--lines` only (logcat has no time window here), and filters by
  the app's pid, so the app must be running unless you pass `--all`.
- Not for Android: `--sim`, `pull`, `build`, `runtimes`, `gui`.

**Project commands** declare `"platform": "android"` to run there: they take the
Android claim, get `{serial}`, `{avd}`, `{package}`, `{android_home}` and the
Android `{port}`, and run with `ANDROID_HOME`, `JAVA_HOME` and the SDK's tools on
`PATH`. `ready_app` waits for the app's process on the emulator. Name them
`android-<word>` and the CLI reads `mac-sim android <word>` as that command, so
`mac-sim android run --profile e2e` is `android-run`. A word that is also a
built-in (`release`, `status`, ...) always means the built-in, so do not name a
command `android-release`.

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
  literal root), `{udid}`, `{device}`, `{bundle_id}`, `{scheme}`, `{port}` (the
  targeted simulator's bundler port), `{sim}` (its name, empty for the shared
  one), plus any name in `params`. A rendered value is always one argv element; it is never re-split and
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
- **`platform`** (`"ios"`, the default, or `"android"`) picks the device, the
  claim and the bundler port; see [The Android emulator](#the-android-emulator).
- **`private_tmp`** gives the command its own `TMPDIR`, one per project, command
  and variant, under `~/.local/state/cc-container/mac-sim-tmp/`. Use it for a
  bundler that caches under `TMPDIR` by file content and not by the env flags it
  inlines: sharing one cache, a release build of one variant can ship another's
  bundle.
- **`variants`** overlay the base spec: `argv` is replaced, `env` is merged.
  Use them for build profiles. `--profile` selects one, unless the command
  declares a parameter of that name.
- **`kill_port`** frees a TCP port first, killing its listener only (not the
  port's clients, which include the app on every simulator using it). This matters for bundlers whose env
  flags are inlined at build time: a stale server left from another profile
  silently serves the wrong bundle, and the failure looks like a selector bug
  rather than a wrong-build error.
- **`detach`** runs a server that must outlive the request: it is started in
  its own session and the shim replies once it is ready, leaving it running and
  releasing the claim. Ready means `ready_url` answers with `ready_match`, and,
  with **`ready_app`**, that the project's app has a live process on the device
  (the installed copy is stopped first, so a stale one cannot pass). Declare a
  build that goes on to serve the bundle (`expo run:ios`) this way: it never
  exits, so as a plain command it holds the request and the claim until its
  timeout. A detached command that exits, or is not ready within `timeout`
  (after which it is stopped), has failed.
- **`log`** overrides where the output goes. Leave it unset: the default is
  inside the checkout, where the guest can read it.

## Watching a command, and knowing when the app is ready

The output of a project command that claims the simulator, or is detached, is
written to `.mac-sim/logs/<command>.log` in the calling checkout (`<command>@<sim>.log` on a private simulator) as the host
produces it. The CLI prints it live, so a build that takes minutes shows which
phase it is in; `-q` prints only the end. From another shell, `mac-sim lock`
names the log of whatever is executing, and the file can be read directly. Add
`.mac-sim/` to the project's `.gitignore`.

`mac-sim status` answers "can I drive the app now?" in one call and exits 0
only when the answer is yes:

```
simulator: iPhone 17 Pro (ACAB998C-...) Booted
claim: simulator held by /workspace
  last command: run, 0m 12s ago
bundler: running on 8081, started by `run` (profile e2e) from /workspace, 0m 14s ago
app: com.example.app running
ready: yes
```

The bundler line is the only way to learn which build profile a running bundler
carries: its flags are inlined when it starts and it cannot report them. A
bundler started outside mac-sim is reported as such, with its profile unknown.

## Lifecycle: nothing to run by hand

`ccup` and `ccrun`/`pirun`/`ccsh` start the shim automatically **when the directory you
launch from is inside a declared project root**, and `ccdown` stops it (with the
proxy) once no containers remain. So the normal flow is just `ccup`.

Outside those roots the shim is deliberately not started: it could not act on
that project anyway, so running it would add host attack surface for nothing.
`CC_SIM_AUTO=0` disables auto-start; `cc-sim-up` starts it manually, `cc-sim-down`
stops it, `cc-sim-log` tails it, and `ccst` shows whether it is listening.

It cannot be started from inside the container: the guest has no way to spawn a
host process, so something host-side has to launch it first. Startup is ~0.3 s
(polled, not a fixed sleep) and it idles at ~24 MB.

Unlike `guest/mac-sim` -- which is bind-mounted and re-read on every invocation,
so a `git pull` reaches a running session on its next command -- `host/mac-sim-shim.py`
is a long-running process. An edit to it needs `cc-sim-down && cc-sim-up` (or
just `cc-sim-up` again) before it takes effect; the running process does not
notice a changed file on disk.

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
