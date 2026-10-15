# cc-container: run Claude Code inside Apple's native `container` runtime.
#
# Mounts the current directory at its own host path (so git worktree links mean
# the same thing on the Mac and in the guest), with /workspace as a symlink to
# it; the container's home lives in the `cc-home` named volume so login,
# history, and settings persist across runs.
#
# Source this from ~/.zshrc or ~/.bashrc (install.sh does it for you):
#   source /path/to/cc-container/bin/cc-container.sh
#
# Everything user-specific lives in ~/.config/cc-container/config.sh, which this
# file sources if present. Nothing here should ever need editing to adopt it.

# --- locate the repo (works when sourced from bash or zsh) -------------------
if [ -z "${CC_CONTAINER_REPO:-}" ]; then
  if [ -n "${ZSH_VERSION:-}" ]; then
    # %x names the file containing the source code and survives the eval that
    # hides zsh-only syntax from bash's parser. %N does NOT: inside eval it
    # reports "(eval)", which silently resolves the repo to the wrong directory.
    _cc_src="$(eval 'print -r -- ${(%):-%x}')"
    [ -e "${_cc_src}" ] || _cc_src="$0"
  else
    _cc_src="${BASH_SOURCE[0]:-$0}"
  fi
  CC_CONTAINER_REPO="$(cd "$(dirname "${_cc_src}")/.." && pwd)"
  unset _cc_src
fi

CC_CONFIG_DIR="${CC_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/cc-container}"
CC_STATE_DIR="${CC_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/cc-container}"
mkdir -p "${CC_STATE_DIR}" 2>/dev/null

# --- defaults (override any of these in config.sh) --------------------------
CC_CONTAINER_IMAGE="${CC_CONTAINER_IMAGE:-claude-code:local}"

# Host-derived so the defaults suit the machine rather than the author's.
# Half the cores and half the RAM, floored at 1 cpu / 4 GB.
_cc_half_cpus() {
  local n; n="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
  [ "$((n / 2))" -ge 1 ] && echo "$((n / 2))" || echo 1
}
_cc_half_memory_gb() {
  local b g; b="$(sysctl -n hw.memsize 2>/dev/null || echo 8589934592)"
  g="$((b / 1073741824 / 2))"
  [ "${g}" -ge 4 ] && echo "${g}" || echo 4
}

# Proxy mode: auto | on | off.
#   off  - never use a proxy (correct on a normal Mac)
#   on   - always route guest traffic through the host proxy
#   auto - use it only if `cc-doctor` recorded that direct egress is broken
# Some hosts (corporate endpoint-security stacks, in particular network/socket
# filter system extensions) drop the vmnet NAT path, leaving guests with no
# outbound TCP at all. Guest -> host TCP still works, so a local proxy is the
# escape hatch. See docs/NETWORK.md.
CC_PROXY="${CC_PROXY:-auto}"
CC_PROXY_HOST="${CC_PROXY_HOST:-192.168.64.1}"
CC_PROXY_PORT="${CC_PROXY_PORT:-8888}"

# Extra host directories to expose beyond $PWD. Only the mounted directory is
# visible inside the VM, so anything the agent must read from elsewhere on the
# Mac goes here as one --volume per entry. Default to :ro; mount read-write only
# where the agent is meant to write back. Define it in config.sh, e.g.
#   CC_EXTRA_VOLUME_ARGS=( --volume "$HOME/Notes:/mnt/notes:ro" )
# NOTE: every entry widens the isolation boundary. See SECURITY.md.
CC_EXTRA_VOLUME_ARGS=(${CC_EXTRA_VOLUME_ARGS+"${CC_EXTRA_VOLUME_ARGS[@]}"})

# Pass --dangerously-skip-permissions by default. The VM is the sandbox, so an
# unrestricted agent can still only reach what you mounted. CC_SKIP_PERMISSIONS=0
# opts out for one command or a whole shell.
CC_SKIP_PERMISSIONS="${CC_SKIP_PERMISSIONS:-1}"

# Subscription-only mode: 1 makes Claude Code (and pi-claude-bridge, which runs
# through it) refuse anything but a Claude.ai subscription login, and refuses to
# start pi while it holds or sees non-subscription credentials. Claude Code
# enforces it from a managed policy mounted read-only at /etc/claude-code, so the
# guest cannot edit it; guest/cc-subscription-guard checks the rest at start.
CC_SUBSCRIPTION_ONLY="${CC_SUBSCRIPTION_ONLY:-0}"

# Long-lived shared session (cc-up / cc-attach).
CC_SESSION_NAME="${CC_SESSION_NAME:-cc-session}"

# The mac-sim bridge: scoped guest access to the Mac's Xcode/simctl toolchain.
# Xcode is macOS-only and a Linux guest can never run it, so host/mac-sim-shim.py
# exposes a fixed allowlist of verbs on the bridge address and guest/mac-sim
# forwards to it. Entirely opt-in: with no mac-sim.json there is no listener, no
# MAC_SIM_* env in the container and nothing extra on the host, so an install
# that never configures it is unaffected. See docs/MAC-SIM.md.
CC_SIM_CONFIG="${CC_SIM_CONFIG:-${CC_CONFIG_DIR}/mac-sim.json}"
CC_SIM_TOKEN_FILE="${CC_SIM_TOKEN_FILE:-${CC_STATE_DIR}/mac-sim.token}"
CC_SIM_LOG="${CC_SIM_LOG:-${CC_STATE_DIR}/mac-sim-shim.log}"
CC_SIM_AUTO="${CC_SIM_AUTO:-1}"

# Extension points, for host-side tooling this project does not ship. (The
# mac-sim bridge above is the one exception, because it is inert until you
# declare a project; everything else of your own goes through these.)
#
#   CC_EXTRA_RUN_ARGS   extra `container run` arguments (--env, --volume, ...),
#                       applied to both one-off runs and the shared session
#   CC_PRE_RUN_HOOK     name of a shell function run on the host immediately
#                       before a container starts; use it to bring up whatever
#                       the session depends on
#   CC_POST_DOWN_HOOK   name of a function run by cc-down once no containers
#                       remain, to tear the same thing down
#
# All three are defined in config.sh. A hook that is named but not defined is an
# error, not a silent skip: a session that quietly starts without its
# dependencies is worse than one that refuses to start.
CC_EXTRA_RUN_ARGS=(${CC_EXTRA_RUN_ARGS+"${CC_EXTRA_RUN_ARGS[@]}"})

# --- user config ------------------------------------------------------------
[ -r "${CC_CONFIG_DIR}/config.sh" ] && . "${CC_CONFIG_DIR}/config.sh"

