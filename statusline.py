#!/usr/bin/env python3
"""Claude Code status line: folder, branch, actual model/account, context and usage.

Reads the status line JSON payload on stdin (schema: `claude` 2.1.x), prints one line.

    WE Opus5.5  ctx23%  5h24%  wk70%

Usage is numeric, with warnings colored by pace and the share spent. Wide panes can show
the weekly projection (`62% →104%`) and reset times. Narrow panes abbreviate the account
and model while keeping context and both available quota percentages visible.

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

So usage is resolved from three sources, newest-wins, and shared through one file that all
sessions read and write:

  1. a live poll of GET /api/oauth/usage, throttled to one request per POLL_SECONDS for
     each account and run detached so rendering never waits on the network
  2. this session's stdin payload
  3. the shared cache below, carrying whatever any other session last observed

Every reading is stamped `at` the moment it was observed: the poll's request time, or when
a session's payload last changed -- nothing but a response changes it, so that is when it
was true. A payload not yet seen to change has no such moment, since its session may have
sat on it for hours, and is left out until it does (see `observe`). Readings are then
merged by:

  1. Discard any window whose `resets_at` is already past, or implausibly far ahead.
  2. Prefer the observation with the latest `resets_at`; a later boundary is a newer window.
  3. Within that window the newest reading wins. Usage only grows until the reset, so a
     newest figure slightly below an older one is lag between sources and the higher one
     stands -- but a limit reset early keeps the boundary and zeroes the figure, so a drop
     of more than RESET_DROP is taken for one.

A stale snapshot therefore can neither pull the number backwards nor revive a dead window,
and an early reset shows at the next reading. (The rule this replaced took the largest
figure within the latest boundary, with no stamps: after the 2026-10-13 reset it held
weekly at 70% while the account read 2%, and would have until the week ran out.)

A later boundary would also make one login's readings outrank another's for up to a week,
so everything is attributed to the account that earned it, and the cache keeps one set of
windows per account.

Which account that is
---------------------
Not the one in ~/.claude.json. That file is shared by every Claude Code process on the
machine, but the login each one uses depends on where it runs: Claude Code reads
CLAUDE_CODE_OAUTH_TOKEN, then the Keychain, then ~/.claude/.credentials.json, and a process
that cannot reach the login Keychain -- every pane of a tmux server started over ssh or
mosh -- is refused and falls through to the file. So one Mac can bill two accounts at once,
one for the windows on its own screen and one for everything inside that tmux, while
`oauthAccount` names whichever logged in last. Its `cachedUsageUtilization` is no help
either: Claude Code stamps it with `oauthAccount`, whichever token fetched it.

So this resolves the token the same way from its own process, which runs where its
session does, and asks GET /api/oauth/profile once per token whose it is (see `identify`).
Claude Code re-reads its credentials when they change on disk, so that token is the one
the session sends. The payload names no account and lags a switch -- it holds what the
last response said -- so it is credited only once it has changed since the switch, and
only when its weekly window is the one the poll saw for the same account (see `anchored`).

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

When the session uses Magpie, its latest completed request in the local usage ledger
identifies the actual model and account. Quotas come from Magpie's cache for that exact
account, not the CLI's native login or another session's latest request. No OAuth lookup
or network poll runs on this path. Before the first request the account reads Magpie….

`--account` still prints the native login's short name for older tmux integrations. It
cannot identify a gateway's routed account; the main line shows that per session instead.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

BRANCH_CELLS = 32
BRANCH_FLOOR = 10
STATUS_INSET = 4
ANSI = re.compile(r"\033\[[0-9;]*m")

CACHE_PATH = os.path.expanduser("~/.claude/statusline-usage.json")
LOCK_PATH = CACHE_PATH + ".lock"
ACCOUNTS_PATH = os.path.expanduser("~/.claude/statusline-accounts.json")
CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
KEYCHAIN_SERVICE = "Claude Code-credentials"
SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
INSTALL_PATH = os.path.expanduser("~/.claude/statusline.py")
MAGPIE_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "magpie")
MAGPIE_CACHE_DIR = os.path.expanduser("~/.claude/statusline-magpie")
MAGPIE_LOG_BYTES = 1024 * 1024
# The object form: newer Claude Code also takes `"attribution": false`, but older versions
# reject a boolean there. `includeCoAuthoredBy` is the deprecated key those older versions read.
NO_ATTRIBUTION = {"commit": "", "pr": "", "sessionUrl": False}
KEEP_ATTRIBUTION = os.environ.get("CLAUDE_STATUSBAR_KEEP_ATTRIBUTION", "0") != "0"

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
OAUTH_BETA = "oauth-2025-04-20"
POLL_SECONDS = float(os.environ.get("CLAUDE_STATUSLINE_POLL_SECONDS", "60"))
POLL_ENABLED = os.environ.get("CLAUDE_STATUSLINE_POLL", "1") != "0"
REFRESH_INTERVAL = 1
FAILURE_BACKOFF = 120.0
THROTTLED_BACKOFF = 300.0
LOCK_STALE = 120.0
WINDOW_TOLERANCE = 120.0
# Percentage points a newer reading of the same window must fall by to count as an early
# reset rather than lag between the poll and the response headers.
RESET_DROP = 5.0
MAX_WINDOW = 8 * 86400
SESSION_TTL = 7 * 86400
MAX_SESSIONS = 256
# How often a session looks up its login again. A Keychain read is a 24 ms subprocess, as
# much as the rest of a render, so not every second; a switch shows within this long.
IDENTITY_SECONDS = 10.0
MAX_TOKENS = 32
# A tmux bar that stopped reporting (old config, server gone) hands the cell back this soon.
TMUX_SHOWN_SECONDS = 60.0
WINDOWS = ("five_hour", "seven_day")
WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
PACE_FLOOR = 0.15
PACE_WARN = 80
CONTEXT_WARN = 60
CONTEXT_FULL = 80
# A weekly projection is useful on wide panes; short five-hour windows need only a timer.
PROJECTED = ("seven_day",)

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


def reading(entry, now):
    """An observation as merged: {used_percentage, resets_at, at}, {idle, at}, or None.

    `at` is when it was observed; one stored before readings carried it counts as oldest.
    An idle one is the poll finding no window running, which outranks older figures too.
    """
    if not isinstance(entry, dict):
        return None
    at = entry.get("at")
    at = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 0.0
    if entry.get("idle") is True:
        return {"idle": True, "at": at}
    found = normalize(entry)
    return dict(found, at=at) if plausible(found, now) else None


def fold(one, other):
    """The better of two readings of one window (see the module docstring for the rule).

    Boundaries are compared with a tolerance because each response recomputes `resets_at`
    at its own sub-second precision -- the same 03:20:00 window arrives as ...800.997,
    ...800.225 and ...800.027 from the three sources. Exact comparison reads that drift as
    a newer window. A real rollover moves the boundary by hours, so no tolerance this small
    can merge two genuinely different windows.
    """
    newer, older = (one, other) if one["at"] >= other["at"] else (other, one)
    if newer.get("idle") or older.get("idle"):
        return newer
    if abs(newer["resets_at"] - older["resets_at"]) > WINDOW_TOLERANCE:
        return newer if newer["resets_at"] > older["resets_at"] else older
    if older["used_percentage"] - newer["used_percentage"] > RESET_DROP:
        return newer
    return dict(newer, used_percentage=max(newer["used_percentage"], older["used_percentage"]))


def merge(entries, now):
    """Best current estimate for one window, or None if nothing describes a running one."""
    best = None
    for entry in entries:
        found = reading(entry, now)
        if found is not None:
            best = found if best is None else fold(best, found)
    return best


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


def load_cache():
    """The shared cache: one set of windows per account, and what each session runs on.

    Anything else in the file -- the single-account layout this replaced, stamped from
    ~/.claude.json -- is dropped on the next write.
    """
    raw = read_json(CACHE_PATH)
    return {"accounts": sub_dict(raw, "accounts"), "sessions": sub_dict(raw, "sessions")}


def known_account(accounts, token):
    """The account UUID `identify` recorded for `token`, or None if it has not seen it yet."""
    entry = sub_dict(sub_dict(accounts, "tokens"), token_key(token))
    uuid = entry.get("account")
    return uuid if isinstance(uuid, str) and uuid else None


def token_key(token):
    """A short digest to file a token under. The token itself is never written anywhere."""
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()[:16]


def account_label(profile):
    """`personal` for an individual plan, else the organization's own name.

    Claude.ai gives each individual subscriber an organization named "<email>'s
    Organization"; a team or enterprise one carries the name its admin chose, which is what
    tells two work accounts apart.
    """
    name = sub_dict(profile, "organization").get("name")
    if not isinstance(name, str) or not name.strip() or name.endswith("'s Organization"):
        return "personal"
    return name.strip()


def account_name(accounts, account):
    label = sub_dict(sub_dict(accounts, "accounts"), account).get("label") if account else None
    return label if isinstance(label, str) and label else None


# --------------------------------------------------------------------------- Magpie


def magpie_configured():
    """Use the session's endpoint first, including an explicit non-Magpie override."""
    base = os.environ.get("ANTHROPIC_BASE_URL")
    if base is None:
        base = sub_dict(read_json(SETTINGS_PATH), "env").get("ANTHROPIC_BASE_URL", "")
    return isinstance(base, str) and bool(re.match(
        r"^https?://(?:127\.0\.0\.1|localhost|\[::1\]):3425(?:/|$)", base
    ))


