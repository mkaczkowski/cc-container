# cc-container profile for a trainyard train: the agent runner profile
# (profile.runner.example.sh) set up for trainyard, whose driver manages tasks
# across several repos from one container. Copy it to
# ~/.config/cc-container/profiles/train.sh and use it with
# `cc-profile train <verb>` (see README "Profiles").
#
# Differences from the runner profile: it mounts every repo listed in
# trainyard's machine.json, whichever directory `up` runs from, and `attach`
# starts Claude Code through `trainyard claude`. The image must have
# `trainyard` on its PATH (an image overlay, see README "Profiles"). Start it
# from inside a project root that mac-sim.json declares, or it gets no bridge.

# --- identity ----------------------------------------------------------------
CC_SESSION_NAME=cc-train
CC_HOME_VOLUME=train-home

# --- size --------------------------------------------------------------------
# For about four concurrent sessions with their tests. Measure what one session
# of your project peaks at (`cat /sys/fs/cgroup/memory.peak` in the container,
# after a run), multiply, and leave the simulators their share of the Mac.
CC_SESSION_CPUS=8
CC_SESSION_MEMORY=16g

# --- billing -----------------------------------------------------------------
# Unattended sessions must not fall back to an API key: the managed policy
# accepts only a Claude.ai login (README "Subscription-only mode").
CC_SUBSCRIPTION_ONLY=1

# --- what it must not inherit ------------------------------------------------
# Only the project directories: no notes, vaults or tool directories of yours.
# Every repo in trainyard's machine.json "repos" is mounted at its Mac path, so
# a second repo's train runs its sessions here too.
_train_machine_json="${XDG_CONFIG_HOME:-$HOME/.config}/trainyard/machine.json"
if [ ! -r "${_train_machine_json}" ]; then
  echo "train profile: ${_train_machine_json} does not exist; create it with a \"repos\" list" >&2
  return 1
fi
_train_repos="$(awk '/"repos"[[:space:]]*:/{r=1;next} r&&/\]/{exit} r{ if (match($0, /"\/[^"]+"/)) print substr($0, RSTART+1, RLENGTH-2) }' "${_train_machine_json}")"
if [ -z "${_train_repos}" ]; then
  echo "train profile: no \"repos\" found in ${_train_machine_json}; list the repos to mount there" >&2
  return 1
fi
CC_EXTRA_VOLUME_ARGS=()
_train_repos_json=
while IFS= read -r _repo; do
  CC_EXTRA_VOLUME_ARGS+=(--volume "${_repo}:${_repo}")
  _train_repos_json="${_train_repos_json:+${_train_repos_json}, }\"${_repo}\""
done <<EOF
${_train_repos}
EOF
unset _repo _train_repos _train_machine_json
# A directory of screenshots, read-only at its Mac path, so a screenshot path
# pasted into a session resolves unchanged in the guest:
# CC_EXTRA_VOLUME_ARGS+=(--volume "$HOME/Pictures/Screenshots:$HOME/Pictures/Screenshots:ro")
# No MCP servers or mcp.env secrets, no pi config.
CC_MCP=0
CC_PI_SYNC=0
# Host hooks belong to the shared session.
CC_PRE_RUN_HOOK=
CC_POST_DOWN_HOOK=
CC_EXTRA_RUN_ARGS=()

# --- secrets -----------------------------------------------------------------
# Only the mcp.env keys named here reach the container (CC_MCP stays 0, so the
# rest of that file does not), e.g. for a CLI baked into the image:
# _train_env_keys=(SUPABASE_ACCESS_TOKEN SUPABASE_PROJECT_ID)
_train_env_keys=()
_train_mcp_env="${CC_CONFIG_DIR:-$HOME/.config/cc-container}/mcp.env"
for _key in ${_train_env_keys+"${_train_env_keys[@]}"}; do
  _val="$(sed -n "s/^${_key}=//p" "${_train_mcp_env}")"
  if [ -z "${_val}" ]; then
    echo "train profile: ${_key} is missing from ${_train_mcp_env}; add ${_key}=... there" >&2
    return 1
  fi
  CC_EXTRA_RUN_ARGS+=(--env "${_key}=${_val}")
done
unset _key _val _train_env_keys _train_mcp_env

# --- what it keeps -----------------------------------------------------------
# The mac-sim shim and its token, so sessions here claim simulators exactly as
# the shared session's do. Each one should use a private simulator
# (`mac-sim sim create`) so they do not queue on the shared one.
CC_SIM_AUTO=1

# --- attach ------------------------------------------------------------------
# `cc-profile train attach` starts Claude Code through trainyard.
CC_ATTACH_COMMAND=(trainyard claude)

# --- what a recreated home volume loses ---------------------------------------
# trainyard's launcher and the container's own machine.json live in the home
# volume, not the image. When the volume is recreated (a teardown, a volume
# removal) both are gone and the driver fails with "failed to find target
# executable trainyard". cc-up checks for the launcher at every start and, if it
# is missing, runs this once: installs it from the mounted checkout (a copy, so
# the container does not follow your working tree) and writes the machine.json
# with the same repos. The checkout must be one of those repos. The Claude.ai
# login is the one thing it cannot restore: `cc-profile train attach`, /login.
_train_checkout="${TRAINYARD_CHECKOUT:-$HOME/IdeaProjects/trainyard}"
_train_container_json='/root/.config/trainyard/machine.json'
CC_REQUIRED_COMMANDS=(trainyard)
CC_SETUP_COMMAND="npm i -g --install-links --prefix /root/.local '${_train_checkout}/packages/engine' \
  && mkdir -p /root/.config/trainyard \
  && { [ -e ${_train_container_json} ] || printf '{\"maxSessions\": 4, \"repos\": [%s]}\n' '${_train_repos_json}' > ${_train_container_json}; }"
unset _train_checkout _train_container_json _train_repos_json
