"""Claude Code hook entry points (hooks/hooks.json). Each reads the hook's JSON
payload from stdin; a hook must return quickly and never break the session."""
import json
import subprocess
import sys

from . import claude_files, ghostty, messaging, program, settings, state


def _debug_log(event, payload):
    if not settings.debug_enabled():
        return
    state.ensure_dir()
    with open(state.path("debug.jsonl"), "a") as f:
        f.write(json.dumps({"event": event, "payload": payload, "TMUX_PANE": settings.claude_pane(),
                            "options": settings.snapshot()}) + "\n")


def subagent_start(payload):
    """Open a pane, in a detached helper, so the hook returns at once."""
    _debug_log("start", payload)
    terminal = settings.terminal()
    if settings.plugin_disabled() or not terminal:
        return
    if not payload.get("agent_id") or not payload.get("agent_type"):
        return      # Claude Code's internal agents (prompt suggestions, /btw)
    if payload["agent_type"] in settings.skipped_agent_types():
        return
    _place(program.argv("place"), payload, terminal)


def post_bash(payload):
    """PostToolUse on Bash: give a background shell its own pane."""
    _debug_log("post-bash", payload)
    terminal = settings.terminal()
    if settings.plugin_disabled() or not terminal or not settings.shells_enabled():
        return
    response = payload.get("tool_response")
    if not isinstance(response, dict) or not response.get("backgroundTaskId"):
        return          # an ordinary command: Claude shows it inline
    _place(program.argv("place-shell"), payload, terminal)


def session_end(payload):
    """SessionEnd: close the panes of the session's background shells."""
    state.ensure_dir()
    state.mark_session_ended(payload.get("session_id"))


def _place(command, payload, terminal):
    """Hand the payload to a detached helper that opens the pane."""
    if terminal == "ghostty":
        # the helper has no tty; Claude's tty is how it finds Claude's terminal
        payload = {**payload, "ccmm_tty": ghostty.controlling_tty()}
    helper = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True)
    helper.stdin.write(json.dumps(payload).encode())
    helper.stdin.close()


def subagent_stop(payload):
    """Hand a foreground agent your unread messages, or mark the agent done."""
    _debug_log("stop", payload)
    if not payload.get("agent_id"):
        return
    state.ensure_dir()
    agent = state.Agent(payload["agent_id"])
    meta = claude_files.read_meta(payload.get("transcript_path"), payload["agent_id"])
    reply = messaging.stop_decision(agent, background=meta.get("requestShape") == "background")
    if reply:
        print(json.dumps(reply))
    else:
        agent.mark_done(payload.get("agent_transcript_path"))


def post_agent(payload):
    """PostToolUse on Agent/Task: tell the caller what you sent its subagent."""
    if not payload.get("tool_use_id") or not payload.get("transcript_path"):
        return
    note = messaging.note_for_caller(payload["transcript_path"], payload["tool_use_id"])
    if note:
        print(json.dumps(note))


def read_payload():
    return json.load(sys.stdin)
