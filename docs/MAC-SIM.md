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
mac-sim browse https://github.com/o/r/pull/1  # open in the Mac's browser (allowlisted https only)
mac-sim appearance dark                   # light|dark; no arg prints the current one
mac-sim screenshot .mac-sim/x.png
mac-sim logs --last 2m --lines 300        # the app's own process only
mac-sim logs --all | --predicate '...'    # the whole simulator log, or your filter
mac-sim status                            # device, claim, bundler, app; exit 0 = ready
mac-sim pull db                           # copy a file out of the app's data
mac-sim pull Documents/x.json out.json    # container: a declared alias, or a
                                          # path relative to the container
mac-sim build                             # xcodebuild the declared workspace
mac-sim terminate | uninstall | shutdown | gui
mac-sim -d "iPhone 16" boot               # target another device

mac-sim commands                          # what this project declares (or: projects)
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

Screenshots must be written under the project so they land in the bind mount
and become readable in the container. That is how UI verification works: capture,
then read the PNG. Add `.mac-sim/` to the project's `.gitignore`.

Relative paths are absolutized guest-side before being sent, and `-d` is accepted
before or after the subcommand.

### Opening a link in the Mac's browser

`mac-sim browse <url>` runs `open <url>` on the Mac, for a tool in the container
that shows links (the train dashboard opening a PR or Notion page). That browser
is outside the container's network policy and carries your sign-ins, so the shim
opens only https URLs under a prefix the host config allows, with no credentials
or port in them:

```json
"browse": { "allow": ["https://github.com/", "https://app.notion.com/"] }
```

The default (no `browse` block) is GitHub and Notion; `[]` turns the verb off.
Narrow a prefix to your own account (`https://github.com/<you>/`) to keep a guest
agent from opening anything else there. Any page under an allowed prefix can
still be opened, with whatever query string the guest puts in the URL.

### Forwarding a console port to the container

A project command can keep a port open on the Mac for a service in the
container. `console-forward` is the example: it listens on
`127.0.0.1:<console_port>` on the Mac and relays to the train console at
`<guest>`. Use it when the Mac cannot reach the container's subnet directly,
for example when another network tool captures that subnet.

`console-forward` is a plain command. It starts the relay detached and exits once
the port answers. The relay manages its own lifetime, so the command's `timeout`
only bounds the start. Both params are validated, so only the console's address
and a port in the range can be used:

```json
"console-forward": {
  "argv": ["node", "tools/train/consoleForward.mjs", "--guest", "{guest}", "--port", "{console_port}"],
  "params": {
    "guest": { "pattern": "^192\\.168\\.64\\.([2-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-4])$" },
    "console_port": { "pattern": "^43[2-4][0-9]$", "default": "4320" }
  }
}
```

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

