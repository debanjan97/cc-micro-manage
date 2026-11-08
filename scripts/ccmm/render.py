"""Transcript records -> coloured terminal text.

`render_record` prints one line of a subagent transcript (JSON) the way Claude
Code shows it: the task, the agent's text (markdown styled), thinking, tool
calls and their results.
"""
import json
import re
import sys

C = {
    "dim": "\033[2m", "it": "\033[3m", "b": "\033[1m", "cyan": "\033[36m",
    "red": "\033[31m", "green": "\033[32m", "yel": "\033[33m", "mag": "\033[35m", "r": "\033[0m",
    "blue": "\033[34m", "ul": "\033[4m",
}

TASK_PREVIEW = {True: 240, False: 400}      # first message (the task) vs later ones
RESULT_LINES = 3
REPORT_LINES = 60


def paint(text, *styles):
    return "".join(C[s] for s in styles) + text + C["r"]


# ── markdown ────────────────────────────────────────────────────────────
_CODE_SPAN = re.compile(r"(`[^`\n]+`)")
_BOLD = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*|__(?!\s)(.+?)(?<!\s)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])|(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+[.)])\s+(.*)$")
_RULE = re.compile(r"\s*([-*_])(\s*\1){2,}\s*")


def inline(text):
    """Code spans, links, bold and italics within one line."""
    out = []
    for part in _CODE_SPAN.split(text):
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"{C['yel']}{part[1:-1]}{C['r']}")
            continue
        part = _LINK.sub(lambda m: f"{C['ul']}{C['blue']}{m.group(1)}{C['r']}", part)
        part = _BOLD.sub(lambda m: f"{C['b']}{m.group(1) or m.group(2)}{C['r']}", part)
        part = _ITALIC.sub(lambda m: f"{C['it']}{m.group(1) or m.group(2)}{C['r']}", part)
        out.append(part)
    return "".join(out)


def markdown(text, max_lines=None):
    """Style the markdown agents write: headings, emphasis, code, lists, quotes
    and rules. Plain text passes through unchanged."""
    lines, in_code = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            lines.append(_fence(in_code, stripped[3:].strip()))
        elif in_code:
            lines.append(f"{C['dim']}│{C['r']} {C['green']}{line}{C['r']}")
        else:
            lines.append(_markdown_line(line, stripped))
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines] + [paint(f"… +{len(lines) - max_lines} lines", "dim")]
    return "\n".join(lines)


def _fence(opening, lang):
    if not opening:
        return paint("└─", "dim")
    return paint("┌─ " + lang if lang else "┌─", "dim")


def _markdown_line(line, stripped):
    m = _HEADING.match(line)
    if m:
        color = C["mag"] if len(m.group(1)) <= 2 else C["cyan"]
        return f"{C['b']}{color}{inline(m.group(2))}{C['r']}"
    if _RULE.fullmatch(line):
        return paint("─" * 30, "dim")
    m = _BULLET.match(line)
    if m:
        return f"{m.group(1)}{C['cyan']}•{C['r']} {inline(m.group(2))}"
    m = _NUMBERED.match(line)
    if m:
        return f"{m.group(1)}{C['cyan']}{m.group(2)}{C['r']} {inline(m.group(3))}"
    if stripped.startswith(">"):
        return f"{C['dim']}▎{C['r']} {C['it']}{inline(stripped.lstrip('>').strip())}{C['r']}"
    return inline(line)


# ── small text helpers ──────────────────────────────────────────────────
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


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


# ── transcript records ──────────────────────────────────────────────────

def render_record(record_line, first):
    """Print one transcript line. `first` marks the agent's first message (its task)."""
    try:
        record = json.loads(record_line)
    except json.JSONDecodeError:
        return
    if _render_forwarded_message(record):
        return
    # only conversation turns; isMeta entries (e.g. our stop-hook feedback) are already shown
    if record.get("type") not in ("user", "assistant") or record.get("isMeta"):
        return
    message = record.get("message") or {}
    role = message.get("role") or record.get("type")
    content = message.get("content")
    if isinstance(content, str):
        _render_text(role, content, first)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                _render_block(role, block, first)
    sys.stdout.flush()


def _render_forwarded_message(record):
    """A SendMessage from Claude reached this agent (e.g. a relayed message)."""
    attachment = record.get("attachment") if record.get("type") == "attachment" else None
    if not (isinstance(attachment, dict) and attachment.get("type") == "queued_command"
            and attachment.get("prompt")):
        return False
    sender = "Claude" if (attachment.get("origin") or {}).get("kind") == "coordinator" else "caller"
    text = truncate(without_reminders(str(attachment["prompt"])), 300)
    print(f"{C['yel']}{C['b']}✉ received from {sender}:{C['r']} {C['yel']}{text}{C['r']}")
    return True


def _render_text(role, text, first):
    text = without_reminders(text)
    if not text:
        return
    if role == "user":
        label = "task" if first else "user"
        print(f"{C['mag']}{C['b']}▶ {label}{C['r']} {C['dim']}{task_preview(text, first)}{C['r']}\n")
    else:
        print(markdown(text) + "\n")


def _render_block(role, block, first):
    kind = block.get("type")
    if kind == "text":
        _render_text(role, block.get("text"), first)
    elif kind == "thinking" and block.get("thinking", "").strip():
        thought = truncate(block["thinking"].strip().replace("\n", " "), 240)
        print(f"{C['dim']}{C['it']}✻ {thought}{C['r']}")
    elif kind == "tool_use" and block.get("name") == "SubagentHandback" and isinstance(block.get("input"), dict):
        _render_handback(block["input"])
    elif kind == "tool_use":
        name = block.get("name", "?")
        print(f"{C['cyan']}⏺ {C['b']}{name}{C['r']}{C['cyan']}({tool_summary(block.get('input'))}){C['r']}")
    elif kind == "tool_result":
        _render_tool_result(block)


def _render_handback(tool_input):
    report = str(tool_input.get("message") or "").strip()
    print(f"\n{C['green']}{C['b']}⏎ report to caller{C['r']}")
    if report:
        print(markdown(report, max_lines=REPORT_LINES) + "\n")


def _render_tool_result(block):
    lines = result_text(block.get("content")).strip().splitlines() or [""]
    color = C["red"] if block.get("is_error") else C["dim"]
    for i, line in enumerate(lines[:RESULT_LINES]):
        prefix = "  ⎿ " if i == 0 else "    "
        print(f"{color}{prefix}{truncate(line, 160)}{C['r']}")
    if len(lines) > RESULT_LINES:
        print(paint(f"    … +{len(lines) - RESULT_LINES} lines", "dim"))
