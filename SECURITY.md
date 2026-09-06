# Security model

## What the container actually isolates

Each session runs in a lightweight Linux VM, not a namespace on your Mac. That
is a real boundary, and it is what makes `--dangerously-skip-permissions`
defensible as a default: an agent that installs packages, deletes files, and
runs arbitrary commands is doing so inside a VM you can throw away.

**But the boundary is exactly as wide as you configure it.** Three settings
widen it, and you should understand each before using it:

| Setting | What it grants the agent |
|---|---|
| `--volume "$PWD:/workspace"` (always) | Full read-write access to the directory you launched from, on the real filesystem. Files it creates land on the host owned by you. |
| `CC_EXTRA_VOLUME_ARGS` | The same, for every extra path you list. `:ro` limits it to reading. |
| `CC_PRE_RUN_HOOK` / `CC_EXTRA_RUN_ARGS` | Whatever host-side helper you wire up yourself (see below). |

The first is the point of the tool. The second is opt-in per path. The third is
entirely yours, and deserves real thought.

## If you bridge a host-only tool

Some toolchains cannot exist in a Linux guest — Xcode is the obvious one. Guest →
host TCP works, so the fix is a small host-side listener that the container can
call. This project ships exactly one, `mac-sim` (see
[docs/MAC-SIM.md](docs/MAC-SIM.md)), and it is **off until you write
`~/.config/cc-container/mac-sim.json`**: no config, no listener. The rules below
are what it implements, and what to get right if you build another.

**A bridge is a hole in the VM boundary.** Everything it can be asked to run
happens on your Mac, as you, outside the sandbox. Treat it as an RPC surface
with a threat model, not as a convenience script:

- Expose a **fixed allowlist of verbs**. Never a verb that takes a free-form
  command, and never one that takes a shell string — build argv lists.
- **Validate every argument** against a regex, or resolve it inside an
  allowlisted root. Resolve symlinks *before* the containment check, or a
  symlink inside the root pointing at `/etc/hosts` will be followed.
- **Never read the bridge's own config from a path inside the bind mount.** The
  project directory is writable by the container, so a config file there would
  let the guest define its own host commands and defeat the allowlist entirely.
  Read it from `~/.config/` only.
- **Authenticate.** A source-IP allowlist is not enough on a shared bridge
  subnet: use a shared secret, compare it in constant time, and bind the bridge
  address rather than `0.0.0.0`.
- **Bound everything**: body size, output size, and a timeout per verb. Log
  every accepted and rejected call.

Even done well, the residual risk is real: if a bridged command is something like
`npm test`, and the agent can edit that project's `package.json`, then the agent
can run arbitrary host code. That is not hypothetical for `mac-sim` -- a declared
`run` command that shells out to a package manager has exactly this shape. Scope
the roots deliberately, declare only the commands you need, and note that the
shim starts only while you are working inside one of those roots.

## Running as root

The image runs as root with `IS_SANDBOX=1`. apple/container surfaces bind-mounted
host files as `root:root` with no uid mapping, so a non-root user often cannot
write `/workspace`. This is contrary to the usual devcontainer convention; the
justification is that the VM is the sandbox, not the user account inside it.

## The proxy

If you enable the host proxy, tinyproxy accepts connections from the whole
bridge subnet (`192.168.64.0/24` by default). On a machine where that subnet is
shared with VMs you do not control, narrow the `Allow` line in
`tinyproxy.conf`. The proxy is off unless `cc-doctor` finds that guests have no
direct egress.

## What is not covered

- **No enterprise policy or telemetry.** The host's managed-settings file is not
  mounted, so container sessions are unconstrained by it and invisible to it. To
  opt in, bind-mount it read-only at `/etc/claude-code/managed-settings.json`.
  Never `COPY` it into the image: it can contain a live bearer token.
- **Credentials persist.** `/login` inside the container writes to the `cc-home`
  volume and stays there until you delete the volume. Every project shares it.
  So do MCP API keys: `mcp.env` values are substituted into the guest's
  `~/.claude.json`, so they live in that volume in plaintext, and
  `claude mcp list` prints them in full. `mcp.env` keeps them out of a file you
  might commit — it does not keep them out of the container.
- **Sessions share a filesystem.** In multi-session mode two agents editing the
  same file will overwrite each other. That is a correctness property, not a
  security one, but it surprises people.

## Reporting a vulnerability

Open a GitHub security advisory rather than a public issue.