**Write command paths relative, not absolute.** A `"default": "<root>/.maestro"`
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
`pull`, `status`, and `appearance` without an argument. Neither does `browse`,
which touches the Mac's browser, not the simulator.
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
  "info_plist": { "RCTMetroPort": "{port}" },
  "global_defaults": { "RCT_jsLocation": "localhost:{port}" }
}
```

- **`clone_app`** installs a copy of `defaults.bundle_id` from the shared
  simulator, so a worktree with no native project still gets a binary. The shared
  simulator must be booted with the app installed. It is the shared build: a
  native change still has to be built, on either device. A just-booted iOS 26
  simulator refuses its first install for a few seconds ("Failed to create
  promise"), so that refusal is retried for up to 25 seconds; any other install
  error fails the create at once.
- **`port_base`** is where bundler ports are allocated from, one per simulator.
  Commands reach it as `{port}`; `defaults.port` is the shared simulator's.
- **`copy_prefs`** are files from the shared device's `Library/Preferences`,
  copied before the first boot. The example carries the approval that stops iOS
  asking "Open in ...?" on every deep link.
- **`info_plist`** keys are set in the private copy's `Info.plist` (`{port}`
  and `{sim}` are filled in), which is then ad-hoc re-signed; the shared
  simulator's app is untouched. The bundle survives an app-state reset, so the
  value holds on every launch. The example points an Expo debug build at the
  private bundler: from SDK 57 it rewrites its bundler address from
  `RCTMetroPort` at every start, so the copy otherwise loads from 8081.
- **`global_defaults`** are written to the device's global defaults domain. The
  example points a React Native debug build that does not rewrite its address
  at start at the private bundler; the global domain survives an app-state
  reset, the app's own domain does not.
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

## The Apple Watch app

A project whose iOS app embeds a watch target declares a `watch` block, and
`mac-sim` then builds that target and drives it on a watch simulator paired with
the iPhone one:

```
mac-sim sim create mine --with-watch   # a private iPhone, plus a watch paired with it
mac-sim --sim mine watch build         # xcodebuild by target, for the watch simulator
mac-sim --sim mine watch install       # the build from this checkout
mac-sim --sim mine watch launch -- -MyFixture home
mac-sim --sim mine watch screenshot .mac-sim/watch.png
mac-sim --sim mine watch logs --lines 300
mac-sim --sim mine watch terminate
```

`watch boot | install [app.app] | launch [--bundle-id ID] [-- ARG...] | terminate
| screenshot [out.png] | logs` act on the watch paired with the iPhone simulator
the call targets (`--sim`, or the shared one). They share that simulator's claim,
so a watch is never driven by a checkout that does not hold its phone. There is
no `appearance` or privacy verb for the watch. `launch` restarts the app, because
launch arguments only reach a fresh process; each argument is a plain token
(letters, digits and `._:=/-`), at most 16 of them.

```json
"watch": {
  "device": "Apple Watch Series 11 (46mm)",
  "project": "apps/mobile/ios/MyApp.xcodeproj",
  "target": "MyAppWatch",
  "bundle_id": "com.example.myapp.watchkitapp",
  "stamp": "apps/mobile/ios/.myapp-watch",
  "rebuild": { "command": "prebuild", "variant": "clean" }
}
```

- **`device`** is the watch model `sim create --with-watch` creates (a name from
  `xcrun simctl list devicetypes`). It is created on the newest installed watchOS
  runtime that supports it; `runtime` pins one (an identifier, a version such as
  `26.0`, or a name). The pair is booted before `sim create` returns, and
  `sim delete` deletes both.
- **`project`**, **`target`**, **`configuration`** (`Debug` by default) and
  **`product`** (the `.app` name, the target's by default) describe the build:
  `xcodebuild -project <project> -target <target> -sdk watchsimulator`, with no
  scheme and no pods. Products land in `.mac-sim-build/watch/` of the checkout.
- **`bundle_id`** is what `launch`, `terminate` and `logs` act on.
- Without a paired watch, the watch verbs say so and how to make one. The shared
  simulator has one only if you paired it by hand (`xcrun simctl pair`).

### `--watch` on project commands

Whether the generated native project contains the watch target is decided when
it is generated, so a command that generates or builds it takes `--watch`, and
combines it with any profile. A command opts in with a `watch` block, which is
overlaid on the command like a variant (its `env` is merged, other keys replace);
a command without one (a bundler, a test runner) refuses the flag.

```json
"prebuild": {
  "argv": ["pnpm", "--filter", "@myapp/mobile", "exec", "expo", "prebuild", "--platform", "ios"],
  "cwd": "apps/mobile",
  "watch_stamp": "write",
  "watch": { "env": { "MYAPP_WATCH": "1" } },
  "variants": { "clean": { "argv": ["pnpm", "--filter", "@myapp/mobile", "exec", "expo",
                                    "prebuild", "--platform", "ios", "--clean"] } }
},
"run": {
  "argv": ["..."],
  "watch_stamp": "check",
  "watch": { "env": { "MYAPP_WATCH": "1" } }
}
```

The project's `stamp` file records which way the native project was generated:
one word, `watch` or `plain`; a missing file means `plain`. A command with
`"watch_stamp": "write"` writes it after it succeeds. One with `"check"` compares
it with the flag first, in either direction, and on a mismatch runs the
project's `rebuild` command (with `--watch` or without, and the named variant),
then goes on; `rebuild.command` must itself be a `write` command. So toggling
`--watch` on and off on one checkout regenerates the project each time it
changes and never otherwise. Without a `rebuild` the mismatch is an error that
says how to regenerate by hand. The stamp is a hint kept in the checkout, which
the guest can edit: at worst it makes a build stale, never run something else.

Run natively on the Mac, set the same environment variable yourself and keep the
stamp in step: `MYAPP_WATCH=1 pnpm exec expo prebuild --clean`, then
`echo watch > apps/mobile/ios/.myapp-watch` (`echo plain >` without it).

## The Android emulator

The Android emulator needs a hypervisor the guest does not have, so it runs on
the Mac as well, next to the SDK that builds for it. A project that declares an
`android` block gets the same verbs against one or more emulators there:

```json
"android": {
  "sdk": "/opt/homebrew/share/android-commandlinetools",
  "java_home": "/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home",
  "port": 8097,
  "emulator_args": ["-no-window", "-no-audio", "-read-only", "-no-boot-anim"],
  "default": "phone",
  "devices": {
    "phone":  { "avd": "Pixel_8_API_35", "console_port": 5560 },
    "tablet": { "avd": "Pixel_Tablet_API_35", "console_port": 5572, "port": 8098 }
  }
}
```

A project with one emulator can put `avd` and `console_port` at the top level
instead of `devices`. The fields outside `devices` (`port`, `emulator_args`) are
the defaults each device can override.

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
mac-sim android -d tablet boot            # every verb and command takes -d <device>
```

