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
# Half the cores (at least 1) and half the RAM, kept between 4 and 8 GB. The VM
# keeps every guest page it has touched until it stops (guest page cache
# included), so over a long session the memory cap becomes what the Mac pays,
# taken from the simulators running beside it.
_cc_half_cpus() {
  local n; n="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
  [ "$((n / 2))" -ge 1 ] && echo "$((n / 2))" || echo 1
}
_cc_default_memory_gb() {
  local b g; b="$(sysctl -n hw.memsize 2>/dev/null || echo 8589934592)"
  g="$((b / 1073741824 / 2))"
  [ "${g}" -lt 4 ] && g=4
  [ "${g}" -gt 8 ] && g=8
  echo "${g}"
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

# Free space on the Mac's data volume, in GB. The VM disks, the home volumes,
# the images and every mounted checkout all live there, and when it fills, a
# running guest's root file system hits I/O errors and turns read-only until
# the container is recreated. Starting a container or a build below
# CC_DISK_MIN_GB is refused; below CC_DISK_WARN_GB every command warns.
# CC_DISK_MIN_GB=0 turns the refusal off.
CC_DISK_WARN_GB="${CC_DISK_WARN_GB:-25}"
CC_DISK_MIN_GB="${CC_DISK_MIN_GB:-10}"

# The named volume mounted as the container's home (/root): both agents'
# logins, settings and caches. Only one running container can hold a named
# volume, so a profile that runs beside the shared session needs its own.
CC_HOME_VOLUME="${CC_HOME_VOLUME:-cc-home}"

# 1 mounts the declarative MCP list (and passes mcp.env) and the pi config into
# every container; 0 leaves both out, so neither the servers nor their secrets
# reach it. A profile for unattended work usually wants 0.
CC_MCP="${CC_MCP:-1}"
CC_PI_SYNC="${CC_PI_SYNC:-1}"

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

# host/bin holds commands for the Mac itself: `mac-sim` there drives the same
# shim from a Mac-side tool. Appended, and only once, so it never shadows a
# command of yours and re-sourcing this file does not stack copies.
case ":${PATH}:" in
  *":${CC_CONTAINER_REPO}/host/bin:"*) ;;
  *) PATH="${PATH}:${CC_CONTAINER_REPO}/host/bin" ;;
esac

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
# The shell keeps what config.sh said when this file was sourced; cc-up refuses
# to create a session from values the file no longer has.
_CC_CONFIG_LOADED_MTIME="$(stat -f %m "${CC_CONFIG_DIR}/config.sh" 2>/dev/null)"

# --- profile ----------------------------------------------------------------
# CC_PROFILE=<name> layers ${CC_CONFIG_DIR}/profiles/<name>.sh over config.sh:
# a second container with its own name, home volume, image, mounts and size,
# running beside the shared session. It is set by cc-profile, which sources this
# file in a fresh shell: every setting is bound when this file is sourced, so a
# shell that loaded the default config cannot switch to a profile in place.
CC_PROFILE="${CC_PROFILE:-}"
if [ -n "${CC_PROFILE}" ]; then
  case "${CC_PROFILE}" in
    *[!A-Za-z0-9_-]*)
      echo "cc-container: CC_PROFILE must be letters, digits, '-' or '_', got '${CC_PROFILE}'" >&2
      return 1 ;;
  esac
  if [ ! -r "${CC_CONFIG_DIR}/profiles/${CC_PROFILE}.sh" ]; then
    echo "cc-container: CC_PROFILE=${CC_PROFILE} but ${CC_CONFIG_DIR}/profiles/${CC_PROFILE}.sh does not exist." >&2
    echo "  Start one from ${CC_CONTAINER_REPO}/config/profile.example.sh" >&2
    return 1
  fi
  # shellcheck source=/dev/null
  . "${CC_CONFIG_DIR}/profiles/${CC_PROFILE}.sh"
fi

CC_SESSION_CPUS="${CC_SESSION_CPUS:-$(_cc_half_cpus)}"
CC_SESSION_MEMORY="${CC_SESSION_MEMORY:-$(_cc_default_memory_gb)g}"
CC_PROXY_URL="http://${CC_PROXY_HOST}:${CC_PROXY_PORT}"

