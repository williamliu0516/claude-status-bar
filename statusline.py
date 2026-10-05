#!/usr/bin/env python3
"""Claude Code status line: folder, git branch, model, effort, 5h + weekly usage.

Reads the status line JSON payload on stdin (schema: `claude` 2.1.x), prints one line.

    we-rewrite-compact │ rewrite-0809-integrated │ Opus 5.5 1M xhigh │ 5h ██░░ 24% ·1h43m │ …

Usage is colored by pace -- the figure each window is on course to reach by its reset --
rather than by how much is spent so far, and the projection is printed (`62% →104%`) once
it runs hot. On a narrow terminal the line sheds detail in a fixed order (see `LAYOUTS`)
instead of letting Claude Code cut the weekly bar off the end.

Install with `python3 statusline.py --install`.

Why this is more than a formatter
---------------------------------
The `rate_limits` block in the payload comes from a *per-process, in-memory* cache. Its
only writers are the startup prefetch and the response headers of that same session's API
calls -- there is no timer, so nothing refreshes it while a session sits idle. Observed
live: six concurrent sessions reporting 5h=3/17/25/29/90 and wk=10/12/67/89 at the same
instant, the extremes being snapshots from a previous window and a previous week.
`refreshInterval` re-runs this command but hands it that same frozen cache, so it cannot
fix either problem.

So usage is resolved from four sources, newest-wins, and shared through one file that all
sessions read and write:

  1. a live poll of GET /api/oauth/usage, throttled to one request per POLL_SECONDS across
     every session and run detached so rendering never waits on the network
  2. this session's stdin payload
  3. `cachedUsageUtilization` in ~/.claude.json, which Claude Code maintains account-wide
  4. the shared cache below, carrying whatever any other session last observed

They are merged by a rule that needs no trust in write order or clock skew:

  1. Discard any window whose `resets_at` is already past, or implausibly far ahead.
  2. Prefer the observation with the latest `resets_at`; a later boundary is a newer window.
  3. Within that window take the LARGEST utilization, since usage only grows until reset.

A stale snapshot therefore can neither pull the number backwards nor revive a dead window.

The same rule makes every reading from a previous login permanent -- a higher figure or a
later boundary outranks the new account's for up to a week -- so everything is attributed
to an account. The cache is stamped with `oauthAccount.accountUuid` and discarded when it
changes, ~/.claude.json's figures count only when stamped with the same account, and a
session's payload only when that session started under it and its window matches one the
account is known to have.

Why it has to be fast
---------------------
It is registered at `refreshInterval: 1`, because the payload's own change-detection
watches the model, effort, token usage and permission mode but *not* the working directory
or the git branch -- switching branch fires no event, so a once-per-second re-run is the
only thing that keeps those two cells honest. That budget is why the network and
subprocess imports are deferred into the paths that actually use them: `urllib.request`
alone costs 47 ms, three times the work of a whole render, and only the detached poll child
ever needs it.

Credentials are read, never written, from where Claude Code keeps them: the Keychain on
macOS, then ~/.claude/.credentials.json. Refreshing the OAuth token is Claude Code's job;
on 401 this backs off and keeps rendering from cache.
"""

import json
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timezone

BAR_CELLS = 8
BAR_FULL = "█"
BAR_EMPTY = "░"
BRANCH_CELLS = 32
BRANCH_FLOOR = 10
STATUS_INSET = 4
ANSI = re.compile(r"\033\[[0-9;]*m")

CACHE_PATH = os.path.expanduser("~/.claude/statusline-usage.json")
LOCK_PATH = CACHE_PATH + ".lock"
CONFIG_PATH = os.path.expanduser("~/.claude.json")
CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
KEYCHAIN_SERVICE = "Claude Code-credentials"
SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
INSTALL_PATH = os.path.expanduser("~/.claude/statusline.py")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
POLL_SECONDS = float(os.environ.get("CLAUDE_STATUSLINE_POLL_SECONDS", "60"))
POLL_ENABLED = os.environ.get("CLAUDE_STATUSLINE_POLL", "1") != "0"
REFRESH_INTERVAL = 1
FAILURE_BACKOFF = 120.0
THROTTLED_BACKOFF = 300.0
LOCK_STALE = 120.0
WINDOW_TOLERANCE = 120.0
MAX_WINDOW = 8 * 86400
SESSION_TTL = 7 * 86400
MAX_SESSIONS = 256
WINDOWS = ("five_hour", "seven_day")
WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
PACE_FLOOR = 0.15
PACE_WARN = 80

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
SEP = f"{DIM}  │  {RESET}"
SEP_NARROW = f"{DIM} │ {RESET}"