- **Each emulator is fixed by its `console_port`.** Its adb serial is
  `emulator-<console_port>`, and every adb call names it, so another device on
  the Mac is never the one acted on. `-d <name>` (or `MAC_SIM_ANDROID_DEVICE`)
  picks one; without it, `default` (the first declared, unless set). Pick a port other than 5554, which an
  emulator started by hand takes, and add `-read-only` to `emulator_args` if
  both may run the same AVD at once (it also means nothing installed survives a
  shutdown). The port must be even, in 5554..5682.
- **Each has its own claim**: `<project>@android` for the default device,
  `<project>@android-<name>` for another, with the same rules as the
  simulator's. An Android run never waits on an iOS one, a phone run never on a
  tablet one. A command's log on a non-default device is
  `.mac-sim/logs/<command>@<name>.log`.
- **`package`** defaults to `defaults.bundle_id`; set it when the two differ.
  **`port`** is the bundler port the Android commands get as `{port}`; give it
  its own, so an iOS bundler and an Android one can both run, and give a second
  device its own too if both will run a dev build.
- **`status`** reports whether the installed build is debuggable: a debug build
  is not ready without its bundler, a release build embeds its bundle.
- `logs` takes `--lines` only (logcat has no time window here), and filters by
  the app's pid, so the app must be running unless you pass `--all`.
- Not for Android: `--sim`, `pull`, `build`, `runtimes`, `gui`.

**Project commands** declare `"platform": "android"` to run there: they take the
claim of the device they target, get `{serial}`, `{avd}`, `{package}`,
`{android_home}`, `{android_device}` (its name) and its `{port}`, and run with `ANDROID_HOME`, `JAVA_HOME` and the SDK's tools on
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
- **`settle`** (`{"match", "fail_match", "pass_match", "after"}`) ends a command
  that printed its final summary and then went quiet: once its log matches
  `match` and has not grown for `after` seconds (default 30) while the process is
  still alive, it is stopped, exiting 1 if the log matches `fail_match` or lacks
  `pass_match`, else 0. A settling command is also stopped when the guest
  disconnects (a killed `mac-sim` call), so it does not go on driving the device.
  Declare it on a logged, non-detached test runner (Maestro has hung after its
  summary on a log-store exception), never on a build, which may outlive the
  guest's read timeout. A run with no end marker (Maestro prints none after a
  passing single flow) never settles and is ended by its `timeout` as before.
- Every logged command's log is also kept, when the command exits or is
  stopped, as `<command>[@<target>]-<UTC time>.log` beside it, the last 20 per
  command: the next run replaces the log itself. A detached command that became
  ready keeps running and writing, so its log is not copied.
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

Whenever any request has waited three seconds with no new output (under `-q`
too), a status line on stderr shows the time spent and how long the host has
been silent, for example
`| mac-sim run: waiting for the host, 4m12s, no new output for 35s`. It is
drawn only on a terminal, so a script or an agent that captures stderr sees
exactly what it did before.

`mac-sim status` answers "can I drive the app now?" in one call and exits 0
only when the answer is yes:

```
simulator: iPhone 17 Pro (ACAB998C-...) Booted
claim: simulator held by /Users/you/dev/my-app
  last command: run, 0m 12s ago
bundler: running on 8081, started by `run` (profile e2e) from /Users/you/dev/my-app, 0m 14s ago
app: com.example.app running
ready: yes
```

The bundler line is the only way to learn which build profile a running bundler
carries: its flags are inlined when it starts and it cannot report them. A
bundler started outside mac-sim is reported as such, with its profile unknown.

## JSON output for scripts

The text above is for people and agents. A tool that parses mac-sim's answers
passes `--json` instead and gets one JSON object on stdout:

```
mac-sim status --json            # also with --sim NAME
mac-sim lock --json              # release --json and reset --json too
mac-sim sim list --json
mac-sim projects --json          # same as commands --json
mac-sim android status --json    # also with -d NAME
mac-sim android list --json
mac-sim android lock --json      # release and reset too
```

Exit codes are the same as without `--json`: `status --json` exits 0 only when
`ready` is true, and a busy claim still exits 75. When the host step fails there
is no object: stdout stays empty and the error goes to stderr. `--json` is
written after the verb, and needs a shim that has it (`cc-sim-down && cc-sim-up`
after updating; an older one is refused with that hint, never answered in text).

Every object carries `"schemaVersion": 1`. A new field keeps the version; a field
renamed, removed or changed in meaning bumps it and is listed here. Check it,
and fail on a version you do not know. Paths are guest paths, as in the text
output. Times (`at`, `started`, `created`) are Unix seconds; `age` and `elapsed`
are whole seconds.

`status` (iOS):

