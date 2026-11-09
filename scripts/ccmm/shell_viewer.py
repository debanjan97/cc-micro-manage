"""The program in a background shell's pane: streams the command's output as
Claude Code writes it, and closes when the command ends.

The pane reads like a Claude Code tool call, `⏺ Bash(command)` with the output
hanging off `⎿`. Its dot turns green or red when the command exits.
"""
import os
import re
import sys
import time

from . import claude_files, layout, panes, program, render, state
from .render import DOT, RESULT, UNDER, emit, paint
from .screen import Screen
from .text import C, use_colors, wrap
from .viewer import CHECK_EVERY, CLAUDE_GONE_LINGER, PaneProgram, _linger_seconds, parse_args

FIND_OUTPUT_FOR = 15            # seconds
READ_ONLY = "read-only · background shell (Claude can stop it)"
_EXITED = re.compile(r"^\[exited with code (-?\d+)\]$")
_KILLED = "[killed]"
_SGR = re.compile(r"(\x1b\[[0-9;]*m)")
# everything but colours: cursor moves, screen clears, titles, other escapes
_CONTROL = re.compile(r"\x1b\[[0-9;?]*[A-Za-ln-z@`]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[^\[\]]|[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f]")


def shell_view_command(task_id, output, session_id, linger, claude_pane, colors="256"):
    """The command a background shell's pane runs (see viewer.view_command). The
    shell's own command line stays out of it, in a state file: Claude often stops
    a server with `pkill -f <its command>`, which would kill this pane too."""
    return program.shell_command(
        "shell-view", "--task-id", task_id, "--output", output or "", "--session", session_id or "",
        "--linger", linger, "--parent-pane", claude_pane, "--colors", colors)


def clean(line):
    """One line of command output, safe to print: colours kept, any other
    escape dropped, and only what's left after the last carriage return
    (progress bars redraw themselves with \\r)."""
    line = _CONTROL.sub("", line.rsplit("\r", 1)[-1])
    return _expand_tabs(line) if "\t" in line else line


def _expand_tabs(line):
    """Tabs to spaces, counting only visible columns (colour codes take none)."""
    out, column = [], 0
    for piece in _SGR.split(line):
        if _SGR.fullmatch(piece):
            out.append(piece)
            continue
        for ch in piece:
            if ch == "\t":
                out.append(" " * (8 - column % 8))
                column += 8 - column % 8
            else:
                out.append(ch)
                column += 1
    return "".join(out)


def ending(line):
    """("exited", code) or ("killed", None) for Claude Code's closing line, else None."""
    match = _EXITED.match(line.strip())
    if match:
        return "exited", int(match.group(1))
    if line.strip() == _KILLED:
        return "killed", None
    return None