# ---------------------------------------------------------------- identity
#
# SESSION IDENTIFIER SPEC v1 -- implemented identically here and in
# context-keyboard-display/collect.py, which draws the same tag and colour on a
# 142x428 keyboard panel. The two repositories share no code, so the
# specification below is the entire contract; it is reproduced verbatim in both
# READMEs. Integer arithmetic only, deliberately: no float, no locale, no
# terminal metrics, so two independent implementations cannot drift.
#
#   tag   = session_id[:6], lowercased          (a UUID, so these are hex)
#   slot  = sha1(session_id utf-8).digest()[0] % 8
#   xterm = IDENT_PALETTE[slot]
#   rgb   = the xterm-256 colour cube entry for that index:
#             i = xterm - 16;  r = i // 36;  g = (i // 6) % 6;  b = i % 6
#             rgb = (IDENT_CUBE[r], IDENT_CUBE[g], IDENT_CUBE[b])
#
# Eight slots, not sixteen, and the reason is measured rather than assumed. The
# panel's own source records that two of its semantic colours "are too close in
# hue to tell apart" at a 12 px dot; that pair is dE 37.3 in CIE-Lab. A
# sixteen-slot palette gets its two nearest members down to dE 30.5 -- below
# the distance already proven indistinguishable. Eight slots hold dE 61.5,
# 1.65x that threshold, and stay dE 34.1 clear of every colour the panel uses
# to mean something.
#
# Eight also divides 256, so `digest[0] % 8` is exactly uniform where % 10 or
# % 12 would over-weight the low slots.
#
# Fewer slots means colours do repeat across concurrent sessions. That is the
# honest trade: a repeat is *visibly identical*, which reads as "check the
# tag", where a sixteen-slot near-miss would read as "these are different"
# when they are not. The tag is the authority; the colour is the fast path.
#
# 256-colour SGR, not 24-bit: Terminal.app renders the former and ignores the
# latter, and the panel quantises to the same cube so both show one colour.
IDENT_CUBE = (0, 95, 135, 175, 215, 255)
IDENT_PALETTE = (45, 46, 49, 69, 201, 202, 211, 228)
IDENT_DOT = "\u25cf"


