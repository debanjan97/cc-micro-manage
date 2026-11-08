# Contributing to cc-micro-manage

Thanks for helping. This is a small project, so there's not much process.

## Reporting bugs

Please [open an issue](https://github.com/debanjan97/cc-micro-manage/issues) and include:

- the output of `claude --version`, `tmux -V` and `python3 --version`, plus your OS
- what you expected to see and what you saw (a screenshot helps)
- if panes misbehave, the hook log: run with `CC_MICRO_MANAGE_DEBUG=1`, then attach `/tmp/cc-micro-manage-$(id -u)/debug.jsonl`. Remove anything private first; it contains your prompts.

## Development setup

```bash
git clone https://github.com/debanjan97/cc-micro-manage
claude plugin marketplace add ./cc-micro-manage
claude plugin install cc-micro-manage@cc-micro-manage
```

A plugin installed from a local marketplace loads in place, so restart `claude` (inside tmux) to pick up your edits.

### How the code is laid out

`scripts/cc_micro_manage.py` is the entry point that `hooks/hooks.json` and tmux call. The code is in the [`scripts/ccmm/`](scripts/ccmm/) package, one module per job:

| Module | Job |
|---|---|
| `cli` | dispatches the subcommands below |
| `hooks` | the SubagentStart, SubagentStop and PostToolUse entry points |
| `placement` | opens an agent's pane and records it |
| `layout` | the column beside Claude, relayout, border titles, status dots |
| `viewer` | the program in each agent pane: follows the transcript, shows messages, closes at the end |
| `screen` | that pane's terminal UI: scrolling output plus a message box and footer |
| `render` | transcript records to coloured text, markdown included |
| `stats` | the footer: model, effort, tokens, elapsed time |
| `messaging` | queueing your messages, and delivery at SubagentStop |
| `relay` | delivery to background agents through Claude's input box |
| `state` | small per-agent files in the state directory |
| `claude_files` | where Claude Code keeps transcripts and meta files |
| `tmux`, `settings`, `program` | the tmux command line, plugin options and env vars, re-running ourselves |

| Subcommand | Called by | Job |
|---|---|---|
| `start` / `place` | `SubagentStart` hook | open the pane (detached so the hook returns instantly) |
| `stop` | `SubagentStop` hook | deliver queued messages or mark the agent done |
| `post-agent` | `PostToolUse` on `Agent`/`Task` | tell the calling agent what the user sent |
| `view` | the pane | tail and render the transcript, message box, relay to Claude's input box |
| `send` | `prefix + a` binding | queue a message typed in tmux's prompt (inbox for foreground agents, relay for background ones) |
| `relayout` | the viewer, on exit | re-even the column |

## Testing your change

### Automated

```bash
python3 -m unittest discover -s tests -v
```

There are two suites. **Unit tests** ([`tests/test_units.py`](tests/test_units.py)) cover the parts with no tmux or terminal: settings precedence, reading Claude's input box, markdown and transcript rendering, the footer, and message queueing. They run in well under a second.

**End-to-end tests** ([`tests/test_e2e.py`](tests/test_e2e.py)) need tmux and Python 3.9+, and take about 30 seconds. No Claude account or network is needed. Each test starts its own tmux server on a private socket, with a stand-in for Claude's pane ([`tests/fake_claude.py`](tests/fake_claude.py)) that draws an input box like Claude Code's. The tests drive the hook script the way Claude Code does, writing subagent transcripts and meta files in Claude Code's layout, and then check the real panes. They cover:

- panes opening beside Claude, titles, rendering, the footer, and status colours
- the FIFO column, nested agents splitting their parent, the `window` layout, and border titles
- cleanup when an agent finishes, including the backstop for background agents
- foreground messages delivered at SubagentStop
- background messages relayed through Claude's input box, including waiting for (and never submitting over) a draft
- settings from plugin options, with environment variables taking precedence

CI runs them on Linux and macOS with Python 3.9 and 3.13 on every push. Please add a test with any behaviour change.

### By hand, with real Claude Code

The tests stand in for Claude Code, so check against the real thing before a release:

1. A real run inside tmux, using the quick-start prompt in the README: panes open, the nested agents split their parent, and everything closes at the end.
2. If you touched messaging, check both delivery paths:
   - **Background (relay):** in interactive `claude`, ask for a background subagent that makes ~15 separate Bash calls that each take a few seconds and print a step number. Type a message into its pane and check that the pane shows `✉ handed to Claude`, then `✉ received from Claude`, and that later steps change.
   - **Foreground (stop hook):** a headless run, where subagents run in the foreground:
     ```bash
     claude -p "Use the Agent tool to spawn ONE general-purpose subagent: 'run sleep 15, then reply with a one-line summary.'"
     ```
     While it sleeps, type a message into its pane and check that it's acted on.

## Guidelines

- **Keep it dependency-free.** Use the standard library only, and target Python 3.9 (macOS's system Python).
- **Keep units small.** One module per job, short functions named for what they do. If a function needs a comment to explain its steps, the steps probably want names.
- **Hooks must never break a session.** Exit 0, return fast, and catch everything.
- **Be defensive about Claude Code internals.** Transcript and meta formats are undocumented, so fail soft.
- **Respect the user's tmux.** Never rearrange panes you didn't create or overwrite bindings that aren't yours.
- Add a line to `CHANGELOG.md` under *Unreleased*.