CC_SESSION_CPUS="${CC_SESSION_CPUS:-$(_cc_half_cpus)}"
CC_SESSION_MEMORY="${CC_SESSION_MEMORY:-$(_cc_half_memory_gb)g}"
CC_PROXY_URL="http://${CC_PROXY_HOST}:${CC_PROXY_PORT}"

CC_MCP_DIR="${CC_MCP_DIR:-${CC_CONFIG_DIR}/mcp}"
CC_PI_DIR="${CC_PI_DIR:-${CC_CONFIG_DIR}/pi}"
CC_MCP_ENV_FILE="${CC_MCP_ENV_FILE:-${CC_CONFIG_DIR}/mcp.env}"
CC_PROXY_PID_FILE="${CC_STATE_DIR}/tinyproxy.pid"
CC_EGRESS_STATE="${CC_STATE_DIR}/egress"
CC_IMAGE_VERSIONS="${CC_STATE_DIR}/image-versions"

# ---------------------------------------------------------------------------
# MCP servers
#
# ${CC_CONFIG_DIR}/mcp/mcp-servers.json is the declarative source of truth. The
# directory is mounted read-only at /opt/cc-mcp and merged into the guest's USER
# scope by guest/cc-mcp-sync at every session start, so `claude mcp list` and
# /mcp behave as if you had run `claude mcp add --scope user`. Project .mcp.json
# still applies on top; servers added by hand in the guest are left alone.
#
# It is mounted as a DIRECTORY, never as the single JSON file. An editor's
# atomic save replaces the host file's inode, and a single-file bind mount does
# not follow that: the guest stops seeing the file entirely, so the sync would
# silently no-op on every edit. A directory mount resolves the name at read time.
#
# mcp.env stays OUTSIDE that directory. Its KEY=VALUE lines are what the JSON
# references as ${KEY}, and they reach the guest as --env. Note what that does
# and does not buy you: cc-mcp-sync substitutes the value into the guest's
# ~/.claude.json (that is how `claude mcp list` and /mcp see the server), so the
# resolved secret DOES persist in the cc-home volume. The point of mcp.env is
# that the secret is not sitting in a JSON file you might commit or share.
# ---------------------------------------------------------------------------
_cc_mcp_args() {
  [ -r "${CC_MCP_DIR}/mcp-servers.json" ] || return 0
  printf '%s\0' --volume "${CC_MCP_DIR}:/opt/cc-mcp:ro"
  [ -r "${CC_MCP_ENV_FILE}" ] || return 0
  local line
  while IFS= read -r line || [ -n "${line}" ]; do
    case "${line}" in
      ''|'#'*) continue ;;
      [A-Za-z_]*=*) printf '%s\0' --env "${line}" ;;
      *) echo "cc-container: ignoring malformed line in mcp.env: ${line}" >&2 ;;
    esac
  done < "${CC_MCP_ENV_FILE}"
}

# ${CC_CONFIG_DIR}/pi/ holds the declarative global pi config (settings.json,
# keybindings.json, extensions/, prompts/). Mounted as a DIRECTORY for the same
# reason as the MCP list, and merged into ~/.pi/agent by guest/pi-config-sync.
_cc_pi_args() {
  [ -d "${CC_PI_DIR}" ] || return 0
  printf '%s\0' --volume "${CC_PI_DIR}:/opt/cc-pi:ro"
}

# The policy is a DIRECTORY mount for the same reason as the MCP list.
_cc_policy_collect() {
  CC_POLICY_ARGS=()
  case "${CC_SUBSCRIPTION_ONLY}" in
    0) return 0 ;;
    1) CC_POLICY_ARGS=(
         --volume "${CC_CONTAINER_REPO}/policy/subscription-only:/etc/claude-code:ro"
         --env CC_SUBSCRIPTION_ONLY=1
       ) ;;
    *) echo "cc-container: CC_SUBSCRIPTION_ONLY must be 0 or 1, got '${CC_SUBSCRIPTION_ONLY}'" >&2
       return 1 ;;
  esac
}

# Run the guard in the shared session before attaching an agent. It reads the
# session's own policy mount, so a session started before CC_SUBSCRIPTION_ONLY=1
# was set is refused rather than trusted.
_cc_attach_guard() {
  [ "${CC_SUBSCRIPTION_ONLY}" = "1" ] || return 0
  container exec --env CC_SUBSCRIPTION_ONLY=1 "${CC_SESSION_NAME}" \
    /opt/cc-tools/cc-subscription-guard
}

_cc_mcp_collect() {
  CC_MCP_ARGS=()
  local arg
  while IFS= read -r -d '' arg; do
    CC_MCP_ARGS+=("${arg}")
  done < <(_cc_mcp_args)
  while IFS= read -r -d '' arg; do
    CC_MCP_ARGS+=("${arg}")
  done < <(_cc_pi_args)
}

# Emit --dangerously-skip-permissions unless disabled, already passed, or the
# invocation is a `claude` subcommand that takes its own flags.
_cc_default_flags() {
  [ "${CC_SKIP_PERMISSIONS}" = "1" ] || return 0
  local arg
  for arg in "$@"; do
    case "${arg}" in
      --dangerously-skip-permissions|--help|-h|--version|-v) return 0 ;;
      config|mcp|update|install|migrate-installer|doctor|setup-token) return 0 ;;
    esac
  done
  echo "--dangerously-skip-permissions"
}

# ---------------------------------------------------------------------------
# Host-side helpers: runtime, proxy, sim shim
# ---------------------------------------------------------------------------
_cc_listening() { lsof -iTCP:"$1" -sTCP:LISTEN -n >/dev/null 2>&1; }

cc-runtime-up() {
  if container system status >/dev/null 2>&1; then return 0; fi
  echo "cc-container: container runtime not running, starting it..." >&2
  container system start >/dev/null 2>&1
  if ! container system status >/dev/null 2>&1; then
    echo "cc-container: failed to start the container runtime; run 'container system start' manually" >&2
    return 1
  fi
}

# Is the proxy wanted for this run?
_cc_proxy_wanted() {
  case "${CC_PROXY}" in
    on) return 0 ;;
    off) return 1 ;;
    auto) [ "$(cat "${CC_EGRESS_STATE}" 2>/dev/null)" = "proxy" ] ;;
    *) echo "cc-container: CC_PROXY must be auto|on|off, got '${CC_PROXY}'" >&2; return 1 ;;
  esac
}

