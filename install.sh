#!/bin/sh
# Install WIRK on this machine: https://wirk.life/install
#
#   curl -fsSL https://wirk.life/install | sh
#   sh install.sh --dry-run        show the steps without changing anything
#   sh install.sh --yes            answer yes to every question
#   sh install.sh --url URL        connect to another WIRK service
#
# It installs uv if it is missing (after asking), installs the wirk command and the wirk-mcp server from the
# release, checking their SHA-256 sums, sets up Claude Code and Codex when they are present (after asking), and
# runs wirk login. It never uses sudo, and running it again is safe.
set -eu

VERSION="${WIRK_VERSION:-0.4.1}"
BASE="${WIRK_RELEASE_BASE:-https://github.com/wirkspace}"  # where the releases live; a mirror or a test may change it
DRY=0
YES=0
URL=""

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --yes | -y) YES=1 ;;
    --url) shift; URL="${1:?--url needs the service address}" ;;
    -h | --help) sed -n '2,11p' "$0" 2> /dev/null || true; exit 0 ;;
    *) echo "install.sh: unknown option $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

ok() { printf '  \342\234\223 %s\n' "$*"; }  # ✓
note() { printf '  - %s\n' "$*"; }
fail() { printf '\n%s\n' "$*" >&2; exit 1; }

quietly() {  # run a step without its chatter; show the chatter only if it fails (a dry run shows the command)
  if [ "$DRY" -eq 1 ]; then printf '    would run: %s\n' "$*"; return 0; fi
  "$@" > "$TMP/log" 2>&1 || { cat "$TMP/log" >&2; fail "This step failed: $*"; }
}

ask() {  # yes or no from the person at the terminal; --yes and --dry-run say yes, no terminal says no
  if [ "$YES" -eq 1 ] || [ "$DRY" -eq 1 ]; then return 0; fi
  answer=n
  { printf '%s [Y/n] ' "$1" > /dev/tty && read -r answer < /dev/tty; } 2> /dev/null || answer=n
  case "$answer" in "" | y | Y | yes | Yes) return 0 ;; *) return 1 ;; esac
}

sha256() {
  if command -v sha256sum > /dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

fetch() {  # fetch REPO FILE: one release file, checked against that release's SHA256SUMS
  quietly curl -fsSL -o "$TMP/$1.sums" "$BASE/$1/releases/download/v$VERSION/SHA256SUMS"
  quietly curl -fsSL -o "$TMP/$2" "$BASE/$1/releases/download/v$VERSION/$2"
  if [ "$DRY" -eq 1 ]; then return 0; fi
  want=$(awk -v name="$2" '$2 == name { print $1 }' "$TMP/$1.sums")
  if [ -z "$want" ] || [ "$want" != "$(sha256 "$TMP/$2")" ]; then
    fail "$2 does not match its SHA-256 sum in the $1 release; nothing was installed."
  fi
}

printf 'Installing WIRK %s\n' "$VERSION"
if [ "$DRY" -eq 1 ]; then printf '(dry run: nothing will be changed)\n'; fi
printf '\n'

# uv installs the two commands, each in its own environment
UV=$(command -v uv 2> /dev/null || true)
for candidate in "${XDG_BIN_HOME:-}/uv" "$HOME/.local/bin/uv"; do
  if [ -z "$UV" ] && [ -x "$candidate" ]; then UV="$candidate"; fi
done
if [ -n "$UV" ]; then
  ok "uv is installed"
elif ask "WIRK installs with uv, which is missing. Install uv from astral.sh into your home folder?"; then
  quietly sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
  UV="${XDG_BIN_HOME:-$HOME/.local/bin}/uv"
  ok "uv installed"
else
  fail "WIRK needs uv. Install it (curl -LsSf https://astral.sh/uv/install.sh | sh, or brew install uv), then run this again."
fi

# the wirk command and the wirk-mcp server
CLI="wirk-$VERSION-py3-none-any.whl"
MCP="wirk_mcp-$VERSION-py3-none-any.whl"
installed() { [ "$DRY" -eq 0 ] && "$UV" tool list 2> /dev/null | grep -qx "$1 v$VERSION"; }
if installed wirk && installed wirk-mcp; then
  ok "wirk and wirk-mcp $VERSION are already installed"
else
  fetch wirk-cli "$CLI"
  fetch wirk-mcp "$MCP"
  ok "Downloaded wirk and wirk-mcp $VERSION; their SHA-256 sums match the release"
  quietly "$UV" tool install --force "$TMP/$CLI"
  quietly "$UV" tool install --force "$TMP/$MCP" --with "$TMP/$CLI"
  ok "Installed wirk and wirk-mcp"
fi
BIN=$("$UV" tool dir --bin 2> /dev/null || printf '%s' "$HOME/.local/bin")

# the agent hosts on this machine: the MCP server and the WIRK skill
for host in claude codex; do
  if [ "$host" = claude ]; then name="Claude Code"; add="claude mcp add --scope user"; else name="Codex"; add="codex mcp add"; fi
  if ! command -v "$host" > /dev/null 2>&1; then
    note "$name is not installed; skipped"
    continue
  fi
  if ! ask "Set up $name with the WIRK MCP server and skill?"; then
    note "$name left as it is (later: $add wirk -- $BIN/wirk-mcp)"
    continue
  fi
  if [ "$DRY" -eq 0 ] && "$host" mcp get wirk > /dev/null 2>&1; then
    note "$name already has an MCP server named wirk; left as it is"
  else
    # shellcheck disable=SC2086  # $add is two or three words on purpose
    quietly $add wirk -- "$BIN/wirk-mcp"
  fi
  if [ -z "${SKILL:-}" ]; then fetch wirk-skill SKILL.md; SKILL=1; fi
  quietly mkdir -p "$HOME/.$host/skills/wirk"
  quietly cp "$TMP/SKILL.md" "$HOME/.$host/skills/wirk/SKILL.md"
  ok "$name: the WIRK MCP server and skill"
done

# connect this machine
case ":$PATH:" in *":$BIN:"*) ;; *) note "To type wirk directly, add $BIN to your PATH (uv tool update-shell does it)" ;; esac
printf '\nConnecting this machine (wirk login)\n'
if [ -n "$URL" ]; then set -- login --url "$URL"; else set -- login; fi
if [ "$DRY" -eq 1 ]; then
  printf '    would run: %s/wirk %s\n' "$BIN" "$*"
elif ! "$BIN/wirk" "$@"; then
  printf '\nwirk login did not finish; run it again when you are ready: wirk login\n'
fi
printf '\nDone. Start with: wirk status\n'
