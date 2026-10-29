# cc-container user config. Copy to ~/.config/cc-container/config.sh and edit.
# This file is sourced by bin/cc-container.sh; everything is optional.

# --- image -------------------------------------------------------------------
# CC_CONTAINER_IMAGE=claude-code:local

# --- resources ---------------------------------------------------------------
# Default: half the host's cores (min 1) and half its RAM, kept between 4 and
# 8 GB. The VM holds on to the memory it has used until it stops, so a larger
# value is memory taken from the Mac and its simulators.
# CC_SESSION_CPUS=8
# CC_SESSION_MEMORY=12g
# Guest kernel sysctls set at boot (space-separated key=value). The default
# keeps a 256 MB reserve so virtiofs reads of the project do not fail with
# ENOMEM under memory pressure; "" boots with the kernel's defaults.
# CC_SESSION_SYSCTLS="vm.min_free_kbytes=262144 vm.watermark_scale_factor=200"

# --- disk --------------------------------------------------------------------
# Free GB on the Mac below which starting a container or a build is refused,
# and below which every command warns. A full disk turns running guests' root
# file systems read-only. CC_DISK_MIN_GB=0 turns the refusal off.
# CC_DISK_MIN_GB=10
# CC_DISK_WARN_GB=25
# drop deletes the buildkit builder and its layer cache once no container
# exists (after a build, or at cc-down); keep holds it for faster rebuilds.
# CC_BUILD_CACHE=drop

# --- network -----------------------------------------------------------------
# auto | on | off. `auto` uses the host proxy only if `cc-doctor` found that
# guests have no direct egress. Set `off` to skip the check entirely.
# CC_PROXY=auto
# CC_PROXY_HOST=192.168.64.1
# CC_PROXY_PORT=8888

# --- extra mounts ------------------------------------------------------------
# Only $PWD is visible inside the VM. Anything else the agent must read lives
# here, one --volume per entry. Prefer :ro. EVERY ENTRY WIDENS THE ISOLATION
# BOUNDARY -- see SECURITY.md before adding one.
#
# Tip: mounting a host directory at the SAME absolute path it has on the Mac
# makes tooling that refers to it by absolute path work unchanged in the guest.
# Brace variables there: zsh reads "$DIR:$DIR:ro" as $DIR with the :r modifier
# and drops the :ro. Write "${DIR}:${DIR}:ro".
#
# CC_EXTRA_VOLUME_ARGS=(
#   --volume "$HOME/Documents/Notes:$HOME/Documents/Notes"       # read-write
#   --volume "$HOME/Pictures/Screenshots:/mnt/screenshots:ro"    # read-only
# )

# --- behaviour ---------------------------------------------------------------
# 1 (default) passes --dangerously-skip-permissions; 0 opts out.
# CC_SKIP_PERMISSIONS=1
# Name of the long-lived shared container used by cc-up / cc-attach.
# CC_SESSION_NAME=cc-session
# The command cc-attach runs, as argv; claude's arguments follow it.
# CC_ATTACH_COMMAND=(claude)
# Commands the session must have on its PATH after cc-up, for a tool that lives
# in the home volume (not the image). cc-up fails naming any that are missing,
# or runs CC_SETUP_COMMAND (a shell command, run in the session) to install
# them and checks again. Only the shared session is checked (cc-up), not
# one-off cc-container runs. See config/profile.train.example.sh.
# CC_REQUIRED_COMMANDS=(mytool)
# CC_SETUP_COMMAND='npm i -g --prefix /root/.local mytool'
# Named volume mounted as the container's home (/root). Only one running
# container can hold it; a profile running beside the session needs its own.
# CC_HOME_VOLUME=cc-home
# 0 keeps the MCP servers (and mcp.env secrets) / the pi config out of the
# container. Usually set in a profile, not here.
# CC_MCP=1
# CC_PI_SYNC=1
# 1 allows only a Claude.ai subscription login: Claude Code (and pi through
# pi-claude-bridge) refuses API keys, Console logins, Bedrock and Vertex, and pi
# refuses to start while it sees Anthropic, AWS or Claude Code credentials (keys
# of pi's other providers are not checked). Recycle a running session
# (cc-down && cc-up) after changing it. See README "Subscription-only mode".
# CC_SUBSCRIPTION_ONLY=0

# --- adding tools to the image ----------------------------------------------
# Put a Dockerfile.local next to this file to layer your own tools on top of the
# repo image (see README "Adding tools to the image"). No setting is needed:
# cc-container-build picks it up if it exists. CC_IMAGE_OVERLAY names another
# file (it must exist), or "" for none.
# CC_IMAGE_OVERLAY="$HOME/.config/cc-container/Dockerfile.local"

# --- profiles ----------------------------------------------------------------
# A second container beside the shared session, configured by
# profiles/<name>.sh (start from config/profile.example.sh in the repo).
# A short name per profile you use:
# ccworker() { cc-profile worker "$@"; }

# --- extension points --------------------------------------------------------
# Extra `container run` arguments, applied to one-off runs and the shared
# session alike. Use it to pass an --env your own host tooling needs.
# CC_EXTRA_RUN_ARGS=( --env "MY_HELPER=http://192.168.64.1:9000" )
#
# A function run on the host just before a container starts, and one run by
# cc-down once no containers remain. Define them here; naming a function that
# does not exist is an error, so a session never starts half-configured.
# my_helper_up()   { pgrep -f my-helper >/dev/null || my-helper & }
# my_helper_down() { pkill -f my-helper; }
# CC_PRE_RUN_HOOK=my_helper_up
# CC_POST_DOWN_HOOK=my_helper_down
