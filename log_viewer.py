"""Log viewer for one GitHub Actions run, shown in a herdr popup.

Jobs and steps on the left, the selected step's log on the right, with the
runner's timestamps stripped, ANSI colours rendered and ##[error] lines marked.
"""

import base64
import curses
import os
import shutil
import re
import subprocess
import sys
import threading
import time

from gh_runs import (FAILED, CmdError, Colors, fit, gh_api, parse_time, put, run,
                     short_span, status_glyph)

TS_RE = re.compile(r"^\ufeff?(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z ?")
ESC_RE = re.compile(
    r"\x1b\[([0-9;?]*)([A-Za-z])"          # CSI (SGR when it ends in m)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[()][A-Za-z0-9]|\x1b.?|\r"
)
# Lines worth stopping at with `e` besides the runner's own ##[error] markers.
ERRORISH_RE = re.compile(r"(--- FAIL|\bFAIL\b|\bERROR\b|\bError:|error TS\d+|\bpanic:|Traceback \(most recent)")
# Control characters curses can't draw (NUL raises). ESC stays for colour codes.
CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1a\x1c-\x1f\x7f]")
SPECIAL_STARTS = {"Complete job": "Cleaning up orphan processes"}


# ---------------------------------------------------------------- log parsing

class Palette:
    """Allocates curses colour pairs on demand for ANSI foreground colours."""

    def __init__(self, colors):
        self.colors = colors
        self.cache = {}
        self.next_pair = 16
        self.enabled = curses.has_colors()

    def fg(self, n):
        if n is None or not self.enabled:
            return 0
        if n >= curses.COLORS:
            n = n % 8 if n < 16 else 7
        if n not in self.cache:
            if self.next_pair >= curses.COLOR_PAIRS:
                return 0
            curses.init_pair(self.next_pair, n, self.colors.bg)
            self.cache[n] = curses.color_pair(self.next_pair)
            self.next_pair += 1
        return self.cache[n]


def rgb_to_256(r, g, b):
    return 16 + 36 * round(r / 255 * 5) + 6 * round(g / 255 * 5) + round(b / 255 * 5)


def parse_ansi(text, pal, base=0):
    """Split text into (str, curses attr) segments, applying SGR codes."""
    segs = []
    fg, bold, dim, ul = None, False, False, False

    def attr():
        a = base | pal.fg(fg)
        a |= curses.A_BOLD if bold else 0
        a |= curses.A_DIM if dim else 0
        a |= curses.A_UNDERLINE if ul else 0
        return a

    pos = 0
    for m in ESC_RE.finditer(text):
        if m.start() > pos:
            segs.append((text[pos:m.start()], attr()))
        pos = m.end()
        if m.group(2) != "m":
            continue
        params = [int(p) if p.isdigit() else 0 for p in (m.group(1) or "0").split(";")]
        i = 0
        while i < len(params):
            p = params[i]
            if p == 0:
                fg, bold, dim, ul = None, False, False, False
            elif p == 1:
                bold = True
            elif p == 2:
                dim = True
            elif p == 4:
                ul = True
            elif p == 22:
                bold = dim = False
            elif p == 24:
                ul = False
            elif 30 <= p <= 37:
                fg = p - 30
            elif 90 <= p <= 97:
                fg = p - 90 + 8
            elif p == 39:
                fg = None
            elif p in (38, 48) and i + 1 < len(params):
                if params[i + 1] == 5 and i + 2 < len(params):
                    if p == 38:
                        fg = params[i + 2]
                    i += 2
                elif params[i + 1] == 2 and i + 4 < len(params):
                    if p == 38:
                        fg = rgb_to_256(*params[i + 2:i + 5])
                    i += 4
            i += 1
    if pos < len(text):
        segs.append((text[pos:], attr()))
    merged = []
    for t, a in segs:
        if merged and merged[-1][1] == a:
            merged[-1] = (merged[-1][0] + t, a)
        elif t:
            merged.append((t, a))
    return merged