def magpie_record(session_id):
    """Last routed request for this CLI session, never a machine-wide last account.

    Scan only the ledger's tail and keep a small per-session result. An idle session's
    account survives a busy ledger or log rotation; unrelated sessions cannot replace it.
    Partial trailing records are ignored and retried on the next render.
    """
    if not isinstance(session_id, str) or not session_id:
        return {}
    import hashlib

    cache_path = os.path.join(MAGPIE_CACHE_DIR, hashlib.sha256(session_id.encode()).hexdigest() + ".json")
    cached = read_json(cache_path)
    record = sub_dict(cached, "record")
    try:
        with open(os.path.join(MAGPIE_DIR, "usage.jsonl"), "rb") as handle:
            stat = os.fstat(handle.fileno())
            signature = [stat.st_ino, stat.st_size, stat.st_mtime_ns]
            if cached.get("signature") == signature:
                return record
            start = max(0, stat.st_size - MAGPIE_LOG_BYTES)
            handle.seek(start)
            if start:
                handle.readline()  # the scan may begin partway through a record
            tail = handle.read(MAGPIE_LOG_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return record
    for line in reversed(tail.splitlines()):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if not isinstance(candidate, dict):
            continue
        actual_session = candidate.get("native_session") or candidate.get("session")
        if actual_session != session_id or not candidate.get("providerAccount"):
            continue
        record = {key: candidate[key] for key in (
            "provider", "providerAccount", "model", "served", "effort"
        ) if key in candidate}
        break
    try:
        os.makedirs(MAGPIE_CACHE_DIR, mode=0o700, exist_ok=True)
        write_json(cache_path, {"signature": signature, "record": record})
    except OSError:
        pass
    return record


def magpie_account_label(provider, user, accounts):
    if provider == "codex":
        return "Codex"
    if " · " in user:
        return user.rsplit(" · ", 1)[1]
    for person in sub_dict(accounts, "accounts").values():
        email = person.get("email") if isinstance(person, dict) else None
        if isinstance(email, str) and email.lower() == user.lower():
            label = person.get("label")
            if isinstance(label, str) and label:
                return "Personal" if label.lower() == "personal" else label
    return user.split("@", 1)[0] or provider


def magpie_model_name(served, payload_model):
    requested = payload_model.get("id")
    requested = requested.rsplit("/", 1)[-1].replace("[1m]", "") if isinstance(requested, str) else ""
    display = payload_model.get("display_name")
    if requested == served and isinstance(display, str) and display:
        return display
    match = re.match(r"^claude-(opus|sonnet|haiku|fable)-(\d+)(?:[.-](\d{1,2}))?(?:-|$)", served)
    if match:
        version = match[2] + ("." + match[3] if match[3] else "")
        return match[1].capitalize() + " " + version
    if served.startswith("gpt-"):
        return "GPT-" + "-".join(piece.capitalize() for piece in served[4:].split("-"))
    return served


def magpie_state(payload, accounts, now):
    """A routed account and its own cached windows, or None for native Claude use."""
    if not magpie_configured():
        return None
    record = magpie_record(payload.get("session_id"))
    state = {"account": "Magpie…", "model": None, "effort": None,
             "windows": [], "usage": {}, "seen": {}}
    if not record:
        return state
    provider, user = record.get("provider", ""), record.get("providerAccount", "")
    if not isinstance(provider, str) or not isinstance(user, str):
        return state
    state["account"] = magpie_account_label(provider, user, accounts)
    served = record.get("served") or record.get("model")
    if isinstance(served, str) and served:
        state["model"] = magpie_model_name(served.rsplit("/", 1)[-1], sub_dict(payload, "model"))
    state["effort"] = record.get("effort")
    # Exact match, case-insensitive: two seats of the same email can have distinct quotas.
    wanted = (provider + "/" + user).lower()
    quota = {}
    for key, entry in read_json(os.path.join(MAGPIE_DIR, "quotas.json")).items():
        if key.lower() == wanted and isinstance(entry, dict):
            quota = sub_dict(entry, "quota")
            break
    names = {"5 hours": "five_hour", "5h": "five_hour", "7 days": "seven_day", "7d": "seven_day"}
    windows = quota.get("windows", [])
    for item in windows if isinstance(windows, list) else []:
        if not isinstance(item, dict):
            continue
        name = names.get(str(item.get("name", "")).lower())
        if not name:
            continue  # a model-specific weekly pool has a different denominator
        value = item.get("used")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        state["seen"][name] = True
        found = normalize({"used_percentage": value, "resets_at": item.get("resetsAt")})
        if found and plausible(found, now):
            state["usage"][name] = found
    state["windows"] = [name for name in WINDOWS if name in state["seen"]]
    return state


def observe(seen, limits, now, switched):
    """The payload's windows, each stamped `at` with when its response arrived, else None.

    `seen` is what this session's previous render recorded. Claude Code rewrites every
    window from each response's headers, so when any of them changes or appears, a response
    has just arrived and all of them are as of now -- an unchanged weekly figure included.
    One dropping out is no news: Claude Code removes a window once its boundary passes.

    With nothing recorded -- a new session, or one that predates the stamps -- nobody saw
    the windows arrive, and after a login switch they still hold what the last response
    said under the old login. Either way they get no stamp until the next change.
    """
    windows = {name: normalize(limits.get(name)) for name in WINDOWS}
    windows = {name: found for name, found in windows.items() if found is not None}
    if not isinstance(seen, dict) or switched:
        return {name: dict(found, at=None) for name, found in windows.items()}

    def unchanged(name, found):
        before = sub_dict(seen, name)
        return (before.get("used_percentage"), before.get("resets_at")) == (
            found["used_percentage"],
            found["resets_at"],
        )

    if all(unchanged(name, found) for name, found in windows.items()):
        return {name: dict(found, at=seen[name].get("at")) for name, found in windows.items()}
    return {name: dict(found, at=now) for name, found in windows.items()}


def track_session(cache, accounts, session_id, limits, now):
    """The account this session bills, and the readings its payload may be credited with.

    Returns (account, readings, unidentified). The login is looked up again every
    IDENTITY_SECONDS and kept in the session's record in between. A token nobody has
    identified yet -- every refresh mints one -- leaves the previous account in place, and
    `unidentified` asks for a poll child to find out whose it is.

    `readings` are the payload windows `observe` could stamp; without a session id there
    is no record to watch them change in, so there are none.
    """
    sessions = sub_dict(cache, "sessions")
    record = sessions.get(session_id) if session_id else None
    record = dict(record) if isinstance(record, dict) else {}
    unidentified = switched = False
    checked = record.get("checked")
    if not isinstance(checked, (int, float)) or not 0 <= now - checked < IDENTITY_SECONDS:
        token = stored_login().get("accessToken")
        account = known_account(accounts, token) if token else None
        previous = record.get("account")
        if token and account is None:
            unidentified = True
            account = previous
        switched = previous is not None and account is not None and account != previous
        record["account"] = account
        # An unidentified token is looked at again in two seconds, by when the poll child
        # sent to identify it has usually answered.
        record["checked"] = now - (IDENTITY_SECONDS - 2) if unidentified else now
    record["windows"] = observe(record.get("windows"), limits, now, switched)
    record.pop("stale", None)  # the set-aside payload `observe` replaced
    if session_id:
        if session_id not in sessions:
            live = {
                key: value
                for key, value in sessions.items()
                if isinstance(value, dict)
                and isinstance(value.get("checked"), (int, float))
                and now - value["checked"] < SESSION_TTL
            }
            newest = sorted(live.items(), key=lambda item: item[1]["checked"])
            sessions = dict(newest[-(MAX_SESSIONS - 1) :])
        sessions[session_id] = record
        cache["sessions"] = sessions
    account = record.get("account")
    readings = {
        name: window for name, window in record["windows"].items() if window["at"] is not None
    }
    return (account if isinstance(account, str) else None), readings, unidentified


def anchored(readings, anchor, now):
    """The payload readings that belong to the account the poll saw `anchor` for.

    The payload names no account. Within IDENTITY_SECONDS of a switch, or when Claude Code
    and this script resolve a login differently, it can describe someone else's windows,
    and a five-hour boundary alone cannot tell -- two accounts' five-hour windows can close
    on the same minute. The weekly one can: it stays put for the account's whole week. So a
    payload whose weekly window is the one the poll saw is credited in full, including a
    five-hour window the poll has not seen yet -- a new one, or one an early reset moved,
    which would otherwise wait for the next poll that gets through, and the endpoint has
    answered 429 with an hour's Retry-After. Short of that, each window must match its own.
    """

    def agrees(name):
        entry = normalize(readings.get(name))
        known = normalize(anchor.get(name))
        return (
            plausible(entry, now)
            and plausible(known, now)
            and abs(known["resets_at"] - entry["resets_at"]) <= WINDOW_TOLERANCE
        )

    if agrees("seven_day"):
        return dict(readings)
    return {name: readings[name] for name in WINDOWS if agrees(name)}


def write_json(path, data):
    """Atomic replace. A lost race between sessions self-heals on the next render.

    The temp file is same-directory and pid-suffixed so `os.replace` stays atomic without
    dragging `tempfile` into the render path.
    """
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
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


def stored_login():
    """The OAuth login Claude Code would use from this process's context, or {}.

    Claude Code's own order: CLAUDE_CODE_OAUTH_TOKEN, then on macOS the Keychain, then
    ~/.claude/.credentials.json. Where this runs decides the answer -- a security session
    that cannot reach the login Keychain gets exit 36 and falls through to the file, which
    Claude Code wrote the last time it was in the same position, often for another account.
    The status line runs as a child of its session, so it lands where its session does.
    """
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        return {"accessToken": token}
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
            block = json.loads(found.stdout).get("claudeAiOauth")
            if isinstance(block, dict) and block.get("accessToken"):
                return block
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            pass
    block = read_json(CREDENTIALS_PATH).get("claudeAiOauth")
    return block if isinstance(block, dict) and block.get("accessToken") else {}


def live_token(login, now):
    """The login's access token, or None once it has expired: refreshing is Claude Code's job."""
    expires = login.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 <= now:
        return None
    return login.get("accessToken")


def api_get(url, token):
    """GET an OAuth endpoint and parse the JSON. Raises urllib/OS/JSON errors to the caller."""
    import urllib.request

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.load(response)


def identify(token, now):
    """Ask whose `token` is, record the answer, and return the account UUID (None on failure).

    One request per token, and an access token lives about eight hours. Only a digest of
    the token is kept, next to the account's short name and email.
    """
    import urllib.error

    try:
        profile = api_get(PROFILE_URL, token)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    account = sub_dict(profile, "account")
    uuid = account.get("uuid")
    if not isinstance(uuid, str) or not uuid:
        return None
    accounts = read_json(ACCOUNTS_PATH)
    tokens = {
        key: value
        for key, value in sub_dict(accounts, "tokens").items()
        if isinstance(value, dict) and isinstance(value.get("seen"), (int, float))
    }
    tokens[token_key(token)] = {"account": uuid, "seen": now}
    newest = sorted(tokens.items(), key=lambda item: item[1]["seen"])[-MAX_TOKENS:]
    accounts["tokens"] = dict(newest)
    people = sub_dict(accounts, "accounts")
    people[uuid] = {"label": account_label(profile), "email": account.get("email")}
    accounts["accounts"] = people
    write_json(ACCOUNTS_PATH, accounts)
    return uuid


def polled(response, now):
    """The usage endpoint's windows as readings observed `now`.

    A window it lists with nothing running -- no usage since it last closed, or since an
    early reset -- becomes an idle reading, so the figure it replaces does not linger until
    its old boundary. One it does not list at all says nothing.
    """
    readings = {}
    for name in WINDOWS:
        if not isinstance(response, dict) or name not in response:
            continue
        entry = response[name]
        found = normalize(entry)
        if found is not None:
            readings[name] = dict(found, at=now)
        elif entry is None or (isinstance(entry, dict) and entry.get("resets_at") is None):
            readings[name] = {"idle": True, "at": now}
    return readings


def has_session_limit(response):
    """Does the account's plan meter a five-hour window? True, False, or None for unknown.

    `five_hour: null` cannot say: it is also what a plan with the window reports while none
    is running. The `limits` rows can, since they list the meters the plan has, and Claude
    Code's schema says to classify them on `kind`. A plan's idle five-hour row may still be
    left out, so `main` lets any running window override a False here.
    """
    if not isinstance(response, dict):
        return None
    if normalize(response.get("five_hour")) is not None:
        return True
    rows = response.get("limits")
    if not isinstance(rows, list) or not rows:
        return None
    return any(isinstance(row, dict) and "session" in (row.get("kind"), row.get("group")) for row in rows)


def poll():
    """Refresh this context's account in the shared cache. Runs detached; output ignored."""
    import urllib.error

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
        token = live_token(stored_login(), now)
        if not token:
            return
        account = known_account(read_json(ACCOUNTS_PATH), token) or identify(token, now)
        if account is None:
            return
        # Re-check under the lock. Children spawned while the previous poller held it would
        # otherwise each fire the moment it is released, turning one due refresh into a
        # burst of requests -- which is what earns a 429.
        bucket = sub_dict(load_cache()["accounts"], account)
        if now - bucket.get("polled_at", 0) < POLL_SECONDS or now < bucket.get("retry_after", 0):
            return

        fresh, backoff = None, None
        try:
            fresh = api_get(USAGE_URL, token)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
            # Expired token, offline, throttled, or a bad response: stay quiet, serve cache.
            # A 429 means we are asking too often, so back off harder than for a blip -- but
            # not so hard that the figures go visibly stale.
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

        # Read again just before writing: sessions kept rendering while the request was out.
        cache = load_cache()
        buckets = cache["accounts"]
        bucket = sub_dict(buckets, account)
        if fresh is not None:
            bucket = blend_into_cache(bucket, [polled(fresh, now)], now)
            bucket.pop("retry_after", None)
            metered = has_session_limit(fresh)
            if metered is not None:
                bucket["session_limit"] = metered
        else:
            if not any(name in bucket for name in WINDOWS):
                # Nothing to serve in the meantime -- an account's first poll. A long backoff
                # here is five minutes of blank bars, so retry at the poll interval.
                backoff = POLL_SECONDS
            bucket["retry_after"] = now + backoff
        bucket["polled_at"] = now
        buckets[account] = bucket
        cache["accounts"] = {
            key: value
            for key, value in buckets.items()
            if isinstance(value, dict) and now - value.get("polled_at", 0) < SESSION_TTL
        }
        write_json(CACHE_PATH, cache)
    finally:
        try:
            os.unlink(LOCK_PATH)
        except OSError:
            pass


def spawn_poll(bucket, unidentified, now):
    """Kick off a detached refresh if this session's account is due for one, or unknown."""
    if not POLL_ENABLED:
        return
    if not unidentified:
        if bucket is None:
            return  # no OAuth login to poll for
        if now < bucket.get("retry_after", 0):
            return
        if now - bucket.get("polled_at", 0) < POLL_SECONDS:
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


def print_account(argv):
    """`--account [--tmux SOCKET]`: the short name of the login this context uses, or nothing.

    Meant for a tmux `#(...)` job, which tmux runs in the background and shows the last
    output of, so the one profile request a new token needs can be made right here.
    """
    now = time.time()
    login = stored_login()
    token = login.get("accessToken")
    if not token:
        return
    account = known_account(read_json(ACCOUNTS_PATH), token)
    if account is None and live_token(login, now):
        account = identify(token, now)
    accounts = read_json(ACCOUNTS_PATH)
    label = account_name(accounts, account)
    if not label:
        return
    if "--tmux" in argv:
        # Marked only once there is a name to show: otherwise neither place would show one.
        index = argv.index("--tmux") + 1
        socket = argv[index] if index < len(argv) else ""
        shown = sub_dict(accounts, "tmux")
        seen = shown.get(socket)
        if socket and not (isinstance(seen, (int, float)) and now - seen < TMUX_SHOWN_SECONDS / 3):
            shown[socket] = now
            accounts["tmux"] = {
                key: value
                for key, value in shown.items()
                if isinstance(value, (int, float)) and now - value < TMUX_SHOWN_SECONDS
            }
            write_json(ACCOUNTS_PATH, accounts)
    sys.stdout.write(label + "\n")


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


def window(name, data, was_seen, now, layout):
    """A labeled percentage; reset times and pace are optional wide-pane detail."""
    label = "5h" if name == "five_hour" else "wk"
    gap = "" if layout["compact"] else " "
    prefix = f"{DIM}{label}{RESET}{gap}"
    if data is None:
        return prefix + f"{DIM}{'~0%' if was_seen else '--'}{RESET}"
    pct = data["used_percentage"]
    pace = projected(data, WINDOW_SECONDS[name], now)
    color = usage_color(pct, pace)
    cell = prefix + f"{color}{pct:.0f}%{RESET}"
    if layout["projection"] and name in PROJECTED and pace >= PACE_WARN and round(pace) > round(pct):
        cell += f" {color}→{min(pace, 999):.0f}%{RESET}"
    if name in layout["countdown"]:
        cell += f" {DIM}·{countdown(data['resets_at'], now)}{RESET}"
    return cell


def context_cell(pct, compact=False):
    """How full this session's context is, labelled because the usage bars are not."""
    label = "c" if compact else "ctx"
    if pct is None:
        return f"{DIM}{label}--{RESET}"
    color = RED if pct >= CONTEXT_FULL else YELLOW if pct >= CONTEXT_WARN else GREEN
    return f"{DIM}{label}{RESET}{color}{pct:.0f}%{RESET}"


def short_model(name):
    # "Opus 5 (1M context)" is too wide for a status line; keep the capacity hint.
    return name.replace(" (1M context)", " 1M").replace("Claude ", "")


def model_label(name, mode):
    label = short_model(name)
    if mode == 0:
        return label
    label = label.replace(" 1M", "").replace(" ", "").replace("GPT-", "GPT")
    if mode >= 2:
        label = re.sub(r"^(Opus|Sonnet|Haiku|Fable)", lambda match: match[1][0], label)
        label = re.sub(r"^GPT([\d.]+)-(Sol|Terra|Astra|Luna)$",
                       lambda match: "G" + match[1] + match[2][0], label)
    return label


def account_label(name, full):
    if not name or full:
        return name
    return {"worldengine": "WE", "personal": "P", "codex": "CX", "magpie…": "MP…"}.get(
        name.lower(), elide(name, 8)
    )


def printed_width(text):
    """Columns actually occupied, discounting the SGR escapes woven through every cell."""
    text = ANSI.sub("", text)
    if text.isascii():
        return len(text)
    import unicodedata

    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


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


def layouts_for(columns):
    """Choose density for the pane, then shed optional detail if its content is long.

    The account, model, context and every available quota stay in every layout. Width
    changes remove metadata and abbreviate labels; they never remove a quota or its unit.
    """
    wide = columns >= 110
    countdowns = WINDOWS if columns >= 80 else ()
    return ladder(
        {"sep": SEP_NARROW if columns >= 50 else "  ",
         "branch": wide, "folder": wide,
         "countdown": countdowns,
         "effort": columns >= 60, "full_account": wide,
         "model_mode": 0 if wide else 1,
         "context_compact": False, "compact": columns < 60,
         "projection": wide},
        {"branch": False},
        {"folder": False},
        # The weekly countdown changes once an hour; the five-hour one is the more urgent.
        {"countdown": tuple(name for name in countdowns if name == "five_hour")},
        {"countdown": ()},
        {"projection": False},
        {"effort": False},
        {"full_account": False, "model_mode": 1},
        {"compact": True, "sep": " "},
        {"model_mode": 2, "context_compact": True},
    )


def render(layout, info, now, columns):
    """The status line under `layout`, and whether it fits in `columns`."""
    cells = []
    if layout["folder"]:
        cells.append(f"{CYAN}{BOLD}{info['folder']}{RESET}")
    model_cell = f"{BLUE}{model_label(info['model'], layout['model_mode'])}{RESET}"
    account = account_label(info.get("account"), layout["full_account"])
    if account:
        model_cell = f"{CYAN}{account}{RESET} " + model_cell
    if info["effort"] and layout["effort"]:
        effort = info["effort"]
        if not layout["full_account"]:
            effort = {"xhigh": "xh", "high": "h", "medium": "m", "low": "l"}.get(effort, effort)
        model_cell += f" {DIM}{effort}{RESET}"
    cells.append(model_cell)
    cells.append(context_cell(info["context"], layout["context_compact"]))
    for name in info["windows"]:
        cells.append(window(name, info["usage"].get(name), info["seen"][name], now, layout))

    sep = layout["sep"]
    room = columns - printed_width(sep.join(cells))
    if info["branch"] and layout["branch"]:
        # The branch takes only what the fixed cells leave, down to a floor below which a
        # name is no longer recognisable; under that, this layout does not fit.
        room = min(BRANCH_CELLS, room - printed_width(sep))
        if room < BRANCH_FLOOR:
            return sep.join(cells), False
        branch = elide(info["branch"], room)
        while printed_width(branch) > room and len(branch) > 1:
            branch = elide(branch, len(branch) - 1)
        cells.insert(1, f"{MAGENTA}{branch}{RESET}")
    return sep.join(cells), room >= 0


def render_status(info, now, columns):
    for layout in layouts_for(columns):
        line, fits = render(layout, info, now, columns)
        if fits:
            break
    return line


# ------------------------------------------------------------------------------ main


def install():
    """Copy this script to ~/.claude and register it in settings.json.

    Also turns off Claude's attribution in commits and PRs (no `Co-Authored-By: Claude`
    trailer, no "Generated with Claude Code" line, no session link), unless
    CLAUDE_STATUSBAR_KEEP_ATTRIBUTION=1. That too is a local setting, so it rides along here
    rather than being redone by hand on every machine.

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
    if not KEEP_ATTRIBUTION:
        attribution = settings.get("attribution")
        settings["attribution"] = {
            **(attribution if isinstance(attribution, dict) else {}),
            **NO_ATTRIBUTION,
        }
        settings["includeCoAuthoredBy"] = False
    with open(SETTINGS_PATH, "w") as handle:
        json.dump(settings, handle, indent=2)
        handle.write("\n")
    print(f"registered statusLine in {SETTINGS_PATH}")
    if not KEEP_ATTRIBUTION:
        print("turned off Claude attribution in commits and PRs"
              " (CLAUDE_STATUSBAR_KEEP_ATTRIBUTION=1 leaves it alone)")
    print("open a new session (or restart an existing one) to pick it up")


def main():
    if "--install" in sys.argv:
        install()
        return
    if "--poll" in sys.argv:
        poll()
        return
    if "--account" in sys.argv:
        print_account(sys.argv)
        return

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    now = time.time()
    accounts = read_json(ACCOUNTS_PATH)
    gateway = magpie_state(payload, accounts, now)
    if gateway is None:
        stored = read_json(CACHE_PATH)
        cache = load_cache()
        limits = sub_dict(payload, "rate_limits")
        account, readings, unidentified = track_session(
            cache, accounts, payload.get("session_id"), limits, now
        )
        if account is None:
            # No known login: do not let another account's windows override this one.
            previous, bucket = {}, None
            usage = blend_into_cache({}, [limits], now)
        else:
            previous = sub_dict(cache["accounts"], account)
            usage = bucket = blend_into_cache(previous, [anchored(readings, previous, now)], now)
            cache["accounts"][account] = bucket
        if cache != stored:
            write_json(CACHE_PATH, cache)
        spawn_poll(bucket, unidentified, now)
    else:
        account, previous, bucket, limits = None, {}, None, {}
        usage = gateway["usage"]

    cwd = sub_dict(payload, "workspace").get("current_dir") or payload.get("cwd") or os.getcwd()
    cwd = cwd.rstrip("/") if isinstance(cwd, str) else os.getcwd()
    model = sub_dict(payload, "model").get("display_name")
    model = model if isinstance(model, str) and model else "?"
    effort = sub_dict(payload, "effort").get("level")
    context = sub_dict(payload, "context_window").get("used_percentage")
    context = context if isinstance(context, (int, float)) and not isinstance(context, bool) else None
    # A running window always shows, whatever the last poll concluded about the plan.
    no_session_limit = bucket is not None and bucket.get("session_limit") is False
    shown = [
        name
        for name in WINDOWS
        if not (name == "five_hour" and no_session_limit and "resets_at" not in usage.get(name, {}))
    ]
    if gateway is not None:
        model = gateway["model"] or model
        effort = gateway["effort"] or effort
        shown = gateway["windows"]

    info = {
        "folder": os.path.basename(cwd),
        "branch": git_branch(cwd),
        "model": model,
        "account": gateway["account"] if gateway is not None else account_name(accounts, account),
        "effort": effort,
        "context": context,
        "windows": shown,
        # An idle window shows the same as one that just ran out: nothing spent since.
        "usage": {name: usage[name] for name in WINDOWS if "resets_at" in usage.get(name, {})},
        "seen": gateway["seen"] if gateway is not None else {
            name: name in usage or any(normalize(c.get(name)) for c in (limits, previous))
            for name in WINDOWS
        },
    }
    columns = terminal_columns()
    sys.stdout.write(render_status(info, now, columns))


if __name__ == "__main__":
    main()
