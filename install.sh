#!/usr/bin/env bash
# Install cc-container: seed ~/.config/cc-container from the examples and add a
# source line to your shell rc. Idempotent; --uninstall reverses it.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/cc-container"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cc-container"
MARKER="# cc-container"
LINE="source \"${REPO}/bin/cc-container.sh\"  ${MARKER}"

rc_file() {
  case "$(basename "${SHELL:-/bin/zsh}")" in
    zsh) echo "$HOME/.zshrc" ;;
    bash) [ -f "$HOME/.bash_profile" ] && echo "$HOME/.bash_profile" || echo "$HOME/.bashrc" ;;
    *) echo "$HOME/.profile" ;;
  esac
}

uninstall() {
  local rc; rc="$(rc_file)"
  if [ -f "${rc}" ] && grep -q "${MARKER}" "${rc}"; then
    # Keep a backup: silently rewriting someone's rc file is not acceptable.
    cp "${rc}" "${rc}.cc-container.bak"
    grep -v "${MARKER}" "${rc}.cc-container.bak" > "${rc}"
    echo "removed the source line from ${rc} (backup: ${rc}.cc-container.bak)"
  else
    echo "no source line found in ${rc}"
  fi
  echo "Config left in place at ${CONFIG_DIR} (delete it by hand if you want it gone)."
  echo "The image and the cc-home volume are untouched:"
  echo "  container image delete claude-code:local"
  echo "  container volume delete cc-home     # this DELETES the container's Claude login"
}

[ "${1:-}" = "--uninstall" ] && { uninstall; exit 0; }

echo "repo:    ${REPO}"
mkdir -p "${CONFIG_DIR}/mcp" "${STATE_DIR}"

seed() {  # seed <example> <destination>
  if [ -e "$2" ]; then
    echo "keep:    $2 (already exists)"
  else
    cp "$1" "$2"
    echo "created: $2"
  fi
}
seed "${REPO}/config/config.example.sh"        "${CONFIG_DIR}/config.sh"
seed "${REPO}/config/tinyproxy.conf"           "${CONFIG_DIR}/tinyproxy.conf"
seed "${REPO}/config/mcp-servers.example.json" "${CONFIG_DIR}/mcp/mcp-servers.json"
if [ ! -e "${CONFIG_DIR}/mcp.env" ]; then
  cp "${REPO}/config/mcp.env.example" "${CONFIG_DIR}/mcp.env"
  chmod 600 "${CONFIG_DIR}/mcp.env"
  echo "created: ${CONFIG_DIR}/mcp.env (chmod 600 - put real keys here)"
fi

RC="$(rc_file)"
if grep -qs "${MARKER}" "${RC}"; then
  echo "keep:    source line already in ${RC}"
else
  printf '\n%s\n' "${LINE}" >> "${RC}"
  echo "added:   source line to ${RC}"
fi

cat <<TXT

Next:
  1. Prerequisites (Apple silicon, macOS 15+):
       brew install --cask container && container system start
  2. Open a new terminal (or: source ${RC})
  3. cc-container-build      # build the image (a few minutes)
  4. cc-doctor               # check prerequisites and probe guest egress
  5. cd <a project> && ccrun # first run only: /login inside the container
TXT
