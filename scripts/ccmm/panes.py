"""Pane ids from either terminal. tmux ids start with % (panes) or @ (windows);
Ghostty's are UUIDs. Code that only needs to know whether a pane is still
open uses this and works with both."""
from . import ghostty, tmux


def is_tmux(pane):
    return pane[:1] in ("%", "@")


def is_alive(pane):
    if not pane:
        return False
    return tmux.is_alive(pane) if is_tmux(pane) else ghostty.is_alive(pane)
