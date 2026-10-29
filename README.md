# cc-container

Run Claude Code inside a Linux VM on your Mac, using
[`apple/container`](https://github.com/apple/container). The
[pi coding agent](#running-pi-beside-claude-code) runs in the same VM too.

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

First run only: `/login` inside the container, once per agent (`ccrun` for
Claude Code, `pirun` for pi). Credentials land in the `cc-home` volume, so later
runs start authenticated. Optional features (pi config, the Xcode bridge, your
own image layer) need files `install.sh` does not create; see
[Configuration](#configuration).

## Usage

| Command | Alias | What it does |
|---|---|---|
| `cc-up` | `ccup` | Start the shared container, mounting `$PWD` |
| `cc-attach` | `ccx` | Attach a Claude Code session (one per terminal) |
| `pi-attach` | `pix` | Attach a [pi](#running-pi-beside-claude-code) session instead |
| `cc-attach-shell` | `ccxs` | Bash prompt in the shared container |
| `cc-status` | `ccst` | Runtime, proxy, shim, and container state |
| `cc-reap` | | End attached sessions whose terminal on the Mac is gone (`ccx`, `pix` and `ccup` run it too) |
| `cc-exec` | | Run one command (argv, not a shell string) in the shared container, from a script |
| `cc-profile` | | Run a verb against a [profile](#profiles-a-second-isolated-container): `cc-profile <name> up` |
| `cc-down` | `ccdown` | Stop and remove the shared container, then trim its home volume |
| `cc-trim` | — | Give a home volume's freed space back to the Mac (`cc-trim [volume]`) |
| `cc-container` | `ccrun` | One-off throwaway session (`--rm`) |
| `pi-container` | `pirun` | One-off throwaway pi session (`--rm`) |
| `cc-shell` | `ccsh` | One-off throwaway bash shell |
| `cc-doctor` | — | Check every prerequisite; record the egress verdict |
| `cc-sim-up` | — | Start the Xcode/simctl bridge by hand (`ccup` does it for you) |
| `cc-sim-down` | — | Stop it |
| `cc-sim-log` | — | Its log (`-f` to follow) |
| `cc-update` | `ccupd` | Pull the repo and apply what the pull cannot |
| `cc-container-upgrade` | — | Upgrade Claude Code in the image |
| `pi-container-upgrade` | | Upgrade pi in the image |
| `cc-container-build` | — | Rebuild the image |

Every command forwards its arguments verbatim: `ccx`/`ccrun` to `claude`,
`pix`/`pirun` to `pi`, `ccxs`/`ccsh` to `bash`. Quoting and spaces are preserved.

```bash
ccx -c                               # resume that terminal's last conversation
ccx -p "summarize this repo"
ccx mcp list                         # subcommands work
ccxs -c 'npm test'                   # bash, not claude
```

### The Mac stays awake while a session runs

Every session command (`ccx`, `pix`, `ccxs`, `ccrun`, `pirun`, `ccsh`) runs
under `caffeinate -i`, so idle sleep cannot suspend the VM in the middle of an
unattended agent run. The Mac sleeps as usual once the session ends. A closed
lid still sleeps the Mac unless it is on power with an external display.

The session ends with its terminal, though not at once: closing the Mac side
of `container exec` does not hang up the guest, so the agent keeps running
until the next `ccx`, `pix` or `ccup`, which ends every attached session whose
terminal is gone (`cc-reap` does it on demand). For a run you want to leave and come
back to, start `pix`/`ccx` inside tmux on the Mac and detach. For a long
command, the image has tmux too: `ccxs`, then
`tmux new -s <name> '<command>; exec bash'`, detach with `Ctrl-b d`, and
`tmux attach -t <name>` from another `ccxs` later. It lives in the shared
container, so it survives closing the terminal but not `ccdown`.

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
you ran `ccup` from, mounted at the same path it has on the Mac (`/workspace` is
a symlink to it). An attach starts in your shell's cwd when that is inside the
mount, and at its root otherwise.
State accumulates until `ccdown`, which costs the clean-slate guarantee `--rm`
gives you — and it is not a small amount: a session left up through a day of
real work reclaimed 13 GB when torn down. Expect the container runtime's storage
to grow steadily, and `ccdown` when you are finished rather than leaving a
session up indefinitely.

**`ccrun`, `pirun` and `ccsh` do not work while a shared session is up**, for
the same reason: they want the `cc-home` volume the session is holding, and fail
with `storage device attachment is invalid`. Use `ccx` / `pix` / `ccxs` instead,
or `ccdown` first.

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

Workspace trust is keyed on the path, and each project mounts at its own host
path, so trust is asked once per project.

**The host and the guest agree about the path.** git writes absolute, resolved
paths into worktree links, so a worktree only works where those paths exist.
The project is mounted at the same path in the guest as on the Mac, which makes
a worktree valid on both sides whichever side created it: host git never lists a
guest-made worktree as `prunable` (so `git worktree prune` and `git gc` leave it
alone), and a host-made one works in the guest. (Under the old `/workspace` mount each side's
worktrees were broken on the other; a worktree made then can be relinked with
`git worktree repair <path>` while its metadata still exists.)

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

**Include every directory that install reads, not just the source you edit.**
A tracked directory left out of the sparse set is not deleted, it is marked
skip-worktree, so `git status` stays clean while the file is missing. The usual
casualty is a root-level directory that only the package manager reads, such as
pnpm's `patches/` (`pnpm.patchedDependencies`) or an in-repo `.yarn/` release:
the SessionStart `pnpm install` then fails with `ENOENT` on a file that git
swears is tracked. Spot it with `git ls-files -v | grep '^S'` inside the
worktree; fix it with `git sparse-checkout add <dir>`.

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
fetch.

Worktrees also add to the disk figure above, and unlike container state they land
on **host** disk through the bind mount, so `ccdown` does not reclaim them.

## Running pi beside Claude Code

The image also ships the [pi coding agent](https://pi.dev)
(`@earendil-works/pi-coding-agent`), and every mode above works for it with a
`pi` name in place of `cc`:

```bash
ccup                 # the same shared container
pix                  # pi in this terminal
ccx                  # Claude Code in the next one, against the same checkout
pirun -p "summarize this repo"   # one-off, --rm
```

Both agents share one container and one `cc-home` volume, so pi's state (login,
settings, sessions) lives in `/root/.pi/agent` beside `~/.claude`, persists
across runs, and is entirely separate from the pi you run on the Mac. Its host
config, packages and skills are not mounted; declare the container's global pi
config in `~/.config/cc-container/pi/` instead (below).

- **No permission flag to add.** pi never asks before a tool call, so there is
  no counterpart of `--dangerously-skip-permissions`: the VM is its only
  boundary, as it is for Claude Code here.
- **Project trust is asked once.** pi asks before loading a project's `.pi/`
  extensions, settings or MCP servers, and saves the answer by path. Each
  project mounts at its own host path, so it asks once per project, as with
  Claude Code's trust dialog.
- **Logging in.** Run `/login` inside pi. The OAuth callback binds inside the
  container where your browser cannot reach it, so paste the final redirect URL
  back into pi when it asks. Credentials land in `/root/.pi/agent/auth.json` in
  `cc-home`. For an API key instead, pass it in `config.sh`:
  `CC_EXTRA_RUN_ARGS=( --env "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" )`.
- **MCP servers** come from the same `mcp-servers.json` (see
  [MCP servers](#mcp-servers)).
- **pi packages persist.** `pix install npm:<package>` (or `pirun install ...`)
  writes to `/root/.pi/agent` in `cc-home`, so it survives `ccdown` and `--rm`
  like a login does. [pi-claude-bridge](https://github.com/elidickinson/pi-claude-bridge)
  (`pix install npm:pi-claude-bridge`) fits well here: it serves Claude models
  to pi through the container's own Claude Code and its `/login`, so pi needs
  no separate Anthropic login. Pick them in `/model` as `claude-bridge/<model>`.
  It breaks if `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or
  `ANTHROPIC_BASE_URL` is exported in the container, so do not use the API-key
  setting above together with it.
- **Global pi config.** `~/.config/cc-container/pi/` is mounted read-only at
  `/opt/cc-pi` and merged into `/root/.pi/agent` by `guest/pi-config-sync` at
  every `pix` and `pirun`, the same way `mcp-servers.json` is. Each top-level key
  of its `settings.json` overwrites the guest's; `packages` is merged with
  anything installed by hand (a declared package replaces a hand-installed one of
  the same name). `keybindings.json` is symlinked, and `extensions/`,
  `prompts/` and `skills/` are referenced as `/opt/cc-pi/...` from
  `settings.json` (for example `"skills": ["/opt/cc-pi/skills"]`). A new
  directory needs `ccdown && ccup` to be mounted.
- **Worktrees** are Claude Code's feature; pi has no `--worktree`. Two pi
  sessions in one checkout share files, so isolate them by creating a worktree
  in `ccxs` and starting pi in it with `pix -C <worktree path relative to the
  project>`.
- **`mac-sim`** is a plain command on the guest `PATH`, so pi can call it like
  any other tool.

## Profiles: a second, isolated container

The shared session carries everything `config.sh` gives it: your extra mounts,
the MCP servers and their `mcp.env` secrets, the pi config, the mac-sim token,
and the `cc-home` login. A long-lived job that should see none of that (a
benchmark runner, a nightly build) gets a **profile**: a second container,
beside the session, with its own name, home volume, image, mounts and size.

```bash
mkdir -p ~/.config/cc-container/profiles
cp config/profile.example.sh ~/.config/cc-container/profiles/worker.sh
cc-profile worker up              # from the project directory
cc-profile worker exec make test  # argv, from a script or a terminal
cc-profile worker shell           # bash in it
cc-profile worker status
cc-profile worker down            # the shared session keeps running
```

Verbs: `up`, `down`, `status`, `attach`, `pi`, `shell`, `exec`, `run` (one-off,
`--rm`), `build`, `upgrade`, `doctor`. Give a profile you use often a short name
in `config.sh`: `ccworker() { cc-profile worker "$@"; }`.

How it works:

- `~/.config/cc-container/profiles/<name>.sh` is sourced **after** `config.sh`,
  so a profile inherits everything and overrides what it names. Arrays such as
  `CC_EXTRA_VOLUME_ARGS` and `CC_EXTRA_RUN_ARGS` must be reset there if the
  profile should not inherit them; the example resets both.
- `cc-profile` sources the library again in a fresh shell with
  `CC_PROFILE=<name>`, so the profile's values never reach your shell.
- **Give it its own home volume** (`CC_HOME_VOLUME`): only one running container
  can hold a named volume, so a profile on `cc-home` cannot start beside the
  session. A new volume is created on first use (sparse, 512 GB by default; make
  it explicitly with `container volume create -s <size> <name>` for another
  size). It needs its own login (`cc-profile <name> attach`, then `/login`, or
  a `claude setup-token` token you pass in).
- `CC_MCP=0`, `CC_PI_SYNC=0` and `CC_SIM_AUTO=0` keep the MCP servers and their
  secrets, the pi config and the mac-sim token out of it.
- **Its own image**, if it needs other tools: set `CC_CONTAINER_IMAGE` to a
  different image *name* (`claude-code-worker:local`, not `claude-code:worker`,
  so the two builds never share a `:base` tag) and `CC_IMAGE_OVERLAY` to its
  Dockerfile. `cc-profile <name> build|upgrade` pins its agent versions in
  their own record, so upgrading the session's image never changes it.
- `CC_ATTACH_COMMAND` replaces the `claude` that `attach` runs with a wrapper
  that passes claude's arguments on: `CC_ATTACH_COMMAND=(trainyard claude)`.
- The proxy, the mac-sim shim and the hooks are host-wide. `down` on either
  container leaves them running while the other is up.

Anything a profile adds to `CC_EXTRA_RUN_ARGS` (`--cap-add`, a published port)
widens that container's boundary; read [SECURITY.md](SECURITY.md) first.

### A profile for an agent runner

A task runner that starts several unattended Claude Code sessions and checks
their work on the simulator wants the isolation above, but it also needs the
mac-sim bridge, and it must never bill an API key. `config/profile.runner.example.sh`
is that profile: subscription-only, no extra mounts, MCP servers or pi config,
`CC_SIM_AUTO=1`, and sized for about four sessions.

```bash
cp config/profile.runner.example.sh ~/.config/cc-container/profiles/runner.sh
cd ~/dev/my-app                       # inside a root mac-sim.json declares
cc-profile runner up                  # beside cc-session, on its own volume
cc-profile runner attach              # once: /login, stored on runner-home
cc-profile runner exec mac-sim status # the bridge, from the runner
```

The login is the profile's own: `/login` in `cc-profile runner attach` stores
it on the runner's home volume, separately from `cc-home`, so logging out of
one never signs out the other. With `CC_SUBSCRIPTION_ONLY=1` only a Claude.ai
login works there.

`config/profile.train.example.sh` is the runner profile set up for a
trainyard train: it mounts every repo listed under `"repos"` in
`~/.config/trainyard/machine.json`, wherever `up` runs from, and `attach` runs
`trainyard claude`. Copy it to `profiles/train.sh`.

`trainyard` is not in the image: its launcher and the container's own
`machine.json` live in the `train-home` volume. If that volume is recreated
(`container volume rm`, a teardown), they are gone and the driver fails with
`failed to find target executable trainyard`. The profile sets
`CC_REQUIRED_COMMANDS=(trainyard)`, so `cc-profile train up` notices and runs
its `CC_SETUP_COMMAND`: it installs the launcher from the committed `HEAD` of
the mounted checkout (`TRAINYARD_CHECKOUT`, default `~/IdeaProjects/trainyard`,
which must be one of the mounted repos) and rewrites `machine.json` with the same repos. The one
thing it cannot restore is the Claude.ai login: `cc-profile train attach`, then
`/login`. Also, `container image prune -a` removes the agent image when no
container runs; rebuild it with `cc-profile train build`.

To run the train's task sessions on pi with OpenRouter models
(`trainyard run --harness pi --pi-billing openrouter`), put
`OPENROUTER_API_KEY=...` in `mcp.env` and name it in the profile's
`_train_env_keys`, then `cc-profile train down && cc-profile train up`. Map
trainyard's model aliases in the container's
`/root/.config/trainyard/machine.json` (`"pi": {"models": {"opus": ...,
"sonnet": ..., "haiku": ...}}`, each an `openrouter/<vendor>/<model>` id). Install
pi-subagents into `train-home` too (the profile says where): the train's
skills start subagents through it, and trainyard refuses a pi train without it. Keep the Claude.ai login:
landings and `land auto` still run on Claude Code. The key passes
subscription-only mode by design, so give it a credit limit (`SECURITY.md`).

## Configuration

Everything user-specific lives outside the repo, mostly in
`~/.config/cc-container/`; nothing in the repo needs editing. `install.sh` seeds
the files every setup needs. The rest are opt-in, and you create them yourself.

| Path in `~/.config/cc-container/` | Created by `install.sh` | Purpose |
|---|---|---|
| `config.sh` | yes | Image, resources, proxy mode, extra mounts, defaults |
| `mcp/mcp-servers.json` | yes | Declarative MCP servers, synced into both agents' user scope ([MCP servers](#mcp-servers)) |
| `mcp.env` | yes | Secrets the above references as `${VAR}` (chmod 600, never inline) |
| `tinyproxy.conf` | yes | Only used where guests have no direct egress |
| `pi/` | no | Global pi config ([Running pi](#running-pi-beside-claude-code)); mounted only if it exists when a container starts |
| `mac-sim.json` | no | The opt-in Xcode (and Android emulator) bridge, from `config/mac-sim.example.json` ([Driving the Mac's Xcode toolchain](#driving-the-macs-xcode-toolchain-from-the-guest)) |
| `Dockerfile.local` | no | Your own layer on top of the image ([Adding tools](#adding-tools-to-the-image)) |
| `profiles/<name>.sh` | no | A second container's settings, from `config/profile.example.sh`, `config/profile.runner.example.sh` or `config/profile.train.example.sh` ([Profiles](#profiles-a-second-isolated-container)) |

Nothing else in that directory is read automatically. A directory of your own
guest tools needs a `CC_EXTRA_VOLUME_ARGS` entry in `config.sh` (see
[Extending it](#extending-it-with-your-own-host-tooling)).

Outside it:

- **Your shell rc** gets one `source` line from `install.sh`
  (`./install.sh --uninstall` removes it).
- **`~/.local/state/cc-container/`** is written by the wrapper itself: the
  versions `cc-container-build` reuses, the proxy's pid and log, the mac-sim
  token and log, and the private `TMPDIR`s of mac-sim commands that declare
  `private_tmp`. Each profile records its image versions in its own
  `image-versions.<name>`. Nothing to set up.
- **The `cc-home` volume** is the container's home directory: both agents'
  logins, pi packages installed with `pix install`, the container's own Claude
  Code settings (such as the [statusline](#statusline)), trust decisions, and
  the terminfo entries `ccxs` and `ccsh` copy from the Mac (`~/.terminfo`). It
  survives `ccdown` and rebuilds. Deleting it (`container volume delete
  cc-home`) means logging in and redoing those steps. `CC_HOME_VOLUME` names
  another volume; each profile should have its own. The volume is a sparse
  image on the Mac that files deleted inside it never shrink, so `ccdown`
  trims it (`fstrim`, which needs `CAP_SYS_ADMIN`) from a throwaway container
  with no network that mounts only the volume; the agent containers never get
  that capability. `cc-trim` does it by hand, e.g. after one-off `ccrun`
  sessions.

On a new Mac, copy `~/.config/cc-container/` from the old one, run `./install.sh`
(it keeps existing files), `cc-container-build`, and log in again: the
`cc-home` volume does not move with the config.

Resources default to **half the host's cores** (at least 1) and **half its RAM,
kept between 4 and 8 GB**. The memory limit is what the session ends up costing
the Mac: the VM keeps every guest page it has touched, page cache included,
until it stops, whatever `free` reports inside the guest. Raise it only if
work in the guest gets killed (`cat /sys/fs/cgroup/memory.events` shows
`oom_kill`). A change applies when the container is recreated: `ccup` recreates a
stopped session whose size differs from the config, and for a running one says
what differs (`ccdown`, then `ccup`, applies it). A shell keeps the values
`config.sh` had when it started, so after editing it `ccup` refuses to run in an
older shell; open a new terminal.

The guest kernel boots with `CC_SESSION_SYSCTLS` (default
`vm.min_free_kbytes=262144 vm.watermark_scale_factor=200`), passed as
`--kernel-arg sysctl.<key>=<value>` because `/proc/sys` is read-only inside.
The kernel's own reserve for 12 GB is about 14 MB, too little for the 64 KB
atomic buffers virtiofs allocates: under memory pressure reads of the mounted
project failed with `ENOMEM`, `dmesg` showed `page allocation failure: order:4`,
and git stalled. A changed value is treated like a changed size. Set it to ""
to boot with the kernel's defaults.

Override in `config.sh` or per invocation:

```bash
CC_SESSION_MEMORY=12g CC_SESSION_CPUS=10 ccup
```

**Free disk.** The VM disks, the home volumes, the images and every mounted
checkout share the Mac's data volume. When it fills, a running guest's disk
write fails, its kernel remounts `/` read-only, and only recreating the
container (`ccdown && ccup`) brings it back. So `ccup`, `ccrun`, the profile
`up` and `run`, and `cc-container-build` refuse to start with less than
`CC_DISK_MIN_GB` (10) free, and every command warns below `CC_DISK_WARN_GB`
(25). `container system df` shows what takes the space. `CC_DISK_MIN_GB=0 ccup`
skips the refusal once.

Only `$PWD` is visible inside the VM. To expose more, add `CC_EXTRA_VOLUME_ARGS`
entries in `config.sh` — one `--volume` each, `:ro` unless the agent must write
back. Mounting a host path at the *same absolute path* it has on the Mac makes
tooling that refers to it absolutely work unchanged in the guest.

`config.sh` is sourced by zsh, where `$VAR:r` (and `:h`, `:t`, `:e`, ...) is a
modifier, so `"$DIR:$DIR:ro"` silently loses its `:ro` and mounts read-write at
a wrong path. Brace the name: `"${DIR}:${DIR}:ro"`.

### Subscription-only mode

Set `CC_SUBSCRIPTION_ONLY=1` in `config.sh` to make sure every session bills a
Claude.ai subscription and nothing else: no API key, Console account, Bedrock or
Vertex, for Claude Code and for pi alike. Recycle a running session afterwards
(`ccdown && ccup`); attaching to one started without it is refused.

- **Claude Code enforces it itself.** `policy/subscription-only/` is mounted
  read-only at `/etc/claude-code` as managed settings: `forceLoginMethod:
  "claudeai"` rejects API keys and Console logins, and `allowedProviders:
  ["anthropic"]` rejects Bedrock and Vertex, whether they come from env or from a
  `settings.json` `env` block. The mount is read-only, so root in the guest
  cannot edit it. pi-claude-bridge runs its turns through this Claude Code, so it
  is covered too.
- **pi is checked at every start.** Its own providers are outside Claude Code's
  policy, so `guest/cc-subscription-guard` refuses to start pi (or Claude Code)
  while `ANTHROPIC_*`, `AWS_*` or `CLAUDE_CODE_USE_*` is exported, `~/.aws`
  exists, or pi's `auth.json` holds anything but an OAuth login. The error names
  each problem and how to clear it.
- **pi's other providers are not refused.** Their env keys
  (`OPENROUTER_API_KEY`, `OPENAI_API_KEY` and the like) pass the check, so a
  profile can pass one in on purpose, as the train profile can for
  trainyard's openrouter billing. Nothing passes one unless you add it.

Check which account is in use with `claude auth status` (from pi:
`!claude auth status`): a subscription shows `"authMethod": "claude.ai"` and a
`subscriptionType`. The policy takes `/etc/claude-code`, so it cannot be combined
with mounting your host's managed settings there; copy any keys you need from
that file into `policy/subscription-only/managed-settings.json` instead.

## MCP servers

The container keeps its own MCP list, separate from the host's.
`~/.config/cc-container/mcp/` is mounted read-only at `/opt/cc-mcp` and merged
into the guest's **user scope** at every session start, so `claude mcp list` and
`/mcp` behave exactly as after `claude mcp add --scope user`. A project's
`.mcp.json` still applies on top, and servers you add by hand in the guest are
never touched. The same list is merged into pi's `~/.pi/agent/mcp.json` (same
`mcpServers` format), so `pi mcp list` and pi's `/mcp` see it too.

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
resolved key persists in the `cc-home` volume until you delete it. The same goes
for pi's `~/.pi/agent/mcp.json`. Treat that volume as holding credentials, and
note that `claude mcp list` and `pi mcp list` both print keys in full.

**Only stdio and API-key servers work.** Servers authenticating by OAuth loopback
cannot complete login: the redirect binds a port *inside* the container that your
host browser cannot reach. Add those on the host instead. pi is the exception:
its sign-in (`/mcp login <server>`) accepts the final redirect URL pasted back
in, as its own `/login` does, so OAuth servers can work there.

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

To customise it, copy it into a directory of your own, mount that at
`/opt/cc-local` (also on the guest `PATH`) from `config.sh`, and point the
command there instead. Updates never touch that directory. Nothing mounts it
for you:

```bash
CC_EXTRA_VOLUME_ARGS+=( --volume "${CC_CONFIG_DIR}/local:/opt/cc-local:ro" )
```

## Driving the Mac's Xcode toolchain from the guest

Xcode, `xcrun` and `simctl` are macOS-only. No container configuration changes
that, so an agent in the guest correctly concludes it cannot build an iOS app and
stops. The fix is a channel, not an installation: `host/mac-sim-shim.py` runs on
the Mac and exposes a **fixed allowlist** of verbs over HTTP on the bridge
address, and `guest/mac-sim` forwards to it. The host does the work; results come
back through the bind-mounted workspace, so a screenshot written under the
project is readable in the container.

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

A script that reads the answers passes `--json` (`mac-sim status --json`,
`sim list`, `lock`, `projects`, `android status|list|lock`) and gets one
versioned JSON object instead of text.

A project that declares a `watch` block also builds and drives its **Apple Watch**
app on a watch paired with the iPhone simulator (`mac-sim watch build | install |
launch | screenshot`, `mac-sim sim create <name> --with-watch`).

A project that declares a `device` block can also install, launch, terminate and pull
a diagnostics file from one physical iPhone (`mac-sim device ...`), the one its host
config names.

The same bridge drives **Android emulators** on the Mac (`mac-sim android
[-d tablet] boot | install | screenshot | ...`) for a project whose config declares an
`android` block: the emulator needs a hypervisor the guest does not have.

`mac-sim` also works **on the Mac itself**, for a tool that runs there rather
than in a container (a task runner, a local agent session): sourcing
`bin/cc-container.sh` puts `host/bin/mac-sim` on `PATH`, which runs the same CLI
against the shim with the containers' token, under the same claims
([details](docs/MAC-SIM.md#running-mac-sim-on-the-mac)).

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
| `CC_REQUIRED_COMMANDS` | Commands the shared session must have once `cc-up` has started it (not checked for one-off runs), for tools kept in the home volume; `cc-up` fails naming any that are missing |
| `CC_SETUP_COMMAND` | A shell command `cc-up` runs inside the session when a required command is missing, to install it again (then checks once more) |

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

Its build context is the file's own directory, so it can `COPY` files from
there. `CC_IMAGE_OVERLAY` names another file (it must then exist), or `""` for
none; a profile uses it to build its own image.
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

pi works the same way, through `PI_VERSION`:

```bash
pi-container-upgrade            # latest published version
pi-container-upgrade 1.2.0      # a specific one
```

Each build records the versions it used in
`~/.local/state/cc-container/image-versions`, and any version you do not pass
reuses that record. Upgrading one agent therefore leaves the other where it
was, and a plain `cc-container-build` stays cached. `cc-container-build latest
latest` returns both to unpinned. pi's in-app update check is off in the image
(`PI_SKIP_VERSION_CHECK=1`), since an in-session `pi update` would be thrown
away like `claude update`. `cc-doctor` shows both versions in the image.

## Design notes

**The whole home is one named volume (`cc-home:/root`), not just `~/.claude`.**
Claude Code splits its state: credentials and history live under `~/.claude/`,
but onboarding, theme, and folder-trust live in the separate `~/.claude.json`.
Mounting only `~/.claude` re-runs onboarding on every `--rm` run. Persisting all
of `/root` also keeps `gh auth`, `.gitconfig`, and package caches, and one shared
home across projects means one login total.

**It runs as root with `IS_SANDBOX=1`.** apple/container surfaces bind-mounted
host files as `root:root` with no uid mapping, so a non-root user may not be able
to write the mounted project. Files the container creates still land on the host
owned by you.

**Config directories are mounted as directories, never as single files.** An
editor's atomic save replaces the host file's inode; a single-file bind mount
does not follow that, and the guest stops seeing the file at all — silently.

## Limitations

- **No Xcode, ever.** It cannot be installed in a Linux guest. Drive the Mac's
  copy instead, through the opt-in bridge described in
  [Driving the Mac's Xcode toolchain from the guest](#driving-the-macs-xcode-toolchain-from-the-guest).
- **No browser in the image.** The image has the system libraries Playwright's
  Chromium needs, but not the browser: run `npx playwright install chromium` in
  the project (once per Playwright version, kept in the `cc-home` volume). Runs
  are headless only. Google Chrome has no Linux arm64 build, so point the
  Playwright MCP server at Chromium (`--browser chromium`), not its default
  `chrome` channel.
- **No enterprise policy or telemetry.** The host's managed-settings file is not
  mounted. To opt in, bind-mount it read-only at
  `/etc/claude-code/managed-settings.json` — never `COPY` it into the image, as
  it can contain a live bearer token.
- **Only the mounted directory is visible.** Cross-repo work needs an explicit
  extra mount, and a running `ccup` session only picks up a newly added one after
  `ccdown && ccup`.
- **The guest sees your host path layout.** The project mounts at its Mac path
  (so git worktree links agree on both sides), which means the guest learns your
  home directory's path. Nothing outside the mount is exposed.
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

**`cc-sim-up: shim failed to start` with `Errno 49: Can't assign requested
address` in the shim log.** The shim binds to the vmnet gateway
(`192.168.64.1`), which only exists while a container is running. `cc-sim-up`
now starts a short-lived `cc-bridge-probe` container to bring it up on a cold
start and `cc-up` removes it once the session is running. If it still fails,
check `ifconfig | grep 192.168.64` and `container network inspect default`.

**`401 Unauthorized` from `registry-1.docker.io` on `cc-up`.** The
`claude-code:local` image is missing (a runtime restart can empty the image
store), so the runtime tried to pull it. Run `cc-container-build`.

**A build fails with `no space left on device`.** The runtime's storage grows
with every image and every container's scratch. `ccdown` reclaims the running
session's share, which is usually the largest single piece. Deleting the base
image (above) is the next lever.

**Inside a session, `/tmp` and other writes fail with `Read-only file system`
(`EROFS`).** The Mac's disk filled up: the VM's disk image could not grow, the
guest saw a write error and remounted its root file system read-only
(`grep ' / ' /proc/mounts` shows `emergency_ro`). It stays that way after you
free space. `cc-doctor` shows the free disk and what `container image prune -a`
would reclaim; free space, then recreate the session with `ccdown && ccup` (your
home volume is a separate disk and keeps its data).

**A build fails with `read-only file system` or `input/output error`, and keeps
failing after you free space.** The buildkit helper container wedges when it
runs out of disk mid-build and does not recover on its own:

```bash
container system stop && container system start
container delete buildkit                # recreated on the next build
```

**`ccdown` (or the recreate step of `ccup`) prints `container stop ... did not
finish` or `container kill ... did not finish either`.** The session's VM is
wedged at the host level, not just slow to shut down: `container exec` and
`container logs` against it would hang too. `_cc_session_remove` bounds its
wait for `stop`/`kill` at `CC_SESSION_STOP_TIMEOUT` seconds (30 by default) and
then fails with this message instead of hanging forever. Recover by hand:

```bash
ps aux | grep "container-runtime-linux.*--uuid <session-name>"
kill -9 <pid>                            # drops the wedged VM
container system stop && container system start
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
