#!/usr/bin/env python3
"""Claude Code status line: folder, git branch, model, effort, 5h + weekly usage.

Reads the status line JSON payload on stdin (schema: `claude` 2.1.x), prints one line.

    we-rewrite-compact │ rewrite-0809-integrated │ Opus 5 1M xhigh │ 5h ██░░ 24% ·1h43m │ …

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

Why it has to be fast
---------------------
It is registered at `refreshInterval: 1`, because the payload's own change-detection
watches the model, effort, token usage and permission mode but *not* the working directory
or the git branch -- switching branch fires no event, so a once-per-second re-run is the
only thing that keeps those two cells honest. That budget is why the network and
subprocess imports are deferred into the paths that actually use them: `urllib.request`
alone costs 47 ms, three times the work of a whole render, and only the detached poll child
ever needs it.

Credentials are read, never written. Refreshing the OAuth token is Claude Code's job; on
401 this backs off and keeps rendering from cache.
"""

import json
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
ANSI = re.compile(r"\033\[[0-9;]*m")

CACHE_PATH = os.path.expanduser("~/.claude/statusline-usage.json")
LOCK_PATH = CACHE_PATH + ".lock"
CONFIG_PATH = os.path.expanduser("~/.claude.json")
CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
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
WINDOWS = ("five_hour", "seven_day")
GROUPS = {"five_hour": "session", "seven_day": "weekly"}

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


# --------------------------------------------------------------------------- merging


def normalize(entry):
    """Accept any of the source shapes and return {used_percentage, resets_at}, or None.

    Header-derived payloads carry `used_percentage` and epoch seconds; the usage endpoint
    and ~/.claude.json carry `utilization` and an ISO 8601 string; entries inside `limits`
    carry `percent`. This validates shape only -- whether the window is still running is
    `plausible`'s question, and the two callers want different answers.
    """
    if not isinstance(entry, dict):
        return None
    pct = entry.get("used_percentage", entry.get("utilization", entry.get("percent")))
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


def scoped_limits(utilization):
    """One pseudo-source per entry of the account cache's `limits` list.

    `limits` carries constraints the flat `five_hour`/`seven_day` keys do not. A
    model-scoped weekly cap sits there at 75% with severity `warning` while `seven_day`
    still reads 70% -- the scoped one is what actually stops work, so folding it in through
    the same take-the-largest rule keeps the bar showing the limit you will hit first.
    """
    rows = []
    for item in utilization.get("limits") or []:
        if not isinstance(item, dict):
            continue
        for name, group in GROUPS.items():
            if item.get("group") == group:
                rows.append({name: item})
    return rows


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
    try:
        # Re-check under the lock. Children spawned while the previous poller held it would
        # otherwise each fire the moment it is released, turning one due refresh into a
        # burst of requests -- which is what earns a 429.
        cache = read_json(CACHE_PATH)
        if now - cache.get("polled_at", 0) < POLL_SECONDS or now < cache.get("retry_after", 0):
            return

        token = (read_json(CREDENTIALS_PATH).get("claudeAiOauth") or {}).get("accessToken")
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
        cache = blend_into_cache(read_json(CACHE_PATH), [fresh] + scoped_limits(fresh), now)
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
                backoff = float(retry_after.strip())
        cache = read_json(CACHE_PATH)
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


def usage_color(pct):
    if pct >= 90:
        return BOLD + RED
    if pct >= 75:
        return RED
    if pct >= 50:
        return YELLOW
    return GREEN


def bar(pct):
    filled = min(BAR_CELLS, max(0, round(pct / 100 * BAR_CELLS)))
    return BAR_FULL * filled + BAR_EMPTY * (BAR_CELLS - filled)


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


def window(label, data, was_seen, now):
    """One rate-limit window: `5h ████░░░░  47% ·2h13m`."""
    if data is None:
        if was_seen:
            # The window rolled over, so its old figure is gone and usage restarted at ~0.
            return f"{DIM}{label}{RESET} {GREEN}{BAR_EMPTY * BAR_CELLS}{RESET} {DIM}~0%{RESET}"
        return f"{DIM}{label} {BAR_EMPTY * BAR_CELLS}  --{RESET}"
    pct = data["used_percentage"]
    return (
        f"{DIM}{label}{RESET} {usage_color(pct)}{bar(pct)} {pct:3.0f}%{RESET}"
        f" {DIM}·{countdown(data['resets_at'], now)}{RESET}"
    )


def short_model(name):
    # "Opus 5 (1M context)" is too wide for a status line; keep the capacity hint.
    return name.replace(" (1M context)", " 1M").replace("Claude ", "")


def printed_width(text):
    """Columns actually occupied, discounting the SGR escapes woven through every cell."""
    return len(ANSI.sub("", text))


def terminal_columns():
    """Claude Code exports COLUMNS to the status line command; our stdout is a pipe.

    Read from the environment rather than `shutil.get_terminal_size`, which costs 9 ms of
    imports to reach the same variable and then falls back to 80 anyway.
    """
    try:
        return int(os.environ["COLUMNS"])
    except (KeyError, ValueError):
        return 80


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
    cache = read_json(CACHE_PATH)
    utilization = sub_dict(sub_dict(read_json(CONFIG_PATH), "cachedUsageUtilization"), "utilization")
    contributions = [sub_dict(payload, "rate_limits"), utilization] + scoped_limits(utilization)

    merged = blend_into_cache(cache, contributions, now)
    if merged != cache:
        write_cache(merged)
    spawn_poll(merged, now)

    cwd = sub_dict(payload, "workspace").get("current_dir") or payload.get("cwd") or os.getcwd()
    cwd = cwd.rstrip("/") if isinstance(cwd, str) else os.getcwd()
    model = sub_dict(payload, "model").get("display_name")
    model = model if isinstance(model, str) and model else "?"
    effort = sub_dict(payload, "effort").get("level")

    model_cell = f"{BLUE}{short_model(model)}{RESET}"
    if effort:
        model_cell += f" {DIM}{effort}{RESET}"

    cells = [f"{CYAN}{BOLD}{os.path.basename(cwd)}{RESET}", model_cell]
    for label, name in (("5h", "five_hour"), ("wk", "seven_day")):
        seen = any(normalize(c.get(name)) for c in contributions + [cache])
        cells.append(window(label, merged.get(name), seen, now))

    # The branch takes only what the fixed cells leave. Claude Code renders this on one row
    # and truncates the overflow, so sizing it blind would push the usage bars off a narrow
    # terminal -- and the bars are the reason this script exists.
    branch = git_branch(cwd)
    if branch:
        room = min(
            BRANCH_CELLS,
            terminal_columns() - printed_width(SEP.join(cells)) - printed_width(SEP),
        )
        if room >= BRANCH_FLOOR:
            cells.insert(1, f"{MAGENTA}{elide(branch, room)}{RESET}")

    sys.stdout.write(SEP.join(cells))


if __name__ == "__main__":
    main()