def elapsed(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class ShellViewer(PaneProgram):
    def __init__(self, args):
        self.task_id = args.get("task_id", "")
        self.output = args.get("output") or ""
        self.session_id = args.get("session") or ""
        shell = state.Agent(state.shell_key(self.task_id)).shell
        self.command = shell.get("command") or ""
        self.purpose = shell.get("title") or self.command or "background shell"
        self.claude_pane = args.get("parent_pane") or ""
        self.linger = _linger_seconds(args.get("linger"))
        self.my_pane = os.environ.get("TMUX_PANE")
        self.started = time.time()
        self.lines = 0
        self.blank_held = False         # a blank line, printed only if more output follows
        self.dot_line = None
        self.result = None              # ("exited", code) / ("killed", None) / ("gone", reason)
        self.screen = Screen(lambda text: None)
        self.screen.input_enabled = False
        self.screen.set_status(READ_ONLY)

    # ── the whole life of the pane ─────────────────────────────────────
    def run(self):
        try:
            render.header(self.purpose, f"background shell · {self.task_id}")
            render.blank()
            label, arg = render.tool_title("Bash", {"command": self.command})
            self.dot_line = emit(wrap(f"{C['b']}{label}{C['r']}({arg})", None, paint(DOT, "grey") + " ",
                                      render.INDENT))
            sys.stdout.flush()
            output = self._find_output()
            if output:
                self._follow(output)
            self._finish()
            self._linger()
        finally:
            self.screen.close()
            state.Agent(state.shell_key(self.task_id)).remove_all()
        self._request_relayout()

    def _find_output(self):
        started = time.time()
        while True:
            if self.output and os.path.exists(self.output):
                return self.output
            found = claude_files.shell_output_candidates(self.session_id, self.task_id)
            if found:
                return found[0]
            if time.time() - started > FIND_OUTPUT_FOR or self._gone():
                render.note("Couldn't find this shell's output.", "err")
                self.result = self.result or ("gone", "")
                return None
            self.screen.poll(0.25)

    # ── following the output ───────────────────────────────────────────
    def _follow(self, output):
        partial, last_check, last_footer = "", 0.0, 0.0
        with open(output, errors="replace") as f:
            while True:
                chunk = f.read(65536)
                if chunk:
                    partial += chunk
                    *complete, partial = partial.split("\n")
                    for line in complete:
                        if self._on_line(line):
                            return
                    continue
                now = time.time()
                if now - last_footer >= 1:
                    last_footer = now
                    self._refresh_footer()
                if now - last_check >= CHECK_EVERY:
                    last_check = now
                    if self._gone():
                        if partial:
                            self._on_line(partial)
                        return
                sys.stdout.flush()
                self.screen.poll(0.15)

    def _on_line(self, line):
        """Show one line of output; True once the command has ended."""
        end = ending(line)
        if end:
            self.result = end
            return True
        if not line.strip():
            self.blank_held = True      # Claude Code puts a blank line before its closing line
            return False
        if self.blank_held:
            self._show("")
            self.blank_held = False
        self._show(clean(line))
        return False

    def _show(self, text):
        prefix = paint(RESULT, "grey") if self.lines == 0 else UNDER
        emit(wrap(f"{C['fg']}{text}{C['r']}", None, prefix, UNDER))
        self.lines += 1

    def _gone(self):
        """The command can end without a closing line when its session does."""
        if state.session_ended(self.session_id):
            self.result = ("gone", "Claude session ended")
            return True
        if self.claude_pane and not panes.is_alive(self.claude_pane):
            self.result = ("gone", "Claude pane closed")
            self.linger = min(self.linger, CLAUDE_GONE_LINGER) if self.linger >= 0 else CLAUDE_GONE_LINGER
            return True
        return False

    def _refresh_footer(self):
        ran = elapsed(time.time() - self.started)
        lines = f"{self.lines} line{'' if self.lines == 1 else 's'}"
        self.screen.set_footer([f"background shell · running · {ran} · {lines}", f"running · {ran}", ran])
        self.screen.redraw()

    # ── the end ────────────────────────────────────────────────────────
    def _finish(self):
        if self.lines == 0 and self.result and self.result[0] == "exited":
            render.note("(No output)")
        kind, detail = self.result or ("gone", "")
        failed = kind == "exited" and detail != 0
        ok = kind == "exited" and detail == 0
        repaint = getattr(sys.stdout, "repaint", None)
        if repaint and self.dot_line is not None:
            dot = paint(DOT, "ok" if ok else "err" if failed else "grey")
            repaint(self.dot_line, lambda row: row.replace(paint(DOT, "grey"), dot, 1))
        ran = elapsed(time.time() - self.started)
        if kind == "exited":
            what = f"exited with code {detail}"
        elif kind == "killed":
            what = "stopped"
        else:
            what = detail or "ended"
        if failed:
            render.note(f"Exit code {detail}", "err")
        render.done_line(f"{what} · ran for {ran}")
        if self.my_pane:
            layout.show_state(self.my_pane, "error" if failed else "finished")
        self.screen.set_footer([f"background shell · {what} · {ran}", what])
        sys.stdout.flush()


def main(argv):
    args = parse_args(argv)
    use_colors(args.get("colors"))
    ShellViewer(args).run()
