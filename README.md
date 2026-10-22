# gh-runs

A herdr plugin that shows GitHub Actions runs for the account you're logged in
to with `gh` (`gh auth status`, active account).

By default it shows your runs in the repo of the pane you have focused in the
workspace, and follows you as you move between panes. Remotes using SSH host
aliases (`git@github-work:org/repo.git`) are handled.

When one of your runs finishes, herdr shows a notification.

## Requirements

- `gh`, logged in
- `python3` (standard library only)

## Install

```bash
herdr plugin link /path/to/herdr-gh-runs
```

Open it with the toggle action. If the tab has a
[herdr-sidebar](https://github.com/alexarthurs/herdr-sidebar) pane, it opens as
a small pane (14 rows) docked under it and leaves your focus where it is;
otherwise it opens beside the focused pane. Pressing the toggle again focuses
the pane, and once more (while focused) closes it.

```bash
herdr plugin action invoke kkjdaniel.gh-runs.toggle
```

or bind a key in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+g"
type = "plugin_action"
command = "kkjdaniel.gh-runs.toggle"
description = "CI runs"
```

## Keys

| Key | Does |
| --- | --- |
| `↑` `↓` / `j` `k`, `g` `G`, PgUp/PgDn | move |
| `Enter` | watch a running run (`gh run watch`), or open a finished one in the log viewer |
| `l` | log viewer, opened on the failed step's log |
| `o` | open in browser |
| `R` | rerun (failed jobs only if it failed), asks first |
| `x` | cancel a running run, asks first |
| `a` | your runs / everyone's runs |
| `s` | this repo / all your recently active repos |
| `p` | pin the current repo (stop following focus) |
| `r` | refresh now |
| `?` | key list |
| `q` | quit |

Popups close with `q`, or any key once a watched run finishes.

## Log viewer

Opens in a popup. Jobs and steps are listed on the left with their status and
duration; the selected step's log is on the right with the runner's timestamps
stripped, colours rendered, `##[group]` sections shown as `▸` headings and
error lines marked in red. A failed run opens on its first failed step,
scrolled to the end where the error usually is. Below 90 columns it shows one
side at a time.

| Key | Does |
| --- | --- |
| `j` `k` | select a step (left) or scroll (right) |
| `Enter` / `→` / `Tab` | move to the log; `←` / `Esc` / `Tab` go back |
| `e` | next failed step (left), next error line (right); `E` previous |
| `space` `b`, `^d` `^u`, `g` `G` | page, half page, top/bottom of the log |
| `/` `n` `N` | search the selected step's log |
| drag | select log text; it's copied when you let go |
| `v` then `j` `k`, `y` | select whole lines from the keyboard, then copy |
| `y` / `Y` | copy the selection / copy the whole step's log |
| click, wheel | pick a step, scroll |
| `J` `K` | next/previous step without leaving the log |
| `o` | open the selected job on GitHub |
| `r` | reload |
| `q` | close |

Copies are plain text: no colour codes and none of the viewer's own markers
(`▸`, `✗`, `$`). They go through `pbcopy` (or `wl-copy`/`xclip`/`xsel`), falling
back to the terminal clipboard (OSC 52).

Selecting a job row shows that job's whole log. Logs for jobs still running
aren't published by GitHub until the job finishes.

The time on each row is how long the run took, or has taken so far while it's
still going. In narrow panes each row shows the status, the commit title (plus
the workflow name when several runs share a title) and that time.

## Notes

- Refreshes every 10s while a run is active, 30s otherwise. "All repos" mode
  checks up to 12 repos you've recently pushed to or opened PRs on, every
  30s/90s, to stay well inside the API rate limit.
- The docked height is `DOCK_ROWS` in `gh_runs.py` (or `GH_RUNS_ROWS` in the
  herdr server's environment).
- Set `GH_RUNS_REPO=owner/name` in the pane's env to start pinned to a repo.
- If the pane crashes, the traceback is in the plugin state dir as `crash.log`.