class Line:
    __slots__ = ("segs", "text", "kind", "prefix")

    def __init__(self, segs, kind="normal", prefix=0):
        self.segs = segs
        self.text = "".join(t for t, _ in segs)
        self.kind = kind  # normal | group | error | errorish | warning
        self.prefix = prefix  # leading glyph chars ("✗ ", "$ ") left out of copies


def plain(text):
    return ESC_RE.sub("", text)


def render_line(content, pal, col):
    content = CONTROL_RE.sub("", content.lstrip("\ufeff").expandtabs(4))
    if content.startswith("##[endgroup]"):
        return None
    for marker, glyph, color, kind in (
        ("##[group]", "▸ ", "cyan", "group"),
        ("##[error]", "✗ ", "red", "error"),
        ("##[warning]", "! ", "yellow", "warning"),
        ("##[notice]", "ℹ ", "cyan", "normal"),
        ("##[debug]", "  ", "dim", "normal"),
    ):
        if content.startswith(marker):
            attr = col(color) | (curses.A_BOLD if kind in ("group", "error") else 0)
            return Line([(glyph + plain(content[len(marker):]), attr)], kind, len(glyph))
    if content.startswith("[command]"):
        return Line([("$ ", col("dim"))] + parse_ansi(content[9:], pal, curses.A_DIM), prefix=2)
    line = Line(parse_ansi(content, pal))
    if ERRORISH_RE.search(line.text):
        line.kind = "errorish"
    return line


def parse_job_log(raw):
    """[(second-precision timestamp, content)] from a raw job log."""
    out = []
    for line in raw.splitlines():
        m = TS_RE.match(line)
        if m:
            out.append((m.group(1), line[m.end():]))
        else:
            out.append((out[-1][0] if out else "", line))
    return out


def step_ranges(lines, steps):
    """Map step number -> (start, end) line indexes in its job's log.

    Steps start at their `##[group]<name>` header where there is one, and
    otherwise at the first line logged at or after the step's start time.
    """
    ran = [s for s in steps if s.get("started_at") and s.get("conclusion") != "skipped"]
    starts, pos = [], 0
    for i, s in enumerate(ran):
        if i == 0:
            starts.append(0)
            continue
        wanted = (f"##[group]{s['name']}", SPECIAL_STARTS.get(s["name"]),
                  "Post job cleanup." if s["name"].startswith("Post ") else None)
        idx = next((j for j in range(pos + 1, len(lines))
                    if lines[j][1].lstrip("\ufeff") in wanted), None)
        if idx is None:
            key = s["started_at"][:19]
            idx = next((j for j in range(pos + 1, len(lines)) if lines[j][0] >= key), len(lines))
            # Snap to a step header logged in the same second, if any.
            if idx < len(lines):
                for j in range(idx, len(lines)):
                    if lines[j][0] != lines[idx][0]:
                        break
                    if lines[j][1].startswith("##[group]Run "):
                        idx = j
                        break
        pos = max(idx, pos)
        starts.append(pos)
    return {s["number"]: (starts[i], starts[i + 1] if i + 1 < len(starts) else len(lines))
            for i, s in enumerate(ran)}


def span(item):
    start = parse_time(item.get("started_at"))
    end = parse_time(item.get("completed_at"))
    if not start:
        return ""
    if not end:
        return short_span(time.time() - start.timestamp())
    return short_span((end - start).total_seconds())


# ---------------------------------------------------------------- viewer

HINTS = {
    "tree": "j/k select  ⏎ log  e next failure  o browser  r reload  q close",
    "log": "j/k scroll  / search  e errors  drag or v select  y copy  Y copy step  ← steps  q close",
    "visual": "j/k extend selection  y copy  Esc cancel",
}


