"""The column of agent panes beside Claude, border titles and status dots.

Every pane that agents hang off (Claude's pane, or a parent agent's pane for
nested agents) owns a column: its first agent splits it to the right, later
ones stack below, oldest on top (FIFO). The column is kept in col-<owner pane>.
"""
from . import settings, state, tmux

CLAUDE_SHARE = "55%"                 # Claude's width once the column exists
FIRST_SPLIT = {"claude": "45%", "agent": "50%"}

# Border label: a coloured dot (the agent's state) and its purpose. Other panes
# keep a neutral label. Applied only to windows holding agent panes, and only if
# you haven't turned on pane borders there yourself.
BORDER_FORMAT = (" #{?#{@ccmm_title},#[fg=#{@ccmm_color}]●#[default] #{@ccmm_title},"
                 "#{@ccmm_label}} ")
STATE_COLOR = {"running": "yellow", "finished": "green", "error": "red"}

# pane / window user options
TITLE = "@ccmm_title"
COLOR = "@ccmm_color"
LABEL = "@ccmm_label"
OUR_BORDERS = "@ccmm_borders"


class Column:
    def __init__(self, owner_pane):
        self.owner = owner_pane
        self.file = state.path("col-" + state.safe_key(owner_pane))

    def panes(self):
        """Live panes in the column, oldest first."""
        return [p for p in state.read(self.file).split() if tmux.is_alive(p)]

    def save(self, panes):
        state.write(self.file, "\n".join(panes))

    def add(self, viewer_command, owner_is_claude):
        """Open a pane for an agent at the bottom of the column; returns its id."""
        panes = self.panes()
        if panes:
            pane = tmux.run("split-window", "-d", "-v", "-t", panes[-1], "-P", "-F", "#{pane_id}", viewer_command)
        else:
            width = FIRST_SPLIT["claude" if owner_is_claude else "agent"]
            pane = tmux.run("split-window", "-d", "-h", "-l", width, "-t", self.owner,
                            "-P", "-F", "#{pane_id}", viewer_command)
        self.save(panes + [pane])
        return pane


def relayout(claude_pane):
    """Claude on the left, agent panes stacked evenly on the right; but only when
    the window holds nothing except Claude and its agents, so panes you made
    yourself are never rearranged."""
    column = Column(claude_pane)
    agents = column.panes()
    column.save(agents)
    claude_alive = tmux.is_alive(claude_pane)
    if not agents:
        if claude_alive:
            borders_off_if_idle(tmux.window_of(claude_pane))
        return
    if not claude_alive:
        return
    window = tmux.window_of(claude_pane)
    if tmux.panes_in(window) != [claude_pane] + agents:
        return
    tmux.run("set-window-option", "-t", window, "main-pane-width", CLAUDE_SHARE)
    tmux.run("select-layout", "-t", window, "main-vertical")


def label_agent_pane(pane, title):
    tmux.run("select-pane", "-t", pane, "-T", title)
    tmux.set_pane_option(pane, TITLE, title)
    show_state(pane, "running")


def show_state(pane, agent_state):
    tmux.set_pane_option(pane, COLOR, STATE_COLOR.get(agent_state, "yellow"))


def borders_on(pane):
    if not settings.borders_enabled():
        return
    window = tmux.window_of(pane)
    if tmux.window_option(window, OUR_BORDERS) or _user_shows_borders(window):
        return
    tmux.set_window_option(window, "pane-border-status", "top")
    tmux.set_window_option(window, "pane-border-format", BORDER_FORMAT)
    tmux.set_window_option(window, OUR_BORDERS, "1")
    claude = settings.claude_pane()
    if claude:
        tmux.set_pane_option(claude, LABEL, "claude")


def _user_shows_borders(window):
    current = tmux.window_option(window, "pane-border-status") or tmux.global_option("pane-border-status")
    return current not in ("", "off")


def borders_off_if_idle(window):
    """Restore the window once its last agent pane is gone."""
    if not tmux.window_option(window, OUR_BORDERS):
        return
    try:
        if any(t.strip() for t in tmux.panes_in(window, "#{" + TITLE + "}")):
            return
        panes = tmux.panes_in(window)
    except tmux.TmuxError:
        return
    for option in ("pane-border-status", "pane-border-format", OUR_BORDERS):
        tmux.unset_window_option(window, option)
    for pane in panes:
        tmux.unset_pane_option(pane, LABEL)