CC_MCP_DIR="${CC_MCP_DIR:-${CC_CONFIG_DIR}/mcp}"
CC_PI_DIR="${CC_PI_DIR:-${CC_CONFIG_DIR}/pi}"
CC_MCP_ENV_FILE="${CC_MCP_ENV_FILE:-${CC_CONFIG_DIR}/mcp.env}"
CC_PROXY_PID_FILE="${CC_STATE_DIR}/tinyproxy.pid"
CC_EGRESS_STATE="${CC_STATE_DIR}/egress"
# Per profile, so building one image never changes the agent versions another
# image is pinned to.
CC_IMAGE_VERSIONS="${CC_IMAGE_VERSIONS:-${CC_STATE_DIR}/image-versions${CC_PROFILE:+.${CC_PROFILE}}}"
# The Dockerfile layered on the repo image. Set it to an empty string for none.
# Only the default path may be absent; a path you set must exist.
_CC_IMAGE_OVERLAY_DEFAULT="${CC_CONFIG_DIR}/Dockerfile.local"
CC_IMAGE_OVERLAY="${CC_IMAGE_OVERLAY-${_CC_IMAGE_OVERLAY_DEFAULT}}"

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
  [ "${CC_MCP}" = "1" ] || return 0
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
  [ "${CC_PI_SYNC}" = "1" ] || return 0
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
  case "${CC_MCP}:${CC_PI_SYNC}" in
    [01]:[01]) ;;
    *) echo "cc-container: CC_MCP and CC_PI_SYNC must be 0 or 1, got CC_MCP='${CC_MCP}' CC_PI_SYNC='${CC_PI_SYNC}'" >&2
       return 1 ;;
  esac
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

# Runs a slow command that prints nothing until it is done under a spinner with
# the elapsed time, then passes its output on. Only on a terminal: elsewhere it
# just runs the command. The command runs in a subshell (which also keeps
# interactive shells from announcing it as a job), so it cannot set variables
# here.
_cc_spin() {
  local label="$1"; shift
  if [ ! -t 2 ]; then "$@"; return; fi
  local out err rc
  out="$(mktemp "${TMPDIR:-/tmp}/cc-spin.XXXXXX")" || return 1
  err="$(mktemp "${TMPDIR:-/tmp}/cc-spin.XXXXXX")" || { rm -f "${out}"; return 1; }
  (
    "$@" >"${out}" 2>"${err}" &
    pid=$! start=$(date +%s) i=0 spin='|/-\'
    while kill -0 "${pid}" 2>/dev/null; do
      printf '\r\033[K%s %s, %ss' "${spin:$((i % 4)):1}" "${label}" "$(($(date +%s) - start))" >&2
      i=$((i + 1))
      sleep 0.2
    done
    printf '\r\033[K' >&2
    wait "${pid}"
  )
  rc=$?
  cat "${out}"
  cat "${err}" >&2
  rm -f "${out}" "${err}"
  return "${rc}"
}
_cc_quiet() { "$@" >/dev/null 2>&1; }

cc-runtime-up() {
  if container system status >/dev/null 2>&1; then return 0; fi
  echo "cc-container: container runtime not running, starting it..." >&2
  _cc_spin "starting the container runtime" _cc_quiet container system start
  if ! container system status >/dev/null 2>&1; then
    echo "cc-container: failed to start the container runtime; run 'container system start' manually" >&2
    return 1
  fi
}

# Free GB on the volume that holds $HOME (the Mac's data volume).
_cc_free_gb() { df -Pk "${HOME}" | awk 'NR==2 {printf "%d", $4 / 1048576}'; }

