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

- **`apple/container` gives each session its own real VM**, not a namespace on a
  shared Docker daemon — no Docker Desktop, no licence, no daemon to share.
- **Several agents, one container.** A shared session takes ~0.1 s to attach, so
  running four Claude Code instances across four terminals against one repo is
  the normal mode rather than a heavyweight one.
- **It stays out of your way.** Its own home volume, its own MCP list, its own
  login — nothing it does can corrupt the Claude Code you run on the host.

## Requirements

- Apple silicon, macOS 15+ (26 recommended)
- [`apple/container`](https://github.com/apple/container) 1.2.0+ (`brew install --cask container`)
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
Attaching takes ~0.1 s against ~1.4 s for a cold `ccrun`.

**Caveats.** Sessions share one filesystem and one `~/.claude`: conversations are
independent, but two agents editing the same file will overwrite each other.
Every session sees the directory you ran `ccup` from, not your shell's cwd.
State accumulates until `ccdown` (~2.8 GB of disk while it exists), which costs
the clean-slate guarantee `--rm` gives you.

Switching projects means recycling: `ccdown && cd <other> && ccup`.

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

## Extending it with your own host tooling

Some tools only exist on macOS and never will in a Linux guest. Rather than
bundle any particular one, the wrapper gives you three seams in `config.sh`:

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
- **Slower startup.** Each `ccrun` boots a VM: a few seconds.
- **Some hosts have no guest network egress at all** (endpoint-security packet
  filters). `cc-doctor` detects it and enables a host proxy;
  see [docs/NETWORK.md](docs/NETWORK.md).

## Troubleshooting

```bash
cc-doctor                                # start here
container system status                  # runtime up?
container image list | grep claude       # image present?
container system logs | tail -30         # runtime errors
cc-shell                                 # poke around inside
tail -f ~/.local/state/cc-container/*.log
```

## Licence

MIT. See [LICENSE](LICENSE).
