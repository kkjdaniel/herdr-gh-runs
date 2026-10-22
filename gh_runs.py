#!/usr/bin/env python3
"""herdr pane: GitHub Actions runs for the account logged in via gh.

Run with no arguments to start the pane UI, or with --toggle to open, focus
or close the pane in the current workspace (used by the plugin action).
"""

import curses
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "kkjdaniel.gh-runs")
PLUGIN_ROOT = os.path.realpath(
    os.environ.get("HERDR_PLUGIN_ROOT") or os.path.dirname(os.path.abspath(__file__))
)
TOKEN = "gh-runs"

# Seconds between API refreshes: (while any run is active, when all are done).
REPO_INTERVAL = (10, 30)
GLOBAL_INTERVAL = (30, 90)
TICK = 2  # how often to check which repo the focused pane is in
GLOBAL_REPOS = 12
REPO_LIST_TTL = 600
LOGIN_TTL = 300

REMOTE_RE = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$")
ACTIVITY_EVENTS = {"PushEvent", "PullRequestEvent", "CreateEvent", "ReleaseEvent"}
FAILED = {"failure", "timed_out", "startup_failure"}


class CmdError(Exception):
    pass


def run(args, cwd=None, timeout=20):
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CmdError(str(e))
    if p.returncode != 0:
        lines = (p.stderr or p.stdout).strip().splitlines()
        raise CmdError(lines[-1] if lines else f"{args[0]} exited {p.returncode}")
    return p.stdout


def gh_api(path):
    return json.loads(run(["gh", "api", path]))


def herdr(*args):
    out = run([HERDR, *args])
    try:
        return json.loads(out)
    except ValueError:
        return {}


# ---------------------------------------------------------------- herdr panes

def current_pane():
    try:
        return herdr("pane", "current", "--current")["result"]["pane"]
    except (CmdError, KeyError):
        return None


def list_panes(workspace_id):
    args = ["pane", "list"] + (["--workspace", workspace_id] if workspace_id else [])
    try:
        return herdr(*args)["result"]["panes"]
    except (CmdError, KeyError):
        return []


def pane_cwd(p):
    return p.get("foreground_cwd") or p.get("cwd")


_repo_cache = {}


def repo_for(cwd):
    """owner/name of the GitHub remote for a directory, or None.

    Parsed from the remote URL rather than via `gh repo view`, so SSH host
    aliases like git@github-work:org/repo.git still resolve.
    """
    if not cwd or os.path.realpath(cwd) == PLUGIN_ROOT:
        return None
    if cwd in _repo_cache:
        return _repo_cache[cwd]
    repo = None
    try:
        remotes = run(["git", "-C", cwd, "remote"]).split()
        name = "origin" if "origin" in remotes else (remotes[0] if remotes else None)
        if name:
            url = run(["git", "-C", cwd, "remote", "get-url", name]).strip()
            m = REMOTE_RE.search(url)
            if m and "github" in url:
                repo = f"{m.group(1)}/{m.group(2)}"
    except CmdError:
        pass
    _repo_cache[cwd] = repo
    return repo


def pick_target(prev):
    """(repo, cwd) to show: the focused pane's repo, else keep the previous one."""
    me = current_pane()
    my_id = me and me.get("pane_id")
    panes = [p for p in list_panes(me and me.get("workspace_id")) if p.get("pane_id") != my_id]
    focused = next((p for p in panes if p.get("focused")), None)
    if focused:
        repo = repo_for(pane_cwd(focused))
        if repo:
            return repo, pane_cwd(focused)
    if prev:
        return prev
    same_tab = [p for p in panes if me and p.get("tab_id") == me.get("tab_id")]
    for cwd in [os.getcwd()] + [pane_cwd(p) for p in same_tab + panes]:
        repo = repo_for(cwd)
        if repo:
            return repo, cwd
    return None


# ---------------------------------------------------------------- GitHub

