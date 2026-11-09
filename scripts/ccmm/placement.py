"""Opening a pane for an agent or a background shell.

Runs detached from the hooks (a hook itself must return instantly).
"split" layout: the pane joins the column beside its owner, which is the
parent agent's pane for a nested agent (or a shell an agent started),
otherwise Claude's pane. "window" layout: all panes tile in a separate
"agents" window (a tab, in Ghostty).
"""
import re

from . import claude_files, ghostty, ghostty_layout, layout, messaging, panes, settings, state, tmux
from .shell_viewer import shell_view_command
from .viewer import view_command

AGENTS_WINDOW = "agents"
SHELL_OUTPUT_WAIT = 3.0       # seconds to wait for Claude Code to create the output file


def describe(agent_type, agent_id, meta):
    """(purpose, title): what the agent is for (Claude's own description, e.g.
    "Scout: describe every .go file") and its border title. Nested agents get
    one ↳ per level."""
    purpose = _one_line(meta.get("description"))
    depth = int(meta.get("spawnDepth") or 1)
    title = ("↳ " * (depth - 1)) + (purpose or agent_type) + f" · {agent_id[:6]}"
    return purpose, title


def describe_shell(description, command, task_id, nested):
    """(purpose, title) of a background shell: Claude's description of the
    command, else the command itself."""
    purpose = _one_line(description) or _one_line(command)
    title = ("↳ " if nested else "") + "$ " + purpose[:60] + f" · {task_id[:6]}"
    return purpose, title


def _one_line(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def find_claude_pane(payload):
    if settings.terminal() == "ghostty":
        return ghostty.terminal_for_tty(payload.get("ccmm_tty") or "")
    return settings.claude_pane() or tmux.run("display-message", "-p", "#{pane_id}")


class _Placement:
    """What agent and shell panes share: where a pane goes and how it opens.
    Subclasses set self.state (state.Agent), self.title and self.claude_pane,
    and provide _view_command and _owner_pane."""
    ghostty = False

    def run(self):
        if not self.claude_pane:
            return              # Ghostty: Claude's terminal wasn't found
        state.ensure_dir()
        state.prune_stale()
        with state.Lock("panes"):
            state.forget_dead_panes(panes.is_alive)
            if panes.is_alive(self.state.pane):
                return          # SubagentStart also fires on resume and per teammate message
            self.state.clear_done()
            self._prepare()
            pane = self._open_pane()
            self._decorate(pane)
            self._record(pane)

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

    def _pane_of_agent(self, agent_id):
        """That agent's pane if it's still open, else Claude's."""
        if agent_id:
            pane = state.Agent(agent_id).pane
            if panes.is_alive(pane):
                return pane
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

    def _prepare(self):
        """Anything the pane needs from state before it starts."""

    def _record(self, pane):
        self.state.pane = pane


class Placement(_Placement):
    """A subagent's pane (SubagentStart)."""

    def __init__(self, payload):
        self.agent_id = payload["agent_id"]
        self.agent_type = payload["agent_type"]
        self.session_id = payload.get("session_id") or ""
        self.session_transcript = payload.get("transcript_path") or ""
        self.state = self.agent = state.Agent(self.agent_id)
        self.ghostty = settings.terminal() == "ghostty"
        self.claude_pane = find_claude_pane(payload)
        self.meta = claude_files.read_meta(self.session_transcript, self.agent_id, wait=claude_files.META_WAIT)
        self.purpose, self.title = describe(self.agent_type, self.agent_id, self.meta)

    def _view_command(self):
        return view_command(self.agent_id, self.agent_type, self.session_id, self.session_transcript,
                            settings.linger(), self.claude_pane, self.purpose, settings.color_depth())

    def _owner_pane(self):
        """The parent agent's pane for a nested agent (if it's still open), else Claude's."""
        return self._pane_of_agent(self.meta.get("parentAgentId"))

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


class ShellPlacement(_Placement):
    """A background shell's pane (PostToolUse on Bash with run_in_background)."""

    def __init__(self, payload):
        tool_input = payload.get("tool_input") or {}
        self.task_id = str(payload["tool_response"]["backgroundTaskId"])
        self.session_id = payload.get("session_id") or ""
        self.command = str(tool_input.get("command") or "")
        self.parent_agent = payload.get("agent_id") or ""     # set when a subagent started it
        self.state = state.Agent(state.shell_key(self.task_id))
        self.ghostty = settings.terminal() == "ghostty"
        self.claude_pane = find_claude_pane(payload)
        self.output = claude_files.find_shell_output(self.session_id, self.task_id, wait=SHELL_OUTPUT_WAIT)
        self.purpose, self.title = describe_shell(tool_input.get("description"), self.command, self.task_id,
                                                  nested=bool(self.parent_agent))

    def _prepare(self):
        self.state.shell = {"command": self.command, "title": self.purpose}

    def _view_command(self):
        return shell_view_command(self.task_id, self.output, self.session_id, settings.linger(), self.claude_pane,
                                  settings.color_depth())

    def _owner_pane(self):
        return self._pane_of_agent(self.parent_agent)