def ident_cell(session_id):
    """Colour dot + six-character tag, or None when the payload carries no id.

    None rather than a placeholder: a tag that matches nothing on the panel is
    a cell's worth of noise in a line that is already fighting for columns.
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    slot = hashlib.sha1(session_id.encode("utf-8")).digest()[0] % len(IDENT_PALETTE)
    xterm = IDENT_PALETTE[slot]
    return f"\033[38;5;{xterm}m{IDENT_DOT}{RESET} {DIM}{session_id[:6].lower()}{RESET}"


# --------------------------------------------------------------------------- merging


def normalize(entry):
    """Accept any of the source shapes and return {used_percentage, resets_at}, or None.

    Header-derived payloads carry `used_percentage` and epoch seconds; the usage endpoint
    and ~/.claude.json carry `utilization` and an ISO 8601 string. This validates shape
    only -- whether the window is still running is `plausible`'s question, and the two
    callers want different answers.
    """
    if not isinstance(entry, dict):
        return None
    pct = entry.get("used_percentage", entry.get("utilization"))
    resets = entry.get("resets_at")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    if isinstance(resets, str):
        try:
            # `fromisoformat` only learned to accept a trailing Z in 3.11.
            parsed = datetime.fromisoformat(resets.replace("Z", "+00:00"))
        except ValueError:
            return None
        # A naive stamp is UTC. Left naive, .timestamp() reads it as local time and shifts
        # every countdown by the machine's offset -- seven hours, on this one.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        resets = parsed.timestamp()
    if not isinstance(resets, (int, float)) or isinstance(resets, bool):
        return None
    return {"used_percentage": float(pct), "resets_at": float(resets)}


def plausible(entry, now):
    """Is this observation describing a window that is currently running?

    The upper bound is not decoration. `merge` prefers the observation with the latest
    boundary, so a single absurd future timestamp -- a corrupt response, a skewed clock, a
    harness feeding synthetic values -- wins forever, evicts every real reading, and
    freezes the countdown at nonsense until someone deletes the cache by hand.
    """
    return entry is not None and now < entry["resets_at"] <= now + MAX_WINDOW


def merge(entries, now):
    """Best current estimate for one window, given observations of unknown age.

    Boundaries are compared with a tolerance because each response recomputes `resets_at`
    at its own sub-second precision -- the same 03:20:00 window arrives as ...800.997,
    ...800.225 and ...800.027 from the three sources. Exact grouping reads that drift as
    three different windows and throws away every observation but one, freezing the number.
    A real rollover moves the boundary by hours, so no tolerance this small can merge two
    genuinely different windows.
    """
    live = [e for e in (normalize(x) for x in entries) if plausible(e, now)]
    if not live:
        return None
    newest = max(e["resets_at"] for e in live)
    current = [e for e in live if newest - e["resets_at"] <= WINDOW_TOLERANCE]
    return {
        "used_percentage": max(e["used_percentage"] for e in current),
        "resets_at": newest,
    }


# ----------------------------------------------------------------------------- store


def read_json(path):
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def sub_dict(mapping, key):
    """`mapping[key]` when it is a dict, else {}.

    ~/.claude.json is a large file maintained by Claude Code and freely edited by hand, so
    a key holding a list or a null instead of an object is a real possibility -- and an
    AttributeError here blanks the whole status line.
    """
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


def current_account(config):
    """The logged-in account's UUID from ~/.claude.json, or None when there is no login."""
    uuid = sub_dict(config, "oauthAccount").get("accountUuid")
    return uuid if isinstance(uuid, str) and uuid else None


def account_cache(account):
    """The shared cache, or a fresh one when it was written for a different account.

    Every figure in it is a max over observations, so a window left over from the account
    you just switched away from is not merely stale: if it reads higher, or resets later,
    it outranks every reading of the new account until its own window expires -- a week,
    for the weekly bar. The poll bookkeeping goes too, so the new account is fetched on the
    next render rather than after the old one's backoff.
    """
    cache = read_json(CACHE_PATH)
    if cache.get("account") != account:
        cache = {"account": account, "sessions": sub_dict(cache, "sessions")}
    return cache


def own_session(cache, session_id, account, now):
    """Did this session start under `account`? Records it on first sight.

    A running session keeps the token it started with, so after a switch its payload goes
    on describing the previous login -- and window boundaries alone cannot tell the two
    apart, since both accounts' five-hour windows can close on the same minute. The
    account current when a session first renders is the one it runs on, so that is
    recorded once and trusted for the session's lifetime. A session that did pick up the
    new login merely loses its payload; the poll and ~/.claude.json still cover it.
    """
    if not isinstance(session_id, str) or not session_id:
        return True
    sessions = sub_dict(cache, "sessions")
    seen = sessions.get(session_id)
    if not isinstance(seen, list) or len(seen) != 2:
        live = {
            key: value
            for key, value in sessions.items()
            if isinstance(value, list)
            and len(value) == 2
            and isinstance(value[1], (int, float))
            and now - value[1] < SESSION_TTL
        }
        newest = sorted(live.items(), key=lambda item: item[1][1])[-(MAX_SESSIONS - 1) :]
        seen = [account, now]
        cache["sessions"] = dict(newest, **{session_id: seen})
    return seen[0] == account


def config_usage(config, account):
    """`cachedUsageUtilization` from ~/.claude.json, when it belongs to `account`.

    Claude Code stamps it with the account it was fetched for, and does not clear it on a
    switch; until it next refreshes, it still describes the previous login.
    """
    cached = sub_dict(config, "cachedUsageUtilization")
    owner = cached.get("accountUuid")
    if owner is not None and owner != account:
        return {}
    return sub_dict(cached, "utilization")


