"""Transcript records -> terminal text that looks like Claude Code.

`render_record` prints one line of a subagent transcript (JSON) the way Claude
Code draws its own: ❯ prompts on a grey band, ⏺ before Claude's words and each
tool call, ⎿ under it for the result, ✻ for thinking. A tool's ⏺ starts grey
and turns green or red when its result arrives.
"""
import json
import os
import re
import sys

from .text import C, paint, strip_ansi, terminal_width, visible_width, wrap, wrap_block

TASK_PREVIEW = {True: 240, False: 400}      # first message (the task) vs later ones
RESULT_LINES = 3
REPORT_LINES = 60
DIFF_LINES = 8

DOT = "⏺"
RESULT = "  ⎿  "
UNDER = "     "            # continuation of a ⎿ result
INDENT = "  "              # continuation of a ⏺ block


def emit(lines):
    """Print rows; return the output line number of the first, if the screen counts them."""
    first = getattr(sys.stdout, "emitted", None)
    for line in lines:
        print(line)
    return first


def blank():
    print()


# ── markdown ────────────────────────────────────────────────────────────
_CODE_SPAN = re.compile(r"(`[^`\n]+`)")
_BOLD = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*|__(?!\s)(.+?)(?<!\s)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])|(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+[.)])\s+(.*)$")
_RULE = re.compile(r"\s*([-*_])(\s*\1){2,}\s*")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def inline(text):
    """Code spans, links, bold and italics within one line."""
    out = []
    for part in _CODE_SPAN.split(text):
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"{C['code']}{part[1:-1]}{C['fg']}")
            continue
        part = _LINK.sub(lambda m: f"{C['ul']}{m.group(1)}{C['r']}", part)
        part = _BOLD.sub(lambda m: f"{C['b']}{m.group(1) or m.group(2)}{C['r']}", part)
        part = _ITALIC.sub(lambda m: f"{C['it']}{m.group(1) or m.group(2)}{C['r']}", part)
        out.append(part)
    return "".join(out)


def markdown(text, max_lines=None, width=None):
    """Style the markdown agents write the way Claude Code shows it. Returns
    logical lines (not yet wrapped); tables come out already drawn."""
    lines, in_code, source = [], False, text.splitlines()
    i = 0
    while i < len(source):
        line = source[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
        elif in_code:
            lines.append(line)
        elif stripped.startswith("|") and i + 1 < len(source) and _TABLE_SEP.match(source[i + 1]):
            rows, i = _table_rows(source, i)
            lines += _table(rows, width)
            continue
        else:
            lines.append(_markdown_line(line, stripped))
        i += 1
    while lines and not strip_ansi(lines[-1]).strip():
        lines.pop()
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines] + [paint(f"… +{len(lines) - max_lines} lines", "grey")]
    return lines


def _markdown_line(line, stripped):
    m = _HEADING.match(line)
    if m:
        return f"{C['b']}{inline(m.group(2))}{C['r']}"
    if _RULE.fullmatch(line):
        return paint("─" * 30, "grey")
    m = _BULLET.match(line)
    if m:
        return f"{m.group(1)}- {inline(m.group(2))}"
    m = _NUMBERED.match(line)
    if m:
        return f"{m.group(1)}{m.group(2)} {inline(m.group(3))}"
    if stripped.startswith(">"):
        return f"{C['dim']}▎{C['r']} {C['it']}{inline(stripped.lstrip('>').strip())}{C['r']}"
    return inline(line)


def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [inline(c.strip()) for c in line.split("|")]


def _table_rows(source, i):
    rows = [_cells(source[i])]
    i += 2                                   # header, separator
    while i < len(source) and source[i].strip().startswith("|"):
        rows.append(_cells(source[i]))
        i += 1
    return rows, i


