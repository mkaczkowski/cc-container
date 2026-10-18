FROM docker.io/library/node:22-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ripgrep fd-find curl ca-certificates gnupg less jq python3 openssh-client unzip tmux \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI: agents reach for `gh` constantly (PRs, issues, CI logs).
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# System libraries and fonts for Playwright's Chromium, so a project's own
# Playwright (and the Playwright MCP server) can run headless. Only the apt
# packages are baked in: the browser itself must match the project's Playwright
# version, so `npx playwright install chromium` fetches it at run time into
# ~/.cache/ms-playwright, which lives in the cc-home volume and survives
# sessions. The npx cache is dropped because /root is shadowed by that volume.
RUN npx -y playwright@latest install-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.npm

# Yarn/pnpm via corepack, so a repo's packageManager field just works.
RUN corepack enable

# Claude Code version. Bump CC_VERSION (or pass --build-arg CC_VERSION=x.y.z) to
# upgrade: the value is part of this layer's cache key, so changing it forces a
# reinstall while leaving the apt layers above cached. A plain rebuild does NOT
# upgrade -- use `cc-container-upgrade`.
ARG CC_VERSION=latest
RUN npm install -g "@anthropic-ai/claude-code@${CC_VERSION}" \
    && claude --version

# Pi coding agent (pi.dev), the second harness the wrapper can attach
# (`pirun` / `pix`). Same upgrade model as Claude Code: bump PI_VERSION or use
# `pi-container-upgrade`. It sits after the Claude Code layer, so upgrading
# Claude Code also re-resolves an unpinned pi to its latest release.
# --ignore-scripts follows pi's own container recipe. Its find/grep tools use
# fd and rg from the PATH (Debian's `fdfind` is recognised), so it never has to
# download its own copies into ~/.pi/agent/bin.
ARG PI_VERSION=latest
RUN npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && pi --version

# Extra MCP server packages go here. Installing them into the image rather than
# resolving `npx -y ...` at every server start keeps session startup off the
# network -- a stdio MCP server is respawned for each session. Example:
#   RUN npm install -g @upstash/context7-mcp@4
# Then declare it in ~/.config/cc-container/mcp/mcp-servers.json.

# Two directories on PATH for host-supplied guest tools, both bind-mounted at
# run time rather than baked in:
#   /opt/cc-tools  this repo's guest/ directory (cc-entry, pi-entry, cc-mcp-sync)
#   /opt/cc-local  anything of your own, mounted via CC_EXTRA_VOLUME_ARGS
# They are mounted as DIRECTORIES, never as single files: a single-file bind
# mount pins an inode, so editing the host file while a container runs leaves
# the guest reading a stale or half-written copy. Resolving at exec time always
# picks up the current version.
ENV PATH="/opt/cc-tools:/opt/cc-local:${PATH}"

# ENV PATH alone is not enough: Debian's /etc/profile assigns PATH
# unconditionally, so any LOGIN shell (bash -l, and anything that spawns one)
# drops both directories and the tools silently stop resolving. This puts them
# back, guarded so repeated sourcing cannot duplicate the entries.
RUN printf '%s\n' \
      'case ":${PATH}:" in' \
      '  *:/opt/cc-tools:*) ;;' \
      '  *) PATH="/opt/cc-tools:/opt/cc-local:${PATH}"; export PATH ;;' \
      'esac' \
      > /etc/profile.d/cc-path.sh

# Runs as root on purpose: apple/container surfaces bind-mounted host files as
# root:root with no uid mapping, so a non-root user may not be able to write
# /workspace. IS_SANDBOX=1 is what lets Claude Code accept
# --dangerously-skip-permissions under root. The VM is the sandbox here.
#
# PI_SKIP_VERSION_CHECK: the image is the source of truth for pi's version (see
# pi-container-upgrade). An in-session `pi update` would land in the container's
# throwaway layer, so the "update available" notice only points somewhere wrong.
ENV HOME=/root \
    SHELL=/bin/bash \
    IS_SANDBOX=1 \
    PI_SKIP_VERSION_CHECK=1

WORKDIR /workspace
ENTRYPOINT ["claude"]