```json
{
  "schemaVersion": 1,
  "platform": "ios",
  "project": "my-app",
  "lock": "my-app",
  "ready": false,
  "blockers": ["no bundler", "app not running"],
  "device": {"name": "iPhone 17 Pro", "udid": "ACAB998C-...", "state": "Booted",
             "booted": true, "sim": null},
  "claim": {"workdir": "/Users/you/dev/my-app", "verb": "run", "at": 1792495986.1,
            "age": 12, "expired": false},
  "executing": null,
  "bundler": {"port": 8081, "serving": false, "started_by_mac_sim": false,
              "command": null, "variant": null, "workdir": null, "started": null},
  "app": {"bundle_id": "com.example.app", "running": false}
}
```

- `device.sim` is the private simulator's name, or null on the shared one.
- `claim` is null when nobody holds it. `expired` means its `lock.ttl` has passed
  and another checkout may take it without `-f`.
- `executing` is null, or `{"verb", "pid", "workdir", "log", "elapsed"}` for the
  command running now (`pid` is null while it starts).
- `bundler` is null when the project declares no `port`. `variant` is null both
  for the default profile and when the bundler was not started through mac-sim;
  `started_by_mac_sim` tells them apart.
- `app` is null when the project declares no `bundle_id`.
- `blockers` holds the same reasons as the text's `ready: no (...)` line.

`android status` has the same top-level fields, with `"platform": "android"`,
`device` as `{"name", "avd", "serial", "state", "booted"}` (`state` is adb's, or
null when no emulator holds the port) and `app` as `{"package", "installed",
"running", "debuggable"}`, each null while the emulator is not booted.

`lock` answers `{"schemaVersion", "lock", "action": null, "claim", "executing"}`.
`release` and `reset` answer `{"schemaVersion", "lock", "action", "stopped"}`,
where `stopped` is the command a reset killed (`{"verb", "pid", "elapsed",
"workdir"}`) or null.

`sim list` answers `{"schemaVersion", "project", "max", "simulators": [...]}`,
one entry per private simulator, sorted by name: `{"name", "state", "udid",
"port", "watch_udid", "workdir", "created", "age", "claim"}`. `watch_udid` is the
paired watch from `sim create --with-watch`, or null. `state` is simctl's (`Booted`,
`Shutdown`, ...), `creating` while `sim create` runs, or `gone` when the
simulator was deleted outside mac-sim.

`android list` answers `{"schemaVersion", "project", "package", "default",
"emulators": [{"name", "avd", "serial", "port", "default"}], "avds": [...],
"attached": [{"serial", "state", ...}]}`. `avds` is every AVD on the Mac;
`attached` is `adb devices -l`, one object per device with its `key:value`
properties as fields.

`projects` (and `commands`) answers `{"schemaVersion", "checkout", "lock_ttl",
"projects": {...}}`. `checkout` is the checkout this call acts on (null outside
one); `projects` is the shim's `/health` description of each declared project:
its `root`, `owner`, `executing`, `defaults`, `pull` aliases, `simulators`,
`android` emulators, `watch` (`stamp`, `rebuild`, `bundle_id`, or null) and
`commands` (each with `platform`, `variants`, `params`, `watch` and
`watch_stamp`).

## Running mac-sim on the Mac

A tool that runs on the Mac rather than in a container (a task runner's driver,
an agent session started on the Mac) uses the same CLI. Sourcing
`bin/cc-container.sh` appends the repo's `host/bin` to `PATH`, and
`host/bin/mac-sim` there runs `guest/mac-sim` with:

- `MAC_SIM_SHIM` as `http://127.0.0.1:<listen.port>`, unless already set;
- `MAC_SIM_TOKEN` from `~/.local/state/cc-container/mac-sim.token`, the token
  every container gets, unless already set.

```bash
cd ~/dev/my-app            # a checkout under a declared root
mac-sim status
mac-sim sim list --json
```

Everything behaves as in a container: the checkout you call from is the one
acted on, and the claims are the same ones, so a Mac-side caller and a session
in a container queue on one simulator rather than overwriting each other's
build. Paths are the Mac's, which are also the guest's (projects are mounted at
their host path). A process started outside such a shell (launchd, an IDE run
configuration) needs `host/bin` on its `PATH`, or the full path to the script.

The shim must be running. Besides the bridge address (`listen.host`) it
listens on `127.0.0.1` on the same port, and the Mac-side CLI connects there:
a VPN that routes `192.168.64.0/24` into its tunnel catches the Mac's own
connections to the bridge address and they time out, while loopback is never
routed. Loopback callers still need the token; `listen.allow` does not apply
to them. The bridge address exists only while some container runs, so on a
Mac with no container up `cc-sim-up` starts a small probe container to hold
it; `cc-sim-down` stops both.

This adds nothing to what the guest can reach: the CLI only makes requests to
the shim, with a token that was already in a file on the Mac.

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
