# cc-container profile for an agent runner: a container beside the shared
# session where a task runner starts several Claude Code sessions of its own,
# unattended, and verifies their work on the Mac's simulators. Copy it to
# ~/.config/cc-container/profiles/<name>.sh and use it with
# `cc-profile <name> <verb>` (see README "Profiles").
#
# It is profile.example.sh's isolated worker with two differences: it keeps the
# mac-sim bridge, and it may bill only a Claude subscription. Start it from
# inside a project root that mac-sim.json declares, or it gets no bridge.

# --- identity ----------------------------------------------------------------
CC_SESSION_NAME=cc-runner
CC_HOME_VOLUME=runner-home

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
# Only the project directory: no notes, vaults or tool directories of yours.
CC_EXTRA_VOLUME_ARGS=()
# No MCP servers or mcp.env secrets, no pi config.
CC_MCP=0
CC_PI_SYNC=0
# Host hooks belong to the shared session.
CC_PRE_RUN_HOOK=
CC_POST_DOWN_HOOK=
CC_EXTRA_RUN_ARGS=()

# --- what it keeps -----------------------------------------------------------
# The mac-sim shim and its token, so sessions here claim simulators exactly as
# the shared session's do. Each one should use a private simulator
# (`mac-sim sim create`) so they do not queue on the shared one.
CC_SIM_AUTO=1