class Viewer:
    def __init__(self, repo, run_id, focus_log):
        self.repo, self.run_id = repo, run_id
        self.run = {}
        self.jobs = []
        self.items = []  # ("job", job) / ("step", job, step)
        self.sel = 0
        self.focus = "log" if focus_log else "tree"
        self.logs = {}  # job id -> list[(ts, content)] | str error | None while loading
        self.rendered = {}  # (job id, step number|None) -> list[Line]
        self.rows_cache = {}
        self.top = 0
        self.pending_tail = True
        self.search = ""
        self.matches = []
        self.match_i = -1
        self.input = None
        self.message, self.message_until = None, 0
        self.lock = threading.Lock()
        self.error = None
        # Selection: (line, col) anchor and end, end inclusive. Linewise for `v`.
        self.sel_a = self.sel_b = None
        self.linewise = self.visual = self.dragging = False
        self.geo_tree = self.geo_log = None  # set while drawing, used for mouse hits

    def flash(self, text):
        self.message, self.message_until = text, time.time() + 3

    # data

    def load_jobs(self):
        try:
            self.run = gh_api(f"repos/{self.repo}/actions/runs/{self.run_id}")
            jobs = gh_api(f"repos/{self.repo}/actions/runs/{self.run_id}/jobs?per_page=100")["jobs"]
        except (CmdError, ValueError, KeyError) as e:
            self.error = str(e)
            return
        self.jobs = jobs
        self.items = []
        for job in jobs:
            self.items.append(("job", job))
            for step in job.get("steps") or []:
                self.items.append(("step", job, step))
        # Start on the first failed step (or job), else the first job.
        self.sel = 0
        for i, it in enumerate(self.items):
            if it[0] == "step" and it[2].get("conclusion") in FAILED:
                self.sel = i
                break
        else:
            for i, it in enumerate(self.items):
                if it[0] == "job" and it[1].get("conclusion") in FAILED:
                    self.sel = i
                    break
        it = self.items[self.sel] if self.items else None
        node = (it[2] if it[0] == "step" else it[1]) if it else {}
        self.pending_tail = node.get("conclusion") in FAILED
        for job in sorted(jobs, key=lambda j: j.get("conclusion") not in FAILED):
            self.fetch_log(job)

    def fetch_log(self, job, force=False):
        jid = job["id"]
        if jid in self.logs and not force:
            return
        self.logs[jid] = None

        def go():
            try:
                raw = run(["gh", "api", "--allow-escape-sequences",
                           f"repos/{self.repo}/actions/jobs/{jid}/logs"], timeout=90)
                result = parse_job_log(raw)
            except CmdError as e:
                result = ("No log yet: GitHub publishes a job's log when the job finishes."
                          if job.get("status") != "completed" else f"Couldn't load log: {e}")
            with self.lock:
                self.logs[jid] = result
                self.rendered = {k: v for k, v in self.rendered.items() if k[0] != jid}
                self.rows_cache = {}

        threading.Thread(target=go, daemon=True).start()

    def current(self):
        return self.items[self.sel] if self.items else None

    def view_lines(self, pal, col):
        """Rendered lines for the selected job or step, or a status string."""
        it = self.current()
        if not it:
            return "Loading jobs…"
        job = it[1]
        with self.lock:
            log = self.logs.get(job["id"])
        if log is None:
            return "Loading log…"
        if isinstance(log, str):
            return log
        step = it[2] if it[0] == "step" else None
        key = (job["id"], step["number"] if step else None)
        if key not in self.rendered:
            if step:
                if step.get("conclusion") == "skipped":
                    return "Step was skipped."
                start, end = step_ranges(log, job.get("steps") or []).get(step["number"], (0, 0))
                raw = log[start:end]
            else:
                raw = log
            lines = [ln for ln in (render_line(c, pal, col) for _, c in raw) if ln]
            self.rendered[key] = lines
        return self.rendered[key]

    def rows(self, lines, width):
        """Wrapped rows: [(line index, start column)]."""
        key = (id(lines), width)
        if key not in self.rows_cache:
            out = []
            for i, ln in enumerate(lines):
                n = max(1, -(-len(ln.text) // width))
                out.extend((i, k * width) for k in range(n))
            self.rows_cache[key] = out
        return self.rows_cache[key]

    # navigation

    def select(self, i):
        if self.items:
            self.sel = max(0, min(i, len(self.items) - 1))
            self.top = 0
            self.pending_tail = self.current()[0] == "step" and \
                self.current()[2].get("conclusion") in FAILED
            self.matches, self.match_i = [], -1
            self.clear_selection()
            self.fetch_log(self.current()[1])

    # selection and copying

    def clear_selection(self):
        self.sel_a = self.sel_b = None
        self.linewise = self.visual = self.dragging = False

    def selection_span(self, li, length):
        """[start, end) of line li that is selected, or None."""
        if self.sel_a is None or self.sel_b is None:
            return None
        a, b = sorted([self.sel_a, self.sel_b])
        if not a[0] <= li <= b[0]:
            return None
        if self.linewise:
            return 0, length
        start = a[1] if li == a[0] else 0
        end = b[1] + 1 if li == b[0] else length
        return start, min(end, length)

    def selected_text(self, lines):
        if self.sel_a is None or not isinstance(lines, list):
            return ""
        lo, hi = sorted([self.sel_a[0], self.sel_b[0]])
        out = []
        for li in range(lo, min(hi, len(lines) - 1) + 1):
            ln = lines[li]
            start, end = self.selection_span(li, len(ln.text)) or (0, 0)
            out.append(ln.text[max(start, ln.prefix):end])
        return "\n".join(out)

    def next_failure(self):
        """Select the next failed step; failed jobs only count if no step failed."""
        n = len(self.items)
        failed_steps = [i for i, it in enumerate(self.items)
                        if it[0] == "step" and it[2].get("conclusion") in FAILED]
        targets = failed_steps or [i for i, it in enumerate(self.items)
                                   if it[0] == "job" and it[1].get("conclusion") in FAILED]
        if not targets:
            self.flash("nothing failed in this run")
            return
        nxt = min(targets, key=lambda i: (i - self.sel - 1) % n)
        if nxt == self.sel:
            self.flash("that's the only failure")
        self.select(nxt)

    def run_search(self, lines):
        q = self.search.lower()
        self.matches = []
        if q and isinstance(lines, list):
            for i, ln in enumerate(lines):
                t = ln.text.lower()
                start = t.find(q)
                while start != -1:
                    self.matches.append((i, start))
                    start = t.find(q, start + 1)
        self.match_i = -1

    def jump_match(self, rows, width, body_h, step):
        if not self.matches:
            self.flash(f"no matches for {self.search}" if self.search else "no search")
            return
        anchor = self.top + body_h // 3
        row_of = [self.row_index(rows, i, c, width) for i, c in self.matches]
        if step > 0:
            cand = [k for k, r in enumerate(row_of) if r > anchor] or [0]
            self.match_i = cand[0]
        else:
            cand = [k for k, r in enumerate(row_of) if r < anchor] or [len(row_of) - 1]
            self.match_i = cand[-1]
        self.top = max(0, row_of[self.match_i] - body_h // 3)
        self.flash(f"match {self.match_i + 1}/{len(self.matches)}")

    @staticmethod
    def row_index(rows, line_i, col, width):
        lo, hi = 0, len(rows)
        while lo < hi:  # first row of line_i
            mid = (lo + hi) // 2
            if rows[mid][0] < line_i:
                lo = mid + 1
            else:
                hi = mid
        return lo + col // width

    def jump_error(self, lines, rows, body_h, step):
        errs = [k for k, (i, c) in enumerate(rows)
                if c == 0 and lines[i].kind in ("error", "errorish")]
        anchor = self.top + 2
        if step > 0:
            nxt = [r for r in errs if r > anchor]
        else:
            nxt = [r for r in errs if r < anchor][-1:]
        if not nxt:
            self.flash("no more errors here" if errs else "no errors in this step")
            return
        self.top = max(0, nxt[0] - 2)


# ---------------------------------------------------------------- drawing

def draw_tree(scr, v, col, x0, y0, width, height):
    if not v.items:
        return
    top = max(0, min(v.sel - height // 2, len(v.items) - height))
    v.geo_tree = (x0, y0, width, height, top)
    for row, it in enumerate(v.items[top:top + height]):
        i = top + row
        node = it[2] if it[0] == "step" else it[1]
        glyph, color = status_glyph(node)
        indent = "" if it[0] == "job" else "  "
        dur = span(node) if node.get("conclusion") != "skipped" else ""
        name_w = max(0, width - len(indent) - 2 - len(dur) - 2)
        selected = i == v.sel
        if selected and v.focus == "tree":
            base = curses.A_REVERSE
        elif selected:
            base = curses.A_BOLD | curses.A_UNDERLINE
        else:
            base = 0
        name_attr = curses.A_BOLD if it[0] == "job" else (col("dim") if dur == "" else 0)
        y = y0 + row
        put(scr, y, x0, " " * width, base)
        put(scr, y, x0, indent + glyph + " ", col(color) | base | curses.A_BOLD)
        put(scr, y, x0 + len(indent) + 2, fit(node.get("name"), name_w), name_attr | base)
        put(scr, y, x0 + width - len(dur) - 1, dur, col("dim") | base)


def draw_log(scr, v, pal, col, x0, y0, width, height):
    it = v.current()
    if it:
        job = it[1]
        title = job.get("name", "")
        if it[0] == "step":
            title += " › " + it[2].get("name", "")
        put(scr, y0, x0, fit(" " + title, width), col("cyan") | curses.A_BOLD)
    lines = v.view_lines(pal, col)
    body_y, body_h = y0 + 1, height - 1
    if isinstance(lines, str):
        put(scr, body_y + 1, x0 + 2, fit(lines, width - 4), col("dim"))
        return None, None, body_h
    text_w = max(1, width - 2)
    rows = v.rows(lines, text_w)
    v.geo_log = (x0 + 2, body_y, text_w, body_h, rows, lines)
    if v.pending_tail:
        v.top = max(0, len(rows) - body_h)
        v.pending_tail = False
    v.top = max(0, min(v.top, max(0, len(rows) - body_h)))
    if not rows:
        put(scr, body_y + 1, x0 + 2, "(no output)", col("dim"))
    current = v.matches[v.match_i] if 0 <= v.match_i < len(v.matches) else None
    q = len(v.search)
    for r, (li, start) in enumerate(rows[v.top:v.top + body_h]):
        y = body_y + r
        ln = lines[li]
        if start == 0 and ln.kind in ("error", "errorish"):
            put(scr, y, x0, "▌", col("red"))
        x, pos = x0 + 2, 0
        for text, attr in ln.segs:
            seg_end = pos + len(text)
            if seg_end > start and pos < start + text_w:
                a, b = max(start, pos), min(start + text_w, seg_end)
                put(scr, y, x + (a - start), text[a - pos:b - pos], attr)
            pos = seg_end
        if q:
            for mi, mc in v.matches:
                if mi == li and mc + q > start and mc < start + text_w:
                    a, b = max(start, mc), min(start + text_w, mc + q)
                    hl = curses.A_REVERSE | (col("yellow") if (mi, mc) == current else 0)
                    put(scr, y, x + (a - start), ln.text[a:b], hl | curses.A_BOLD)
        span_ = v.selection_span(li, len(ln.text))
        if span_:
            a, b = max(span_[0], start), min(span_[1], start + text_w)
            if b > a:
                put(scr, y, x + (a - start), ln.text[a:b], curses.A_REVERSE | col("cyan"))
            elif not ln.text and start == 0:
                put(scr, y, x, " ", curses.A_REVERSE | col("cyan"))
    # Scroll position
    if rows and len(rows) > body_h:
        pct = int(100 * min(len(rows), v.top + body_h) / len(rows))
        put(scr, y0, x0 + width - 6, f"{pct:>4}%", col("dim"))
    return lines, rows, body_h


def draw(scr, v, pal, col):
    h, w = scr.getmaxyx()
    scr.erase()
    r = v.run
    glyph, color = status_glyph(r) if r else ("…", "dim")
    left = f" {glyph} {r.get('name', '')} #{r.get('run_number', '')} · {r.get('head_branch', '')} · {r.get('display_title', '')}"
    right = f" {v.repo} "
    put(scr, 0, 0, fit(left, max(0, w - len(right))), col(color) | curses.A_BOLD)
    put(scr, 0, max(0, w - len(right)), right, col("dim"))
    put(scr, 1, 0, "─" * w, col("dim"))
    body_y, body_h = 2, max(1, h - 3)
    v.geo_tree = v.geo_log = None

    if v.error:
        put(scr, body_y + 1, 2, fit(f"⚠ {v.error}", w - 4), col("red"))
        lines = rows = None
        log_h = body_h
    elif w >= 90:
        tree_w = min(44, max(28, w // 3))
        draw_tree(scr, v, col, 0, body_y, tree_w, body_h)
        for y in range(body_y, body_y + body_h):
            put(scr, y, tree_w, "│", col("dim"))
        lines, rows, log_h = draw_log(scr, v, pal, col, tree_w + 2, body_y, w - tree_w - 2, body_h)
    elif v.focus == "tree":
        draw_tree(scr, v, col, 0, body_y, w, body_h)
        lines = rows = None
        log_h = body_h
    else:
        lines, rows, log_h = draw_log(scr, v, pal, col, 0, body_y, w, body_h)

    if v.input is not None:
        put(scr, h - 1, 0, fit("/" + v.input, w), curses.A_BOLD)
        try:
            curses.curs_set(1)
            scr.move(h - 1, min(w - 1, len(v.input) + 1))
        except curses.error:
            pass
    else:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        msg = v.message if time.time() < v.message_until else None
        hint = HINTS["visual" if v.visual else v.focus]
        put(scr, h - 1, 0, fit(" " + (msg or hint), w), col("yellow") if msg else col("dim"))
    scr.refresh()
    return lines, rows, log_h


def copy_to_clipboard(text):
    """Copy via the system clipboard tool, else ask the terminal (OSC 52)."""
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text, text=True, timeout=5, check=True)
                return True
            except (OSError, subprocess.SubprocessError):
                pass
    try:
        payload = base64.b64encode(text.encode()).decode()
        sys.stdout.write(f"\x1b]52;c;{payload}\x07")
        sys.stdout.flush()
        return True
    except OSError:
        return False


def copy_lines(v, text):
    if not text:
        v.flash("nothing selected: drag over the log or press v")
        return
    n = text.count("\n") + 1
    if copy_to_clipboard(text):
        v.flash(f"copied {n} line{'s' if n != 1 else ''}" if n > 1 or len(text) > 60
                else f"copied: {text}")
    else:
        v.flash("couldn't reach the clipboard")


def log_pos(v, mx, my, clamp):
    """(line, col) under the mouse in the log, or None."""
    if not v.geo_log:
        return None
    text_x, body_y, text_w, body_h, rows, lines = v.geo_log
    if not rows:
        return None
    r = my - body_y
    if not clamp and (not 0 <= r < body_h or mx < text_x - 2):
        return None
    r = max(0, min(body_h - 1, r))
    idx = min(v.top + r, len(rows) - 1)
    li, start = rows[idx]
    col_ = start + max(0, min(mx - text_x, text_w - 1))
    return li, max(0, min(col_, len(lines[li].text) - 1))


MOUSE_ON = "\x1b[?1002h\x1b[?1006h"  # button-drag tracking, SGR coordinates
MOUSE_OFF = "\x1b[?1006l\x1b[?1002l"


def read_mouse(scr, first):
    """Parse a mouse report after ESC (or KEY_MOUSE).

    Returns (kind, x, y) with kind in press/drag/release/wheel_up/wheel_down,
    "esc" for a plain Escape key, or None for anything else.
    """
    scr.nodelay(True)
    try:
        if first == curses.KEY_MOUSE:
            c = ord("M")  # ncurses consumed ESC [ M; the three bytes follow
        else:
            c1 = scr.getch()
            if c1 == -1:
                return "esc", 0, 0
            if c1 != ord("["):
                return None
            c = scr.getch()
        if c == ord("<"):  # SGR: ESC [ < b ; x ; y M|m
            buf = ""
            while True:
                k = scr.getch()
                if k == -1:
                    return None
                if chr(k) in "Mm":
                    break
                buf += chr(k)
            try:
                btn, x, y = (int(n) for n in buf.split(";"))
            except ValueError:
                return None
            released = chr(k) == "m"
        elif c == ord("M"):  # X10: ESC [ M b x y, each offset by 32
            raw = [scr.getch() for _ in range(3)]
            if -1 in raw:
                return None
            btn, x, y = raw[0] - 32, raw[1] - 32, raw[2] - 32
            released = (btn & 3) == 3
        else:
            while c != -1 and not 64 <= c <= 126:  # drain unknown CSI
                c = scr.getch()
            return None
    finally:
        scr.nodelay(False)
        scr.timeout(200)
    x, y = x - 1, y - 1
    if btn & 64:
        return ("wheel_down" if btn & 1 else "wheel_up"), x, y
    if released:
        return "release", x, y
    if btn & 32:
        return "drag", x, y
    if btn & 3 == 0:
        return "press", x, y
    return None


def handle_mouse(v, kind, mx, my):
    g = v.geo_tree
    in_tree = bool(g) and g[0] <= mx < g[0] + g[2] and g[1] <= my < g[1] + g[3]
    in_log = bool(v.geo_log) and mx >= v.geo_log[0] - 2 and my >= v.geo_log[1] - 1

    if kind in ("wheel_up", "wheel_down"):
        step = -1 if kind == "wheel_up" else 1
        if in_tree:
            v.select(v.sel + step)
        else:
            v.top = max(0, v.top + 3 * step)
    elif kind == "press":
        if in_tree:
            i = g[4] + (my - g[1])
            if i < len(v.items):
                v.select(i)
            v.focus = "tree"
        elif in_log:
            v.clear_selection()
            pos = log_pos(v, mx, my, clamp=True)
            if pos:
                v.sel_a = v.sel_b = pos
                v.dragging = True
            v.focus = "log"
    elif v.dragging and kind in ("drag", "release"):
        # Extend the selection, scrolling when dragged past the log's edges.
        _, body_y, _, body_h, _, lines = v.geo_log
        if my < body_y:
            v.top = max(0, v.top - 1)
        elif my >= body_y + body_h:
            v.top += 1
        pos = log_pos(v, mx, my, clamp=True)
        if pos:
            v.sel_b = pos
        if kind == "release":
            v.dragging = False
            if v.sel_a == v.sel_b:
                v.clear_selection()  # plain click
            else:
                copy_lines(v, v.selected_text(lines))


def open_url(url):
    if url:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ui(scr, repo, run_id, focus_log):
    curses.curs_set(0)
    try:
        curses.set_escdelay(25)
    except AttributeError:
        pass
    scr.timeout(200)
    scr.keypad(True)
    sys.stdout.write(MOUSE_ON)
    sys.stdout.flush()
    col = Colors()
    pal = Palette(col)
    v = Viewer(repo, run_id, focus_log)
    draw(scr, v, pal, col)
    v.load_jobs()

    while True:
        lines, rows, log_h = draw(scr, v, pal, col)
        h, w = scr.getmaxyx()
        text_w = max(1, (w - min(44, max(28, w // 3)) - 4) if w >= 90 else w - 2)
        ch = scr.getch()
        if ch == -1:
            continue

        if ch in (27, curses.KEY_MOUSE):
            ev = read_mouse(scr, ch)
            if ev is None:
                continue
            if ev[0] != "esc":
                handle_mouse(v, *ev)
                continue
            ch = 27  # a plain Escape key

        if v.input is not None:  # typing a search
            if ch in (10, 13, curses.KEY_ENTER):
                v.search, v.input = v.input, None
                v.run_search(lines)
                if rows:
                    v.jump_match(rows, text_w, log_h, 1)
            elif ch == 27:
                v.input = None
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                v.input = v.input[:-1]
            elif 32 <= ch < 127:
                v.input += chr(ch)
            continue


        if v.visual and lines and rows:  # extending a `v` selection
            if ch in (curses.KEY_DOWN, ord("j"), curses.KEY_UP, ord("k")):
                step = 1 if ch in (curses.KEY_DOWN, ord("j")) else -1
                li = max(0, min(len(lines) - 1, v.sel_b[0] + step))
                v.sel_b = (li, 0)
                r = v.row_index(rows, li, 0, text_w)
                if r < v.top:
                    v.top = r
                elif r >= v.top + log_h:
                    v.top = r - log_h + 1
                continue
            if ch in (ord("y"), 10, 13, curses.KEY_ENTER):
                copy_lines(v, v.selected_text(lines))
                v.clear_selection()
                continue
            if ch in (27, ord("v")):
                v.clear_selection()
                continue

        if ch == ord("q"):
            break
        if ch == 9:  # Tab
            v.focus = "log" if v.focus == "tree" else "tree"
        elif ch == ord("o"):
            it = v.current()
            open_url((it[1] if it else v.run).get("html_url"))
        elif ch == ord("r"):
            v.logs.clear()
            v.rendered.clear()
            v.rows_cache.clear()
            sel = v.sel
            v.load_jobs()
            v.select(sel)
        elif v.focus == "tree":
            if ch == 27:
                break
            elif ch in (curses.KEY_DOWN, ord("j")):
                v.select(v.sel + 1)
            elif ch in (curses.KEY_UP, ord("k")):
                v.select(v.sel - 1)
            elif ch in (ord("g"), curses.KEY_HOME):
                v.select(0)
            elif ch in (ord("G"), curses.KEY_END):
                v.select(len(v.items) - 1)
            elif ch == ord("e"):
                v.next_failure()
            elif ch in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT, ord("l")):
                v.focus = "log"
        else:  # log focus
            page = max(1, log_h - 2)
            if ch == 27 and v.sel_a is not None:
                v.clear_selection()
            elif ch in (27, curses.KEY_LEFT, ord("h")):
                v.focus = "tree"
            elif ch == ord("v") and rows:
                li = rows[min(v.top, len(rows) - 1)][0]
                v.sel_a = v.sel_b = (li, 0)
                v.linewise = v.visual = True
            elif ch == ord("y"):
                copy_lines(v, v.selected_text(lines))
            elif ch == ord("Y") and isinstance(lines, list):
                copy_lines(v, "\n".join(ln.text[ln.prefix:] for ln in lines))
            elif ch in (curses.KEY_DOWN, ord("j")):
                v.top += 1
            elif ch in (curses.KEY_UP, ord("k")):
                v.top = max(0, v.top - 1)
            elif ch in (ord(" "), curses.KEY_NPAGE, ord("f")):
                v.top += page
            elif ch in (ord("b"), curses.KEY_PPAGE):
                v.top = max(0, v.top - page)
            elif ch == 4:  # ^D
                v.top += page // 2
            elif ch == 21:  # ^U
                v.top = max(0, v.top - page // 2)
            elif ch in (ord("g"), curses.KEY_HOME):
                v.top = 0
            elif ch in (ord("G"), curses.KEY_END):
                v.top = len(rows) if rows else 0
            elif ch == ord("/"):
                v.input = ""
            elif ch in (ord("n"), ord("N")) and rows:
                if v.search and not v.matches:
                    v.run_search(lines)
                v.jump_match(rows, text_w, log_h, 1 if ch == ord("n") else -1)
            elif ch in (ord("e"), ord("E")) and rows:
                v.jump_error(lines, rows, log_h, 1 if ch == ord("e") else -1)
            elif ch == ord("J"):
                v.select(v.sel + 1)
            elif ch == ord("K"):
                v.select(v.sel - 1)


def main(argv):
    repo, run_id = argv[0], argv[1]
    focus_log = "--focus-log" in argv
    try:
        curses.wrapper(ui, repo, run_id, focus_log)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(MOUSE_OFF)
        sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv[1:])