# Guard a command that can grow the Mac's disk usage. "enforce" refuses below
# CC_DISK_MIN_GB; "warn" only warns (attaching to a running session allocates
# nothing new, and refusing it would only stop you from cleaning up from inside).
_cc_disk_guard() {
  local mode="$1" free
  case "${CC_DISK_WARN_GB}:${CC_DISK_MIN_GB}" in
    *[!0-9:]*|:*|*:)
      echo "cc-container: CC_DISK_WARN_GB and CC_DISK_MIN_GB must be whole numbers of GB, got '${CC_DISK_WARN_GB}' and '${CC_DISK_MIN_GB}'" >&2
      return 1 ;;
  esac
  free="$(_cc_free_gb)"
  case "${free}" in
    ''|*[!0-9]*) echo "cc-container: cannot read the free space of ${HOME} from df (got '${free}')" >&2; return 1 ;;
  esac
  if [ "${mode}" = "enforce" ] && [ "${CC_DISK_MIN_GB}" -gt 0 ] && [ "${free}" -lt "${CC_DISK_MIN_GB}" ]; then
    echo "cc-container: only ${free} GB free on the Mac (CC_DISK_MIN_GB=${CC_DISK_MIN_GB}); refusing to start." >&2
    echo "  A full disk turns running guests' root file systems read-only. Free space first:" >&2
    echo "    container system df            # what images, containers and volumes take" >&2
    echo "    container image prune -a       # unused images (rebuilt when next needed)" >&2
    echo "  Or run this one command anyway: CC_DISK_MIN_GB=0 <command>" >&2
    return 1
  fi
  if [ "${free}" -lt "${CC_DISK_WARN_GB}" ]; then
    echo "cc-container: warning: only ${free} GB free on the Mac (CC_DISK_WARN_GB=${CC_DISK_WARN_GB}). Builds, installs and simulators need tens of GB; see 'container system df'." >&2
  fi
  return 0
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

# The Mac must route the bridge subnet through the bridge itself. Something (a
# VPN, a network change) can leave a route for it on another interface, en0 as
# a rule; mac-sim still works then (the guest calls the Mac), but nothing on the
# Mac reaches a guest, so a train console's relay times out. The repair needs
# root: it runs through `sudo -n`, which a sudoers rule can allow for exactly
# these two commands (docs/MAC-SIM.md), and otherwise prints them to run by hand.
_cc_sim_check_route() {
  local host bridge mask out_if prefix net
  host="$(_cc_sim_host)"
  bridge="$(ifconfig 2>/dev/null | awk -v a="${host}" '/^[a-z]/{i=$1; sub(":$","",i)} $1=="inet" && $2==a {print i}')"
  [ -n "${bridge}" ] || return 0  # no bridge yet: _cc_sim_ensure_bridge reports that
  mask="$(ifconfig "${bridge}" | awk -v a="${host}" '$1=="inet" && $2==a {print $4}')"
  out_if="$(_cc_route_if "${host%.*}.2")"
  [ "${out_if}" = "${bridge}" ] && return 0
  prefix=0
  local m=$((mask)) i n=0 a b c d
  for i in $(seq 31 -1 0); do (( (m >> i) & 1 )) && prefix=$((prefix + 1)); done
  IFS=. read -r a b c d <<< "${host}"
  n=$(( ((a << 24) | (b << 16) | (c << 8) | d) & m ))
  net="$(( (n >> 24) & 255 )).$(( (n >> 16) & 255 )).$(( (n >> 8) & 255 )).$(( n & 255 ))/${prefix}"
  local del=(/sbin/route -n delete -net "${net}" -interface "${out_if}")
  local add=(/sbin/route -n add -net "${net}" -interface "${bridge}")
  if { [ -z "${out_if}" ] || sudo -n "${del[@]}" >/dev/null 2>&1; } \
     && sudo -n "${add[@]}" >/dev/null 2>&1 \
     && [ "$(_cc_route_if "${host%.*}.2")" = "${bridge}" ]; then
    echo "cc-sim-up: rerouted ${net} from ${out_if:-nothing} to ${bridge}" >&2
    return 0
  fi
  echo "cc-sim-up: the Mac routes ${net} through ${out_if:-nothing}, not ${bridge}:" >&2
  echo "  nothing on the Mac can reach a container (a train console's relay times out). Fix it with:" >&2
  [ -n "${out_if}" ] && echo "    sudo ${del[*]}" >&2
  echo "    sudo ${add[*]}" >&2
  echo "  A sudoers rule for these two lets cc-sim-up run them itself (docs/MAC-SIM.md)." >&2
  return 0
}
_cc_route_if() { route -n get "$1" 2>/dev/null | awk '$1=="interface:" {print $2}'; }