cc-proxy-up() {
  _cc_listening "${CC_PROXY_PORT}" && return 0
  if ! command -v tinyproxy >/dev/null 2>&1; then
    echo "cc-container: tinyproxy is not installed but CC_PROXY resolves to on." >&2
    echo "  brew install tinyproxy   (or set CC_PROXY=off if your guests have direct egress)" >&2
    return 1
  fi
  tinyproxy -d -c "${CC_CONFIG_DIR}/tinyproxy.conf" \
    >"${CC_STATE_DIR}/tinyproxy.log" 2>&1 &
  echo "$!" > "${CC_PROXY_PID_FILE}"
  # Poll rather than sleeping a flat interval: it is usually ready well inside
  # the first tick, and a fixed sleep would tax every container start.
  local waited=0
  while [ "${waited}" -lt 20 ]; do
    _cc_listening "${CC_PROXY_PORT}" && return 0
    sleep 0.2
    waited=$((waited + 1))
  done
  echo "cc-container: proxy failed to start on port ${CC_PROXY_PORT}; see ${CC_STATE_DIR}/tinyproxy.log" >&2
  return 1
}

# Kill only the tinyproxy this tool started, never someone else's.
cc-proxy-down() {
  local pid
  pid="$(cat "${CC_PROXY_PID_FILE}" 2>/dev/null)"
  if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" 2>/dev/null
    rm -f "${CC_PROXY_PID_FILE}"
    return 0
  fi
  rm -f "${CC_PROXY_PID_FILE}"
  return 1
}

# Proxy env vars, emitted only when the proxy is actually in play.
_cc_proxy_env() {
  _cc_proxy_wanted || return 0
  printf '%s\0' --env "http_proxy=${CC_PROXY_URL}"
  printf '%s\0' --env "https_proxy=${CC_PROXY_URL}"
  printf '%s\0' --env "HTTP_PROXY=${CC_PROXY_URL}"
  printf '%s\0' --env "HTTPS_PROXY=${CC_PROXY_URL}"
  # The host bridge address stays direct: guest -> host TCP works, and the
  # mac-sim shim there is plain HTTP on a port the proxy does not CONNECT to.
  printf '%s\0' --env "no_proxy=localhost,127.0.0.1,${CC_PROXY_HOST}"
  printf '%s\0' --env "NO_PROXY=localhost,127.0.0.1,${CC_PROXY_HOST}"
  # Node's built-in fetch/http/https ignore the variables above unless asked.
  # The flag logs an "experimental" warning from every node process, which
  # would land in every tool's stderr, so that one warning is silenced.
  printf '%s\0' --env "NODE_USE_ENV_PROXY=1"
  printf '%s\0' --env "NODE_OPTIONS=--disable-warning=UNDICI-EHPA"
}

_cc_proxy_collect() {
  CC_PROXY_ARGS=()
  local arg
  while IFS= read -r -d '' arg; do CC_PROXY_ARGS+=("${arg}"); done < <(_cc_proxy_env)
  _cc_proxy_wanted && { cc-proxy-up || return 1; }
  return 0
}

# ---------------------------------------------------------------------------
# mac-sim: the host-side Xcode/simctl bridge
# ---------------------------------------------------------------------------
_cc_sim_configured() { [ -r "${CC_SIM_CONFIG}" ]; }

# Read the address from the config rather than keeping a second copy here: a
# divergence would leave this polling one port while the shim listened on
# another, reporting a failure to start when it had in fact started elsewhere.
_cc_sim_host() { jq -r '.listen.host // "192.168.64.1"' "${CC_SIM_CONFIG}" 2>/dev/null; }
_cc_sim_port() { jq -r '.listen.port // 8890'           "${CC_SIM_CONFIG}" 2>/dev/null; }

