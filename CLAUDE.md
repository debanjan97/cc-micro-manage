# CLAUDE.md

cc-micro-manage is a Claude Code plugin that gives every subagent a live pane: a tmux pane, or a native Ghostty split on macOS. Each pane shows the subagent's transcript, drawn like Claude Code, with a message box for steering it. Read [CONTRIBUTING.md](CONTRIBUTING.md) for the module map, the subcommands and the manual test plan. This file covers what an agent needs to know before changing anything.

## Commands

```bash
python3 -m unittest discover -s tests -v                     # everything: unit + e2e (~35s, needs tmux)
python3 -m unittest discover -s tests -p 'test_units.py'     # unit only, <1s, no tmux
python3 -m unittest discover -s tests -p 'test_units.py' -k GhosttyTest   # one class
python3 -m py_compile scripts/cc_micro_manage.py             # what CI compiles
```

CI runs the full suite on Linux (Python 3.11 and 3.13) and macOS (3.13). Run it before you say a change is done.

## How it fits together

- `hooks/hooks.json` → `scripts/cc_micro_manage.py` → `scripts/ccmm/cli.py`, which dispatches subcommands.
- `SubagentStart` hands the payload to a **detached** `place` helper and returns at once. The helper opens the pane, and the pane runs `view`, which tails the subagent's transcript.
- The hook, the helper and the panes share nothing except small files in the state directory (`state.py`, fixed at `/tmp/cc-micro-manage-<uid>`). **Panes don't inherit Claude's environment**, so anything a pane needs travels as an argument (`viewer.view_command`) or a state file.
- There are two terminal backends. `settings.terminal()` returns `"tmux"` when `$TMUX` is set, `"ghostty"` for `TERM_PROGRAM=ghostty` on macOS, and otherwise `None`, which makes the plugin do nothing. tmux wins over Ghostty.
  - tmux: `tmux.py`, `layout.py` and `placement.py`.
  - Ghostty: `ghostty.py` (an AppleScript wrapper around `osascript`) and `ghostty_layout.py`.
  - `panes.is_alive()` tells the backends apart by pane id: tmux ids start with `%` or `@`, Ghostty's are UUIDs. Use it wherever code only needs to know whether a pane is still open.

## Rules that aren't negotiable

- **Hooks never break a session.** They exit 0, return fast and catch everything (`cli.HOOK_COMMANDS`). Slow work belongs in the detached helper, not the hook.
- **Standard library only, Python 3.11+.** No dependencies, ever.
- **Never touch what the user made.** Don't rearrange or resize panes the plugin didn't create, and don't overwrite tmux bindings that aren't ours. Layout code checks that a window or tab holds only Claude and agent panes before evening it out.
- **The relay never types over a draft.** It types into Claude's input box only while `relay.input_box_state()` reads that box as empty.
- **Claude Code's transcript and meta formats are undocumented.** Parse them defensively and fail soft. The worst case should be a pane that shows less, never a crash.
- **The e2e tests use a private tmux server** (`tmux -L …`) and their own `CC_MICRO_MANAGE_STATE_DIR`. Never point tests or experiments at the user's real tmux server or state directory.

## Gotchas

- **tmux reuses pane ids** across restarts. That's why each pane records which agent it shows (`messaging.AGENT_OPTION`) and why dead panes' state is forgotten.
- **Ghostty can't show a terminal's screen to scripts**, so background agents' panes are read-only there (`messaging.RELAY_NEEDS_TMUX`).
- **Ghostty leaves a split open after its command exits.** The viewer starts a detached `ghostty-closed` helper on the way out, which closes the split and evens out the rest.
- **Ghostty splits get a minimal `PATH`** (`/usr/bin/python3` is 3.9). Re-run the program with `program.argv()` and `program.shell_command()`, which use `sys.executable`; never use a bare `python3`.
- **On macOS, `$TMPDIR` differs** between Claude's environment and the tmux server's, which is why the state directory is a fixed `/tmp` path.
- **Setting names must not end in `KEY`.** The plugin directory's scanner treats them as credentials.
- **Environment variable names in `settings._SOURCES` are spelled out literally.** Don't build them at run time.

## How to work here

