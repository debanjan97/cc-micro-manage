# cc-micro-manage

Micro-manage your Claude Code subagents. Every subagent gets its own live tmux pane: its task, its text and thinking, every tool call and result, streamed as it works. The panes close themselves when the subagents finish.

```
┌─────────────┬───────────┬──────────┐
│             │           │ ↳ child1 │
│             │  agent A  ├──────────┤
│   Claude    │           │ ↳ child2 │
│             ├───────────┴──────────┤
│             │       agent B        │
└─────────────┴──────────────────────┘
```

- **FIFO column:** subagents stack on the right, oldest on top. When one finishes, its pane closes and the rest move up.
- **Nested subagents:** a subagent's own subagents split *its* pane, using the same rule at every level.
- **Zero-touch:** it works with regular subagents (Agent/Task tool, `.claude/agents/*.md`). Agent teams aren't required, and neither are any changes to your agents.
- **Message a subagent:** every pane has a message box at the bottom. Click into it, type, and press Enter. The subagent gets your message before it finishes.
- **Safe:** panes are read-only views, and the hook returns instantly. Outside tmux it does nothing.

## Install

```
/plugin marketplace add debanjan97/cc-micro-manage
/plugin install cc-micro-manage@cc-micro-manage
```

Then run `claude` **inside tmux**. Requirements: tmux ≥ 3.0 and `python3` on your PATH. There are no other dependencies.

## Try it

From any repo, paste:

```
Demo of parallel and nested subagents. In ONE message, launch these 4 general-purpose subagents in parallel with the Agent tool:
1. "Scout": describe this repo's structure in 3 bullets, then run `sleep 5`.
2. "Lead": in ONE message, launch 2 general-purpose subagents in parallel:
   "Lead/Docs" summarizes the README in 2 sentences, then runs `sleep 8`;
   "Lead/Tests" finds how tests are organized (don't run them), then runs `sleep 12`.
   Combine their answers.
3. "Hunter": grep for TODO/FIXME/HACK and list the 5 most interesting, then run `sleep 15`.
4. "Counter": count lines of code per language, then run `sleep 20`.
When all are done, give me a 5-line summary.
```

## Messaging a subagent

Each pane has a message box on its bottom line, and output scrolls above it. Focus the pane (click it with tmux mouse mode on, or use `prefix + arrow`), type, and press **Enter**. Backspace and `Ctrl-U` work as usual.

Or, from anywhere in the pane, press **`prefix + a`** to type into tmux's own prompt.

- **Queued:** the pane shows `✉ you: …`. Your message is **delivered when the subagent tries to finish**, and it keeps working to address it. You can send more than one message.
- **Delivered:** the pane shows `✉ delivered`. The subagent mentions what it did about your message in its final reply.
- **Claude is told:** the main Claude learns what you sent, so it doesn't mistake the extra work for the subagent going off-brief.

**Why at the end, not instantly?** Text that appears mid-task claiming "the user says…" looks exactly like a prompt injection, and Claude rightly ignores it. We tested that. Blocking a subagent from finishing, with a reason, is the documented way to steer an agent, so the message gets followed. Use this for "also check X" or "you missed Y". To stop a subagent that's going the wrong way, interrupt it from the main Claude pane.

Your text goes through a tmux pane option, never through a shell, so quotes, `$(…)` and `#{…}` are all safe.

## Settings

Put these in the `env` block of `~/.claude/settings.json`:

| Variable | Default | Meaning |
|---|---|---|
| `CC_MICRO_MANAGE_LAYOUT` | `split` | `split`: column beside Claude, as shown above. `window`: tile all subagent panes in a separate "agents" window. |
| `CC_MICRO_MANAGE_LINGER` | `10` | Seconds a pane stays open after its subagent finishes. `-1` keeps it until you press Enter. |
| `CC_MICRO_MANAGE_KEY` | `a` | Key after your tmux prefix that opens the message prompt. `none` disables it. An existing binding that isn't ours is never overwritten. |
| `CC_MICRO_MANAGE_SKIP` | none | Comma-separated agent types to ignore, e.g. `Explore` |
| `CC_MICRO_MANAGE_OFF` | none | `1` disables the plugin without uninstalling it |
| `CC_MICRO_MANAGE_DEBUG` | none | `1` logs hook payloads to `/tmp/cc-micro-manage-<uid>/debug.jsonl` |

## How it works

1. **`SubagentStart` hook:** hands the event to a detached helper and returns immediately.
2. **The helper:** reads `<session>/subagents/agent-<id>.meta.json` to find the parent agent, then opens a pane next to Claude, or next to the parent agent for a nested subagent.
3. **The pane:** tails `<session>/subagents/agent-<id>.jsonl` and pretty-prints it.
4. **`SubagentStop` hook:** if you sent messages the subagent hasn't seen, it blocks the finish and hands them over. Otherwise it signals the pane to finish. The pane lingers for a moment, closes, and the column re-evens.
5. **`PostToolUse` hook on Agent/Task:** tells the calling agent what you sent its subagent.
6. **Safety net:** if the Claude pane disappears, its subagent panes close. If a transcript is idle for 30 minutes, its pane closes too.

If you've split Claude's window yourself, the plugin never rearranges your panes.

## Caveats

The hook events are documented. The transcript files and `meta.json` (parent links) are **undocumented Claude Code internals**, and a future release could change them. Parsing is defensive, and the worst case is a pane that says it couldn't find the transcript. Tested with Claude Code 2.1.319 and tmux 3.5a on macOS.

## Uninstall

```
/plugin uninstall cc-micro-manage@cc-micro-manage
```

## License

MIT
