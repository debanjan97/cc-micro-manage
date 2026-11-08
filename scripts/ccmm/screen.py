"""An agent pane's terminal UI: output scrolls at the top, and the bottom three
rows hold a status rule, a message box and the footer.

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

from .render import C

MIN_ROWS = 7
CHROME_ROWS = 3        # status rule, message box, footer
HISTORY = 400          # lines replayed after a resize
DEFAULT_STATUS = "type a message for this agent · Enter to send"
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
        self.footer = [""]              # alternatives, widest first
        self.status = DEFAULT_STATUS
        self.input_enabled = True
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.fd = self._terminal_input()
        if self.fd is None or self._size()[1] < MIN_ROWS:
            return
        self._enter_raw_mode()
        self.active = True
        sys.stdout = self
        signal.signal(signal.SIGWINCH, lambda *_: self._resize())
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

    def _resize(self):
        self.cols, self.rows = self._size()
        # clear (tmux reflow leaves fragments of old lines), then replay the tail
        self.real.write(f"\033[r\033[2J\033[1;{self._last_output_row}r")
        for line in list(self.history)[-self._last_output_row:]:
            self.real.write(f"\033[{self._last_output_row};1H\n{line}")
        self.redraw()

    def redraw(self):
        """Redraw the bottom three rows: status rule, message box, footer."""
        if not self.active:
            return
        self._draw_status_rule()
        self._draw_footer()          # footer before the box, so the cursor ends in the box
        self._draw_message_box()
        self.real.flush()

    def _draw_status_rule(self):
        label = f"─ {self.status} " if self.status else ""
        self._draw_row(self.rows - 2, f"{C['dim']}{(label + '─' * self.cols)[:self.cols]}{C['r']}")

    def _draw_footer(self):
        room = max(self.cols - 5, 0)
        fitting = next((v for v in self.footer if len(v) <= room), self.footer[-1] if self.footer else "")
        text = fitting[:room]
        rule = (f"─ {text} " if text else "") + "─" * self.cols
        self._draw_row(self.rows, f"{C['dim']}{rule[:self.cols]}{C['r']}")

    def _draw_message_box(self):
        if self.finished or not self.input_enabled:
            self._draw_row(self.rows - 1, "")
        else:
            shown = self.typed[-max(self.cols - 5, 1):]
            self._draw_row(self.rows - 1, f"{C['yel']}✉ ›{C['r']} {shown}")

    def _draw_row(self, row, text):
        self.real.write(f"\033[{row};1H\033[2K{text}")

    def _emit(self, line):
        self.history.append(line)
        # to the bottom of the scroll region; the newline scrolls it
        self.real.write(f"\033[{self._last_output_row};1H\n{line}")
        self.redraw()

    def set_status(self, text):
        self.status = text
        self.redraw()

    def set_footer(self, alternatives):
        self.footer = alternatives or [""]

    # ── file-like, so print() works unchanged ──────────────────────────
    def write(self, data):
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
