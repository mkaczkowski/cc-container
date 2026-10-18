# cc-container profile. Copy to ~/.config/cc-container/profiles/<name>.sh and
# use it with `cc-profile <name> <verb>` (see README "Profiles").
#
# A profile is sourced AFTER config.sh, so it inherits everything config.sh set
# and changes only what it names here. This example is an isolated worker: a
# long-lived container beside the shared session that sees the project and
# nothing else of yours. That suits unattended jobs (a benchmark runner, a
# nightly build) that must not read your notes, MCP secrets or agent login.

# --- identity ----------------------------------------------------------------
# Its own container, and its own home volume: only one running container can
# hold a named volume, so sharing cc-home would fail beside the shared session.
CC_SESSION_NAME=cc-worker
CC_HOME_VOLUME=worker-home

# --- image -------------------------------------------------------------------
# A separate image NAME (not just a tag), built from the repo image plus this
# profile's own overlay, with agent versions pinned apart from the main image's
# (cc-profile worker build|upgrade). Leave both unset to reuse the main image.
# CC_CONTAINER_IMAGE=claude-code-worker:local
# CC_IMAGE_OVERLAY="${CC_CONFIG_DIR}/profiles/worker.Dockerfile"

# --- size --------------------------------------------------------------------
CC_SESSION_CPUS=4
CC_SESSION_MEMORY=8g

# --- what it must not inherit ------------------------------------------------
# Reset every mount config.sh added: only the project directory remains.
CC_EXTRA_VOLUME_ARGS=()
# No MCP servers, no mcp.env secrets, no pi config, no mac-sim token.
CC_MCP=0
CC_PI_SYNC=0
CC_SIM_AUTO=0
# Host hooks belong to the shared session.
CC_PRE_RUN_HOOK=
CC_POST_DOWN_HOOK=

# --- what it adds ------------------------------------------------------------
# Replaces config.sh's CC_EXTRA_RUN_ARGS. Every capability added here widens
# what root inside this VM can do (still inside the VM); see SECURITY.md.
CC_EXTRA_RUN_ARGS=()
# CC_EXTRA_RUN_ARGS=( --cap-add CAP_SYS_ADMIN --cap-add CAP_NET_ADMIN )