cc-sim-up() {
  if ! _cc_sim_configured; then
    echo "cc-sim-up: no config at ${CC_SIM_CONFIG}" >&2
    echo "Copy ${CC_CONTAINER_REPO}/config/mac-sim.example.json there, then declare a project root." >&2
    return 1
  fi
  local port; port="$(_cc_sim_port)"
  [ -n "${port}" ] || { echo "cc-sim-up: cannot read listen.port from ${CC_SIM_CONFIG}" >&2; return 1; }
  if _cc_listening "${port}"; then _cc_sim_check_route; return 0; fi
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
  _cc_sim_check_route
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
  _cc_disk_guard enforce || return 1
  _cc_proxy_collect || return 1
  _cc_sim_auto_up || return 1
  _cc_call_hook "${CC_PRE_RUN_HOOK}" CC_PRE_RUN_HOOK || return 1
  _cc_mcp_collect || return 1
  _cc_policy_collect || return 1
  # -it needs a real TTY; fall back for piped/scripted use (e.g. claude -p).
  local tty_args=()
  if [ -t 0 ] && [ -t 1 ]; then tty_args=(-it); fi
  _cc_awake ${agent_env+"${agent_env[@]}"} container run --rm "${tty_args[@]}" \
    --volume "${PWD}:${PWD}" \
    --volume "${CC_HOME_VOLUME}:/root" \
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
# Only one container can hold a home volume (cc-home) at a time, so parallel
# `cc-container` runs fail with "storage device attachment is invalid". To run
# several Claude Code instances at once, start ONE long-lived container and
# attach to it from as many terminals as you like.
#
#   cc-up            # once, from the project directory
#   cc-attach        # in each terminal window (pi-attach for pi)
#   cc-down          # when finished
# ---------------------------------------------------------------------------
# The command a hint should name: the profile's form when one is active.
_cc_cmd() {
  if [ -n "${CC_PROFILE}" ]; then echo "cc-profile ${CC_PROFILE} $1"; else echo "$2"; fi
}

_cc_session_state() {
  container inspect "${CC_SESSION_NAME}" 2>/dev/null \
    | jq -r '.[0].status.state // empty' 2>/dev/null
}

_cc_config_current() {
  local now; now="$(stat -f %m "${CC_CONFIG_DIR}/config.sh" 2>/dev/null)"
  [ "${now}" = "${_CC_CONFIG_LOADED_MTIME}" ] && return 0
  echo "cc-container: ${CC_CONFIG_DIR}/config.sh changed after this shell loaded it, so this shell still holds the old values (CC_SESSION_MEMORY=${CC_SESSION_MEMORY}, CC_SESSION_CPUS=${CC_SESSION_CPUS}). Open a new terminal and run it there." >&2
  return 1
}

_cc_mem_bytes() {
  case "$1" in
    *[gG]) echo "$(( ${1%?} * 1073741824 ))" ;;
    *[mM]) echo "$(( ${1%?} * 1048576 ))" ;;
    *) echo "cc-container: CC_SESSION_MEMORY=$1 is not <n>g or <n>m" >&2; return 1 ;;
  esac
}

# Empty when the existing session has the configured CPUs and memory, else a
# sentence saying what it has and what config.sh wants.
_cc_session_size_change() {
  local have want mem cpus
  have="$(container inspect "${CC_SESSION_NAME}" \
    | jq -r '.[0].configuration.resources | "\(.memoryInBytes) \(.cpus)"')" || return 1
  want="$(_cc_mem_bytes "${CC_SESSION_MEMORY}")" || return 1
  [ "${have}" = "${want} ${CC_SESSION_CPUS}" ] && return 0
  read -r mem cpus <<<"${have}"
  echo "${CC_SESSION_NAME} has $(( mem / 1048576 )) MB and ${cpus} CPUs, config wants $(( want / 1048576 )) MB and ${CC_SESSION_CPUS} CPUs"
}

