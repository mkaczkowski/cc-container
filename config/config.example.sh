# cc-container user config. Copy to ~/.config/cc-container/config.sh and edit.
# This file is sourced by bin/cc-container.sh; everything is optional.

# --- image -------------------------------------------------------------------
# CC_CONTAINER_IMAGE=claude-code:local

# --- resources ---------------------------------------------------------------
# Default: half the host's cores and half its RAM (min 1 cpu / 4 GB).
# CC_SESSION_CPUS=8
# CC_SESSION_MEMORY=16g

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

# --- adding tools to the image ----------------------------------------------
# Put a Dockerfile.local next to this file to layer your own tools on top of the
# repo image (see README "Adding tools to the image"). No setting is needed:
# cc-container-build picks it up if it exists.

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
