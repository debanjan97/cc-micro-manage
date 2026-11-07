# Privacy

cc-micro-manage runs entirely on your computer. It has no server, sends nothing over the network, and collects no analytics or telemetry.

## What it reads

- **Subagent transcripts** that Claude Code writes under `~/.claude/projects/`, to show each subagent's work in its tmux pane. These can contain anything in your session, including code and personal data. The plugin only displays them in your terminal.
- **Hook payloads** from Claude Code (agent IDs, agent type, transcript paths) and **tmux state** (pane IDs, pane options).
- **Your settings**, from the plugin's options or `CC_MICRO_MANAGE_*` environment variables.
- **Claude's input box**, read from its tmux pane, only to check that it's empty before relaying a message you typed for a background subagent.

## What it writes

- **Small state files** in `/tmp/cc-micro-manage-<your uid>/`: which pane belongs to which agent, and the messages you type to subagents until they're delivered. They're removed when the agent's pane closes, and anything left over is deleted after a day.
- **A debug log**, only if you start Claude with `CC_MICRO_MANAGE_DEBUG=1`: `/tmp/cc-micro-manage-<uid>/debug.jsonl` holds hook payloads, which can include your prompts. Delete it when you're done.
- **Text into Claude's input box**, only when you message a background subagent and the box is empty: a one-line request asking Claude to forward your message. It becomes part of your Claude conversation like anything else you type.

## What it sends

Nothing leaves your machine because of this plugin. Messages you relay go to Claude through Claude Code, like any other prompt.

## Contact

Questions: [open an issue](https://github.com/debanjan97/cc-micro-manage/issues).
