#!/bin/sh
# One-command install for the Claude Code status line.
#
#   curl -fsSL https://xiaweiliu.com/claude-status-bar/install.sh | sh
#
# Downloads statusline.py to ~/.claude/ and registers it in ~/.claude/settings.json,
# preserving every other setting. Safe to re-run; that is also how you upgrade.
set -eu

SOURCE="${CLAUDE_STATUSBAR_SOURCE:-https://raw.githubusercontent.com/williamliu0516/claude-status-bar/main/statusline.py}"

fail() {
	printf 'install: %s\n' "$*" >&2
	exit 1
}

command -v python3 >/dev/null 2>&1 || fail "python3 is required. Install it and re-run."

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' ||
	fail "python3 3.8 or newer is required (found $(python3 -V 2>&1))."

tmp="$(mktemp "${TMPDIR:-/tmp}/statusline.XXXXXX")" || fail "cannot create a temporary file."
trap 'rm -f "$tmp"' EXIT INT TERM

if command -v curl >/dev/null 2>&1; then
	curl -fsSL "$SOURCE" -o "$tmp" || fail "download failed: $SOURCE"
elif command -v wget >/dev/null 2>&1; then
	wget -qO "$tmp" "$SOURCE" || fail "download failed: $SOURCE"
else
	fail "need curl or wget to download the status line."
fi

# A captive portal or a redirect to a login page answers 200 with HTML, which -f cannot
# catch. Refuse to install anything that is not the script we asked for.
head -n 1 "$tmp" | grep -q '^#!/usr/bin/env python3' ||
	fail "downloaded file is not statusline.py. Check $SOURCE"

python3 "$tmp" --install
