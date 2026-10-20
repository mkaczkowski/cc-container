# Plan: one train driver for several repos

Status: proposal, nothing implemented yet.

The goal is one `cc-train` container running one train driver that manages
tasks across several local repos, started from wherever, instead of a train
bound to a single Gymlog checkout.

## Where things stand

`cc-train` is not part of this repo. It is a profile
(`~/.config/cc-container/profiles/train.sh`, based on
`config/profile.runner.example.sh`) with its own home volume (`train-home`),
16g / 8 CPUs, subscription-only, no extra mounts, no MCP or pi config, and the
mac-sim bridge kept. Compared with the shared `cc-session` it narrows the
boundary to the one directory it was started from.

The train itself lives in `gymlog-app/tools/train` and is bound to one repo in
two ways.

The container:

- `cc-up` mounts only `$PWD`, at its host path, and the mount is fixed for the
  container's lifetime (`bin/cc-container.sh`, `cc-up`).
- `cc-up` on a running container prints "already running" and keeps the old
  mount, even when started from another directory.
- `_cc_attach_workdir` falls back to the mount root when `$PWD` is outside it.
- `_cc_sim_in_project` starts the mac-sim shim only when `$PWD` is inside a
  root declared in `mac-sim.json`. Only `~/IdeaProjects/gymlog-app` is
  declared.

The tooling:

- The driver runs in the guest and spawns `claude` / `pi` itself
  (`train.mjs`, `TRAIN_CLAUDE_BIN`).
- State lives in `<repo>/.mac-sim/train`, the warm pool in
  `<repo>/.claude/worktrees/train-pool-<n>`, the Android checkout in
  `.claude/worktrees/train-android[-<n>]`.
- Gymlog is hard-coded in many places: `@gymlog/*` package filters, the i18n
  steps in `land.mjs`, `gymlog://` deep links,
  `screen-inventory/projects/gymlog/scenarios.json`, `gymlog-` stamp
  prefixes in `watch.mjs`, and the Expo prebuild / pod install recipes in
  `mac-sim.json`.
- `root` is passed around as a parameter (about 600 uses in `train.mjs`)
  rather than read from a global, so moving it onto each task is feasible.

## What already works

For one other repo at a time, with no code change:

```bash
cc-profile train down            # removes the container, so the mount goes too
cd ~/IdeaProjects/other-repo
cc-profile train up
```

The repo needs its own `projects[]` entry in `mac-sim.json` if it uses
simulators. This does not give a train, since the tooling is Gymlog-only.

## Why one driver, not one per repo

One driver per repo would put several drivers in one VM, each sizing its
concurrency without knowing about the others, and two trains at
`--concurrency 4` in a VM sized for one run out of memory. One driver gives:

- one global concurrency limit, so the VM is sized for that number;
- one scheduler for the Mac-wide simulators and emulators, instead of trains
  contending for them;
- one console and one `watch` view across repos.

## What it needs

### Train tooling (the bulk of the work)

1. **Per-task root.** Each task records its repo. The scheduler, concurrency
   limit and disk and memory guards become global. The disk check sums every
   repo's pool plus each Android checkout (`ANDROID_CHECKOUT_GB = 7`).
2. **Per-repo pools stay in the repo.** Git worktrees and `mac-sim`'s
   checkout-root lookup (`guest/mac-sim`, `checkout_root`) both expect
   `<project>/.claude/worktrees/<name>`.
3. **A project adapter.** Everything Gymlog-specific becomes per-repo config:
   gates, landing steps, deep-link scheme, prebuild / pod install recipes,
   stamp names, which simulator and emulator lanes the project uses. Gymlog is
   the first adapter.
4. **A neutral state directory.** The driver's state cannot live in one repo's
   `.mac-sim/train`. It needs its own directory, mounted read-write, that the
   guest driver and a Mac-side console or `watch` can both read.
5. **Its own home.** A driver for several repos cannot live inside one of them.
   It moves to its own repo or package, mounted read-only or installed in the
   container, outside cc-container's scope (see Scope in `CONTRIBUTING.md`).

### cc-container

1. **A list of project directories** for a profile, for example
   `CC_PROJECT_DIRS=(...)`, each mounted read-write at its host path, like
   `$PWD` today. It must parse in bash and zsh.
2. **Read the mounts from `container inspect`**, not from a new env var, so
   they cannot drift from what is mounted and paths with spaces need no
   delimiter. `_cc_attach_workdir` then starts an attach in whichever mounted
   repo the caller is in.
3. **The simulator check** passes when any mounted directory is a declared
   project. The token is host-wide and the guest's `mac-sim` resolves the
   project from the caller's checkout, so one shim already serves several
   projects: only the start-time check changes.
4. **Mount mismatch on `up`** warns and prints the `down` / `up` commands,
   rather than failing, so `ccup` from anywhere keeps working for `cc-session`.
5. **Refuse unsafe mounts** (see Security).
6. Mount the train's state directory.
7. Docs: `config/profile.runner.example.sh`, `config/config.example.sh`, the
   Profiles and Configuration sections of `README.md`, and `CHANGELOG.md`.
   The one-off `cc-container` run can follow later: the train does not use it.

### mac-sim.json

One `projects[]` entry per repo that uses simulators, with lock names and
`android` blocks that keep projects apart on shared emulators.

## Security

- **Never mount a parent directory like `~/IdeaProjects`.** It contains this
  repo, and an unattended agent that can write `bin/cc-container.sh`, which
  the Mac shell sources, has a way out of the VM. The project list must refuse
  any entry that contains or sits inside `CC_CONTAINER_REPO` or
  `~/.config/cc-container`.
- **Review each project's mac-sim commands as host execution.** The Gymlog
  entry already runs repo scripts on the Mac (`tools/train/mac-cleanup.sh`,
  `node tools/train/consoleForward.mjs`), and the guest can write those files.
  That exists today. Copying the pattern to more projects multiplies it, so
  check each new entry against `SECURITY.md`.
- **No isolation between repos.** An agent on a task in repo A can write
  repo B, since both are mounted read-write in one VM.

## Performance and resources

- **Recreating the container is the expensive operation.** Changing the repo
  list removes the container, ending attached sessions and stopping the
  driver. Choose the list deliberately.
- **Disk grows per repo.** Each repo keeps a warm pool with `node_modules`,
  plus Android checkouts. Free space on the Mac runs out before CPU does.
- **Extra mounts themselves cost nothing that matters.**
- **Shared blast radius.** A driver crash or VM restart stops every repo's
  train at once.

## Order

1. Design the project adapter and per-task root inside `gymlog-app`, with
   Gymlog as the first adapter and its tests proving nothing regressed.
2. Move the train to its own repo, with state in a neutral directory.
3. Add the cc-container changes above.
4. Add the second repo: its adapter, and a reviewed `mac-sim.json` entry.

## Open questions

- Where the train's state directory lives on the Mac, and whether the console
  runs in the guest, on the Mac, or both.
- How the driver picks a repo for a task: an explicit field on each task, or
  inferred from where the task was added.
- Whether one global concurrency limit is enough, or each repo also needs a
  cap of its own.
- How simulator and emulator lanes are shared when two projects both want
  Android.