def anchored(rate_limits, anchors, account, now):
    """The payload's windows that agree with a reading known to be this account's.

    The payload names no account, and a session keeps the one it started with in memory
    -- it goes on billing, and reporting, the previous login for hours after a switch made
    in another window. So each of its windows is kept only when an attributable source
    (the poll, ~/.claude.json) has seen the same window. That costs nothing when it
    agrees: the payload still lifts the figure between polls, which is what it is for.
    With no login to attribute anything to there is nothing to confuse it with.
    """
    if account is None:
        return rate_limits
    kept = {}
    for name in WINDOWS:
        entry = normalize(rate_limits.get(name))
        if not plausible(entry, now):
            continue
        for anchor in (normalize(a.get(name)) for a in anchors):
            if (
                plausible(anchor, now)
                and abs(anchor["resets_at"] - entry["resets_at"]) <= WINDOW_TOLERANCE
            ):
                kept[name] = rate_limits[name]
                break
    return kept


def write_cache(data):
    """Atomic replace. A lost race between sessions self-heals on the next render.

    The temp file is same-directory and pid-suffixed so `os.replace` stays atomic without
    dragging `tempfile` into the render path.
    """
    tmp = f"{CACHE_PATH}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as handle:
            json.dump(data, handle)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def blend_into_cache(cache, contributions, now):
    """Fold new observations into the cache, keeping its non-window bookkeeping."""
    merged = dict(cache)
    for name in WINDOWS:
        best = merge([cache.get(name)] + [c.get(name) for c in contributions], now)
        if best is not None:
            merged[name] = best
        else:
            merged.pop(name, None)
    return merged


# ------------------------------------------------------------------------ live poll


