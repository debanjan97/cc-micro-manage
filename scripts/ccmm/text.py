"""Terminal text: the colours Claude Code uses, visible width, and wrapping
that keeps colours and a hanging indent across line breaks."""
import os
import re
import sys
import unicodedata


def _fg(n):
    return f"\033[38;5;{n}m"


def _bg(n):
    return f"\033[48;5;{n}m"


def _rgb(r, g, b):
    return f"\033[38;2;{r};{g};{b}m"


def _rgb_bg(r, g, b):
    return f"\033[48;2;{r};{g};{b}m"


_STYLES = {
    "r": "\033[0m", "b": "\033[1m", "dim": "\033[2m", "it": "\033[3m", "ul": "\033[4m", "strike": "\033[9m",
    "fg": "\033[39m", "nobg": "\033[49m",
    # plain ANSI colours, still used by a few callers
    "red": "\033[31m", "green": "\033[32m", "yel": "\033[33m", "cyan": "\033[36m", "mag": "\033[35m",
    "blue": "\033[34m",
}

# Claude Code's palette in a 256-colour terminal (tmux without truecolor), read off its panes
COLORS_256 = {
    "text": _fg(231),        # body text and the ⏺ before Claude's words
    "grey": _fg(246),        # ⎿ results, hints, the status line
    "rule": _fg(244),        # the rules around the input box
    "code": _fg(153),        # `inline code`
    "ok": _fg(114),          # ⏺ of a tool that succeeded
    "err": _fg(203),         # ⏺ and text of a tool that failed
    "warn": _fg(220),
    "claude": _fg(173),      # Claude's orange: ✻ and the welcome box
    "prompt": _bg(237),      # background of your prompts
    "del": _fg(167) + _bg(52), "add": _fg(77) + _bg(22),     # diff lines
}

# the same in a truecolor terminal (Ghostty, or tmux with COLORTERM=truecolor),
# read off Claude Code's panes in Ghostty
COLORS_24BIT = {
    "text": _rgb(255, 255, 255),
    "grey": _rgb(153, 153, 153),
    "rule": _rgb(136, 136, 136),
    "code": _rgb(177, 185, 249),
    "ok": _rgb(78, 186, 101),
    "err": _rgb(255, 107, 128),
    "warn": _rgb(255, 193, 7),
    "claude": _rgb(215, 119, 87),
    "prompt": _rgb_bg(55, 55, 55),
    "del": _rgb(248, 248, 242) + _rgb_bg(61, 1, 0), "add": _rgb(248, 248, 242) + _rgb_bg(2, 40, 0),
}

C = {**_STYLES, **COLORS_256}


def use_colors(depth):
    """Draw with Claude Code's "24bit" or "256" palette, matching how Claude
    itself draws in the same terminal."""
    C.update(COLORS_24BIT if depth == "24bit" else COLORS_256)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SGR = re.compile(r"\x1b\[[0-9;]*m")
_TOKEN = re.compile(r" +|(?:\x1b\[[0-9;]*m|[^ \x1b])+")
_PIECE = re.compile(r"\x1b\[[0-9;]*m|.", re.S)

DEFAULT_WIDTH = 100
width_override = None          # tests pin the width here


def paint(text, *styles):
    return "".join(C[s] for s in styles) + text + C["r"]


def strip_ansi(text):
    return _ANSI.sub("", text)


def char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def visible_width(text):
    return sum(char_width(ch) for ch in strip_ansi(text))


def terminal_width():
    if width_override:
        return width_override
    for stream in (sys.__stdout__, sys.__stdin__):
        try:
            return os.get_terminal_size(stream.fileno()).columns
        except (OSError, ValueError, AttributeError):
            pass
    return DEFAULT_WIDTH


class _Line:
    """One output row being filled; remembers which colours are switched on."""

    def __init__(self, prefix, active):
        self.text = prefix + active
        self.col = self.start = visible_width(prefix)

    def add(self, piece, width_of_piece):
        self.text += piece
        self.col += width_of_piece


def wrap(text, width=None, first="", rest=None):
    """Wrap one line of (possibly coloured) text to `width` columns.

    `first` prefixes the first row and `rest` every later one, so
    wrap(t, 80, "⏺ ", "  ") gives Claude Code's hanging indent. Colours that
    are on at a break carry over to the next row.
    """
    width = max(width or terminal_width(), 10)
    rest = first if rest is None else rest
    rows, active = [], ""
    line = _Line(first, "")
    pending_space = ""

    def break_line():
        nonlocal line
        rows.append(line.text + (C["r"] if active else ""))
        line = _Line(rest, active)

    def track(piece):
        nonlocal active
        for esc in _SGR.findall(piece):
            active = "" if esc in ("\033[0m", "\033[m") else active + esc

    for token in _TOKEN.findall(text):
        if token.startswith(" "):
            pending_space += token
            continue
        w = visible_width(token)
        needed = len(pending_space) + w
        if needed <= width - line.col:
            line.add(pending_space + token, needed)
        elif line.col > line.start and w <= width - visible_width(rest):
            break_line()
            line.add(token, w)
        else:                                   # a word longer than a row: split it
            if pending_space and line.col + len(pending_space) < width:
                line.add(pending_space, len(pending_space))
            for piece in _PIECE.findall(token):
                if piece.startswith("\x1b"):
                    line.add(piece, 0)
                    track(piece)
                    continue
                cw = char_width(piece)
                if line.col + cw > width:
                    break_line()
                line.add(piece, cw)
            pending_space = ""
            continue
        track(token)
        pending_space = ""
    rows.append(line.text)
    return rows


def wrap_block(lines, width=None, first="", rest=None):
    """wrap() for several lines: `first` only before the very first row."""
    rest = first if rest is None else rest
    out = []
    for i, line in enumerate(lines):
        out += wrap(line, width, first if i == 0 else rest, rest)
    return out
