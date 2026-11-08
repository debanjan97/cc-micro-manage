"""Ghostty's AppleScript interface (macOS, Ghostty 1.3 or newer).

Terminals (split panes) have stable UUIDs, so those are the pane ids here.
Ghostty can't show us a terminal's screen, so messages for background agents
can't be relayed through Claude's input box; everything else works as in tmux.
"""
import os
import subprocess
import time

ALIVE_CACHE = 0.5          # seconds a list of live terminals stays good


class GhosttyError(RuntimeError):
    pass


def run(script):
    """Run AppleScript inside `tell application "Ghostty"`; returns its result as text."""
    result = subprocess.run(["osascript", "-e", 'tell application "Ghostty"\n' + script + "\nend tell"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise GhosttyError(result.stderr.strip())
    return result.stdout.strip()


def quote(text):
    """An AppleScript string literal."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _lines(script):
    """Run a script that returns a list, as one item per line."""
    out = run("set AppleScript's text item delimiters to linefeed\n" + script)
    return [line for line in out.split("\n") if line]


# ── finding terminals ──────────────────────────────────────────────────

def controlling_tty(pid=None):
    """The tty of this process or its nearest ancestor that has one, e.g.
    /dev/ttys004. Hooks have no tty of their own on stdin, but Claude does."""
    pid = pid or os.getpid()
    for _ in range(8):
        out = subprocess.run(["ps", "-o", "tty=,ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.split()
        if len(out) < 2:
            return ""
        tty, parent = out[0], out[1]
        if tty not in ("??", "?", "-"):
            return tty if tty.startswith("/dev/") else "/dev/" + tty
        if parent in ("0", "1"):
            return ""
        pid = int(parent)
    return ""


def terminal_for_tty(tty):
    if not tty:
        return ""
    return run(f"""repeat with t in terminals
  if tty of t is {quote(tty)} then return id of t
end repeat
return \"\"""")


_alive = (0.0, frozenset())


def terminal_ids():
    """Every live terminal's id (briefly cached: placement asks about many)."""
    global _alive
    if time.time() - _alive[0] > ALIVE_CACHE:
        _alive = (time.time(), frozenset(_lines("return (id of terminals) as text")))
    return _alive[1]


def _changed():
    global _alive
    _alive = (0.0, frozenset())


def is_alive(terminal):
    try:
        return bool(terminal) and terminal in terminal_ids()
    except GhosttyError:
        return False


def tab_of(terminal):
    """Ids of every terminal in the same tab as `terminal`, in Ghostty's order."""
    return _lines(f"""repeat with w in windows
  repeat with tb in tabs of w
    set ids to id of terminals of tb
    if ids contains {quote(terminal)} then return ids as text
  end repeat
end repeat
return \"\"""")


# ── opening, closing, arranging ────────────────────────────────────────

def _configuration(command):
    return f"""set cfg to new surface configuration
set command of cfg to {quote(command)}
"""


def split(terminal, direction, command, title=""):
    """Split `terminal` (right or down) to run `command`; returns the new id.
    Ghostty keeps focus where it was."""
    _changed()
    return run(_configuration(command) + f"""set t to split terminal id {quote(terminal)} direction {direction} with configuration cfg
{_title("t", title)}
return id of t""")


def new_tab_beside(terminal, command, title="", tab_title=""):
    """Open a tab running `command` in the window holding `terminal`, without
    leaving that terminal's tab. Returns the new terminal's id."""
    _changed()
    return run(_configuration(command) + f"""repeat with w in windows
  repeat with tb in tabs of w
    if (id of terminals of tb) contains {quote(terminal)} then
      set newTab to new tab in w with configuration cfg
      set t to terminal 1 of newTab
      {_title("t", title)}
      {_title("newTab", tab_title)}
      select tab tb
      return id of t
    end if
  end repeat
end repeat
error "terminal not found\"""")


def _title(var, title):
    return f"set name override of {var} to {quote(title)}" if title else ""


def equalize(terminal):
    run(f'perform action "equalize_splits" on terminal id {quote(terminal)}')


def close(terminal):
    _changed()
    run(f"""if exists terminal id {quote(terminal)} then close terminal id {quote(terminal)}""")