def recent_repos(login):
    """Repos the user has recently pushed to or opened PRs on."""
    repos = []
    try:
        for ev in gh_api(f"users/{login}/events?per_page=100"):
            name = (ev.get("repo") or {}).get("name")
            if ev.get("type") in ACTIVITY_EVENTS and name and name not in repos:
                repos.append(name)
    except (CmdError, ValueError):
        pass
    if len(repos) < GLOBAL_REPOS:
        try:
            for r in gh_api("user/repos?sort=pushed&per_page=30&affiliation=owner"):
                if r["full_name"] not in repos:
                    repos.append(r["full_name"])
        except (CmdError, ValueError):
            pass
    return repos[:GLOBAL_REPOS]


def fetch_runs(repo, login, mine, per_page):
    q = f"per_page={per_page}" + (f"&actor={login}" if mine else "")
    return gh_api(f"repos/{repo}/actions/runs?{q}").get("workflow_runs", [])


def parse_time(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def short_span(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m{s:02d}s" if m < 10 else f"{m}m"
    if seconds < 86400:
        h, m = divmod(seconds // 60, 60)
        return f"{h}h{m:02d}m"
    return f"{seconds // 86400}d"


def run_duration(r):
    start = parse_time(r.get("run_started_at") or r.get("created_at"))
    if not start:
        return ""
    end = parse_time(r.get("updated_at")) if r.get("status") == "completed" else None
    end = end or datetime.now(timezone.utc)
    return short_span((end - start).total_seconds())


def is_active(r):
    return r.get("status") != "completed"


# ---------------------------------------------------------------- state

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.quit = False
        self.login = None
        self.login_at = 0
        self.scope = "repo"  # or "global"
        self.mine = True
        self.pinned = bool(os.environ.get("GH_RUNS_REPO"))
        self.target = (os.environ["GH_RUNS_REPO"], None) if self.pinned else None
        self.repos = []
        self.repos_at = 0
        self.runs = []  # list of (repo, run dict)
        self.fetched_at = 0
        self.fetched_key = None
        self.loading = False
        self.error = None
        self.seen = {}  # run id -> status, for completion notifications
        self.selected_id = None
        self.message = None
        self.message_until = 0
        self.confirm = None  # (prompt, callable)
        self.help = False
        self.my_pane = None

    def flash(self, text, secs=4):
        with self.lock:
            self.message = text
            self.message_until = time.time() + secs

    def key(self):
        repo = self.target[0] if self.target else None
        return (self.scope, self.mine, repo if self.scope == "repo" else None, self.login)

    def interval(self):
        fast, slow = REPO_INTERVAL if self.scope == "repo" else GLOBAL_INTERVAL
        return fast if any(is_active(r) for _, r in self.runs) else slow


def notify(login, repo, r):
    if (r.get("actor") or {}).get("login") != login:
        return
    ok = r.get("conclusion") == "success"
    mark = "✓" if ok else "✗"
    title = f"{mark} {r.get('name', 'workflow')} {r.get('conclusion') or 'finished'}"
    body = f"{repo} · {r.get('head_branch') or ''} · {r.get('display_title') or ''}"
    try:
        run([HERDR, "notification", "show", title, "--body", body,
             "--sound", "done" if ok else "request"])
    except CmdError:
        pass


def tag_pane(st, summary):
    """Mark our pane so --toggle can find it, and show a short summary title."""
    pane = current_pane()
    if not pane:
        return
    st.my_pane = pane
    try:
        run([HERDR, "pane", "report-metadata", pane["pane_id"], "--source", PLUGIN_ID,
             "--token", f"{TOKEN}=1", "--title", summary])
    except CmdError:
        pass


def refresh(st):
    with st.lock:
        st.loading = True
    try:
        if not st.login or time.time() - st.login_at > LOGIN_TTL:
            st.login = gh_api("user")["login"]
            st.login_at = time.time()
        login = st.login
        if st.scope == "repo":
            if not st.target:
                raise CmdError("no GitHub repo found in this workspace's panes")
            repo = st.target[0]
            runs = [(repo, r) for r in fetch_runs(repo, login, st.mine, 30)]
        else:
            if not st.repos or time.time() - st.repos_at > REPO_LIST_TTL:
                st.repos = recent_repos(login)
                st.repos_at = time.time()

            def one(repo):
                try:
                    return [(repo, r) for r in fetch_runs(repo, login, st.mine, 10)]
                except (CmdError, ValueError):
                    return []

            with ThreadPoolExecutor(6) as ex:
                runs = [x for chunk in ex.map(one, st.repos) for x in chunk]
            runs.sort(key=lambda x: x[1].get("created_at") or "", reverse=True)
            runs = runs[:60]

        for repo, r in runs:
            prev = st.seen.get(r["id"])
            if prev and prev != "completed" and r.get("status") == "completed":
                notify(login, repo, r)
            st.seen[r["id"]] = r.get("status")

        with st.lock:
            st.runs = runs
            st.error = None
            st.fetched_at = time.time()
            st.fetched_key = st.key()

        active = sum(1 for _, r in runs if is_active(r))
        tag_pane(st, "GH Actions" + (f" · {active} running" if active else ""))
    except (CmdError, ValueError, KeyError) as e:
        with st.lock:
            st.error = str(e)
            st.fetched_at = time.time()
            st.fetched_key = st.key()
    finally:
        with st.lock:
            st.loading = False


def worker(st):
    while not st.quit:
        if st.scope == "repo" and not st.pinned:
            st.target = pick_target(st.target)
        stale = time.time() - st.fetched_at >= st.interval()
        if st.key() != st.fetched_key or stale:
            refresh(st)
        st.wake.wait(TICK)
        st.wake.clear()


# ---------------------------------------------------------------- actions

def background(fn):
    threading.Thread(target=fn, daemon=True).start()


def open_popup(st, cmd, cwd):
    """Run a shell command in a herdr popup over the whole session."""
    try:
        run([HERDR, "plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "detail",
             "--cwd", cwd or os.path.expanduser("~"), "--env", f"GH_RUNS_CMD={cmd}"])
    except CmdError as e:
        st.flash("close the open dialog first" if "ui_busy" in str(e) else f"popup failed: {e}")


def selected(st):
    with st.lock:
        for repo, r in st.runs:
            if r["id"] == st.selected_id:
                return repo, r
    return None


PAUSE = "; echo; read -rsn1 -p $'\\e[2mpress any key to close\\e[0m'"


def action_view(st, logs):
    sel = selected(st)
    if not sel:
        return
    repo, r = sel
    ref = f"{r['id']} -R {shlex.quote(repo)}"
    viewer = f"exec python3 {shlex.quote(os.path.join(PLUGIN_ROOT, 'gh_runs.py'))} --logs {shlex.quote(repo)} {r['id']}"
    if is_active(r) and not logs:
        cmd = f"gh run watch {ref} --compact --exit-status; gh run view {ref}{PAUSE}"
    else:
        cmd = viewer + (" --focus-log" if logs else "")
    cwd = st.target[1] if st.target and st.target[0] == repo else None
    background(lambda: open_popup(st, cmd, cwd))


def action_browser(st):
    sel = selected(st)
    if not sel:
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, sel[1]["html_url"]], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as e:
        st.flash(str(e))


def action_gh(st, verb, extra, done_msg):
    sel = selected(st)
    if not sel:
        return
    repo, r = sel

    def go():
        try:
            run(["gh", "run", verb, str(r["id"]), "-R", repo, *extra])
            st.flash(done_msg)
        except CmdError as e:
            st.flash(f"{verb} failed: {e}")
        st.fetched_key = None
        st.wake.set()

    background(go)


def ask_rerun(st):
    sel = selected(st)
    if not sel:
        return
    _, r = sel
    if is_active(r):
        st.flash("run is still going; cancel it first (x)")
        return
    failed_only = r.get("conclusion") in FAILED
    what = "failed jobs of" if failed_only else "all jobs of"
    st.confirm = (f"Rerun {what} {r.get('name')} #{r.get('run_number')}? y/n",
                  lambda: action_gh(st, "rerun", ["--failed"] if failed_only else [],
                                    "rerun requested"))


def ask_cancel(st):
    sel = selected(st)
    if not sel:
        return
    _, r = sel
    if not is_active(r):
        st.flash("run already finished")
        return
    st.confirm = (f"Cancel {r.get('name')} #{r.get('run_number')}? y/n",
                  lambda: action_gh(st, "cancel", [], "cancel requested"))


# ---------------------------------------------------------------- drawing

SPINNER = "◐◓◑◒"


def status_glyph(r):
    status, concl = r.get("status"), r.get("conclusion")
    if status == "in_progress":
        return SPINNER[int(time.time() * 4) % 4], "yellow"
    if status != "completed":
        return "◌", "yellow"
    if concl == "success":
        return "✓", "green"
    if concl in FAILED:
        return "✗", "red"
    if concl == "action_required":
        return "!", "magenta"
    return "⊘", "dim"


def fit(text, width):
    text = (text or "").replace("\n", " ")
    if width <= 0:
        return ""
    if len(text) <= width:
        return text.ljust(width)
    return text[: max(0, width - 1)] + "…"


class Colors:
    def __init__(self):
        self.pairs = {}
        self.sel_pairs = {}
        self.bg = -1
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        self.bg = bg
        grey = 8 if curses.COLORS > 8 else curses.COLOR_WHITE
        spec = {"green": curses.COLOR_GREEN, "red": curses.COLOR_RED,
                "yellow": curses.COLOR_YELLOW, "magenta": curses.COLOR_MAGENTA,
                "cyan": curses.COLOR_CYAN, "dim": grey}
        for i, (name, fg) in enumerate(spec.items(), start=1):
            curses.init_pair(i, fg, bg)
            self.pairs[name] = curses.color_pair(i)
        # Selected rows: a dark grey bar with light text and bright status
        # colours, instead of reverse video (which put grey text on grey).
        if curses.COLORS >= 256:
            sel_bg = 237
            sel = {"text": 255, "dim": 250, "green": 10, "red": 9, "yellow": 11,
                   "magenta": 13, "cyan": 14}
        else:
            sel_bg = curses.COLOR_BLUE
            sel = {"text": curses.COLOR_WHITE, "dim": curses.COLOR_WHITE,
                   "green": curses.COLOR_GREEN, "red": curses.COLOR_RED,
                   "yellow": curses.COLOR_YELLOW, "magenta": curses.COLOR_MAGENTA,
                   "cyan": curses.COLOR_CYAN}
        for i, (name, fg) in enumerate(sel.items(), start=7):
            curses.init_pair(i, fg, sel_bg)
            self.sel_pairs[name] = curses.color_pair(i)

    def __call__(self, name):
        return self.pairs.get(name, curses.A_DIM if name == "dim" else 0)

    def selected(self, name="text"):
        """Attribute for text on a selected row."""
        return self.sel_pairs.get(name, self.sel_pairs.get("text", curses.A_REVERSE))


def put(scr, y, x, text, attr=0):
    h, w = scr.getmaxyx()
    if y >= h or x >= w:
        return
    try:
        scr.addnstr(y, x, text.replace("\0", ""), w - x, attr)
    except (curses.error, ValueError):
        pass  # bottom-right cell raises curses.error; never crash on one bad string


HINTS = ("↑↓ move  ⏎ watch/view  l logs  o browser  R rerun  x cancel  "
         "a mine/everyone  s repo/all repos  p pin  r refresh  ? keys  q quit")
SHORT_HINTS = "⏎ view  l logs  o web  ? keys"
HELP = [
    ("↑↓ j k", "move"),
    ("⏎", "watch / view run"),
    ("l", "logs (failed steps)"),
    ("o", "open in browser"),
    ("R", "rerun (asks first)"),
    ("x", "cancel (asks first)"),
    ("a", "mine / everyone"),
    ("s", "this repo / all repos"),
    ("p", "pin repo"),
    ("r", "refresh"),
    ("q", "quit"),
]


def draw_header(scr, st, col, w, loading, fetched):
    narrow = w < 50
    if st.scope == "repo":
        where = st.target[0] if st.target else "no repo"
        if narrow:
            where = where.split("/")[-1]
    else:
        where = "all repos" if narrow else f"my recent repos ({len(st.repos)})"
    left = f" {where}" if narrow else f" CI · {st.login or '…'} · {where}"
    flags = ["mine" if st.mine else "all"]
    if st.pinned and st.scope == "repo":
        flags.append("pin" if narrow else "pinned")
    if loading or not fetched:
        flags.append("updating")
    else:
        flags.append(f"{short_span(time.time() - fetched)} ago")
    right = " · ".join(flags) + " "
    put(scr, 0, 0, fit(left, max(0, w - len(right))), col("cyan") | curses.A_BOLD)
    put(scr, 0, max(0, w - len(right)), right, col("dim"))
    put(scr, 1, 0, "─" * w, col("dim"))


def row_cells(st, runs, w):
    """Column layout: list of (key, width) between the glyph and the time columns."""
    if w < 50:
        return [("text", max(0, w - 2 - 7))], False
    show_repo = st.scope == "global"
    w_repo = (14 if w >= 80 else 11) if show_repo else 0
    w_wf = min(18 if w >= 80 else 14, max((len(r.get("name") or "") for _, r in runs), default=8))
    w_br = min(20, max((len(r.get("head_branch") or "") for _, r in runs), default=6)) if w >= 64 else 0
    fixed = 2 + sum(c + 1 for c in (w_repo, w_wf, w_br) if c) + 6 + 1
    cells = [("repo", w_repo), ("wf", w_wf), ("branch", w_br), ("title", max(0, w - fixed))]
    return [c for c in cells if c[1]], True


def draw(scr, st, col, offset):
    h, w = scr.getmaxyx()
    scr.erase()
    with st.lock:
        runs = list(st.runs)
        error, loading, fetched = st.error, st.loading, st.fetched_at
        message = st.message if time.time() < st.message_until else None

    draw_header(scr, st, col, w, loading, fetched)
    body_top, body_h = 2, max(0, h - 3)

    if st.help:
        for i, (k, desc) in enumerate(HELP[:body_h]):
            put(scr, body_top + i, 1, k.ljust(7), col("cyan") | curses.A_BOLD)
            put(scr, body_top + i, 9, fit(desc, w - 10))
        put(scr, h - 1, 0, fit(" any key to close", w), col("dim"))
        scr.refresh()
        return offset

    if error and not runs:
        put(scr, body_top, 1, fit(f"⚠ {error}", w - 2), col("red"))
    elif not runs and fetched:
        put(scr, body_top, 1, fit("no runs by you here" if st.mine else "no runs here", w - 2), col("dim"))
        if st.mine:
            put(scr, body_top + 1, 1, fit("a: show everyone's", w - 2), col("dim"))

    ids = [r["id"] for _, r in runs]
    if runs and st.selected_id not in ids:
        st.selected_id = ids[0]
    idx = ids.index(st.selected_id) if runs else 0
    if idx < offset:
        offset = idx
    elif idx >= offset + body_h:
        offset = idx - body_h + 1

    cells, _ = row_cells(st, runs, w)
    # Narrow rows show the commit title; add the workflow name when titles repeat.
    title_count = {}
    for _, r in runs:
        title_count[r.get("display_title")] = title_count.get(r.get("display_title"), 0) + 1

    for row, (repo, r) in enumerate(runs[offset: offset + body_h]):
        y = body_top + row
        is_sel = r["id"] == st.selected_id
        c = col.selected if is_sel else col
        text_attr = c("text") if is_sel else 0
        if is_sel:
            put(scr, y, 0, " " * w, text_attr)
        glyph, color = status_glyph(r)
        put(scr, y, 0, f"{glyph} ", c(color) | curses.A_BOLD)
        x = 2
        for key, width in cells:
            if key == "text":
                parts = []
                if st.scope == "global":
                    parts.append((repo.split("/")[-1] + " ", c("dim")))
                if title_count.get(r.get("display_title"), 0) > 1:
                    parts.append(((r.get("name") or "") + " · ", text_attr | curses.A_BOLD))
                parts.append((r.get("display_title") or "", text_attr))
                used = 0
                for text, attr in parts:
                    chunk = fit(text, width - used).rstrip() if used + len(text) > width else text
                    put(scr, y, x + used, chunk, attr)
                    used += len(chunk)
                    if used >= width:
                        break
            else:
                text = {"repo": repo.split("/")[-1], "wf": r.get("name"),
                        "branch": r.get("head_branch"), "title": r.get("display_title")}[key]
                attr = {"repo": c("dim"), "wf": text_attr | curses.A_BOLD,
                        "branch": c("magenta")}.get(key, text_attr)
                put(scr, y, x, fit(text, width), attr)
            x += width + 1
        # How long the run took, or has taken so far while it's still going.
        put(scr, y, w - 7, run_duration(r).rjust(6), c(color if is_active(r) else "dim"))

    if st.confirm:
        put(scr, h - 1, 0, fit(" " + st.confirm[0], w), col("yellow") | curses.A_BOLD)
    elif message:
        put(scr, h - 1, 0, fit(" " + message, w), col("yellow"))
    elif error and runs:
        put(scr, h - 1, 0, fit(f" ⚠ {error}", w), col("red"))
    else:
        put(scr, h - 1, 0, fit(" " + (SHORT_HINTS if w < 80 else HINTS), w), col("dim"))
    scr.refresh()
    return offset


def ui(scr):
    curses.curs_set(0)
    try:
        curses.set_escdelay(25)
    except AttributeError:
        pass
    scr.timeout(250)
    col = Colors()
    st = State()
    threading.Thread(target=worker, args=(st,), daemon=True).start()
    offset = 0

    while True:
        offset = draw(scr, st, col, offset)
        ch = scr.getch()
        if ch == -1:
            continue
        if st.help:
            st.help = False
            continue
        if st.confirm:
            prompt, fn = st.confirm
            st.confirm = None
            if ch in (ord("y"), ord("Y")):
                fn()
            continue

        with st.lock:
            ids = [r["id"] for _, r in st.runs]
        idx = ids.index(st.selected_id) if st.selected_id in ids else 0
        h, w = scr.getmaxyx()
        page = max(1, h - 4)

        if ch in (ord("q"), 27):
            break
        elif ch in (curses.KEY_DOWN, ord("j")) and ids:
            st.selected_id = ids[min(idx + 1, len(ids) - 1)]
        elif ch in (curses.KEY_UP, ord("k")) and ids:
            st.selected_id = ids[max(idx - 1, 0)]
        elif ch in (curses.KEY_NPAGE, 4) and ids:  # PgDn, ^D
            st.selected_id = ids[min(idx + page, len(ids) - 1)]
        elif ch in (curses.KEY_PPAGE, 21) and ids:  # PgUp, ^U
            st.selected_id = ids[max(idx - page, 0)]
        elif ch in (ord("g"), curses.KEY_HOME) and ids:
            st.selected_id = ids[0]
        elif ch in (ord("G"), curses.KEY_END) and ids:
            st.selected_id = ids[-1]
        elif ch in (10, 13, curses.KEY_ENTER):
            action_view(st, logs=False)
        elif ch == ord("l"):
            action_view(st, logs=True)
        elif ch == ord("o"):
            action_browser(st)
        elif ch == ord("R"):
            ask_rerun(st)
        elif ch == ord("x"):
            ask_cancel(st)
        elif ch == ord("a"):
            st.mine = not st.mine
            st.flash("showing your runs" if st.mine else "showing everyone's runs", 2)
            st.wake.set()
        elif ch == ord("s"):
            st.scope = "global" if st.scope == "repo" else "repo"
            st.flash("all your recent repos" if st.scope == "global" else "following focused repo", 2)
            st.wake.set()
        elif ch == ord("p"):
            st.pinned = not st.pinned
            st.flash(f"pinned to {st.target[0]}" if st.pinned and st.target else "following focused pane", 2)
            st.wake.set()
        elif ch == ord("?"):
            st.help = True
        elif ch == ord("r"):
            st.fetched_key = None
            st.repos_at = 0
            st.wake.set()

    st.quit = True


# ---------------------------------------------------------------- toggle action

def toggle():
    """Close our pane in this tab if it's focused, focus it if not, else open one."""
    try:
        ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        ctx = {}
    ws = ctx.get("workspace_id") or os.environ.get("HERDR_WORKSPACE_ID")
    tab = ctx.get("tab_id") or os.environ.get("HERDR_TAB_ID")
    focused_id = ctx.get("focused_pane_id") or os.environ.get("HERDR_PANE_ID")
    panes = list_panes(ws)
    ours = [p for p in panes if TOKEN in (p.get("tokens") or {})
            and (not tab or p.get("tab_id") == tab)]
    if ours:
        p = ours[0]
        verb = "close" if p["pane_id"] == focused_id else "focus"
        run([HERDR, "plugin", "pane", verb, p["pane_id"]])
        return
    sidebar = find_sidebar([p for p in panes if not tab or p.get("tab_id") == tab])
    if sidebar:
        # Small pane docked under herdr-sidebar; leave focus where it is.
        target, direction, focus = sidebar["pane_id"], "down", "--no-focus"
    elif focused_id:
        target, direction, focus = focused_id, "right", "--focus"
    else:
        raise CmdError("no focused pane to open beside")
    args = ["plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "runs",
            "--target-pane", target, "--direction", direction, focus]
    cwd = ctx.get("focused_pane_cwd") or ctx.get("workspace_cwd")
    if cwd:
        args += ["--cwd", cwd]
    pane_id = herdr(*args)["result"]["plugin_pane"]["pane"]["pane_id"]
    # Tag now so a quick second toggle finds it before the UI's first refresh.
    run([HERDR, "pane", "report-metadata", pane_id, "--source", PLUGIN_ID,
         "--token", f"{TOKEN}=1"])
    if sidebar:
        shrink_to(pane_id, DOCK_ROWS)


DOCK_ROWS = int(os.environ.get("GH_RUNS_ROWS") or 14)


def find_sidebar(panes):
    """The herdr-sidebar pane in a tab, if there is one."""
    for p in panes:
        tokens = p.get("tokens") or {}
        if any(k.startswith("herdr-sidebar") for k in tokens) or p.get("label") == "Sidebar":
            return p
    return None


def shrink_to(pane_id, rows):
    """Move the divider above our pane down until the pane is about `rows` tall."""
    for _ in range(4):  # each resize moves the divider by at most 0.5
        layout = herdr("pane", "layout", "--pane", pane_id)["result"]["layout"]
        mine = next(p["rect"] for p in layout["panes"] if p["pane_id"] == pane_id)
        parent = [s["rect"] for s in layout["splits"]
                  if s["direction"] == "down"
                  and s["rect"]["x"] == mine["x"] and s["rect"]["width"] == mine["width"]
                  and s["rect"]["y"] + s["rect"]["height"] == mine["y"] + mine["height"]]
        if not parent or mine["height"] <= rows:
            return
        total = min(parent, key=lambda r: r["height"])["height"]
        amount = min(0.5, (mine["height"] - rows) / total)
        if amount < 0.01:
            return
        run([HERDR, "pane", "resize", "--pane", pane_id, "--direction", "down",
             "--amount", f"{amount:.4f}"])


def main():
    if "--toggle" in sys.argv[1:]:
        try:
            toggle()
        except CmdError as e:
            print(f"gh-runs: {e}", file=sys.stderr)
            sys.exit(1)
        return
    try:
        if sys.argv[1:2] == ["--logs"]:
            import log_viewer
            log_viewer.main(sys.argv[2:])
        else:
            curses.wrapper(ui)
    except KeyboardInterrupt:
        pass
    except Exception:
        # Plugin panes close as soon as the process exits, so leave a trace.
        import traceback
        state = os.environ.get("HERDR_PLUGIN_STATE_DIR") or PLUGIN_ROOT
        with open(os.path.join(state, "crash.log"), "a") as f:
            f.write(f"--- {datetime.now().isoformat()}\n{traceback.format_exc()}")
        raise


if __name__ == "__main__":
    main()
