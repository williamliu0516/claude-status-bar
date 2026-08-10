# claude-status-bar

A status line for [Claude Code](https://claude.com/claude-code) that shows the working
folder, the git branch, the model and effort level, and — the part that takes real work —
5-hour and weekly usage that is actually correct.

```
we-rewrite-compact  │  rewrite-0809-integrated  │  Opus 5 1M xhigh  │  5h ██░░░░░░  24% ·1h43m  │  wk ██████░░  70% ·2d22h
```

## Install

```sh
curl -fsSL https://xiaweiliu.com/claude-status-bar/install.sh | sh
```

That downloads `statusline.py` to `~/.claude/` and registers it in
`~/.claude/settings.json`, leaving every other setting alone. Re-run it to upgrade. Then
open a new session, or restart an existing one, to pick it up.

Needs `python3` (3.8+) and `curl` or `wget`. No packages, no virtualenv, nothing to build.

The installer tries `xiaweiliu.com` first and falls back to `raw.githubusercontent.com`, so
it works whether or not GitHub Pages is serving the custom domain. It refuses to install
anything whose first line is not the expected shebang, which is what a captive portal or a
login redirect would return with a 200.

<details>
<summary>Install straight from GitHub instead</summary>

```sh
curl -fsSL https://raw.githubusercontent.com/williamliu0516/claude-status-bar/main/install.sh | sh
```
</details>

## Uninstall

Delete the `statusLine` key from `~/.claude/settings.json` and remove
`~/.claude/statusline.py`.

## What it shows

| Cell | Notes |
| --- | --- |
| folder | basename of the session's working directory |
| branch | current git branch, `@a1b2c3d` when HEAD is detached, hidden outside a repo |
| model | `display_name`, with `(1M context)` shortened to `1M` |
| effort | current effort level, when set |
| `5h` | 5-hour usage window: bar, percentage, time until reset |
| `wk` | weekly usage window, same |

Bars turn yellow at 50%, red at 75%, bold red at 90%.

## Why the usage numbers need this much work

The `rate_limits` block Claude Code hands the status line comes from a **per-process,
in-memory cache**. Its only writers are the startup prefetch and the response headers of
that same session's API calls — there is no timer, so nothing refreshes it while a session
sits idle. Six concurrent sessions were observed reporting 5h = 3/17/25/29/90 and
wk = 10/12/67/89 at the same instant, the extremes being snapshots from a previous window
and a previous week. `refreshInterval` re-runs the command but hands it that same frozen
cache, so it cannot fix either problem.

Usage is therefore resolved from four sources, newest-wins, shared through one file that
every session reads and writes:

1. a live poll of `GET /api/oauth/usage`, throttled to one request per minute across every
   session and run detached so rendering never waits on the network
2. the session's own stdin payload
3. `cachedUsageUtilization` in `~/.claude.json`, which Claude Code maintains account-wide
4. the shared cache, carrying whatever any other session last observed

They are merged by a rule that needs no trust in write order or clock skew:

1. Discard any window whose `resets_at` has passed, or is implausibly far ahead.
2. Prefer the observation with the latest `resets_at` — a later boundary is a newer window.
3. Within that window take the **largest** utilization, since usage only grows until reset.

A stale snapshot can therefore neither pull the number backwards nor revive a dead window.

Two details that are easy to get wrong:

- **Boundaries are compared with a ±120 s tolerance.** Each response recomputes `resets_at`
  at its own sub-second precision, so the same window arrives as `…800.997`, `…800.225` and
  `…800.027`. Exact grouping reads that drift as three different windows, keeps one
  observation, and freezes the number.
- **The `limits[]` array is deliberately ignored.** It carries a second weekly figure — a
  model-scoped cap that can read 75% while the flat `seven_day` key reads 70% — but the two
  have *different denominators*. Combining them produces a number that matches neither, and
  disagrees with what `/usage` reports. `5h` and `wk` are exactly `five_hour` and
  `seven_day`, nothing else, so the bar and `/usage` always tell the same story.

## Why it runs every second

Claude Code re-runs the status line on an event only when one of these changes:
`messageId`, `tokenUsage`, `permissionMode`, `vimMode`, `mainLoopModel`, `fastMode`,
`effortValue`, `thinkingEnabled`, `prStatus`. Model and effort are in that list, so those
cells update the moment you change them. **The working directory and the git branch are
not** — they are read fresh inside the command but nothing triggers a re-render when they
change. Switching branch would leave a stale cell until the next message.

So it is registered at `refreshInterval: 1`, the lowest Claude Code accepts, and the script
is kept cheap enough to justify it: about 30 ms per render, most of which is interpreter
startup. `urllib.request` costs 47 ms on its own — three times a whole render — so the
network and subprocess imports are deferred into the paths that actually use them, and only
the once-a-minute detached poll child ever pays for them. Polling stays at one request per
minute no matter how often the line redraws.

## Notes

- The git branch is read straight off `.git/HEAD` rather than by shelling out, because this
  runs 60 times a minute. Linked worktrees and submodules keep a `.git` **file** holding a
  `gitdir:` pointer instead of a directory, which the resolver follows — a naive walk up the
  tree reports "not a repository" for every worktree checkout.
- The branch cell is sized to whatever the other cells leave on the current terminal, and is
  dropped entirely when there is no room, so it can never push the usage bars off-screen.
  Long names are elided from the middle: `rewrite-0809-integrated` and
  `rewrite-0809-integrated-compact` differ only in the suffix, so trimming the tail would
  render two different branches identically.
- Credentials are read, never written. Refreshing the OAuth token is Claude Code's job; on
  401 this backs off and keeps rendering from cache.

## Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `CLAUDE_STATUSLINE_POLL` | `1` | set to `0` to disable the live usage poll entirely |
| `CLAUDE_STATUSLINE_POLL_SECONDS` | `60` | seconds between polls, shared across sessions |
| `CLAUDE_STATUSBAR_SOURCE` | GitHub raw URL | where `install.sh` fetches the script from |

## License

MIT
