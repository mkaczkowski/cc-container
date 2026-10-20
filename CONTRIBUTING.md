# Contributing

## Ground rules

- **Nothing user-specific in tracked files.** No absolute paths containing a
  username, no hostnames, no API keys, no employer-internal URLs. Everything
  configurable belongs in `config/*.example.*` and is read from
  `~/.config/cc-container/` at runtime. CI fails the build if a `/Users/<name>/`
  path appears in a tracked file.
- **Keep it generic.** This tool runs coding agents (Claude Code, pi) in a VM;
  it does not bundle support for any particular language, framework, or host
  toolchain. Anything project-specific belongs behind the `CC_EXTRA_RUN_ARGS` /
  `CC_PRE_RUN_HOOK` seams, in someone's own config.
- **Anything that widens the VM boundary needs a `SECURITY.md` update** in the
  same PR, and an explicit note about it in the description.

## Local checks

```bash
bash -n bin/cc-container.sh install.sh guest/statusline.sh
sh -n guest/cc-entry guest/pi-entry guest/cc-workspace-link guest/shell-entry guest/cc-terminfo-sync host/bin/mac-sim
zsh -n bin/cc-container.sh                 # it is sourced from both shells
shellcheck -s bash bin/cc-container.sh install.sh guest/statusline.sh
python3 -m py_compile guest/cc-mcp-sync guest/cc-subscription-guard guest/pi-config-sync guest/mac-sim host/mac-sim-shim.py
python3 -c 'import json,glob; [json.load(open(f)) for f in glob.glob("**/*.json", recursive=True)]'
```

## What CI can and cannot do

GitHub's macOS runners cannot nest `apple/container`'s VMs, so CI is lint-only:
shell syntax under both bash and zsh, shellcheck, Python compile, JSON validity,
and the no-personal-paths check. **Integration testing is manual, on Apple
silicon.** Say in your PR what you actually ran.

## Testing a change by hand

```bash
cc-container-build && cc-doctor
ccrun -p 'print the working directory and list files'   # one-off session
ccup && ccx && ccdown                                   # shared session
pirun --version                                         # pi in the image
```

## Scope

This tool deliberately does one thing: run coding agents (Claude Code, pi) in an
apple/container VM. Features that belong in the agents themselves, in a
project's own scripts, or behind the extension hooks are out of scope.
