"""An agent pane's terminal UI, laid out like Claude Code's: output scrolls at
the top, and the bottom four rows hold a rule, the ❯ message box, a rule and
the grey status line.

Uses a terminal scroll region (DECSTBM) so streamed output never touches what
you're typing. Falls back to plain output if stdin isn't a terminal or the
pane is too small.
"""
import codecs
import collections
import os
import select
import signal
import sys
import time

from .text import C, visible_width, wrap

MIN_ROWS = 8
CHROME_ROWS = 4        # rule, message box, rule, footer
HISTORY = 400          # lines replayed after a resize
DEFAULT_STATUS = "Message this subagent (Enter to send)"
ENTER, BACKSPACE, CLEAR_LINE, ESCAPE = "\r\n", "\x7f\x08", "\x15", "\x1b"


class Screen:
    def __init__(self, on_send):
        self.on_send = on_send          # called with each message you submit
        self.real = sys.__stdout__
        self.active = self.finished = self.enter_pressed = False
        self.typed = ""                 # what you're typing
        self.partial = ""               # output line not yet complete
        self.in_escape = False
        self.history = collections.deque(maxlen=HISTORY)
        self.emitted = 0                # output lines so far; render.py numbers lines by it
        self.repaint_floor = 0          # lines before this were re-wrapped by a resize
        self.footer = [""]              # alternatives, widest first
        self.status = DEFAULT_STATUS
        self.input_enabled = True
        self.resize_pending = False
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.fd = self._terminal_input()
        if self.fd is None or self._size()[1] < MIN_ROWS:
            return
        self._enter_raw_mode()
        self.active = True
        sys.stdout = self
        # only note it: redrawing inside the handler can land in the middle of a
        # write to stdout, which Python refuses ("reentrant call") and the pane dies
        signal.signal(signal.SIGWINCH, lambda *_: setattr(self, "resize_pending", True))
        self._resize()

    # ── setup and teardown ─────────────────────────────────────────────
    @staticmethod
    def _terminal_input():
        try:
            return sys.stdin.fileno() if sys.stdin and sys.stdin.isatty() else None
        except (ValueError, OSError):
            return None

    def _enter_raw_mode(self):
        import termios
        self._termios = termios
        self._saved_mode = termios.tcgetattr(self.fd)
        mode = termios.tcgetattr(self.fd)
        mode[3] &= ~(termios.ICANON | termios.ECHO)   # keystrokes one at a time, no echo
        mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
        termios.tcsetattr(self.fd, termios.TCSANOW, mode)

    def close(self):
        if not self.active:
            return
        if self.partial:
            self._emit(self.partial)
            self.partial = ""
        self.real.write(f"\033[r\033[{self.rows};1H\033[2K")
        self.real.flush()
        try:
            self._termios.tcsetattr(self.fd, self._termios.TCSANOW, self._saved_mode)
        except Exception:
            pass
        sys.stdout = self.real
        self.active = False

    # ── drawing ────────────────────────────────────────────────────────
    def _size(self):
        # ask the pane itself: shutil.get_terminal_size() prefers $COLUMNS/$LINES,
        # which can be inherited and wrong
        for fd in (self.real.fileno(), self.fd):
            try:
                size = os.get_terminal_size(fd)
                return size.columns, size.lines
            except (OSError, ValueError, TypeError):
                pass
        return 80, 24

    @property
    def _last_output_row(self):
        return self.rows - CHROME_ROWS

    def _resize_if_pending(self):
        """Apply a resize the SIGWINCH handler noted, now that it's safe to draw."""
        if self.resize_pending and self.active:
            self.resize_pending = False
            self._resize()

    def _resize(self):
        self.cols, self.rows = self._size()
        # clear (tmux reflow leaves fragments of old lines), then replay the tail,
        # re-wrapped so every history line is one row again
        rows = [r for line in self.history for r in self._fit(line)]
        self.history = collections.deque(rows, maxlen=HISTORY)
        self.repaint_floor = self.emitted
        self.real.write(f"\033[r\033[2J\033[1;{self._last_output_row}r")
        for line in list(self.history)[-self._last_output_row:]:
            self.real.write(f"\033[{self._last_output_row};1H\n{line}")
        self.redraw()

    def _fit(self, line):
        return [line] if visible_width(line) <= self.cols else wrap(line, self.cols)

    def redraw(self):
        """Redraw the bottom four rows: rule, message box, rule, footer."""
        if not self.active:
            return
        rule = f"{C['rule']}{'─' * self.cols}{C['r']}"
        self._draw_row(self.rows - 3, rule)
        self._draw_row(self.rows - 1, rule)
        self._draw_footer()
        self._draw_message_box()     # last, so the cursor ends in the box
        self.real.flush()

    def _draw_footer(self):
        room = max(self.cols - 3, 0)
        fitting = next((v for v in self.footer if len(v) <= room), self.footer[-1] if self.footer else "")
        self._draw_row(self.rows, f"  {C['grey']}{fitting[:room]}{C['r']}")

    def _draw_message_box(self):
        room = max(self.cols - 3, 1)
        if self.typed and self.input_enabled and not self.finished:
            self._draw_row(self.rows - 2, f"❯ {self.typed[-room:]}")
            return
        hint = self.status[:room]        # Claude Code shows hints dimmed in the empty box
        self._draw_row(self.rows - 2, f"❯ {C['dim']}{hint}{C['r']}")
        if hint:
            self.real.write(f"\033[{self.rows - 2};3H")     # cursor at the start, like Claude Code

    def _draw_row(self, row, text):
        self.real.write(f"\033[{row};1H\033[2K{text}")

    def _emit(self, line):
        for row in self._fit(line):
            self.history.append(row)
            self.emitted += 1
            # to the bottom of the scroll region; the newline scrolls it
            self.real.write(f"\033[{self._last_output_row};1H\n{row}")
        self.redraw()

    def repaint(self, number, change):
        """Rewrite output line `number` (counted by `emitted`) with change(line),
        if it is still on screen. Lets a tool's ⏺ turn green when it finishes."""
        if not self.active or number < self.repaint_floor or number >= self.emitted:
            return
        back = self.emitted - 1 - number
        if back >= len(self.history):
            return
        index = len(self.history) - 1 - back
        new = change(self.history[index])
        if new == self.history[index]:
            return
        self.history[index] = new
        row = self._last_output_row - back
        if row >= 1:
            self.real.write(f"\033[{row};1H\033[2K{new}")
            self.redraw()

    def set_status(self, text):
        self.status = text
        self.redraw()

    def set_footer(self, alternatives):
        self.footer = alternatives or [""]

    # ── file-like, so print() works unchanged ──────────────────────────
    def write(self, data):
        self._resize_if_pending()
        self.partial += data
        while "\n" in self.partial:
            line, _, self.partial = self.partial.partition("\n")
            self._emit(line)
        return len(data)

    def flush(self):
        self.real.flush()

    # ── keyboard ───────────────────────────────────────────────────────
    def poll(self, timeout):
        """Wait up to `timeout` seconds for keystrokes and handle them."""
        if not self.active:
            time.sleep(timeout)
            return
        self._resize_if_pending()
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return
        try:
            data = os.read(self.fd, 1024)
        except OSError:
            return
        for ch in self._decoder.decode(data):
            self._key(ch)
        self.redraw()

    def _key(self, ch):
        if self.in_escape:                   # swallow escape sequences (arrows etc.)
            if ch.isalpha() or ch == "~":
                self.in_escape = False
        elif ch == ESCAPE:
            self.in_escape = True
        elif not self.input_enabled and not self.finished:
            pass
        elif ch in ENTER:
            self._submit()
        elif ch in BACKSPACE:
            self.typed = self.typed[:-1]
        elif ch == CLEAR_LINE:
            self.typed = ""
        elif ch >= " ":
            self.typed += ch

    def _submit(self):
        if self.finished:
            self.enter_pressed = True
            return
        message, self.typed = self.typed.strip(), ""
        if message:
            self.on_send(message)
