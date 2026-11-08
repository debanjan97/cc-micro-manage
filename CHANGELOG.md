# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- **Refactored into a package.** The 1,300-line script is now `scripts/ccmm/`, one module per job, with small named functions (the longest is ~30 lines, down from ~190). `scripts/cc_micro_manage.py` stays as the entry point, so hooks and existing installs are unaffected. No behaviour changes.
- `CC_MICRO_MANAGE_KEY` is now `CC_MICRO_MANAGE_SHORTCUT`, and the `message_key` setting is now `message_shortcut`: the plugin directory's scanner reads names ending in `KEY` as credentials.
- The demo GIF moved to the `assets` branch, so no file in the plugin exceeds the plugin directory's 5 MiB limit.

### Fixed
- Relayed messages are marked off in the request typed into Claude, so Claude forwards only your words. Before, it could forward the whole request, and the agent then read it as an instruction to itself.

### Added
- **Unit tests** (`tests/test_units.py`) for settings, reading Claude's input box, rendering, the footer and message queueing.
- **End-to-end tests** (`tests/`): the real plugin in a private tmux server, with a stand-in for Claude's pane, covering layout, rendering, cleanup, both messaging paths and settings. CI runs them on Linux and macOS, Python 3.9 and 3.13.
- `CC_MICRO_MANAGE_STATE_DIR` sets where the plugin keeps its state files (the tests use it to stay out of your real sessions).

## [0.6.0] - 2026-11-07

### Added
- **Settings in Claude Code's `/config` panel.** Layout, linger time, message shortcut, relay, border titles and ignored agent types are now plugin options (`userConfig`), so you no longer have to edit `env` in `settings.json`. They can also be set from the shell with `claude plugin install --config` or `claude plugin configure --values-stdin`. The `CC_MICRO_MANAGE_*` environment variables still work and take precedence.
- Plugin icon and directory listing metadata (homepage, repository, documentation and support links), ready for Anthropic's community plugin directory.
- The debug log records which settings the hooks received.

### Changed
- The footer is drawn as a rule, like the status line above the message box, and background panes no longer show a "relays it via Claude" hint.

## [0.5.0] - 2026-11-07

### Added
- **Message background subagents.** Typing into a background agent's pane (or `prefix + a`) now relays the message through Claude: once Claude's input box is empty, the plugin types a one-line "pass this on with SendMessage" request into it, and the agent gets it at its next tool call. Works for nested agents too. It never types over a draft, a dialog or copy mode; it waits instead. `CC_MICRO_MANAGE_RELAY=0` turns it off.
- Panes show `✉ received from Claude: …` when a message from Claude reaches the agent.

### Fixed
- Stale state files older than a day are pruned from `/tmp/cc-micro-manage-<uid>`.

## [0.4.0] - 2026-11-07

### Added
- **Pane titles show each agent's purpose**, taken from Claude Code's own description of the agent (for example `● Scout: describe every .go file`), instead of `general-purpose · <id>`.
- **Status colours.** The dot in each pane's border is yellow while the agent runs, red after a failed tool call, and green when it finishes.
- Border titles are switched on automatically, but only in windows with agent panes and only if you haven't enabled borders yourself. They're restored when the last agent finishes. Set `CC_MICRO_MANAGE_BORDERS=0` to opt out.

### Changed
- The task text at the top of each pane is cut to about two lines, so the agent's work appears sooner.
- The footer adapts to the pane's width: full, then `Haiku 5.5 · 59.3k ctx · 4.0k out · 8s`, then just context and time.

## [0.3.3] - 2026-11-07

### Added
- A footer under each pane's message box, like Claude Code's own: model, effort level, current context size, total output tokens and elapsed time (e.g. `Haiku 5.5 · medium effort · 51.0k context · 1.6k out · 9s`). The timer stops when the agent finishes.

## [0.3.2] - 2026-11-07

### Added
- Panes render the agent's markdown: headings, **bold**, *italics*, `code`, lists, code blocks, quotes, rules and links, instead of plain grey text.
- A background agent's final report (`SubagentHandback`) is rendered as markdown under "⏎ report to caller", instead of a one-line JSON summary.

## [0.3.1] - 2026-11-07


### Fixed
- Panes for **background** subagents no longer hang after the agent finishes. The viewer now also watches Claude Code's `finished` flag in the agent's meta file.
- The stop hook no longer tries to block background subagents. They have already handed back their report, so the block was ignored.

### Changed
- For background subagents, the message box is now read-only and the pane explains why, instead of accepting messages that can't be delivered.
- Rewrote the README with a demo GIF, a troubleshooting section and a limitations section. Added `CONTRIBUTING.md`, this changelog, and a reproducible demo (`demo/`).

## [0.3.0] - 2026-11-07

### Added
- A message box on the bottom line of each pane: type and press Enter to message a subagent.
- Panes redraw cleanly when resized.


### Fixed
- Pane width now comes from the terminal itself, not an inherited `$COLUMNS`.

## [0.2.0] - 2026-11-07

### Added
- Messaging via `prefix + a`: queued messages are delivered when the subagent tries to finish, and the calling agent is told what was sent.
- Nested subagents split their parent's pane, using parent links from `meta.json`.


### Fixed
- tmux pane-ID reuse could send a message to the wrong agent. Each pane is now tagged with its agent's ID.
- Claude Code's internal `<system-reminder>` and attachment records are no longer shown in panes.

## [0.1.0] - 2026-11-07

### Added
- First release as a Claude Code plugin: a live tmux pane per subagent in a FIFO column, with automatic cleanup.