def oauth_token(now):
    """The access token Claude Code is currently using, or None when there is no live one.

    On macOS Claude Code keeps its login in the Keychain and writes the plaintext file only
    when the Keychain cannot be reached -- an ssh session, typically. That file is then
    never cleaned up, so reading it first means polling with a token that expired long
    ago, or with a previous account's. Resolve in the same order Claude Code does.
    """
    blocks = []
    if sys.platform == "darwin":
        import subprocess

        try:
            found = subprocess.run(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
            blocks.append(json.loads(found.stdout).get("claudeAiOauth"))
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            pass
    blocks.append(read_json(CREDENTIALS_PATH).get("claudeAiOauth"))
    for block in blocks:
        if not isinstance(block, dict) or not block.get("accessToken"):
            continue
        expires = block.get("expiresAt")
        if isinstance(expires, (int, float)) and expires / 1000 <= now:
            return None  # the current login's token; refreshing it is Claude Code's job
        return block["accessToken"]
    return None


def poll():
    """Refresh the shared cache from the usage endpoint. Runs detached; output ignored."""
    import urllib.error
    import urllib.request

    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        try:
            if time.time() - os.stat(LOCK_PATH).st_mtime < LOCK_STALE:
                return  # another session is already polling
        except OSError:
            return
    except OSError:
        return

    now = time.time()
    account = current_account(read_json(CONFIG_PATH))
    try:
        # Re-check under the lock. Children spawned while the previous poller held it would
        # otherwise each fire the moment it is released, turning one due refresh into a
        # burst of requests -- which is what earns a 429.
        cache = account_cache(account)
        if now - cache.get("polled_at", 0) < POLL_SECONDS or now < cache.get("retry_after", 0):
            return

        token = oauth_token(now)
        if not token:
            return
        request = urllib.request.Request(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": OAUTH_BETA,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            fresh = json.load(response)
        cache = blend_into_cache(account_cache(account), [fresh], now)
        cache["polled_at"] = now
        cache.pop("retry_after", None)
        write_cache(cache)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
        # Expired token, offline, throttled, or a bad response: stay quiet, serve cache.
        # A 429 means we are asking too often, so back off harder than for a blip -- but
        # not so hard that the figures go visibly stale, since ~/.claude.json keeps being
        # refreshed by Claude Code itself and carries us through the gap.
        status = getattr(error, "code", None)
        backoff = THROTTLED_BACKOFF if status == 429 else FAILURE_BACKOFF
        if status == 429:
            headers = getattr(error, "headers", None)
            retry_after = headers.get("retry-after") if headers else None
            if retry_after and retry_after.strip().isdigit():
                # A floor, never a replacement. This endpoint answers `Retry-After: 0`,
                # which taken literally would cancel the backoff entirely and leave us
                # asking again at every poll interval for as long as it keeps refusing.
                backoff = max(backoff, float(retry_after.strip()))
        cache = account_cache(account)
        if not any(name in cache for name in WINDOWS):
            # Nothing to serve in the meantime -- the first poll after an account switch,
            # when the payload has no anchor and ~/.claude.json no figures yet. A long
            # backoff here is five minutes of blank bars, so retry at the poll interval.
            backoff = POLL_SECONDS
        cache["polled_at"] = now
        cache["retry_after"] = now + backoff
        write_cache(cache)
    finally:
        try:
            os.unlink(LOCK_PATH)
        except OSError:
            pass


def spawn_poll(cache, now):
    """Kick off a detached refresh if the shared cache is due for one."""
    if not POLL_ENABLED:
        return
    if now < cache.get("retry_after", 0):
        return
    if now - cache.get("polled_at", 0) < POLL_SECONDS:
        return
    import subprocess

    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--poll"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


# ------------------------------------------------------------------------------- git


def git_dir(start):
    """Locate the git directory governing `start`, or None outside a repository.

    A linked worktree's `.git` is a *file* holding `gitdir: <path>`, and its HEAD lives at
    that path -- there is no `.git` directory anywhere above it, so a plain walk up the tree
    reports "not a repository" for exactly the checkouts worktree users work in. Submodules
    use the same pointer, with a path relative to the file's own directory.
    """
    path = os.path.abspath(start)
    while True:
        candidate = os.path.join(path, ".git")
        if os.path.isdir(candidate):
            return candidate
        if os.path.isfile(candidate):
            try:
                with open(candidate) as handle:
                    pointer = handle.read().strip()
            except OSError:
                return None
            if not pointer.startswith("gitdir:"):
                return None
            target = pointer[len("gitdir:") :].strip()
            return target if os.path.isabs(target) else os.path.normpath(os.path.join(path, target))
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def git_branch(start):
    """Branch name for `start`, `@<short sha>` when HEAD is detached, or None.

    Read straight off disk rather than shelled out to `git`: the payload carries the
    worktree name and the GitHub coordinates but never the branch, and this runs every
    second. Two stats and one small read cost nothing; a subprocess per render would.
    """
    directory = git_dir(start)
    if directory is None:
        return None
    try:
        with open(os.path.join(directory, "HEAD")) as handle:
            head = handle.read().strip()
    except OSError:
        return None
    if head.startswith("ref: "):
        ref = head[len("ref: ") :]
        return ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
    # Detached: a bare rebase/bisect/checkout SHA. Marked so it cannot read as a branch.
    return f"@{head[:7]}" if head else None


def elide(text, width):
    """Trim from the middle, keeping both ends.

    Sibling branches routinely differ only in their suffix -- `rewrite-0809-integrated`
    against `rewrite-0809-integrated-compact` -- so cutting the tail would render two
    different branches identically, which is worse than showing nothing.
    """
    if len(text) <= width:
        return text
    keep = width - 1
    head = keep // 2
    return f"{text[:head]}…{text[head - keep :]}"


# --------------------------------------------------------------------------- display


def projected(data, length, now):
    """Utilization at reset if the rest of the window burns at its average rate so far.

    Elapsed time is floored at PACE_FLOOR of the window. In the first minutes one large
    prompt is most of what has been spent, and dividing by those minutes would read it as
    a runaway; the floor treats it as spread over the first ~45 min (5h) or ~25 h (weekly).
    It also absorbs most of the error from `resets_at` being reported rounded to the hour.
    """
    elapsed = length - (data["resets_at"] - now)
    return data["used_percentage"] / max(elapsed / length, PACE_FLOOR)


def usage_color(pct, pace):
    """Color by where the window is heading, not by how much of it is gone.

    Fixed thresholds on the spent figure paint 60% on the last day of the week the same
    yellow as 60% on its first, when only the second is a problem. `pace` is never below
    `pct`, so a window that is actually nearly full still shows it; past 90% it is flagged
    regardless, since a single long turn can cross the rest.
    """
    if pct >= 90:
        return BOLD + RED
    if pace >= 100:
        return RED
    if pace >= PACE_WARN:
        return YELLOW
    return GREEN


def bar(pct, cells):
    filled = min(cells, max(0, round(pct / 100 * cells)))
    return BAR_FULL * filled + BAR_EMPTY * (cells - filled)


def countdown(resets_at, now):
    """Compact time until the window resets, e.g. `3d4h`, `2h13m`, `47m`."""
    remaining = int(resets_at - now)
    if remaining <= 0:
        return "now"
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes = remaining // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def window(label, name, data, was_seen, now, layout):
    """One rate-limit window: `5h ████░░░░  47% ·2h13m`, or `… 62% →104% ·1h10m` when hot.

    The projection survives every layout step: on a narrow terminal it is the one figure
    that says whether to slow down, so a countdown or the bar goes first.
    """
    cells = layout["bar"]
    if data is None:
        if was_seen:
            # The window rolled over, so its old figure is gone and usage restarted at ~0.
            empty = f"{GREEN}{BAR_EMPTY * cells}{RESET} " if cells else ""
            return f"{DIM}{label}{RESET} {empty}{DIM}~0%{RESET}"
        empty = f" {BAR_EMPTY * cells} " if cells else ""
        return f"{DIM}{label}{empty} --{RESET}"
    pct = data["used_percentage"]
    pace = projected(data, WINDOW_SECONDS[name], now)
    color = usage_color(pct, pace)
    # Padded to three digits beside a bar so the bar does not jump as the figure grows.
    figure = f"{bar(pct, cells)} {pct:3.0f}%" if cells else f"{pct:.0f}%"
    cell = f"{DIM}{label}{RESET} {color}{figure}{RESET}"
    if pace >= PACE_WARN and round(pace) > round(pct):
        cell += f" {color}→{min(pace, 999):.0f}%{RESET}"
    if name in layout["countdown"]:
        cell += f" {DIM}·{countdown(data['resets_at'], now)}{RESET}"
    return cell


def short_model(name):
    # "Opus 5 (1M context)" is too wide for a status line; keep the capacity hint.
    return name.replace(" (1M context)", " 1M").replace("Claude ", "")


def printed_width(text):
    """Columns actually occupied, discounting the SGR escapes woven through every cell."""
    return len(ANSI.sub("", text))


def terminal_columns():
    """Columns the status line actually gets, from the COLUMNS Claude Code exports to it.

    COLUMNS is the whole terminal, but the line is drawn inside a box padded two columns
    on each side; measured on 2.1.289, a 120-column terminal shows 116 before Claude Code
    swaps the last one for `…`. Read from the environment rather than
    `shutil.get_terminal_size`, which costs 9 ms of imports to reach the same variable and
    then falls back to 80 anyway.
    """
    try:
        columns = int(os.environ["COLUMNS"])
    except (KeyError, ValueError):
        columns = 80
    return columns - STATUS_INSET


def ladder(first, *steps):
    """`first`, then each step applied on top of everything before it."""
    layouts = [first]
    for step in steps:
        layouts.append(dict(layouts[-1], **step))
    return tuple(layouts)


# Most detailed first; each step gives up the least useful thing still on the line. Claude
# Code renders the status line on one row and cuts the overflow from the end -- which is
# where the weekly bar sits -- so the trimming has to happen here, and the usage figures,
# the reason this script exists, are the last thing standing.
LAYOUTS = ladder(
    {
        "sep": SEP,
        "branch": True,
        "bar": BAR_CELLS,
        "countdown": WINDOWS,
        "effort": True,
        "folder": True,
        "model": True,
        "ident": True,
    },
    {"sep": SEP_NARROW},  # whitespace goes before anything that carries information
    {"branch": False},
    {"bar": BAR_CELLS // 2},
    {"bar": 0},
    {"countdown": ("five_hour",)},  # the weekly reset is days out; the 5h one is actionable
    {"countdown": ()},
    {"effort": False},
    {"folder": False},
    {"model": False},
    {"ident": False},  # outlasts the model: it is how a session is found on the keyboard panel
)


def render(layout, info, now, columns):
    """The status line under `layout`, and whether it fits in `columns`."""
    cells = []
    # Ahead of the folder: the identifier answers "which session am I looking at", which is
    # the question you ask before any of the numbers matter.
    if info["ident"] and layout["ident"]:
        cells.append(info["ident"])
    if layout["folder"]:
        cells.append(f"{CYAN}{BOLD}{info['folder']}{RESET}")
    branch_slot = len(cells)  # immediately after the folder, wherever the identifier left it
    if layout["model"]:
        model_cell = f"{BLUE}{short_model(info['model'])}{RESET}"
        if info["effort"] and layout["effort"]:
            model_cell += f" {DIM}{info['effort']}{RESET}"
        cells.append(model_cell)
    for label, name in (("5h", "five_hour"), ("wk", "seven_day")):
        cells.append(window(label, name, info["usage"].get(name), info["seen"][name], now, layout))

    sep = layout["sep"]
    room = columns - printed_width(sep.join(cells))
    if info["branch"] and layout["branch"]:
        # The branch takes only what the fixed cells leave, down to a floor below which a
        # name is no longer recognisable; under that, this layout does not fit.
        room = min(BRANCH_CELLS, room - printed_width(sep))
        if room < BRANCH_FLOOR:
            return sep.join(cells), False
        cells.insert(branch_slot, f"{MAGENTA}{elide(info['branch'], room)}{RESET}")
    return sep.join(cells), room >= 0


# ------------------------------------------------------------------------------ main


def install():
    """Copy this script to ~/.claude and register it in settings.json.

    Idempotent, and it preserves every other settings key. Claude Code has no account-level
    settings sync -- `statusLine` lives only in local settings.json, and the sole override
    is enterprise `policySettings` -- so a new machine needs this one command.
    """
    os.makedirs(os.path.dirname(INSTALL_PATH), exist_ok=True)

    source = os.path.abspath(__file__)
    if source != INSTALL_PATH:
        with open(source) as src, open(INSTALL_PATH, "w") as dst:
            dst.write(src.read())
        print(f"installed {INSTALL_PATH}")

    settings = read_json(SETTINGS_PATH)
    if os.path.exists(SETTINGS_PATH) and not settings:
        # Unreadable rather than absent. Overwriting would silently drop every other
        # setting, so keep a copy of whatever is there before replacing it.
        backup = f"{SETTINGS_PATH}.bak"
        os.replace(SETTINGS_PATH, backup)
        print(f"settings.json was unreadable; kept a copy at {backup}")
    settings["statusLine"] = {
        "type": "command",
        "command": f"python3 {INSTALL_PATH}",
        "refreshInterval": REFRESH_INTERVAL,
    }
    with open(SETTINGS_PATH, "w") as handle:
        json.dump(settings, handle, indent=2)
        handle.write("\n")
    print(f"registered statusLine in {SETTINGS_PATH}")
    print("open a new session (or restart an existing one) to pick it up")


def main():
    if "--install" in sys.argv:
        install()
        return
    if "--poll" in sys.argv:
        poll()
        return

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    now = time.time()
    config = read_json(CONFIG_PATH)
    account = current_account(config)
    stored = read_json(CACHE_PATH)
    cache = account_cache(account)
    utilization = config_usage(config, account)
    rate_limits = {}
    if own_session(cache, payload.get("session_id"), account, now):
        rate_limits = anchored(sub_dict(payload, "rate_limits"), [utilization, cache], account, now)
    contributions = [rate_limits, utilization]

    merged = blend_into_cache(cache, contributions, now)
    if merged != stored:
        write_cache(merged)
    spawn_poll(merged, now)

    cwd = sub_dict(payload, "workspace").get("current_dir") or payload.get("cwd") or os.getcwd()
    cwd = cwd.rstrip("/") if isinstance(cwd, str) else os.getcwd()
    model = sub_dict(payload, "model").get("display_name")
    model = model if isinstance(model, str) and model else "?"
    effort = sub_dict(payload, "effort").get("level")

    info = {
        "ident": ident_cell(payload.get("session_id")),
        "folder": os.path.basename(cwd),
        "branch": git_branch(cwd),
        "model": model,
        "effort": effort,
        "usage": merged,
        "seen": {
            name: any(normalize(c.get(name)) for c in contributions + [cache]) for name in WINDOWS
        },
    }
    columns = terminal_columns()
    # Nothing fitting at all leaves the sparsest line, for Claude Code to cut as it must.
    for layout in LAYOUTS:
        line, fits = render(layout, info, now, columns)
        if fits:
            break
    sys.stdout.write(line)


if __name__ == "__main__":
    main()
