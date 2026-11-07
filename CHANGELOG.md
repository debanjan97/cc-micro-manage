# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