- **Read before you write.** Read the module you're changing, its tests, and one similar feature end to end, then follow the patterns you find. Most of what you need already exists: state files, `panes.is_alive`, `program.argv`, `text.wrap`.
- **Reproduce first.** For a bug, write a failing test, or reproduce it in a private tmux server, before you fix anything. Then the fix comes with its proof.
- **Check the real tool, don't guess its API.**
  - tmux: `man tmux`, and try commands on a scratch server.
  - Ghostty: read its AppleScript dictionary at `/Applications/Ghostty.app/Contents/Resources/Ghostty.sdef`, and try commands with `osascript`.
  - Claude Code: look at real transcripts under `~/.claude/projects/`.
- **Unit tests with fakes are not proof.** For anything the user sees, run it for real before calling it done:
  - tmux: run it on a scratch server (`tmux -L scratch …`) and read the pane with `capture-pane -p -e`.
  - Ghostty: run it in a throwaway Ghostty window.
  - Anything involving hooks or messaging: do a real Claude Code run (Haiku is enough).
- **Clean up after experiments.** Kill scratch tmux servers, close any windows you opened, and delete temporary files and scratch state directories. Leave the user's real sessions exactly as you found them.
- **Keep the change focused.** One logical change per commit. No drive-by refactors, renames or reformatting, and no unrelated fixes slipped in; mention those separately instead.
- **Don't break users.** Renaming a setting, an environment variable or a subcommand, or changing a default, is a breaking change. Ask first. If it goes ahead, flag it in the CHANGELOG with an upgrade note.
- **Report honestly.** When you finish, say what you verified (which suites, on what, and any real runs) and what you didn't. Don't call something done while tests fail or a step was skipped.

## Commits and branches

- Commit or push only when asked. Never force-push `main`, never rewrite a published tag, and never skip hooks (`--no-verify`).
- Write the title in the imperative and in sentence case, under about 60 characters, with no trailing period. The repo's own titles look like `Add Ghostty support (macOS)` and `Draw panes like Claude Code`. The body says what changed and why, wrapped at about 72 columns.
- Before committing, run the full suite, update the CHANGELOG under `[Unreleased]`, and update the README and CONTRIBUTING if what users see or the code layout changed.
- Outside contributions go through a PR from a branch, and CI must be green.

## Making common changes

- **New setting:** touch all five places:
  - `settings._SOURCES`, plus an accessor
  - `userConfig` in `.claude-plugin/plugin.json`
  - the README's settings table and its environment-variable list
  - the docstring in `scripts/cc_micro_manage.py`
  - a `SettingsTest` case
- **Rendering:** panes copy Claude Code's look (colours in `text.C`, glyphs `⏺`, `⎿`, `❯`, `✻`). Check changes against a real Claude Code pane, not memory. Keep every emitted line within the pane width (`text.wrap`).
- **Behaviour changes:** add a test (a unit test where possible, e2e for anything involving panes). Add a line under `## [Unreleased]` in `CHANGELOG.md`, and update README and CONTRIBUTING if what users see or how the code is laid out changed.
- **Code style:** one module per job, short functions named for what they do. If a function needs comments to explain its steps, give the steps names instead.

## Docs voice

README, CHANGELOG and CONTRIBUTING use plain, direct sentences: short and specific, no hype. Describe what the user sees, and say why when it isn't obvious. Match the surrounding text.

## Releases

The maintainer decides when to release; don't release or push unprompted. A release:

1. Bump `version` in `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`.
2. Rename the `[Unreleased]` CHANGELOG section to `[X.Y.Z] - YYYY-MM-DD`, and put a fresh `## [Unreleased]` above it.
3. Commit as `vX.Y.Z`, then tag with `git tag -a vX.Y.Z -m vX.Y.Z`.
4. Publish with `gh release create vX.Y.Z --title vX.Y.Z`, using that CHANGELOG section as the notes.

## The demo GIF

Regenerate it with `./demo/setup-demo-repo.sh && vhs demo/demo.tape`. This records real Claude Code (Haiku) in a private tmux server. The GIF lives on the orphan `assets` branch, which keeps every plugin file under the 5 MiB limit; `demo/demo.gif` is gitignored on `main`.
