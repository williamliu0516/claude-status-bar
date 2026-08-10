#!/bin/sh
# One-command install for the Claude Code status line.
#
#   curl -fsSL https://xiaweiliu.com/claude-status-bar/install.sh | sh
#
# Downloads statusline.py to ~/.claude/ and registers it in ~/.claude/settings.json,
# preserving every other setting. Safe to re-run; that is also how you upgrade.
#
# Set CLAUDE_STATUSBAR_SOURCE to install from somewhere else (a fork, a local file://
# path). Otherwise the mirrors below are tried in order, so the install works whether or
# not GitHub Pages is serving the custom domain.
set -eu

MIRRORS="${CLAUDE_STATUSBAR_SOURCE:-}"
if [ -z "$MIRRORS" ]; then
	MIRRORS="https://xiaweiliu.com/claude-status-bar/statusline.py
https://raw.githubusercontent.com/williamliu0516/claude-status-bar/main/statusline.py"
fi

fail() {
	printf 'install: %s\n' "$*" >&2
	exit 1
}

command -v python3 >/dev/null 2>&1 || fail "python3 is required. Install it and re-run."

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' ||
	fail "python3 3.8 or newer is required (found $(python3 -V 2>&1))."

# Mirror failures are expected -- we fall through to the next one -- so their diagnostics
# are suppressed. Only the final "no mirror worked" message reaches the user.
if command -v curl >/dev/null 2>&1; then
	fetch() { curl -fsSL "$1" -o "$2" 2>/dev/null; }
elif command -v wget >/dev/null 2>&1; then
	fetch() { wget -qO "$2" "$1" 2>/dev/null; }
else
	fail "need curl or wget to download the status line."
fi

tmp="$(mktemp "${TMPDIR:-/tmp}/statusline.XXXXXX")" || fail "cannot create a temporary file."
trap 'rm -f "$tmp"' EXIT INT TERM

got=""
for source in $MIRRORS; do
	fetch "$source" "$tmp" || continue
	# A captive portal or a redirect to a login page answers 200 with HTML, which curl -f
	# cannot catch. Refuse to install anything that is not the script we asked for.
	if head -n 1 "$tmp" | grep -q '^#!/usr/bin/env python3'; then
		got="$source"
		break
	fi
done

[ -n "$got" ] || fail "could not download statusline.py from any of:
$MIRRORS
If the repository is private, make it public or set CLAUDE_STATUSBAR_SOURCE."

printf 'install: fetched %s\n' "$got"
python3 "$tmp" --install