cc-up() {
  _cc_config_current || return 1
  cc-runtime-up || return 1
  _cc_disk_guard enforce || return 1
  _cc_proxy_collect || return 1
  _cc_sim_auto_up || return 1
  _cc_call_hook "${CC_PRE_RUN_HOOK}" CC_PRE_RUN_HOOK || return 1
  _cc_mcp_collect || return 1
  _cc_policy_collect || return 1
  local state size; state="$(_cc_session_state)"
  if [ -n "${state}" ]; then
    size="$(_cc_session_size_change)" || return 1
  fi
  if [ "${state}" = "running" ]; then
    echo "${CC_SESSION_NAME} already running. Attach with: $(_cc_cmd attach cc-attach)"
    [ -n "${size}" ] && echo "note: ${size}. ccdown && ccup applies it (it ends attached sessions)."
    return 0
  fi
  # Resources are fixed when a container is created, so a restart would bring
  # the stopped one back at its old size.
  if [ -n "${size}" ]; then
    echo "Recreating ${CC_SESSION_NAME}: ${size}."
    _cc_spin "removing ${CC_SESSION_NAME}" _cc_session_remove || return 1
    state=""
  fi
  if [ -n "${state}" ]; then
    echo "Restarting existing ${CC_SESSION_NAME}..."
    _cc_spin "restarting ${CC_SESSION_NAME}" container start "${CC_SESSION_NAME}" || return 1
  else
    # --init: an init as pid 1 reaps orphans (with `sleep infinity` there, every
    # exited background process stayed a zombie) and forwards stop's SIGTERM.
    _cc_spin "starting ${CC_SESSION_NAME}" container run -d --name "${CC_SESSION_NAME}" \
      --init \
      --cpus "${CC_SESSION_CPUS}" \
      --memory "${CC_SESSION_MEMORY}" \
      --volume "${PWD}:${PWD}" \
      --volume "${CC_HOME_VOLUME}:/root" \
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
  echo "${CC_SESSION_NAME} up, mounting ${PWD}. Attach with: $(_cc_cmd attach cc-attach)"
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
  _cc_disk_guard warn || return 1
  if [ "$(_cc_session_state)" != "running" ]; then
    echo "cc-attach: ${CC_SESSION_NAME} is not running. Start it with $(_cc_cmd up cc-up)." >&2
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
  _cc_disk_guard warn || return 1
  if [ "$(_cc_session_state)" != "running" ]; then
    echo "pi-attach: ${CC_SESSION_NAME} is not running. Start it with $(_cc_cmd up cc-up)." >&2
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
  _cc_disk_guard warn || return 1
  if [ "$(_cc_session_state)" != "running" ]; then
    echo "cc-attach-shell: ${CC_SESSION_NAME} is not running. Start it with $(_cc_cmd up cc-up)." >&2
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

# Run one command in the shared session, from the caller's directory when that
# is inside the mount. Unlike `ccxs -c` it takes argv rather than a shell
# string, and it is meant for scripts on the Mac: a TTY only when there is one,
# and the command's exit code is returned.
cc-exec() {
  _cc_disk_guard warn || return 1
  if [ "$#" -eq 0 ]; then
    echo "cc-exec: usage: cc-exec <command> [args...]" >&2
    return 1
  fi
  if [ "$(_cc_session_state)" != "running" ]; then
    echo "cc-exec: ${CC_SESSION_NAME} is not running. Start it with $(_cc_cmd up cc-up)." >&2
    return 1
  fi
  local tty_args=()
  if [ -t 0 ] && [ -t 1 ]; then tty_args=(-it); fi
  local workdir; workdir="$(_cc_attach_workdir)" || return 1
  _cc_awake container exec ${tty_args+"${tty_args[@]}"} --env TERM --workdir "${workdir}" \
    "${CC_SESSION_NAME}" "$@"
}

# Run a command against a profile: cc-profile <name> <verb> [args...]. It
# sources this file again in a fresh shell with CC_PROFILE=<name>, so the
# profile's settings never leak into the calling shell. Give a profile you use
# often a short name in config.sh:  ccbench() { cc-profile bench "$@"; }
cc-profile() {
  local name="${1:-}" verb="${2:-}" fn
  if [ -z "${name}" ] || [ -z "${verb}" ]; then
    echo "cc-profile: usage: cc-profile <name> <up|down|status|attach|pi|shell|exec|run|build|upgrade|doctor> [args...]" >&2
    return 1
  fi
  shift 2
  case "${verb}" in
    up)      fn=cc-up ;;
    down)    fn=cc-down ;;
    status)  fn=cc-status ;;
    attach)  fn=cc-attach ;;
    pi)      fn=pi-attach ;;
    shell)   fn=cc-attach-shell ;;
    exec)    fn=cc-exec ;;
    run)     fn=cc-container ;;
    build)   fn=cc-container-build ;;
    upgrade) fn=cc-container-upgrade ;;
    doctor)  fn=cc-doctor ;;
    *) echo "cc-profile: unknown verb '${verb}'; use up, down, status, attach, pi, shell, exec, run, build, upgrade or doctor" >&2
       return 1 ;;
  esac
  local sh flags=() repo="${CC_CONTAINER_REPO}"
  if [ -n "${ZSH_VERSION:-}" ]; then sh=zsh; flags=(-f); else sh=bash; flags=(--noprofile --norc); fi
  # The script is expanded by the child shell, not here: hence single quotes.
  # shellcheck disable=SC2016
  CC_PROFILE="${name}" CC_CONTAINER_REPO="${repo}" \
    CC_CONFIG_DIR="${CC_CONFIG_DIR}" CC_STATE_DIR="${CC_STATE_DIR}" \
    "${sh}" "${flags[@]}" -c '. "$1" || exit 1; shift; "$@"' cc-profile \
    "${repo}/bin/cc-container.sh" "${fn}" "$@"
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
    echo "${caller}: ${CC_SESSION_NAME} is not running. Start it with $(_cc_cmd up cc-up)." >&2
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
#
# Fails safe: when the count cannot be read it reports 1, so cc-down leaves the
# helpers up rather than cutting the network of a container it cannot see.
# Older container releases report .status as a string, 1.x as .status.state.
_cc_other_containers() {
  local json n
  if ! json="$(container list --format json)"; then
    echo "cc-container: cannot list containers; leaving the host helpers up" >&2
    echo 1
    return 0
  fi
  n="$(printf '%s' "${json}" | jq -r --arg name "${CC_SESSION_NAME}" \
        '[.[] | select((if (.status | type) == "object" then .status.state else .status end) == "running")
              | .configuration.id // .id
              | select(. != $name and (startswith("buildkit") | not))] | length')"
  case "${n}" in
    ''|*[!0-9]*)
      echo "cc-container: cannot count running containers (jq said '${n}'); leaving the host helpers up" >&2
      echo 1 ;;
    *) echo "${n}" ;;
  esac
}

