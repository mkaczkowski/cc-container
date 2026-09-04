# Changelog

## Unreleased

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
- Host-side tooling is not bundled. `CC_EXTRA_RUN_ARGS`, `CC_PRE_RUN_HOOK` and
  `CC_POST_DOWN_HOOK`, plus a `/opt/cc-local` directory on the guest `PATH`, are
  the seams for wiring up your own; `SECURITY.md` covers how to do it safely.
- `cc-proxy-down` kills only the tinyproxy this tool started; logs and state
  moved out of `/tmp` into `~/.local/state/cc-container/`.
- Container lookups parse JSON instead of prefix-matching `container list`.
- `install.sh` / `install.sh --uninstall`.
