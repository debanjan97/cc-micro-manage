#!/usr/bin/env python3
"""cc-micro-manage: give every Claude Code subagent and background shell its own live tmux or Ghostty pane.

The code lives in the ccmm package next to this file; see ccmm/__init__.py for
a map. This entry point is what hooks/hooks.json, tmux and the plugin call.

Settings: /config (the plugin's options), or these environment variables,
which win over the options (settings.json "env" block or your shell):
  CC_MICRO_MANAGE_LAYOUT     split (default) | window (tile in a separate "agents" window)
  CC_MICRO_MANAGE_LINGER     seconds a pane stays after its subagent finishes (default 10; -1 = until Enter)
  CC_MICRO_MANAGE_SHORTCUT   tmux key (after your prefix) to message the focused subagent (default a)
  CC_MICRO_MANAGE_RELAY      0 = don't relay messages for background subagents through Claude
  CC_MICRO_MANAGE_BORDERS    0 = don't turn on pane-border titles in agent windows
  CC_MICRO_MANAGE_SHELLS     0 = no panes for background shells
  CC_MICRO_MANAGE_SKIP       comma-separated agent types to ignore
  CC_MICRO_MANAGE_OFF        1 = disable
  CC_MICRO_MANAGE_DEBUG      1 = log hook payloads to <state dir>/debug.jsonl
  CC_MICRO_MANAGE_STATE_DIR  where state files live (default /tmp/cc-micro-manage-<uid>)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ccmm.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
