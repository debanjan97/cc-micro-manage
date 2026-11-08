"""Opening an agent's pane.

Runs detached from SubagentStart (the hook itself must return instantly).
"split" layout: the pane joins the column beside its owner, which is the parent
agent's pane for a nested agent, otherwise Claude's pane. "window" layout: all
agent panes tile in a separate "agents" window (a tab, in Ghostty).
"""
import re

from . import claude_files, ghostty, ghostty_layout, layout, messaging, panes, settings, state, tmux
from .viewer import view_command

AGENTS_WINDOW = "agents"


def describe(agent_type, agent_id, meta):
    """(purpose, title): what the agent is for (Claude's own description, e.g.
    "Scout: describe every .go file") and its border title. Nested agents get
    one ↳ per level."""
    purpose = re.sub(r"\s+", " ", str(meta.get("description") or "")).strip()
    depth = int(meta.get("spawnDepth") or 1)
    title = ("↳ " * (depth - 1)) + (purpose or agent_type) + f" · {agent_id[:6]}"
    return purpose, title


class Placement:
    def __init__(self, payload):
        self.agent_id = payload["agent_id"]
        self.agent_type = payload["agent_type"]
        self.session_id = payload.get("session_id") or ""
        self.session_transcript = payload.get("transcript_path") or ""
        self.agent = state.Agent(self.agent_id)
        self.ghostty = settings.terminal() == "ghostty"
        if self.ghostty:
            self.claude_pane = ghostty.terminal_for_tty(payload.get("ccmm_tty") or "")
        else:
            self.claude_pane = settings.claude_pane() or tmux.run("display-message", "-p", "#{pane_id}")
        self.meta = claude_files.read_meta(self.session_transcript, self.agent_id, wait=claude_files.META_WAIT)
        self.purpose, self.title = describe(self.agent_type, self.agent_id, self.meta)

    def run(self):
        if not self.claude_pane:
            return              # Ghostty: Claude's terminal wasn't found
        state.ensure_dir()
        state.prune_stale()
        with state.Lock("panes"):
            state.forget_dead_panes(panes.is_alive)
            if self._already_shown():
                return          # SubagentStart also fires on resume and per teammate message
            self.agent.clear_done()
            pane = self._open_pane()
            self._decorate(pane)
            self._record(pane)

    def _already_shown(self):
        return panes.is_alive(self.agent.pane)

    def _view_command(self):
        return view_command(self.agent_id, self.agent_type, self.session_id, self.session_transcript,
                            settings.linger(), self.claude_pane, self.purpose)

    def _open_pane(self):
        if self.ghostty:
            return self._open_in_ghostty()
        if settings.layout() == "window":
            return self._open_in_agents_window()
        return self._open_in_column()

    def _open_in_ghostty(self):
        if settings.layout() == "window":
            return ghostty_layout.AgentsTab(self.claude_pane).add(self._view_command(), self.title)
        pane = ghostty_layout.Column(self._owner_pane()).add(self._view_command(), self.title)
        try:
            ghostty_layout.even_out(self.claude_pane, new_pane=pane)
        except ghostty.GhosttyError:
            pass
        return pane

    def _open_in_column(self):
        owner = self._owner_pane()
        pane = layout.Column(owner).add(self._view_command(), owner_is_claude=owner == self.claude_pane)
        if owner == self.claude_pane:
            try:
                layout.relayout(self.claude_pane)
            except tmux.TmuxError:
                pass
        return pane

    def _owner_pane(self):
        """The parent agent's pane for a nested agent (if it's still open), else Claude's."""
        parent_id = self.meta.get("parentAgentId")
        if parent_id:
            parent_pane = state.Agent(parent_id).pane
            if panes.is_alive(parent_pane):
                return parent_pane
        return self.claude_pane

    def _open_in_agents_window(self):
        window_file = state.path("win-" + state.safe_key(self.claude_pane))
        window = state.read(window_file)
        if window and tmux.is_alive(window):
            pane = tmux.run("split-window", "-d", "-t", window, "-P", "-F", "#{pane_id}", self._view_command())
            tmux.run("select-layout", "-t", window, "tiled")
            return pane
        session = tmux.show(self.claude_pane, "#{session_id}")
        pane = tmux.run("new-window", "-d", "-t", session + ":", "-n", AGENTS_WINDOW,
                        "-P", "-F", "#{pane_id}", self._view_command())
        state.write(window_file, tmux.window_of(pane))
        return pane

    def _decorate(self, pane):
        if self.ghostty:
            return              # the title was set with the split; splits have no borders
        try:
            layout.label_agent_pane(pane, self.title)
            layout.borders_on(pane)
        except tmux.TmuxError:
            pass

    def _record(self, pane):
        self.agent.pane = pane
        if not settings.relay_enabled():
            self.agent.forbid_relay()     # decided here, in Claude's environment
        elif self.ghostty:
            self.agent.forbid_relay(messaging.RELAY_NEEDS_TMUX)   # Ghostty can't show us Claude's input box
        if self.meta.get("requestShape"):
            self.agent.shape = self.meta["requestShape"]      # last: the pane waits for it
        if self.ghostty:
            return
        # the pane says which agent it shows: tmux reuses pane ids across restarts
        tmux.set_pane_option(pane, messaging.AGENT_OPTION, self.agent_id)
        try:
            messaging.bind_shortcut()
        except tmux.TmuxError:
            pass
