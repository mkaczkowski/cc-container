FROM docker.io/library/node:22-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ripgrep curl ca-certificates gnupg less jq python3 openssh-client unzip \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI: agents reach for `gh` constantly (PRs, issues, CI logs).
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Yarn/pnpm via corepack, so a repo's packageManager field just works.
RUN corepack enable

# Claude Code version. Bump CC_VERSION (or pass --build-arg CC_VERSION=x.y.z) to
# upgrade: the value is part of this layer's cache key, so changing it forces a
# reinstall while leaving the apt layers above cached. A plain rebuild does NOT
# upgrade -- use `cc-container-upgrade`.
ARG CC_VERSION=latest
RUN npm install -g "@anthropic-ai/claude-code@${CC_VERSION}" \
    && claude --version

# Extra MCP server packages go here. Installing them into the image rather than
# resolving `npx -y ...` at every server start keeps session startup off the
# network -- a stdio MCP server is respawned for each session. Example:
#   RUN npm install -g @upstash/context7-mcp@4
# Then declare it in ~/.config/cc-container/mcp/mcp-servers.json.

# Two directories on PATH for host-supplied guest tools, both bind-mounted at
# run time rather than baked in:
#   /opt/cc-tools  this repo's guest/ directory (cc-entry, cc-mcp-sync)
#   /opt/cc-local  anything of your own, mounted via CC_EXTRA_VOLUME_ARGS
# They are mounted as DIRECTORIES, never as single files: a single-file bind
# mount pins an inode, so editing the host file while a container runs leaves
# the guest reading a stale or half-written copy. Resolving at exec time always
# picks up the current version.
ENV PATH="/opt/cc-tools:/opt/cc-local:${PATH}"

# Runs as root on purpose: apple/container surfaces bind-mounted host files as
# root:root with no uid mapping, so a non-root user may not be able to write
# /workspace. IS_SANDBOX=1 is what lets Claude Code accept
# --dangerously-skip-permissions under root. The VM is the sandbox here.
ENV HOME=/root \
    SHELL=/bin/bash \
    IS_SANDBOX=1

WORKDIR /workspace
ENTRYPOINT ["claude"]