def _table(rows, width):
    """Box-drawn table like Claude Code's; plain rows if it can't fit."""
    cols = max(len(r) for r in rows)
    rows = [r + [""] * (cols - len(r)) for r in rows]
    widths = [max(visible_width(r[c]) for r in rows) for c in range(cols)]
    room = (width or terminal_width()) - len(INDENT)
    if sum(widths) + 3 * cols + 1 > room:
        return [" │ ".join(r) for r in rows]

    def border(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def row(cells, center=False):
        out = []
        for cell, w in zip(cells, widths):
            pad = w - visible_width(cell)
            lead = pad // 2 if center else 0
            out.append(" " * (lead + 1) + cell + " " * (pad - lead + 1))
        return "│" + "│".join(out) + "│"

    lines = [border("┌", "┬", "┐"), row(rows[0], center=True)]
    for r in rows[1:]:
        lines += [border("├", "┼", "┤"), row(r)]
    return lines + [border("└", "┴", "┘")]


# ── small text helpers ──────────────────────────────────────────────────
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_TAG = {name: re.compile(rf"<{name}>(.*?)</{name}>", re.S) for name in ("summary", "status")}


def without_reminders(text):
    """Drop the <system-reminder> blocks Claude Code injects."""
    return _REMINDER.sub("", text or "").strip()


def truncate(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def task_preview(text, first):
    """A task prompt can be pages long; show its gist (~3 lines)."""
    flat = re.sub(r"\s+", " ", text).strip()
    limit = TASK_PREVIEW[first]
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


SUMMARY_FIELDS = ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt", "skill")


def tool_summary(tool_input):
    """The one argument that says what a tool call does."""
    if not isinstance(tool_input, dict):
        return truncate(tool_input, 100)
    for field in SUMMARY_FIELDS:
        if tool_input.get(field):
            return truncate(str(tool_input[field]).replace("\n", " "), 110)
    return truncate(tool_input, 110)


def relative(path, cwd):
    if cwd and path and os.path.isabs(path):
        try:
            rel = os.path.relpath(path, cwd)
        except ValueError:
            return path
        return path if rel.startswith("../../..") else rel
    return path or ""


def tool_title(name, tool_input, cwd=None):
    """(label, argument) the way Claude Code titles a tool call: Bash(ls),
    Read(a.go), Update(a.go), Search(pattern: "x"), Fetch(url)..."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    get = lambda k: str(inp.get(k) or "")          # noqa: E731
    path = relative(get("file_path") or get("notebook_path"), cwd)
    if name == "Bash":
        return "Bash", truncate(get("command").replace("\n", " "), 300)
    if name in ("Read", "Write"):
        return name, path
    if name in ("Edit", "MultiEdit"):
        return "Update", path
    if name == "NotebookEdit":
        return "Edit Notebook", path
    if name in ("Grep", "Glob"):
        arg = f'pattern: "{get("pattern")}"'
        if inp.get("path"):
            arg += f', path: "{relative(get("path"), cwd)}"'
        return "Search", truncate(arg, 200)
    if name == "WebFetch":
        return "Fetch", get("url")
    if name == "WebSearch":
        return "Web Search", f'"{get("query")}"'
    if name == "TodoWrite":
        return "Update Todos", ""
    if name in ("Task", "Agent"):
        return "Task", get("description") or truncate(get("prompt"), 80)
    if name.startswith("mcp__") and name.count("__") >= 2:
        _, server, tool = name.split("__", 2)
        return f"{server} - {tool} (MCP)", tool_summary(inp) if inp else ""
    return name, tool_summary(tool_input) if tool_input not in (None, {}) else ""


def result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, dict) and item.get("type") == "image":
                parts.append("[image]")
        return "\n".join(parts)
    return str(content)


def agent_state(record_line):
    """"running" or "error" from a transcript line's tool results; None if it has none."""
    try:
        record = json.loads(record_line)
    except ValueError:
        return None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return None
    results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
    if not results:
        return None
    return "error" if any(b.get("is_error") for b in results) else "running"


# ── pieces the viewer prints too ────────────────────────────────────────

def prompt(text):
    """A message to the agent, drawn like a prompt in Claude Code."""
    band = C["prompt"]
    rows = wrap(f"{C['grey']}❯ {C['text']}{text}", terminal_width() - 1, band, band + "  ")
    return emit(row + C["r"] for row in rows)


def note(text, color="grey"):
    """A ⎿ line hanging under the thing above it."""
    return emit(wrap(f"{C[color]}{text}{C['r']}", None, paint(RESULT, "grey"), UNDER))


def header(title, detail):
    """Claude Code's welcome box, for an agent."""
    room = max(terminal_width() - 4, 10)
    lines = [f"{C['claude']}✻ {C['r']}{C['b']}{truncate(title, room - 2)}{C['r']}",
             paint(f"  {truncate(detail, room - 2)}", "grey")]
    inner = max(visible_width(line) for line in lines)
    out = [paint("╭" + "─" * (inner + 2) + "╮", "claude")]
    for line in lines:
        pad = " " * (inner - visible_width(line))
        out.append(f"{C['claude']}│{C['r']} {line}{pad} {C['claude']}│{C['r']}")
    out.append(paint("╰" + "─" * (inner + 2) + "╯", "claude"))
    emit(out)


def done_line(text):
    """✻ Worked for 37s — Claude Code's line when a turn ends."""
    blank()
    emit([f"{C['grey']}✻ {text}{C['r']}"])


# ── transcript records ──────────────────────────────────────────────────

class Renderer:
    def __init__(self):
        self.cwd = None
        self.calls = {}        # tool_use_id -> (name, input, output line of its ⏺ or None)

    def record(self, record_line, first):
        """Print one transcript line. `first` marks the agent's first message (its task)."""
        try:
            record = json.loads(record_line)
        except json.JSONDecodeError:
            return
        if record.get("cwd"):
            self.cwd = record["cwd"]
        if self._forwarded_message(record):
            return
        # only conversation turns; isMeta entries (e.g. our stop-hook feedback) are already shown
        if record.get("type") not in ("user", "assistant") or record.get("isMeta"):
            return
        message = record.get("message") or {}
        role = message.get("role") or record.get("type")
        content = message.get("content")
        if isinstance(content, str):
            self._text(role, content, first)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    self._block(role, block, first)
        sys.stdout.flush()

    def _forwarded_message(self, record):
        """A SendMessage from Claude reached this agent (e.g. a relayed message)."""
        attachment = record.get("attachment") if record.get("type") == "attachment" else None
        if not (isinstance(attachment, dict) and attachment.get("type") == "queued_command"
                and attachment.get("prompt")):
            return False
        sender = "Claude" if (attachment.get("origin") or {}).get("kind") == "coordinator" else "caller"
        blank()
        prompt(truncate(without_reminders(str(attachment["prompt"])), 300))
        note(f"received from {sender}")
        return True

    def _text(self, role, text, first):
        text = without_reminders(text)
        if not text:
            return
        if role != "user":
            blank()
            emit(wrap_block(markdown(text), None, f"{C['text']}{DOT}{C['r']} ", INDENT))
        elif "<task-notification>" in text:
            self._task_notification(text)
        else:
            blank()
            prompt(task_preview(text, first))

    def _task_notification(self, text):
        summary = _TAG["summary"].search(text)
        status = _TAG["status"].search(text)
        line = summary.group(1).strip() if summary else "Background task " + (status.group(1) if status else "ended")
        blank()
        emit(wrap(paint(truncate(line, 300), "grey"), None, paint(DOT + " ", "grey"), INDENT))

    def _block(self, role, block, first):
        kind = block.get("type")
        if kind == "text":
            self._text(role, block.get("text"), first)
        elif kind == "thinking" and block.get("thinking", "").strip():
            thought = truncate(block["thinking"].strip().replace("\n", " "), 240)
            blank()
            emit([paint("✻ Thinking…", "grey", "it")])
            emit(wrap(paint(thought, "grey", "it"), None, INDENT))
        elif kind == "tool_use" and block.get("name") == "SubagentHandback" and isinstance(block.get("input"), dict):
            self._handback(block["input"])
        elif kind == "tool_use":
            self._tool_use(block)
        elif kind == "tool_result":
            self._tool_result(block)

    def _tool_use(self, block):
        name, inp = block.get("name", "?"), block.get("input")
        label, arg = tool_title(name, inp, self.cwd)
        title = f"{C['b']}{label}{C['r']}" + (f"({arg})" if arg else "")
        blank()
        line = emit(wrap(title, None, paint(DOT, "grey") + " ", INDENT))
        self.calls[block.get("id")] = (name, inp, line)

    def _tool_result(self, block):
        name, inp, line = self.calls.pop(block.get("tool_use_id"), (None, None, None))
        failed = bool(block.get("is_error"))
        repaint = getattr(sys.stdout, "repaint", None)
        if line is not None and repaint:
            dot = paint(DOT, "err" if failed else "ok")
            repaint(line, lambda row: row.replace(paint(DOT, "grey"), dot, 1))
        text = result_text(block.get("content")).strip()
        if failed:
            self._lines(text.splitlines() or ["Error"], "err")
        else:
            self._success(name, inp if isinstance(inp, dict) else {}, text)

    def _success(self, name, inp, text):
        lines = text.splitlines()
        path = relative(str(inp.get("file_path") or ""), self.cwd)
        if name == "Read":
            note(f"Read {C['b']}{len(lines)}{C['r']}{C['grey']} lines")
        elif name == "Glob":
            count = 0 if text.startswith("No files") else len(lines)
            note(f"Found {C['b']}{count}{C['r']}{C['grey']} files")
        elif name == "Write":
            written = len(str(inp.get("content") or "").splitlines())
            note(f"Wrote {C['b']}{written}{C['r']}{C['grey']} lines to {C['b']}{path}{C['r']}")
        elif name in ("Edit", "MultiEdit"):
            self._diff(path, inp)
        elif name == "TodoWrite":
            self._todos(inp.get("todos") or [])
        else:
            self._lines(lines or ["(No output)"], None)

    def _lines(self, lines, color):
        start = C[color] if color else C["fg"]
        shown = [f"{start}{truncate(line, 300)}{C['r']}" for line in lines[:RESULT_LINES]]
        if len(lines) > RESULT_LINES:
            shown.append(paint(f"… +{len(lines) - RESULT_LINES} lines", "dim"))
        emit(wrap_block(shown, None, paint(RESULT, "grey"), UNDER))

    def _diff(self, path, inp):
        edits = inp.get("edits") or [{"old_string": inp.get("old_string"), "new_string": inp.get("new_string")}]
        removed, added = [], []
        for edit in edits:
            removed += str(edit.get("old_string") or "").splitlines()
            added += str(edit.get("new_string") or "").splitlines()
        note(f"Updated {C['b']}{path}{C['r']}{C['grey']} (+{len(added)} -{len(removed)})")
        width = max(terminal_width() - len(UNDER), 10)
        rows = [("del", "-", t) for t in removed] + [("add", "+", t) for t in added]
        for style, sign, line in rows[:DIFF_LINES]:
            body = truncate(f" {sign} {line}", width).ljust(width)
            emit([UNDER + paint(body, style)])
        if len(rows) > DIFF_LINES:
            emit([UNDER + paint(f"… +{len(rows) - DIFF_LINES} lines", "dim")])

    def _todos(self, todos):
        rows = []
        for todo in todos:
            if not isinstance(todo, dict):
                continue
            content = str(todo.get("content") or "")
            status = todo.get("status")
            if status == "completed":
                rows.append(paint(f"☒ {content}", "grey", "strike"))
            elif status == "in_progress":
                rows.append(paint(f"☐ {content}", "b"))
            else:
                rows.append(f"☐ {content}")
        emit(wrap_block(rows or [paint("(no todos)", "grey")], None, paint(RESULT, "grey"), UNDER))

    def _handback(self, tool_input):
        report = str(tool_input.get("message") or "").strip()
        blank()
        emit([f"{C['ok']}{DOT}{C['r']} {C['b']}Report to caller{C['r']}"])
        if report:
            emit(wrap_block(markdown(report, max_lines=REPORT_LINES), None, INDENT))


_renderer = Renderer()


def render_record(record_line, first):
    _renderer.record(record_line, first)
