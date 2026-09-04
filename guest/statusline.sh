#!/usr/bin/env bash
# Optional Claude Code statusline for a containerised session.
#
# Nothing runs this unless you point Claude Code at it. The container has its
# own ~/.claude, in the cc-home volume, separate from the host's -- so enabling
# it here does not touch the statusline you use outside the container. See
# "Statusline" in the README for the one-liner that wires it up.
#
# It renders one line:
#
#   ⬡ container · Opus 5 · ████░░░░░░ 42% 200k · 15m · my-repo:main
#
# The badge keys off IS_SANDBOX=1, which the image sets, so the same script is
# safe to reuse on the host: there, the badge simply does not appear.
#
# Deliberately plain: 16-colour ANSI only, so it reads correctly in any terminal
# theme, light or dark. Copy it to ~/.config/cc-container/local/ and edit freely
# if you want something richer -- updates will not overwrite it there.

# No `set -e`: a statusline must always print something. A missing field or a
# failed git call degrades that part of the line, it does not blank the line.
set -uo pipefail

DIM=$'\033[2m'; RST=$'\033[0m'
MAGENTA=$'\033[35m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'
SEP="${DIM} · ${RST}"

json="$(cat)"

# jq is a hard requirement of the image and of the host setup, but a statusline
# that dies leaves the user with no status at all, so say why instead.
if ! command -v jq >/dev/null 2>&1; then
  printf '%s(statusline: jq not found)%s\n' "${DIM}" "${RST}"
  exit 0
fi

field() { printf '%s' "${json}" | jq -r "$1 // empty" 2>/dev/null; }

model_id="$(field '.model.id')"
ctx_pct="$(field '.context_window.used_percentage')"
ctx_size="$(field '.context_window.context_window_size')"
duration_ms="$(field '.cost.total_duration_ms')"
cwd="$(field '.cwd')"
[ -n "${cwd}" ] || cwd="$(field '.workspace.current_dir')"
[ -n "${cwd}" ] || cwd="${PWD}"
branch="$(field '.worktree.branch')"
[ -n "${branch}" ] || branch="$(git -C "${cwd}" branch --show-current 2>/dev/null)"

# Model id -> a short human name. The fallback strips the vendor prefix and any
# trailing date rather than truncating, so an id this script has never seen
# still reads as a model name.
model_name() {
  case "$1" in
    *opus-5*)     echo "Opus 5" ;;
    *sonnet-5*)   echo "Sonnet 5" ;;
    *fable-5*)    echo "Fable 5" ;;
    *opus-4-6*)   echo "Opus 4.6" ;;
    *opus-4-5*)   echo "Opus 4.5" ;;
    *opus-4*)     echo "Opus 4" ;;
    *sonnet-4-6*) echo "Sonnet 4.6" ;;
    *sonnet-4-5*) echo "Sonnet 4.5" ;;
    *sonnet-4*)   echo "Sonnet 4" ;;
    *haiku-4-5*)  echo "Haiku 4.5" ;;
    *haiku*)      echo "Haiku" ;;
    '')           echo "unknown" ;;
    *)            echo "$1" | sed -E 's/^(us\.)?anthropic\.//; s/^claude-//; s/-[0-9]{8}$//' ;;
  esac
}

# 10-cell bar, coloured by how close the context is to full.
context_slot() {
  local pct="${1%%.*}" width=10 colour bar i
  [ -n "${pct}" ] || return 0
  if   [ "${pct}" -ge 90 ]; then colour="${RED}"
  elif [ "${pct}" -ge 70 ]; then colour="${YELLOW}"
  else                           colour="${GREEN}"
  fi
  local filled=$(( pct * width / 100 ))
  [ "${filled}" -gt "${width}" ] && filled="${width}"
  [ "${filled}" -lt 0 ] && filled=0
  bar="${colour}"
  for ((i = 0; i < filled; i++)); do bar+="█"; done
  bar+="${DIM}"
  for ((i = filled; i < width; i++)); do bar+="░"; done
  bar+="${RST} ${DIM}${pct}%${RST}"
  if [ -n "${ctx_size}" ] && [ "${ctx_size}" -gt 0 ] 2>/dev/null; then
    if [ "${ctx_size}" -ge 1000000 ]; then
      bar+=" ${DIM}$(( ctx_size / 1000000 ))M${RST}"
    else
      bar+=" ${DIM}$(( ctx_size / 1000 ))k${RST}"
    fi
  fi
  printf '%s' "${bar}"
}

# ms -> 45s / 15m / 2h10m
duration() {
  local ms="$1" total min hr
  [ -n "${ms}" ] && [ "${ms}" -gt 0 ] 2>/dev/null || return 0
  total=$(( ms / 1000 )); min=$(( total / 60 )); hr=$(( min / 60 ))
  if   [ "${hr}" -gt 0 ] && [ $(( min % 60 )) -gt 0 ]; then printf '%dh%dm' "${hr}" "$(( min % 60 ))"
  elif [ "${hr}" -gt 0 ]; then printf '%dh' "${hr}"
  elif [ "${min}" -gt 0 ]; then printf '%dm' "${min}"
  else printf '%ds' "${total}"
  fi
}

parts=()
[ "${IS_SANDBOX:-}" = "1" ] && parts+=("${MAGENTA}⬡ container${RST}")
parts+=("$(model_name "${model_id}")")
slot="$(context_slot "${ctx_pct}")"; [ -n "${slot}" ] && parts+=("${slot}")
dur="$(duration "${duration_ms}")"; [ -n "${dur}" ] && parts+=("${DIM}${dur}${RST}")

location="$(basename "${cwd}")"
[ -n "${branch}" ] && location="${location}${DIM}:${RST}${branch}"
parts+=("${location}")

line=""
for part in "${parts[@]}"; do
  [ -n "${line}" ] && line+="${SEP}"
  line+="${part}"
done
printf '%s\n' "${line}"
