# cc-container

Run Claude Code inside a Linux VM on your Mac, using
[`apple/container`](https://github.com/apple/container).

```bash
cd ~/dev/my-project
ccrun                      # Claude Code, in a VM, mounting this directory
```

## Why

`--dangerously-skip-permissions` is the ergonomic way to use a coding agent and
the reckless way to use one — on your own machine. Put the agent in a VM and the
tradeoff changes: it can install packages, delete files, and run whatever it
likes, and the only host state it can touch is the directory you mounted.

Devcontainers get you most of that. What is different here:

- **`apple/container` gives each session its own real VM**, not a namespace
  beside every other container on a shared kernel — and no Docker Desktop and no
  licence. (There is still a lightweight system service, started with
  `container system start`; what you do not share is the kernel.)
- **Several agents, one container.** A shared session takes ~0.1 s to attach, so
  running four Claude Code instances across four terminals against one repo is
  the normal mode rather than a heavyweight one.
- **It stays out of your way.** Its own home volume, its own MCP list, its own
  login — nothing it does can corrupt the Claude Code you run on the host.

## Requirements

- Apple silicon, macOS 15+ (26 recommended)
- [`apple/container`](https://github.com/apple/container) 1.2.0 or newer
  (`brew install container`) — developed and tested against 1.2.0; the CLI is
  young and its flags move, so report breakage on newer releases
- `jq`, `python3` (both standard or one `brew install` away)

## Install

```bash
git clone https://github.com/<you>/cc-container.git
cd cc-container
./install.sh               # seeds ~/.config/cc-container, adds a line to your rc
exec $SHELL                # or open a new terminal

container system start
cc-container-build         # build the image (a few minutes)
cc-doctor                  # check prerequisites, probe guest network egress
```

First run only: `/login` inside the container. Credentials land in the `cc-home`
volume, so later runs start authenticated.

## Usage

| Command | Alias | What it does |
|---|---|---|
| `cc-up` | `ccup` | Start the shared container, mounting `$PWD` |
| `cc-attach` | `ccx` | Attach a Claude Code session (one per terminal) |
| `cc-attach-shell` | `ccxs` | Bash prompt in the shared container |
| `cc-status` | `ccst` | Runtime, proxy, shim, and container state |
| `cc-down` | `ccdown` | Stop and remove the shared container |
| `cc-container` | `ccrun` | One-off throwaway session (`--rm`) |
| `cc-shell` | `ccsh` | One-off throwaway bash shell |
| `cc-doctor` | — | Check every prerequisite; record the egress verdict |
| `cc-sim-up` | — | Start the Xcode/simctl bridge by hand (`ccup` does it for you) |
| `cc-sim-down` | — | Stop it |
| `cc-sim-log` | — | Its log (`-f` to follow) |
| `cc-update` | `ccupd` | Pull the repo and apply what the pull cannot |
| `cc-container-upgrade` | — | Upgrade Claude Code in the image |
| `cc-container-build` | — | Rebuild the image |

Every command forwards its arguments verbatim: `ccx`/`ccrun` to `claude`,
`ccxs`/`ccsh` to `bash`. Quoting and spaces are preserved.

```bash
ccx -c                               # resume that terminal's last conversation
ccx -p "summarize this repo"
ccx mcp list                         # subcommands work
ccxs -c 'npm test'                   # bash, not claude
```

### `--dangerously-skip-permissions` is the default

`ccx` and `ccrun` add it automatically. That is the point of the container: the
VM is the sandbox. It is suppressed where it would be wrong — when you pass it
yourself, for `--version`/`--help`, and for subcommands that take their own flags
(`config`, `mcp`, `update`, `doctor`, …). Shells never receive it.

```bash
CC_SKIP_PERMISSIONS=0 ccx      # one command
export CC_SKIP_PERMISSIONS=0   # whole shell
```

Read [SECURITY.md](SECURITY.md) for what the boundary does and does not cover.

## Several sessions at once

One shared container stays up; each terminal attaches its own Claude Code process.

```bash
cd ~/dev/my-project
ccup                          # once
ccx                           # terminal 1
ccx                           # terminal 2
ccdown                        # when finished
```

You cannot simply run `ccrun` twice: only one container can hold the `cc-home`
volume at a time, and a second parallel run fails with
`storage device attachment is invalid`. That constraint is why this mode exists.
Attaching takes ~0.1 s. A cold `ccrun` boots a VM first — about 1.2 s before
Claude Code itself starts, measured on an M-series Mac.

**Caveats.** Sessions share one filesystem and one `~/.claude`: conversations are
independent, but two agents editing the same file will overwrite each other
unless you isolate them in worktrees (below). Every session sees the directory
you ran `ccup` from, not your shell's cwd.
State accumulates until `ccdown`, which costs the clean-slate guarantee `--rm`
gives you — and it is not a small amount: a session left up through a day of
real work reclaimed 13 GB when torn down. Expect the container runtime's storage
to grow steadily, and `ccdown` when you are finished rather than leaving a
session up indefinitely.

**`ccrun` and `ccsh` do not work while a shared session is up**, for the same
reason: they want the `cc-home` volume the session is holding, and fail with
`storage device attachment is invalid`. Use `ccx` / `ccxs` instead, or `ccdown`
first.

Switching projects means recycling: `ccdown && cd <other> && ccup`.

### Worktrees isolate parallel sessions

`--worktree` puts the worktree under `<repo root>/.claude/worktrees/<name>`,
which is inside the mount, so it needs no extra volume and no configuration:

```bash
ccup                        # once, from the repo root
ccx --worktree feat-auth    # terminal 1
ccx --worktree bugfix-456   # terminal 2
```

Each session gets its own checkout on its own branch, and Claude Code blocks the
edits, `cd`s and `git -C` redirects that would reach back into the main checkout.
A subagent declaring `isolation: worktree` is covered the same way. This is what
removes the file-collision caveat above; the shared `~/.claude` and the shared
container stay shared.

Workspace trust is keyed on the guest path and every project mounts at
`/workspace`, so accepting it once covers every project you ever mount, and
`--worktree` never stops on the trust dialog in a new repo.

**The host and the guest disagree about the path.** A worktree created in the
guest records `/workspace/.claude/worktrees/<name>`, which does not exist on the
Mac: host git lists it as `prunable`, and `git worktree prune` then deletes its
metadata (the files survive, the git linkage does not). It fails in reverse too,
so a worktree created on the host is rejected in the guest with
`Refusing to use <path> as an isolation worktree`. Keep worktree work inside the
guest. To stop host-side git touching them:

```bash
git worktree lock .claude/worktrees/<name>
```

A locked worktree survives both an explicit `git worktree prune` and the expiry
sweep `git gc` runs, and lists as `locked` instead of `prunable`. The cost is
that Claude Code's own cleanup and `git worktree remove` will both refuse it
until you `git worktree unlock` it.

A worktree is a fresh checkout, which implies two more things. Add
`.claude/worktrees/` to the project's `.gitignore`, or every worktree shows up as
untracked in the main checkout. And none of your gitignored files are there, so
name the ones the build needs in a `.worktreeinclude` at the project root
(`.gitignore` syntax; only gitignored files are copied):

```text
.env
.env.local
```

**On a large repo, check out only what you need.** A worktree is a full
checkout, and here that write goes through the bind mount onto host disk, so a
big repo can spend tens of seconds on it. Limit it with `worktree.sparsePaths`
in the project's `.claude/settings.json`:

```json
{"worktree": {"sparsePaths": ["src", "packages/foo"]}}
```

That is a cone-mode sparse checkout: files at the repo root are always present,
the listed directories are materialised, everything else is left out. Check it
with `git sparse-checkout list` inside the worktree. It also holds down the
host-disk growth noted below.

`sparsePaths` and `.worktreeinclude` do not conflict, because they act on
different files. `sparsePaths` chooses which **tracked** directories get checked
out; `.worktreeinclude` copies its **gitignored** files in regardless, creating a
parent directory if the sparse set had excluded it. Such a directory then holds
only the copied file, not the tracked content that was excluded, and `git status`
stays clean because the copies are gitignored either way.

**Installing dependencies is where the time actually goes.** Creating the
worktree is cheap next to populating it. A dependency tree is tens of thousands
of small files, and writing them through the bind mount is bound by per-file
metadata round-trips, not by data. Creating 3,000 files from a warm cache
measured 1.9 s on the bind mount against 0.02 s on the container's own
filesystem, and switching from a copy to a hardlink clone barely moved it
(2.3 s to 1.9 s). Scaled to a real `node_modules` of ~105,000 files that is
roughly a minute, paid again for every worktree.

So hardlinking is a disk win, not a speed win, here. The two levers that do pay:

- **Check that the store is being linked from, not copied out of.** A store on a
  different filesystem from the checkout cannot be hardlinked from at all, though
  package managers differ in whether they notice: pnpm relocates its store onto
  the project's filesystem by itself, so placement is usually already right.
  Placement is not proof, though. The fallback to copying is silent, and it
  happens on the bind mount even with the store correctly placed. Confirm with
  link counts, where `1` means the file was copied:

  ```bash
  find node_modules -type f | head -100 | xargs stat -c %h | sort | uniq -c
  ```

  All `1` means force it, for pnpm with `package-import-method=hardlink`. Put
  that in the guest's `~/.npmrc`, which `cc-home` persists, and not in the
  project's tracked `.npmrc`: that file also reaches cloud builds, and unlike the
  default `auto`, `hardlink` has no fallback and fails where `auto` would copy.
  Expect it to reclaim duplicated gigabytes and, per the numbers above, very
  little time.
- **Move the install off the bind mount entirely**, by pointing the dependency
  directory at a path on the container filesystem. That is where the 94x lives.
  The trade is that the directory is then invisible to the Mac, so a host-side
  IDE loses code resolution, and it persists in `cc-home` rather than being
  reclaimed with the checkout. Worth it for a container-only workflow, not if you
  edit in a host IDE.

**Automate it, but never synchronously.** `SessionStart` hooks block session
initialization, so an install run directly from one presents as a hung session:
the prompt takes input and nothing happens until the install finishes. Register
the worker with `"async": true` so the session starts immediately, and
`"asyncRewake": true` so a failure (exit 2) wakes Claude with the error instead
of losing it. Pair it with a second, synchronous hook that only stats a
directory, costs milliseconds, and tells Claude not to build yet:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [
        { "type": "command",
          "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/bootstrap-notice.sh" },
        { "type": "command",
          "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/bootstrap-worktree.sh",
          "async": true, "asyncRewake": true }
      ] }
    ]
  }
}
```

Hooks talk to Claude, not to you: at exit 0 stdout becomes context Claude sees,
while stderr goes only to the debug log. So the notice hook emits
`additionalContext` to keep Claude from running a build against an empty tree:

```bash
[ -d "${cwd}/node_modules" ] && exit 0
jq -cn --arg d "${cwd}" '{hookSpecificOutput: {hookEventName: "SessionStart",
  additionalContext: ("Dependencies for " + $d + " are installing in the background; "
  + "do not run builds or tests until node_modules exists.")}}'
