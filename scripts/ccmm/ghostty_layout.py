"""Agent panes as Ghostty splits.

"split" layout: the same column as in tmux. An owner's first agent splits it
to the right, later ones stack below, and Ghostty's equalize_splits evens
everything out (Claude keeps half the width). "window" layout: agent panes tile
in an "agents" tab next to Claude's.

Splits are only evened out while the tab holds nothing but Claude and agent
panes, so splits you made yourself are never resized.
"""
import glob
import os

from . import ghostty, layout, panes, state

AGENTS_TAB = "agents"


class Column(layout.Column):
    def add(self, viewer_command, title=""):
        """Open a pane for an agent at the bottom of the column; returns its id."""
        existing = self.panes()
        if existing:
            pane = ghostty.split(existing[-1], "down", viewer_command, title)
        else:
            pane = ghostty.split(self.owner, "right", viewer_command, title)
        self.save(existing + [pane])
        return pane


def agent_panes():
    """Every pane recorded as showing an agent."""
    return {state.read(f) for f in glob.glob(state.path("*.pane"))} - {""}


def even_out(claude_pane, new_pane=""):
    """Equalize the splits in Claude's tab, if it holds only Claude and agents."""
    tab = ghostty.tab_of(claude_pane)
    if len(tab) > 1 and set(tab) <= agent_panes() | {claude_pane, new_pane}:
        ghostty.equalize(claude_pane)


class AgentsTab:
    """All agent panes tiled in one tab beside Claude's."""

    def __init__(self, claude_pane):
        self.claude_pane = claude_pane
        self.file = state.path("tab-" + state.safe_key(claude_pane))

    def panes(self):
        return [p for p in state.read(self.file).split() if panes.is_alive(p)]

    def add(self, viewer_command, title=""):
        existing = self.panes()
        if existing:
            # alternate directions so the tab fills both ways
            direction = "right" if len(existing) % 2 else "down"
            pane = ghostty.split(existing[-1], direction, viewer_command, title)
            ghostty.equalize(pane)
        else:
            pane = ghostty.new_tab_beside(self.claude_pane, viewer_command, title, tab_title=AGENTS_TAB)
        state.write(self.file, "\n".join(existing + [pane]))
        return pane

    def even_out(self):
        if not os.path.exists(self.file):
            return
        existing = self.panes()
        state.write(self.file, "\n".join(existing))
        if len(existing) > 1:
            ghostty.equalize(existing[0])


def relayout(claude_pane):
    """After an agent pane closes: re-even Claude's column and the agents tab."""
    column = Column(claude_pane)
    column.save(column.panes())
    if ghostty.is_alive(claude_pane):
        even_out(claude_pane)
    AgentsTab(claude_pane).even_out()