# Is the launch directory inside a declared project root? The shim can only act
# on a declared project, so starting it anywhere else would add host attack
# surface for nothing.
_cc_sim_in_project() {
  local root
  while IFS= read -r root; do
    [ -n "${root}" ] || continue
    root="${root/#\~/$HOME}"
    case "${PWD}/" in "${root}"/*) return 0 ;; esac
  done < <(jq -r '.projects[]?.root // empty' "${CC_SIM_CONFIG}" 2>/dev/null)
  return 1
}

# The shim binds to the vmnet gateway (192.168.64.1 by default). That address is
# only assigned while a container is running (it vanishes shortly after the last
# one stops), so on a cold start the bind fails with "Can't assign requested
# address". Hold a tiny probe container open while the shim binds; cc-up drops it
# once the session container has taken over keeping the bridge alive.
CC_SIM_PROBE_NAME="${CC_SIM_PROBE_NAME:-cc-bridge-probe}"
_cc_host_has_addr() { ifconfig 2>/dev/null | grep -q "inet $1 "; }

_cc_sim_release_probe() {
  container inspect "${CC_SIM_PROBE_NAME}" >/dev/null 2>&1 || return 0
  container stop "${CC_SIM_PROBE_NAME}" >/dev/null 2>&1
  container rm "${CC_SIM_PROBE_NAME}" >/dev/null 2>&1
  return 0
}

_cc_sim_ensure_bridge() {
  local host; host="$(_cc_sim_host)"
  _cc_host_has_addr "${host}" && return 0
  echo "cc-sim-up: ${host} is not assigned yet, starting a probe container to bring up the vmnet bridge..." >&2
  _cc_sim_release_probe
  if ! container run -d --name "${CC_SIM_PROBE_NAME}" --entrypoint /bin/sleep \
         "${CC_CONTAINER_IMAGE}" infinity >/dev/null 2>&1; then
    echo "cc-sim-up: could not start probe container ${CC_SIM_PROBE_NAME} from ${CC_CONTAINER_IMAGE}; run 'cc-container-build' if the image is missing" >&2
    return 1
  fi
  local _
  for _ in $(seq 1 50); do
    _cc_host_has_addr "${host}" && return 0
    sleep 0.2
  done
  _cc_sim_release_probe
  echo "cc-sim-up: ${host} is still not assigned after starting a container; the shim cannot bind to it." >&2
  echo "  Check 'ifconfig | grep 192.168.64' and 'container network inspect default'." >&2
  return 1
}

cc-sim-up() {
  if ! _cc_sim_configured; then
    echo "cc-sim-up: no config at ${CC_SIM_CONFIG}" >&2
    echo "Copy ${CC_CONTAINER_REPO}/config/mac-sim.example.json there, then declare a project root." >&2
    return 1
  fi
  local port; port="$(_cc_sim_port)"
  [ -n "${port}" ] || { echo "cc-sim-up: cannot read listen.port from ${CC_SIM_CONFIG}" >&2; return 1; }
  _cc_listening "${port}" && return 0
  _cc_sim_ensure_bridge || return 1
  # The token is shared by file, so the guest never has to be told a secret it
  # could log; it is generated once and reused across restarts.
  [ -s "${CC_SIM_TOKEN_FILE}" ] \
    || (umask 077; head -c 32 /dev/urandom | xxd -p -c 64 > "${CC_SIM_TOKEN_FILE}")
  MAC_SIM_CONFIG="${CC_SIM_CONFIG}" MAC_SIM_TOKEN_FILE="${CC_SIM_TOKEN_FILE}" \
    python3 "${CC_CONTAINER_REPO}/host/mac-sim-shim.py" >"${CC_SIM_LOG}" 2>&1 &
  local _
  for _ in $(seq 1 30); do
    _cc_listening "${port}" && break
    sleep 0.1
  done
  _cc_listening "${port}" \
    || { echo "cc-sim-up: shim failed to start; see ${CC_SIM_LOG}" >&2; return 1; }
  echo "mac-sim: shim listening on $(_cc_sim_host):${port}"
}

cc-sim-down() {
  pkill -f "mac-sim-shim.py" 2>/dev/null && echo "stopped:  mac-sim shim"
  _cc_sim_release_probe
  return 0
}

cc-sim-log() {
  [ -r "${CC_SIM_LOG}" ] || { echo "cc-sim-log: no log at ${CC_SIM_LOG}" >&2; return 1; }
  if [ "$#" -eq 0 ]; then tail -n 50 "${CC_SIM_LOG}"; else tail "$@" "${CC_SIM_LOG}"; fi
}

# Run before every container start. Kept in its own array rather than appended
# to CC_EXTRA_RUN_ARGS: that one belongs to config.sh, and appending to it would
# accumulate duplicate --env flags across repeated cc-up calls in one shell.
CC_SIM_RUN_ARGS=()
_cc_sim_auto_up() {
  CC_SIM_RUN_ARGS=()
  [ "${CC_SIM_AUTO}" = "1" ] || return 0
  _cc_sim_configured || return 0
  # Outside every declared root: no shim, and no env pointing at one, so
  # `mac-sim` in the guest reports plainly that it has nothing to talk to.
  _cc_sim_in_project || return 0
  cc-sim-up || return 1
  CC_SIM_RUN_ARGS=(
    --env "MAC_SIM_SHIM=http://$(_cc_sim_host):$(_cc_sim_port)"
    --env "MAC_SIM_TOKEN=$(cat "${CC_SIM_TOKEN_FILE}")"
  )
}

_cc_sim_auto_down() {
  [ "${CC_SIM_AUTO}" = "1" ] || return 0
  _cc_sim_configured || return 0
  cc-sim-down
}

# Run a named host-side hook, if config.sh declared one.
_cc_call_hook() {
  [ -n "$1" ] || return 0
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "cc-container: ${2} names '$1', which is not a defined function" >&2
    return 1
  fi
  "$1"
}

# ---------------------------------------------------------------------------
# One-off sessions
# ---------------------------------------------------------------------------
# Keeps the Mac out of idle sleep while a session runs, then lets it sleep as
# usual. Nothing in the VM can assert this (the guest has no caffeinate), and
# idle sleep suspends the VM, so an unattended agent run would stop a minute
# after the last keypress on a Mac with a short sleep timer. A closed lid still
# sleeps unless the Mac is on power with an external display.
_cc_awake() { caffeinate -i "$@"; }

# The Mac's terminfo entry for its TERM, for guest/cc-terminfo-sync to install
# where the guest lacks one. Empty when the Mac has none either; the guest
# script then fails only if it needs the entry.
CC_TERMINFO_ARGS=()
_cc_terminfo_collect() {
  CC_TERMINFO_ARGS=()
  [ -n "${TERM:-}" ] || return 0
  local src
  src="$(infocmp -x "${TERM}" 2>/dev/null)" || return 0
  CC_TERMINFO_ARGS=(--env "CC_TERMINFO_SRC=${src}")
}

_cc_run() {
  local entrypoint_args=() default_flags=() agent_env=()
  case "$1" in
    --shell)
      shift
      _cc_terminfo_collect
      entrypoint_args=(${CC_TERMINFO_ARGS+"${CC_TERMINFO_ARGS[@]}"}
                       --entrypoint /opt/cc-tools/shell-entry) ;;
    --pi)
      # pi never asks before a tool call, so it needs no counterpart of
      # --dangerously-skip-permissions: the VM is already its only boundary.
      shift
      entrypoint_args=(--entrypoint /opt/cc-tools/pi-entry)
      agent_env=(env HERDR_AGENT=pi) ;;
    *)
      # cc-entry syncs the MCP server list, then execs claude with these args.
      entrypoint_args=(--entrypoint /opt/cc-tools/cc-entry)
      agent_env=(env HERDR_AGENT=claude)
      local flag; flag="$(_cc_default_flags "$@")"
      [ -n "${flag}" ] && default_flags=("${flag}") ;;
  esac
  cc-runtime-up || return 1
  _cc_proxy_collect || return 1
  _cc_sim_auto_up || return 1
  _cc_call_hook "${CC_PRE_RUN_HOOK}" CC_PRE_RUN_HOOK || return 1
  _cc_mcp_collect
  _cc_policy_collect || return 1
  # -it needs a real TTY; fall back for piped/scripted use (e.g. claude -p).
  local tty_args=()
  if [ -t 0 ] && [ -t 1 ]; then tty_args=(-it); fi
  _cc_awake ${agent_env+"${agent_env[@]}"} container run --rm "${tty_args[@]}" \
    --volume "${PWD}:${PWD}" \
    --volume "cc-home:/root" \
    --volume "${CC_CONTAINER_REPO}/guest:/opt/cc-tools:ro" \
    ${CC_EXTRA_VOLUME_ARGS+"${CC_EXTRA_VOLUME_ARGS[@]}"} \
    ${CC_MCP_ARGS+"${CC_MCP_ARGS[@]}"} \
    ${CC_EXTRA_RUN_ARGS+"${CC_EXTRA_RUN_ARGS[@]}"} \
    ${CC_SIM_RUN_ARGS+"${CC_SIM_RUN_ARGS[@]}"} \
    ${CC_PROXY_ARGS+"${CC_PROXY_ARGS[@]}"} \
    ${CC_POLICY_ARGS+"${CC_POLICY_ARGS[@]}"} \
    --workdir "${PWD}" \
    --dns 8.8.8.8 \
    --env TERM \
    --env "CC_HOST_MOUNT=${PWD}" \
    "${entrypoint_args[@]}" \
    "${CC_CONTAINER_IMAGE}" ${default_flags+"${default_flags[@]}"} "$@"
}

cc-container() { _cc_run "$@"; }
cc-shell()     { _cc_run --shell "$@"; }
pi-container() { _cc_run --pi "$@"; }

# ---------------------------------------------------------------------------
# Multi-session mode
#
# Only one container can hold the cc-home volume at a time, so parallel
# `cc-container` runs fail with "storage device attachment is invalid". To run
# several Claude Code instances at once, start ONE long-lived container and
# attach to it from as many terminals as you like.
#
#   cc-up            # once, from the project directory
#   cc-attach        # in each terminal window (pi-attach for pi)
#   cc-down          # when finished
# ---------------------------------------------------------------------------
_cc_session_state() {
  container inspect "${CC_SESSION_NAME}" 2>/dev/null \
    | jq -r '.[0].status.state // empty' 2>/dev/null
}

cc-up() {
  cc-runtime-up || return 1
  _cc_proxy_collect || return 1
  _cc_sim_auto_up || return 1
  _cc_call_hook "${CC_PRE_RUN_HOOK}" CC_PRE_RUN_HOOK || return 1
  _cc_mcp_collect
  _cc_policy_collect || return 1
  local state; state="$(_cc_session_state)"
  if [ "${state}" = "running" ]; then
    echo "${CC_SESSION_NAME} already running. Attach with: cc-attach"
    return 0
  fi
  if [ -n "${state}" ]; then
    echo "Restarting existing ${CC_SESSION_NAME}..."
    container start "${CC_SESSION_NAME}" || return 1
  else
    container run -d --name "${CC_SESSION_NAME}" \
      --cpus "${CC_SESSION_CPUS}" \
      --memory "${CC_SESSION_MEMORY}" \
      --volume "${PWD}:${PWD}" \
      --volume "cc-home:/root" \
      --volume "${CC_CONTAINER_REPO}/guest:/opt/cc-tools:ro" \
      ${CC_EXTRA_VOLUME_ARGS+"${CC_EXTRA_VOLUME_ARGS[@]}"} \
      ${CC_MCP_ARGS+"${CC_MCP_ARGS[@]}"} \
      ${CC_EXTRA_RUN_ARGS+"${CC_EXTRA_RUN_ARGS[@]}"} \
    ${CC_SIM_RUN_ARGS+"${CC_SIM_RUN_ARGS[@]}"} \
      ${CC_PROXY_ARGS+"${CC_PROXY_ARGS[@]}"} \
      ${CC_POLICY_ARGS+"${CC_POLICY_ARGS[@]}"} \
      --workdir "${PWD}" \
      --dns 8.8.8.8 \
      --env "CC_HOST_MOUNT=${PWD}" \
      --entrypoint /bin/bash \
      "${CC_CONTAINER_IMAGE}" -c '/opt/cc-tools/cc-workspace-link && exec sleep infinity' || return 1
  fi
  _cc_sim_release_probe
  echo "${CC_SESSION_NAME} up, mounting ${PWD}. Attach with: cc-attach"
}

# The host path the shared session mounts, read from the container itself
# rather than from $PWD: cc-up may have run in another directory, and the
# mount is fixed for the container's lifetime.
_cc_session_mount() {
  local mount
  mount="$(container inspect "${CC_SESSION_NAME}" 2>/dev/null \
    | jq -r '.[0].configuration.initProcess.environment[]? | select(startswith("CC_HOST_MOUNT=")) | sub("^CC_HOST_MOUNT="; "")' 2>/dev/null)"
  if [ -z "${mount}" ]; then
    echo "cc-container: ${CC_SESSION_NAME} has no CC_HOST_MOUNT, so it predates the host-path mount (/workspace era). Recreate it with: ccdown && ccup" >&2
    return 1
  fi
  printf '%s\n' "${mount}"
}

# Where an attach starts: the caller's own directory when it is inside the
# mount (a worktree pane starts its agent in that worktree), else the mount root.
_cc_attach_workdir() {
  local mount; mount="$(_cc_session_mount)" || return 1
  case "${PWD}/" in
    "${mount}"/*) printf '%s\n' "${PWD}" ;;
    *) printf '%s\n' "${mount}" ;;
  esac
}

cc-attach() {
  if [ "$(_cc_session_state)" != "running" ]; then
    echo "cc-attach: ${CC_SESSION_NAME} is not running. Start it with cc-up." >&2
    return 1
  fi
  local tty_args=()
  if [ -t 0 ] && [ -t 1 ]; then tty_args=(-it); fi
  local default_flags=() flag
  flag="$(_cc_default_flags "$@")"
  [ -n "${flag}" ] && default_flags=("${flag}")
  # Re-apply the MCP list on every attach, not just at cc-up: the shared
  # container is long-lived, so this is where an mcp-servers.json edit lands.
  # The mount is fixed at cc-up, so a NEW ${VAR} still needs cc-down && cc-up.
  local workdir; workdir="$(_cc_attach_workdir)" || return 1
  _cc_attach_guard || return 1
  container exec "${CC_SESSION_NAME}" /opt/cc-tools/cc-mcp-sync || return 1
  # HERDR_AGENT tells herdr (if this runs in a herdr pane) that the foreground
  # job is Claude Code: the claude process itself is inside the VM, invisible
  # to herdr's host-side process detection.
  _cc_awake env HERDR_AGENT=claude container exec "${tty_args[@]}" --env TERM \
    --workdir "${workdir}" \
    "${CC_SESSION_NAME}" claude ${default_flags+"${default_flags[@]}"} "$@"
}

# The pi counterpart of cc-attach. Both agents share the container and the
# cc-home volume, so pi's state lives in /root/.pi/agent beside ~/.claude.
pi-attach() {
  if [ "$(_cc_session_state)" != "running" ]; then
    echo "pi-attach: ${CC_SESSION_NAME} is not running. Start it with cc-up." >&2
    return 1
  fi
  local tty_args=()
  if [ -t 0 ] && [ -t 1 ]; then tty_args=(-it); fi
  _cc_attach_guard || return 1
  container exec "${CC_SESSION_NAME}" /opt/cc-tools/cc-mcp-sync || return 1
  container exec "${CC_SESSION_NAME}" /opt/cc-tools/pi-config-sync || return 1
  # -C <dir> starts pi in a subdirectory of the project root, e.g. a worktree.
  local workdir; workdir="$(_cc_attach_workdir)" || return 1
  if [ "${1:-}" = "-C" ]; then
    local mount; mount="$(_cc_session_mount)" || return 1
    workdir="${mount}/${2:?pi-attach: -C needs a directory relative to the project root}"
    shift 2
  fi
  _cc_awake env HERDR_AGENT=pi container exec "${tty_args[@]}" --env TERM \
    --workdir "${workdir}" \
    "${CC_SESSION_NAME}" pi "$@"
}

cc-attach-shell() {
  if [ "$(_cc_session_state)" != "running" ]; then
    echo "cc-attach-shell: ${CC_SESSION_NAME} is not running. Start it with cc-up." >&2
    return 1
  fi
  local tty_args=()
  if [ -t 0 ] && [ -t 1 ]; then tty_args=(-it); fi
  local workdir; workdir="$(_cc_attach_workdir)" || return 1
  _cc_terminfo_collect
  container exec --env TERM ${CC_TERMINFO_ARGS+"${CC_TERMINFO_ARGS[@]}"} \
    "${CC_SESSION_NAME}" /opt/cc-tools/cc-terminfo-sync || return 1
  _cc_awake container exec "${tty_args[@]}" --env TERM --workdir "${workdir}" \
    "${CC_SESSION_NAME}" /bin/bash "$@"
}

# From a herdr pane: create a git worktree under <repo>/.claude/worktrees/<name>
# (branch worktree-<name>, as `claude --worktree` names it), open it as a herdr
# workspace grouped under the repo, and run <attach> (ccx or pix) in it. The
# worktree is created on the Mac; it is valid in the guest because the project
# is mounted at the same path there. herdr's own "New worktree" places
# checkouts in [worktrees] directory, outside the mount, where the guest cannot
# see them.
# Usage: _cc_worktree <caller> <attach> <name> [agent args...]
_cc_worktree() {
  local caller="$1" attach="$2"
  shift 2
  if [ -z "${1:-}" ]; then
    echo "${caller}: usage: ${caller} <name> [agent args...]" >&2
    return 1
  fi
  local name="$1"
  shift
  if [ "${HERDR_ENV:-}" != "1" ]; then
    echo "${caller}: run this from a herdr pane (HERDR_ENV is not 1); it opens the worktree as a herdr workspace" >&2
    return 1
  fi
  if [ "$(_cc_session_state)" != "running" ]; then
    echo "${caller}: ${CC_SESSION_NAME} is not running. Start it with cc-up." >&2
    return 1
  fi
  local common root mount
  common="$(git rev-parse --path-format=absolute --git-common-dir)" || return 1
  root="$(dirname "${common}")"
  mount="$(_cc_session_mount)" || return 1
  case "${root}/" in
    "${mount}"/*) ;;
    *) echo "${caller}: repo ${root} is outside the session mount ${mount}, so the guest could not see the worktree" >&2
       return 1 ;;
  esac
  local created pane
  created="$(herdr worktree create --cwd "${root}" --branch "worktree-${name}" \
    --path "${root}/.claude/worktrees/${name}" --label "${name}" --focus)" || return 1
  pane="$(printf '%s' "${created}" | jq -r '.result.root_pane.pane_id // empty')"
  if [ -z "${pane}" ]; then
    echo "${caller}: herdr worktree create returned no root pane id: ${created}" >&2
    return 1
  fi
  local cmd="${attach}"
  [ "$#" -gt 0 ] && cmd="${attach} $(printf '%q ' "$@")"
  herdr pane run "${pane}" "${cmd}"
}

cc-worktree() { _cc_worktree cc-worktree ccx "$@"; }
pi-worktree() { _cc_worktree pi-worktree pix "$@"; }

# Running containers other than the shared session and the build helper, i.e.
# anything that might still need the proxy or the sim shim.
_cc_other_containers() {
  container list --format json 2>/dev/null \
    | jq -r --arg name "${CC_SESSION_NAME}" \
        '[.[] | select(.status == "running")
              | .configuration.id // .id
              | select(. != $name and (startswith("buildkit") | not))] | length' \
      2>/dev/null || echo 0
}

cc-down() {
  container stop "${CC_SESSION_NAME}" 2>/dev/null
  sleep 2
  container delete "${CC_SESSION_NAME}" 2>/dev/null
  echo "${CC_SESSION_NAME} removed."
  # Leave the host-side helpers up if anything else might still need them: a
  # one-off `ccrun` in another terminal would lose its network otherwise.
  if [ "$(_cc_other_containers)" -gt 0 ] 2>/dev/null; then
    echo "host helpers: left running (other containers still up)"
    return 0
  fi
  _cc_listening "${CC_PROXY_PORT}" && cc-proxy-down >/dev/null 2>&1 && echo "stopped:  proxy"
  _cc_sim_auto_down
  _cc_call_hook "${CC_POST_DOWN_HOOK}" CC_POST_DOWN_HOOK
  return 0
}

cc-status() {
  echo "runtime:  $(container system status 2>/dev/null | awk '/status/{print $NF; exit}')"
  case "${CC_PROXY}" in
    off) echo "proxy:    disabled (CC_PROXY=off)" ;;
    *)
      if _cc_listening "${CC_PROXY_PORT}"; then
        echo "proxy:    listening on ${CC_PROXY_URL} (mode=${CC_PROXY})"
      else
        echo "proxy:    down (mode=${CC_PROXY}; direct egress assumed, run cc-doctor to verify)"
      fi ;;
  esac
  if _cc_sim_configured; then
    local sim_port; sim_port="$(_cc_sim_port)"
    if _cc_listening "${sim_port}"; then
      echo "sim:      shim listening on $(_cc_sim_host):${sim_port}"
    else
      echo "sim:      down (starts with ccup from inside a declared project root)"
    fi
  fi
  # Parse `container inspect` rather than scraping `container list` columns: a
  # stopped container has no IP, so column offsets shift and produce garbage.
  local info
  info="$(container inspect "${CC_SESSION_NAME}" 2>/dev/null \
    | jq -r '.[0] | [
        .status.state,
        (.configuration.resources.cpus|tostring),
        ((.configuration.resources.memoryInBytes / 1073741824)|floor|tostring),
        ([.configuration.mounts[]?.source | select(test("cc-home")|not)]|first // "-")
      ] | @tsv' 2>/dev/null)"
  if [ -z "${info}" ]; then
    echo "session:  not running (start with: ccup)"
    return 0
  fi
  local state cpus mem mount
  IFS="$(printf '\t')" read -r state cpus mem mount <<< "${info}"
  echo "session:  ${CC_SESSION_NAME} ${state} | ${cpus} cpus, ${mem} GB"
  echo "mount:    ${mount}"
  if [ "${state}" = "running" ]; then echo "attach:   ccx (Claude Code), pix (pi)"; else echo "start:    ccup   (restarts it)"; fi
}

# ---------------------------------------------------------------------------
# cc-doctor: check every prerequisite and record whether guests have direct
# internet egress. This is the only place that pays for the egress probe, so the
# hot path stays fast.
# ---------------------------------------------------------------------------
cc-doctor() {
  local ok=0
  _p() { printf '%-22s %s\n' "$1" "$2"; }

  case "$(uname -m)" in
    arm64) _p "architecture" "arm64 ok" ;;
    *) _p "architecture" "FAIL $(uname -m) - apple/container requires Apple silicon"; ok=1 ;;
  esac

  if command -v container >/dev/null 2>&1; then
    _p "container cli" "$(container --version 2>&1 | head -1)"
  else
    _p "container cli" "FAIL not installed - brew install container"; ok=1
  fi
  if container system status >/dev/null 2>&1; then
    _p "runtime" "running"
  else
    _p "runtime" "stopped - run: container system start"; ok=1
  fi

  if container image list 2>/dev/null | grep -q "${CC_CONTAINER_IMAGE%%:*}"; then
    _p "image" "${CC_CONTAINER_IMAGE} present"
    local versions
    versions="$(container run --rm --entrypoint /bin/sh "${CC_CONTAINER_IMAGE}" -c \
      'claude --version | cut -d" " -f1; pi --version 2>/dev/null || echo none' 2>/dev/null)"
    _p "claude code" "$(echo "${versions}" | sed -n 1p)"
    case "$(echo "${versions}" | sed -n 2p)" in
      none|'') _p "pi" "not in image - run: cc-container-build" ;;
      *) _p "pi" "$(echo "${versions}" | sed -n 2p)" ;;
    esac
  else
    _p "image" "MISSING - run: cc-container-build"; ok=1
  fi

  for tool in jq python3; do
    command -v "${tool}" >/dev/null 2>&1 \
      && _p "${tool}" "ok" || { _p "${tool}" "FAIL not on PATH"; ok=1; }
  done

  _p "config dir" "${CC_CONFIG_DIR}"
  [ -r "${CC_MCP_DIR}/mcp-servers.json" ] \
    && _p "mcp servers" "$(jq -r '[.mcpServers|keys[]]|join(", ")' "${CC_MCP_DIR}/mcp-servers.json" 2>/dev/null)" \
    || _p "mcp servers" "none declared"

  # Egress probe: the answer decides whether CC_PROXY=auto uses the proxy.
  if [ "${ok}" = "0" ]; then
    printf '%-22s %s' "egress probe" "testing direct... "
    if container run --rm --dns 8.8.8.8 --entrypoint /bin/sh \
         "${CC_CONTAINER_IMAGE}" -c 'curl -sfI -m 10 https://registry.npmjs.org >/dev/null' \
         >/dev/null 2>&1; then
      echo "direct egress works"
      echo direct > "${CC_EGRESS_STATE}"
    else
      echo "BLOCKED"
      echo "  Guests cannot reach the internet directly. This is usually a host"
      echo "  security stack filtering the vmnet NAT path (see docs/NETWORK.md)."
      if command -v tinyproxy >/dev/null 2>&1; then
        echo proxy > "${CC_EGRESS_STATE}"
        echo "  Recorded 'proxy': CC_PROXY=auto will now route through the host proxy."
      else
        echo "  Install tinyproxy (brew install tinyproxy) and re-run cc-doctor."
        ok=1
      fi
    fi
  fi
  _p "egress mode" "$(cat "${CC_EGRESS_STATE}" 2>/dev/null || echo unknown) (CC_PROXY=${CC_PROXY})"
  unset -f _p
  return "${ok}"
}

# ---------------------------------------------------------------------------
# Image build / upgrade
# ---------------------------------------------------------------------------
# Build the image. Pass a Claude Code version, a pi version, or both to pin
# them; an empty argument keeps that agent's previous version, e.g.
#   cc-container-build 2.1.220
#   cc-container-build "" 1.2.0
#   cc-container-build latest latest    # back to unpinned
#
# The versions of the last successful build are saved in ${CC_IMAGE_VERSIONS}
# and reused for any argument left empty. Without that, upgrading one agent would
# rebuild with the other's default ("latest"), silently dropping its pin. The
# versions are the npm install layers' cache keys, so passing the same value
# again replays the cached layer rather than reinstalling.
#
# If ~/.config/cc-container/Dockerfile.local exists, the repo image is built as
# <name>:base and your file is built on top of it as the real image. That is the
# supported way to add tools (extra MCP server packages, another runtime, a
# headless browser) without forking the repo's Dockerfile and without this
# project needing a flag per use case. Start it with:
#
#   ARG BASE_IMAGE
#   FROM ${BASE_IMAGE}
#   RUN npm install -g @some/mcp-server
#
# Its build context is the config directory, so it can COPY files from there.
_cc_saved_version() {
  [ -r "${CC_IMAGE_VERSIONS}" ] || return 0
  sed -n "s/^$1=//p" "${CC_IMAGE_VERSIONS}" | tail -1
}

_cc_save_versions() {
  { [ -n "$1" ] && echo "CC_VERSION=$1"
    [ -n "$2" ] && echo "PI_VERSION=$2"
  } > "${CC_IMAGE_VERSIONS}" \
    || { echo "cc-container-build: could not write ${CC_IMAGE_VERSIONS}" >&2; return 1; }
}

cc-container-build() {
  cc-runtime-up || return 1
  _cc_proxy_collect || return 1
  local cc_version="${1:-$(_cc_saved_version CC_VERSION)}"
  local pi_version="${2:-$(_cc_saved_version PI_VERSION)}"
  local args=()
  [ -n "${cc_version}" ] && args+=(--build-arg "CC_VERSION=${cc_version}")
  [ -n "${pi_version}" ] && args+=(--build-arg "PI_VERSION=${pi_version}")
  if _cc_proxy_wanted; then
    args+=(--build-arg "http_proxy=${CC_PROXY_URL}" --build-arg "https_proxy=${CC_PROXY_URL}"
           --build-arg "HTTP_PROXY=${CC_PROXY_URL}" --build-arg "HTTPS_PROXY=${CC_PROXY_URL}")
  fi
  local overlay="${CC_CONFIG_DIR}/Dockerfile.local"
  if [ ! -r "${overlay}" ]; then
    container build ${args+"${args[@]}"} \
      -t "${CC_CONTAINER_IMAGE}" "${CC_CONTAINER_REPO}/image" || return 1
    _cc_save_versions "${cc_version}" "${pi_version}"
    return
  fi
  local base="${CC_CONTAINER_IMAGE%%:*}:base"
  echo "Building ${base} from the repo Dockerfile..."
  container build ${args+"${args[@]}"} \
    -t "${base}" "${CC_CONTAINER_REPO}/image" || return 1
  # The versions live in the base's layers, so they are recorded once it builds,
  # even if the overlay below then fails.
  _cc_save_versions "${cc_version}" "${pi_version}"
  echo "Applying ${overlay} on top, as ${CC_CONTAINER_IMAGE}..."
  container build ${args+"${args[@]}"} --build-arg "BASE_IMAGE=${base}" \
    -f "${overlay}" -t "${CC_CONTAINER_IMAGE}" "${CC_CONFIG_DIR}"
}

# ---------------------------------------------------------------------------
# Staying in sync with the repo
#
# The shell library, the guest scripts and the Dockerfile are all read from the
# repo working tree at run time, so `git pull` is the whole update for most
# changes -- a new shell means new behaviour. The two that need a follow-up are
# a changed Dockerfile (rebuild) and a new config key (nothing reads it until
# you add it). cc-update does the pull and tells you which applies.
# ---------------------------------------------------------------------------
_cc_repo_git() { git -C "${CC_CONTAINER_REPO}" "$@"; }

# Config keys the examples define that the user's own config never mentions.
# Purely informational: a missing key just means the built-in default applies.
_cc_config_drift() {
  local example="${CC_CONTAINER_REPO}/config/config.example.sh"
  local mine="${CC_CONFIG_DIR}/config.sh"
  [ -r "${example}" ] && [ -r "${mine}" ] || return 0
  local key missing=""
  for key in $(grep -oE '^# *(CC_[A-Z_]+)=' "${example}" | tr -d '# =' | sort -u); do
    grep -qE "^[[:space:]]*(export )?${key}=" "${mine}" || missing="${missing} ${key}"
  done
  [ -n "${missing}" ] && echo "  new settings available (defaults apply until set):${missing}"
  return 0
}

cc-update() {
  if ! _cc_repo_git rev-parse --git-dir >/dev/null 2>&1; then
    echo "cc-update: ${CC_CONTAINER_REPO} is not a git checkout" >&2
    return 1
  fi
  if [ -n "$(_cc_repo_git status --porcelain)" ]; then
    echo "cc-update: ${CC_CONTAINER_REPO} has uncommitted changes; commit or stash first" >&2
    _cc_repo_git status --short >&2
    return 1
  fi
  local before after
  before="$(_cc_repo_git rev-parse HEAD)"
  _cc_repo_git pull --ff-only || return 1
  after="$(_cc_repo_git rev-parse HEAD)"
  if [ "${before}" = "${after}" ]; then
    echo "cc-container: already up to date ($(_cc_repo_git rev-parse --short HEAD))"
    _cc_config_drift
    return 0
  fi
  echo "cc-container: ${before:0:7} -> ${after:0:7}"
  _cc_repo_git log --oneline "${before}..${after}" | sed 's/^/  /'
  local changed
  changed="$(_cc_repo_git diff --name-only "${before}" "${after}")"
  echo "${changed}" | sed 's/^/  changed: /'
  _cc_config_drift
  # The image is the only artefact a pull cannot update on its own.
  if echo "${changed}" | grep -q '^image/'; then
    echo "  image inputs changed - rebuilding"
    cc-container-build || return 1
    if [ "$(_cc_session_state)" = "running" ]; then
      echo "  the running session still uses the OLD image; recycle it: ccdown && ccup"
    fi
  fi
  # A changed shell library only takes effect in a new shell.
  if echo "${changed}" | grep -q '^bin/'; then
    echo "  shell library changed - open a new shell, or: source ${CC_CONTAINER_REPO}/bin/cc-container.sh"
  fi
  # Guest scripts are bind-mounted, so they are live for the next session.
  if echo "${changed}" | grep -q '^guest/'; then
    echo "  guest tools changed - picked up automatically on the next session"
  fi
}

ccupd() { cc-update "$@"; }

# Upgrade Claude Code in the image. A plain rebuild does NOT upgrade: the npm
# install sits in a cached layer and replays the old version. Resolving the
# latest version and passing it as --build-arg changes that layer's cache key,
# forcing a real reinstall while leaving the apt layers cached.
#
# Resolved inside the guest rather than on the host, so the answer comes over the
# same network path (and proxy) the build will use.
_cc_latest_npm_version() {
  local version
  version="$(container run --rm --dns 8.8.8.8 \
    ${CC_PROXY_ARGS+"${CC_PROXY_ARGS[@]}"} \
    --entrypoint /bin/bash "${CC_CONTAINER_IMAGE}" \
    -c "npm view '$1' version 2>/dev/null | tail -1" | tr -d '\r\n')"
  if [ -z "${version}" ]; then
    echo "cc-container: could not resolve the latest version of $1 (no egress? run cc-doctor)" >&2
    return 1
  fi
  echo "${version}"
}

cc-container-upgrade() {
  cc-runtime-up || return 1
  _cc_proxy_collect || return 1
  local target="$1"
  if [ -z "${target}" ]; then
    echo "Resolving latest @anthropic-ai/claude-code..."
    target="$(_cc_latest_npm_version @anthropic-ai/claude-code)" || return 1
  fi
  echo "Building ${CC_CONTAINER_IMAGE} with Claude Code ${target}..."
  cc-container-build "${target}"
}

# Upgrade pi in the image, by the same cache-key mechanism as above.
pi-container-upgrade() {
  cc-runtime-up || return 1
  _cc_proxy_collect || return 1
  local target="$1"
  if [ -z "${target}" ]; then
    echo "Resolving latest @earendil-works/pi-coding-agent..."
    target="$(_cc_latest_npm_version @earendil-works/pi-coding-agent)" || return 1
  fi
  echo "Building ${CC_CONTAINER_IMAGE} with pi ${target}..."
  cc-container-build "" "${target}"
}

# Short names. Functions rather than aliases so they work in non-interactive
# shells and scripts too (zsh does not expand aliases there).
ccx()    { cc-attach "$@"; }
ccxs()   { cc-attach-shell "$@"; }
ccwt()   { cc-worktree "$@"; }
ccup()   { cc-up "$@"; }
ccdown() { cc-down "$@"; }
ccst()   { cc-status "$@"; }
ccrun()  { cc-container "$@"; }
ccsh()   { cc-shell "$@"; }
pix()    { pi-attach "$@"; }
piwt()   { pi-worktree "$@"; }
pirun()  { pi-container "$@"; }