```

To tell *yourself*, use the statusline. The worker touches a marker file for the
duration, and [the statusline](#statusline) renders it, so the wait is visible
rather than looking like an idle session:

```bash
BOOT_MARKER="/tmp/cc-bootstrap/$(printf '%s' "$CWD" | sed 's|^/||; s|/|_|g')"
[[ -f "$BOOT_MARKER" ]] && row2_parts+=("${YLW}deps installing...${RST}")
```

The worker itself stays keyed to the lockfile hash, so it is a no-op once warm,
and adopts a pre-existing `node_modules` rather than reinstalling it:

```bash
stamp="${cwd}/node_modules/.bootstrapped"
[ -f "${stamp}" ] && [ "$(cat "${stamp}")" = "${sum}" ] && exit 0
# Both conditions matter: drop the stamp test and a lockfile change stops
# triggering an install forever.
if [ -d "${cwd}/node_modules" ] && [ ! -f "${stamp}" ]; then
  printf '%s' "${sum}" > "${stamp}"; exit 0
fi
trap 'rm -f "${marker}"' EXIT INT TERM
date > "${marker}"
( cd "${cwd}" && pnpm install --frozen-lockfile --prefer-offline ) >"${log}" 2>&1 \
  || { tail -20 "${log}" >&2; exit 2; }   # exit 2 + asyncRewake wakes Claude
