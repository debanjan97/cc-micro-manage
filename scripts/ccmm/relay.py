"""Messages for background subagents, relayed through Claude.

Background subagents ignore SubagentStop blocks, but Claude itself can message
them with SendMessage. So the viewer types a one-line request into Claude's own
input box, as you would, but only while that box is empty: never over a draft,
a dialog or copy mode.
"""
import collections
import re
import time

from . import tmux

EMPTY, DRAFT = "empty", "draft"
RETRY_EVERY = 0.5          # seconds between looks at Claude's input box
WAIT_NOTE_AFTER = 2.0      # seconds of waiting before saying so
ECHO_TRIES, ECHO_DELAY = 10, 0.15

_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_LEADING_SGR = re.compile(r"\s*((?:\x1b\[[0-9;]*m\s*)+)")
PROMPT, RULE = "❯", "──"
OPEN_MARK, CLOSE_MARK = "<<<", ">>>"


def strip_ansi(text):
    return _ANSI.sub("", text)


def input_box_state(screen_lines):
    """Read Claude Code's input box from its screen (lines with ANSI codes).
    Returns EMPTY, DRAFT, or None when no input box is showing."""
    for i in range(len(screen_lines) - 1, 0, -1):
        line = screen_lines[i]
        if not strip_ansi(line).startswith(PROMPT) or not strip_ansi(screen_lines[i - 1]).startswith(RULE):
            continue
        after_prompt = line[line.index(PROMPT) + 1:]
        if not strip_ansi(after_prompt).strip() or _is_dim(after_prompt):
            return EMPTY        # nothing typed, or only the dim placeholder / suggestion
        return DRAFT
    return None


def _is_dim(text):
    leading = _LEADING_SGR.match(text)
    return bool(leading) and any("2" in codes.split(";") for codes in _SGR.findall(leading.group(1)))


def read_input_box(pane):
    if tmux.try_run("display-message", "-p", "-t", pane, "#{pane_in_mode}") == "1":
        return None          # copy mode: what's on screen isn't the live UI
    try:
        return input_box_state(tmux.run("capture-pane", "-e", "-p", "-t", pane).split("\n"))
    except tmux.TmuxError:
        return None


def request(agent_id, purpose, text):
    """What gets typed into Claude's input box."""
    who = f"{agent_id} ({purpose})" if purpose else agent_id
    message = " ".join(text.split())
    # Only the text between the markers is the message: without them, models
    # forwarded this whole request and the agent took it as an instruction.
    return (f"[cc-micro-manage] Use SendMessage to send background subagent {who} my message "
            f"between {OPEN_MARK} and {CLOSE_MARK}, exactly as written and with nothing added. "
            f"Then confirm in one line. {OPEN_MARK} {message} {CLOSE_MARK}")


def type_into(pane, text):
    """Type text into Claude's input box, then submit it once it's echoed.
    Returns True if submitted."""
    tmux.run("send-keys", "-t", pane, "-l", text)
    probe = text[:24]
    for _ in range(ECHO_TRIES):
        time.sleep(ECHO_DELAY)
        if probe in strip_ansi(tmux.try_run("capture-pane", "-p", "-t", pane)).replace("\n", ""):
            tmux.run("send-keys", "-t", pane, "Enter")
            return True
    return False


class Relay:
    """Messages waiting for Claude's input box, sent one at a time."""

    def __init__(self, claude_pane, agent_id, purpose):
        self.claude_pane = claude_pane
        self.agent_id = agent_id
        self.purpose = purpose
        self.waiting = collections.deque()
        self._last_try = 0.0
        self._waiting_since = 0.0

    def add(self, text):
        self.waiting.append(text)

    def step(self, now):
        """Try to send the oldest message. Returns an event:
        "sent", "failed", "waiting-draft", "waiting" or None (nothing to do yet)."""
        if not self.waiting or not self.claude_pane or now - self._last_try <= RETRY_EVERY:
            return None
        self._last_try = now
        self._waiting_since = self._waiting_since or now
        box = read_input_box(self.claude_pane)
        if box == EMPTY:
            text = self.waiting.popleft()
            self._waiting_since = 0.0
            sent = type_into(self.claude_pane, request(self.agent_id, self.purpose, text))
            return "sent" if sent else "failed"
        if now - self._waiting_since > WAIT_NOTE_AFTER:
            return "waiting-draft" if box == DRAFT else "waiting"
        return None
