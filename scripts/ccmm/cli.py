"""Subcommands, called by hooks.json, tmux and the plugin itself.

  start        SubagentStart hook: opens the agent's pane (via `place`)
  stop         SubagentStop hook: delivers your messages or marks the agent done
  post-agent   PostToolUse hook on Agent/Task: tells the caller what you sent
  post-bash    PostToolUse hook on Bash: opens a background shell's pane (via `place-shell`)
  session-end  SessionEnd hook: closes the session's shell panes
  place        detached helper: opens an agent's pane
  place-shell  detached helper: opens a background shell's pane
  view         runs inside an agent pane
  shell-view   runs inside a background shell's pane
  send PANE    the message shortcut's prompt was submitted in PANE
  relayout     re-evens the column after a pane closes
  ghostty-closed TERMINAL CLAUDE
               closes a finished Ghostty pane, then re-evens the rest
"""
import sys

import time

from . import ghostty, ghostty_layout, hooks, layout, messaging, shell_viewer, state, viewer
from .placement import Placement, ShellPlacement

HOOK_COMMANDS = {"start", "stop", "relayout", "place", "send", "post-agent", "ghostty-closed",
                 "post-bash", "session-end", "place-shell"}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    command, rest = (argv[0], argv[1:]) if argv else ("", [])
    try:
        _dispatch(command, rest)
    except Exception as error:
        if command not in HOOK_COMMANDS:
            raise
        print(f"cc_micro_manage: {error}", file=sys.stderr)   # a hook must never break the session
        sys.exit(0)


def _dispatch(command, rest):
    if command == "start":
        hooks.subagent_start(hooks.read_payload())
    elif command == "stop":
        hooks.subagent_stop(hooks.read_payload())
    elif command == "post-agent":
        hooks.post_agent(hooks.read_payload())
    elif command == "post-bash":
        hooks.post_bash(hooks.read_payload())
    elif command == "session-end":
        hooks.session_end(hooks.read_payload())
    elif command == "place":
        Placement(hooks.read_payload()).run()
    elif command == "place-shell":
        ShellPlacement(hooks.read_payload()).run()
    elif command == "view":
        viewer.main(rest)
    elif command == "shell-view":
        shell_viewer.main(rest)
    elif command == "send":
        messaging.send_from_prompt(rest[0])
    elif command == "relayout":
        with state.Lock("panes"):
            layout.relayout(rest[0])
    elif command == "ghostty-closed":
        _ghostty_closed(*rest[:2])
    else:
        print(__doc__)


def _ghostty_closed(terminal, claude_pane):
    time.sleep(0.3)             # let the pane's program exit first
    if terminal:
        ghostty.close(terminal)
    with state.Lock("panes"):
        ghostty_layout.relayout(claude_pane)