printf '%s' "${sum}" > "${stamp}"
```

**Node's recursive `fs` calls are unreliable on the bind mount.** Worth knowing
before it costs you a day, because it presents as a package bug. On `/workspace`,
Node 22's native `fs.cpSync(src, dst, {recursive: true})` fails with `EACCES` on a
destination directory it created moments earlier. It is the mount, not permissions:
a two-file throwaway tree reproduces it, the same Node binary succeeds on the
container's own filesystem, and coreutils succeeds on the identical paths.

```
virtiofs (/workspace):   cpSync FAILED: EACCES
ext4 (/root):            cpSync OK
cp -R, same paths:       OK
```

Three things make it stick rather than read as a flake:

- **The failed copy poisons the tree.** It leaves an entry Node cannot handle, so a
  later `fs.rmSync({recursive: true})` over it dies with `ENOTEMPTY`.
- **So the package cannot repair itself.** Anything that clears its output directory
  before rebuilding it fails in the *cleanup*, before it ever reaches the copy.
- **The install still exits 0.** pnpm runs a postinstall only when it relinks that
  package, so on a tree with intact links and damaged output it reports success in
  seconds having repaired nothing. A green install is not evidence of a good tree.

The fix is a preload that falls back to coreutils when the native call throws, wired
in with `NODE_OPTIONS=--require` so it covers the package manager's own process and
every postinstall it spawns:

```js
fs.cpSync = function (src, dest, opts) {
  try { return nativeCpSync.call(fs, src, dest, opts); }
  catch (err) {
    if (!(err && err.code === 'EACCES' && opts && opts.recursive)) throw err;
    spawnSync('rm', ['-rf', dest]);   // only coreutils can clear the debris
    spawnSync('cp', ['-R', src, dest], { stdio: 'inherit' });
  }
};
```

Patch `fs.rmSync` the same way or the tree can never repair itself, and gate the
whole thing on `uname -s` = `Linux` so the host path is unchanged. Since the exit
code cannot be trusted, have the bootstrap hook verify a real build output before it
records success, and leave a marker when it does not, which the statusline reports as
`deps FAILED`.

Beware when testing this: a package manager that caches build output (pnpm's
side-effects cache) hardlinks a previous success into a new worktree without running
the script at all, so a "fresh worktree" check can pass having executed zero
postinstalls. Force the cold path before believing a fix.

Two smaller notes. New worktrees branch from `origin/HEAD`, so creating one
fetches; under `CC_PROXY=on` that goes through tinyproxy with a five-second cap
and falls back to the local `HEAD`. Set `worktree.baseRef` to `"head"` in the
guest's `~/.claude/settings.json` to branch from your current work and skip the
fetch. And `claude --help` advertises `--tmux`, which this image cannot satisfy:
tmux is not installed.

Worktrees also add to the disk figure above, and unlike container state they land
on **host** disk through the bind mount, so `ccdown` does not reclaim them.

## Configuration

Everything user-specific lives in `~/.config/cc-container/`; nothing in the repo
needs editing. `install.sh` seeds it from `config/*.example.*`.

| File | Purpose |
|---|---|
| `config.sh` | Image, resources, proxy mode, extra mounts, defaults |
| `mcp/mcp-servers.json` | Declarative MCP servers, synced into the guest's user scope |
| `mcp.env` | Secrets the above references as `${VAR}` (chmod 600, never inline) |
| `tinyproxy.conf` | Only used where guests have no direct egress |

Resources default to **half the host's cores and half its RAM** (floor 1 cpu /
4 GB). Override in `config.sh` or per invocation:

```bash
CC_SESSION_MEMORY=24g CC_SESSION_CPUS=10 ccup
```

Only `$PWD` is visible inside the VM. To expose more, add `CC_EXTRA_VOLUME_ARGS`
entries in `config.sh` — one `--volume` each, `:ro` unless the agent must write
back. Mounting a host path at the *same absolute path* it has on the Mac makes
tooling that refers to it absolutely work unchanged in the guest.

## MCP servers

The container keeps its own MCP list, separate from the host's.
`~/.config/cc-container/mcp/` is mounted read-only at `/opt/cc-mcp` and merged
into the guest's **user scope** at every session start, so `claude mcp list` and
`/mcp` behave exactly as after `claude mcp add --scope user`. A project's
`.mcp.json` still applies on top, and servers you add by hand in the guest are
never touched.

Add one by editing `mcp-servers.json`, putting any secret in `mcp.env` as
`KEY=VALUE` and referencing it as `${KEY}`, then restarting
(`ccdown && ccup`, or just `ccrun`). Install the server's package in
`image/Dockerfile` if its command is not already on the guest `PATH`.

Two deliberate deviations from the usual install snippets: packages are baked
into the image rather than fetched by `npx -y` at every server start, and the
list is merged into the user scope rather than passed as `--mcp-config` (that
flag is variadic, so it swallows a bare prompt positional, and `claude mcp list`
rejects it).

**Where the secrets end up.** `mcp.env` keeps keys out of a JSON file you might
commit or share, but the sync substitutes each `${VAR}` into the guest's
`~/.claude.json` so that `claude mcp list` and `/mcp` work — which means the
resolved key persists in the `cc-home` volume until you delete it. Treat that
volume as holding credentials, and note that `claude mcp list` prints keys in
full.

**Only stdio and API-key servers work.** Servers authenticating by OAuth loopback
cannot complete login: the redirect binds a port *inside* the container that your
host browser cannot reach. Add those on the host instead.

## Statusline

The container has its own `~/.claude`, in the `cc-home` volume, so its
statusline is configured separately from the one you use on the host — and by
default it has none. `guest/statusline.sh` ships as an optional one that makes
it obvious at a glance which sessions are containerised:

```
⬡ container · Opus 5 · ████░░░░░░ 42% 200k · 15m · my-repo:main
```

Nothing runs it until you point Claude Code at it. From the host, with a session
up:

```bash
ccxs -c 'f=$HOME/.claude/settings.json; [ -f "$f" ] || echo "{}" > "$f";
  jq ".statusLine = {type: \"command\", command: \"bash /opt/cc-tools/statusline.sh\"}" \
    "$f" > "$f.tmp" && mv "$f.tmp" "$f"'
```

It takes effect for sessions started after that; a session already running keeps
the statusline it started with.

The `⬡ container` badge keys off `IS_SANDBOX=1`, which the image sets, so the
same script is safe to reuse on the host — there, the badge simply does not
appear. It uses 16-colour ANSI only, so it reads correctly in any terminal theme,
and it always prints a line: a missing field or a failed `git` call degrades that
part rather than blanking the status.

To customise it, copy it to `~/.config/cc-container/local/` (mounted at
`/opt/cc-local`, also on the guest `PATH`) and point the command there instead.
Updates will never overwrite it in that location.

## Driving the Mac's Xcode toolchain from the guest

Xcode, `xcrun` and `simctl` are macOS-only. No container configuration changes
that, so an agent in the guest correctly concludes it cannot build an iOS app and
stops. The fix is a channel, not an installation: `host/mac-sim-shim.py` runs on
the Mac and exposes a **fixed allowlist** of verbs over HTTP on the bridge
address, and `guest/mac-sim` forwards to it. The host does the work; results come
back through the bind-mounted workspace, so a screenshot written under
`/workspace` is readable in the container.

```bash
cp config/mac-sim.example.json ~/.config/cc-container/mac-sim.json
$EDITOR ~/.config/cc-container/mac-sim.json   # declare a project root
```

That file is the whole opt-in. **Without it there is no listener, no `MAC_SIM_*`
env in the container and nothing extra running on the Mac**, so an install that
ignores this section is unaffected. With it, `ccup` starts the shim whenever you
launch from inside a declared project root, and `ccdown` stops it.

```bash
mac-sim list                       # simulators, from inside the container
mac-sim boot
mac-sim run --profile e2e          # a project command you declared
mac-sim screenshot .mac-sim/x.png  # then read the PNG to see the UI
```

Beyond the built-in simctl verbs, you declare your own commands as argv
templates in that JSON: a build, a dev server with a readiness probe, an E2E
runner. Nothing is a shell string and nothing takes free-form arguments, so the
guest can only ask for combinations you wrote down. Commands run in **the
checkout you call from**, git worktrees included, and a checkout claims the
simulator while it uses it, so two sessions cannot silently overwrite each
other's build.

Read [docs/MAC-SIM.md](docs/MAC-SIM.md) for the config format and
[SECURITY.md](SECURITY.md) for what this widens: it is a host escape hatch by
design, which is why it is off until you write that file.

## Extending it with your own host tooling

Some tools only exist on macOS and never will in a Linux guest. `mac-sim` above
is the one this repo ships, because an Xcode bridge is the case nearly every
Apple-silicon user hits. For anything else, the wrapper gives you three seams in
`config.sh` rather than bundling more:

| Setting | What it does |
|---|---|
| `CC_EXTRA_RUN_ARGS` | Extra `container run` arguments (`--env`, `--volume`, …) |
| `CC_PRE_RUN_HOOK` | A function run on the host before a container starts |
| `CC_POST_DOWN_HOOK` | A function run by `cc-down` once no containers remain |

Guest-side helpers go in a directory you mount at `/opt/cc-local`, which is on
the guest `PATH`. Mount it as a **directory**, never as a single file: an
editor's atomic save replaces the inode, and the guest then reads a stale copy.

```bash
CC_EXTRA_VOLUME_ARGS=( --volume "$HOME/my-guest-tools:/opt/cc-local:ro" )
CC_EXTRA_RUN_ARGS=( --env "MY_HELPER=http://192.168.64.1:9000" )
CC_PRE_RUN_HOOK=my_helper_up
```

Guest → host TCP works (that is also how the optional proxy works), so a small
host-side listener on the bridge address is the general shape of an escape hatch
for host-only tooling. If you build one, read [SECURITY.md](SECURITY.md) first:
anything the guest can ask the host to run is outside the VM boundary.

## Staying up to date

The shell library, the guest tools and the Dockerfile are all read from the repo
working tree at run time, so `git pull` is most of the update — a new shell picks
up new behaviour, and guest tools are live on the next session. Two things a pull
cannot do for you: rebuild the image when the Dockerfile changed, and set a
config key that did not exist before.

```bash
cc-update          # pull, then report and apply exactly what is needed
```

It refuses to run with uncommitted changes in the repo, lists the commits and
files that moved, rebuilds the image if anything under `image/` changed, names
any new config setting your `config.sh` does not mention, and tells you when a
running session needs recycling.

## Adding tools to the image

Do not fork the Dockerfile. If `~/.config/cc-container/Dockerfile.local` exists,
`cc-container-build` builds the repo image as `claude-code:base` and yours on top
of it:

```dockerfile
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

# MCP server packages, so a stdio server does not resolve `npx -y` per start
RUN npm install -g @upstash/context7-mcp@4

# ...or another runtime, a headless browser, your own CLI
```

Its build context is the config directory, so it can `COPY` files from there.
This survives `cc-update` untouched: the repo never sees it.

The cost is that you now store two images, `<name>:base` and `<name>:local`.
Delete the base with `container image delete claude-code:base` if you need the
space; the next `cc-container-build` recreates it.

## Upgrading Claude Code

```bash
cc-container-upgrade            # latest published version
cc-container-upgrade 2.1.225    # a specific one
cc-shell -c 'claude --version'  # confirm
```

A plain `cc-container-build` does **not** upgrade: the `npm install` sits in a
cached layer and replays the old version. `cc-container-upgrade` resolves the
latest version first and passes it as `--build-arg CC_VERSION=<v>`, which changes
that layer's cache key and forces a real reinstall while leaving the apt layers
cached (seconds, not a full rebuild).

Do not upgrade in place inside a running container. `npm install -g` in a
`cc-shell` works, but `--rm` throws the container away and the image still holds
the old version. Claude Code's own `claude update` is pointless here for the same
reason. **The image is the source of truth.**

## Design notes

**The whole home is one named volume (`cc-home:/root`), not just `~/.claude`.**
Claude Code splits its state: credentials and history live under `~/.claude/`,
but onboarding, theme, and folder-trust live in the separate `~/.claude.json`.
Mounting only `~/.claude` re-runs onboarding on every `--rm` run. Persisting all
of `/root` also keeps `gh auth`, `.gitconfig`, and package caches, and one shared
home across projects means one login total.

**It runs as root with `IS_SANDBOX=1`.** apple/container surfaces bind-mounted
host files as `root:root` with no uid mapping, so a non-root user may not be able
to write `/workspace`. Files the container creates still land on the host owned
by you.

**Config directories are mounted as directories, never as single files.** An
editor's atomic save replaces the host file's inode; a single-file bind mount
does not follow that, and the guest stops seeing the file at all — silently.

## Limitations

- **No Xcode, ever.** It cannot be installed in a Linux guest. Drive the Mac's
  copy instead, through the opt-in bridge described in
  [Driving the Mac's Xcode toolchain from the guest](#driving-the-macs-xcode-toolchain-from-the-guest).
- **No browser.** The Playwright and Chrome DevTools skills need one. Add
  `npx playwright install --with-deps chromium` to the Dockerfile for headless
  runs (large layer).
- **No enterprise policy or telemetry.** The host's managed-settings file is not
  mounted. To opt in, bind-mount it read-only at
  `/etc/claude-code/managed-settings.json` — never `COPY` it into the image, as
  it can contain a live bearer token.
- **Only the mounted directory is visible.** Cross-repo work needs an explicit
  extra mount, and a running `ccup` session only picks up a newly added one after
  `ccdown && ccup`.
- **Git worktrees are guest-only.** They record absolute paths, and the same
  checkout is `/workspace` in the guest and a `~/...` path on the Mac, so a
  worktree works on exactly one side of that line. See
  [Worktrees isolate parallel sessions](#worktrees-isolate-parallel-sessions).
- **Slower startup.** Each `ccrun` boots a VM (~1.2 s) before Claude Code
  starts. `ccx` against a running session is ~0.1 s.
- **Some hosts have no guest network egress at all** (endpoint-security packet
  filters). `cc-doctor` detects it and enables a host proxy;
  see [docs/NETWORK.md](docs/NETWORK.md).

## Troubleshooting

```bash
cc-doctor                                # start here
container system status                  # runtime up?
container image list | grep claude       # image present?
container system logs | tail -30         # runtime errors
ccxs                                     # poke around inside a running session
cc-shell                                 # ...or a throwaway one, if none is up
tail -f ~/.local/state/cc-container/*.log
```

**A build fails with `no space left on device`.** The runtime's storage grows
with every image and every container's scratch. `ccdown` reclaims the running
session's share, which is usually the largest single piece. Deleting the base
image (above) is the next lever.

**A build fails with `read-only file system` or `input/output error`, and keeps
failing after you free space.** The buildkit helper container wedges when it
runs out of disk mid-build and does not recover on its own:

```bash
container system stop && container system start
container delete buildkit                # recreated on the next build
```

**A tool in `/opt/cc-local` reports `command not found`.** Check whether the
directory is mounted at all (`ccxs -c 'ls /opt/cc-local'`) before suspecting
`PATH`: an entry missing from `CC_EXTRA_VOLUME_ARGS` looks identical, from
inside, to a `PATH` problem. Note that a mount added to `config.sh` only reaches
a running session after `ccdown && ccup`.

**A worktree is `prunable` on the host, or Claude Code refuses one with
`Refusing to use <path> as an isolation worktree`.** Both are the same cause: the
worktree was created on the other side of the mount, and its recorded absolute
path does not resolve there. Create worktrees in the guest with
`ccx --worktree <name>` and `git worktree lock` them; a host-created worktree has
to be recreated, its files are left in place to salvage first.

**A command in a worktree session is refused as `too complex to verify`.**
Claude Code parses every Bash command to prove its git operations stay inside the
session's worktree, and fails closed on anything it cannot reduce to a definite
argument list: a loop body, a `$(...)` substitution, an `eval`. `gh` is checked
too, because it shells out to git and `-R` can retarget another repo, so a
read-only `for r in ...; do gh run view $r; done` is refused despite being
harmless. Split it into one flat command per iteration, and move any awkwardly
quoted argument (a `--jq` template, say) out into a separate `jq` call. The check
is per-command, so nothing in `config.sh` or the image turns it off; only a
session started without `--worktree` avoids it.

## Licence

MIT. See [LICENSE](LICENSE).