_cc_session_remove() {
  container stop "${CC_SESSION_NAME}" 2>/dev/null
  sleep 2
  container delete "${CC_SESSION_NAME}" 2>/dev/null
}

cc-down() {
  _cc_spin "stopping ${CC_SESSION_NAME}" _cc_session_remove
  echo "${CC_SESSION_NAME} removed."
  # Leave the host-side helpers up if anything else might still need them: a
  # one-off `ccrun` in another terminal would lose its network otherwise.
  if [ "$(_cc_other_containers)" -gt 0 ] 2>/dev/null; then
    echo "host helpers: left running (other containers still up)"
    return 0
  fi
  _cc_listening "${CC_PROXY_PORT}" && cc-proxy-down >/dev/null 2>&1 && echo "stopped:  proxy"
  # A builder cc-container-build could not stop (an interrupted build, or a
  # plain `container build`). A build still running in another terminal would
  # be cut off, as the proxy already would cut it off.
  _cc_builder_running && _cc_builder_stop && echo "stopped:  buildkit builder"
  _cc_sim_auto_down
  _cc_call_hook "${CC_POST_DOWN_HOOK}" CC_POST_DOWN_HOOK
  return 0
}

cc-status() {
  [ -n "${CC_PROFILE}" ] && echo "profile:  ${CC_PROFILE} (home volume ${CC_HOME_VOLUME}, image ${CC_CONTAINER_IMAGE})"
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
    | jq -r --arg home "${CC_HOME_VOLUME}" '.[0] | [
        .status.state,
        (.configuration.resources.cpus|tostring),
        ((.configuration.resources.memoryInBytes / 1073741824)|floor|tostring),
        ([.configuration.mounts[]?.source | select(contains("/volumes/" + $home + "/")|not)]|first // "-")
      ] | @tsv' 2>/dev/null)"
  if [ -z "${info}" ]; then
    echo "session:  not running (start with: $(_cc_cmd up ccup))"
    return 0
  fi
  local state cpus mem mount
  IFS="$(printf '\t')" read -r state cpus mem mount <<< "${info}"
  echo "session:  ${CC_SESSION_NAME} ${state} | ${cpus} cpus, ${mem} GB"
  echo "mount:    ${mount}"
  if [ "${state}" = "running" ]; then echo "attach:   $(_cc_cmd attach ccx) (Claude Code), $(_cc_cmd pi pix) (pi)"; else echo "start:    $(_cc_cmd up ccup)   (restarts it)"; fi
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

  # The VM's disks, the images and every bind-mounted checkout share the Mac's
  # data volume. When it fills, the guest's root file system goes read-only.
  local free_gb reclaim
  free_gb="$(_cc_free_gb)"
  if [ "${free_gb}" -lt "${CC_DISK_MIN_GB}" ]; then
    _p "mac free disk" "TOO LOW ${free_gb} GB - under CC_DISK_MIN_GB=${CC_DISK_MIN_GB}, so containers and builds are refused"
  elif [ "${free_gb}" -lt "${CC_DISK_WARN_GB}" ]; then
    _p "mac free disk" "LOW ${free_gb} GB - under CC_DISK_WARN_GB=${CC_DISK_WARN_GB}; builds and simulators need tens of GB"
  else
    _p "mac free disk" "${free_gb} GB"
  fi
  reclaim="$(container system df 2>/dev/null | awk '$1 == "Images" {print $6, $7, $8}')"
  [ -n "${reclaim}" ] \
    && _p "images reclaimable" "${reclaim} - container image prune -a frees it (pulled or rebuilt when next needed)"

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
# If CC_IMAGE_OVERLAY (default ~/.config/cc-container/Dockerfile.local) exists,
# the repo image is built as <name>:base and your file is built on top of it as
# the real image. Profiles that build their own image should use a different
# image NAME, not just a tag, so the two builds never share a <name>:base. That is the
# supported way to add tools (extra MCP server packages, another runtime, a
# headless browser) without forking the repo's Dockerfile and without this
# project needing a flag per use case. Start it with:
#
#   ARG BASE_IMAGE
#   FROM ${BASE_IMAGE}
#   RUN npm install -g @some/mcp-server
#
# Its build context is the overlay's directory, so it can COPY files from there.
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

# Whether Apple container's BuildKit builder (the `buildkit` container) is up.
# `container build` starts it and nothing stops it, so it holds its memory
# (2 GB) until someone does; stopping keeps its build cache.
_cc_builder_running() {
  local json
  json="$(container builder status --format json 2>/dev/null)" || return 1
  printf '%s' "${json}" | jq -e '[.[] | if (.status | type) == "object" then .status.state else .status end]
                                 | any(. == "running")' >/dev/null
}

_cc_builder_stop() {
  local out
  out="$(container builder stop 2>&1)" || {
    echo "cc-container: could not stop the buildkit builder: ${out}; run: container builder stop" >&2
    return 1
  }
}

# Stops the builder afterwards unless it was already up: another build may
# still be using it then.
cc-container-build() {
  local builder_was_up=0 rc
  _cc_builder_running && builder_was_up=1
  _cc_container_build "$@"
  rc=$?
  [ "${builder_was_up}" = 0 ] && _cc_builder_running && _cc_builder_stop
  return "${rc}"
}

_cc_container_build() {
  cc-runtime-up || return 1
  _cc_disk_guard enforce || return 1
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
  local overlay="${CC_IMAGE_OVERLAY}"
  if [ -n "${overlay}" ] && [ ! -r "${overlay}" ] && [ "${overlay}" != "${_CC_IMAGE_OVERLAY_DEFAULT}" ]; then
    echo "cc-container-build: CC_IMAGE_OVERLAY=${overlay} does not exist; fix the path or set it to an empty string for no overlay" >&2
    return 1
  fi
  if [ -z "${overlay}" ] || [ ! -r "${overlay}" ]; then
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
    -f "${overlay}" -t "${CC_CONTAINER_IMAGE}" "$(dirname "${overlay}")"
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
