"""cc-micro-manage: give every Claude Code subagent its own live tmux or Ghostty pane.

Modules, from the outside in:

  cli          dispatches the subcommands that hooks.json and tmux call
  hooks        SubagentStart / SubagentStop / PostToolUse entry points
  placement    opens an agent's pane and records it
  layout       the column beside Claude, relayout, border titles, status dots
  viewer       the program running in each agent pane
  screen       that pane's terminal UI: scrolling output plus a message box
  render       transcript records -> text drawn like Claude Code's
  text         Claude Code's colours, visible width, wrapping
  stats        the footer: model, effort, tokens, elapsed time
  messaging    queueing your messages; delivery at SubagentStop
  relay        delivery to background agents through Claude's input box
  state        small per-agent files in the state directory
  claude_files where Claude Code keeps transcripts and meta files
  ghostty_layout  the same column (or an "agents" tab) as Ghostty splits
  ghostty      Ghostty's AppleScript interface (macOS)
  panes        tells tmux pane ids from Ghostty's
  tmux         the tmux command line
  settings     plugin options and environment variables
"""
