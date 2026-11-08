"""Messages you type for a subagent.

Foreground agents get them at SubagentStop: the hook blocks the stop and hands
the messages over as the reason, which models accept as steering (text that
turns up mid-task claiming "the user says..." is rightly read as prompt
injection). Background agents ignore that block, so their messages are relayed
through Claude instead (see relay.py).
"""
from . import claude_files, program, settings, state, tmux

ALREADY_FINISHED = "That subagent has already finished; message not sent."
RELAY_OFF = "read-only · background subagent (relay is turned off)"
RELAY_NEEDS_TMUX = "read-only · background subagent (relaying needs tmux)"
QUEUED_FOR_STOP = "queued, delivered when the subagent wraps up"
QUEUED_FOR_RELAY = "relaying via Claude"

AGENT_OPTION = "@cc_micro_manage_agent"     # which agent a pane shows
MESSAGE_OPTION = "@cc_micro_manage_msg"     # the text typed at the shortcut prompt
OUR_BINDING_MARKERS = ("cc_micro_manage", "subagent_panes")   # current and old name


def queue(agent, text):
    """Queue a message. Returns (error, how) where how is "inbox" or "relay"."""
    if agent.is_done:
        return ALREADY_FINISHED, None
    if agent.is_background:
        if not agent.relay_allowed:
            return agent.relay_off_reason or RELAY_OFF, None
        agent.append_message(".relay", text)
        return None, "relay"
    agent.append_message(".inbox", text)
    return None, "inbox"


def stop_decision(agent, background):
    """SubagentStop: hold a foreground agent that has unread messages and hand
    them over. Returns the hook's JSON reply, or None to let it finish."""
    inbox = agent.inbox
    new = inbox[agent.delivered:]
    if not new or background:
        return None
    agent.delivered = len(inbox)
    return {
        "decision": "block",
        "reason": ("Before you finish: the user is watching you live in a cc-micro-manage tmux pane "
                   f"and sent you {len(new)} message(s):\n{_bullets(new)}\n"
                   "Address them, then finish as normal and mention in your final reply what you did about them."),
    }


def note_for_caller(session_transcript, tool_use_id):
    """PostToolUse on Agent/Task: tell the calling agent what you sent its
    subagent, so the extra work isn't mistaken for going off-brief."""
    agent_id = claude_files.agent_for_tool_use(session_transcript, tool_use_id)
    if not agent_id:
        return None
    agent = state.Agent(agent_id)
    delivered = agent.inbox[: agent.delivered]
    if not delivered:
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": ("cc-micro-manage: while this subagent ran, the user messaged it directly "
                              f"from its tmux pane:\n{_bullets(delivered)}\nIts result includes work done in response."),
    }}


def _bullets(messages):
    return "\n".join(f"- {m}" for m in messages)


# ── the prefix + <key> shortcut ─────────────────────────────────────────

def bind_shortcut():
    """prefix + <key> opens a prompt whose text goes to `send`. Never replaces
    a binding that isn't ours."""
    key = settings.shortcut()
    if not key:
        return
    existing = tmux.try_run("list-keys", "-T", "prefix", key)
    if existing and not any(marker in existing for marker in OUR_BINDING_MARKERS):
        return
    send = program.shell_command("send") + " '#{pane_id}'"
    # the text travels in a pane option, so quotes, $() and # in it are never parsed
    template = f'set-option -p {MESSAGE_OPTION} "%%%" ; run-shell -b "' + send.replace('"', '\\"') + '"'
    tmux.run("bind-key", "-T", "prefix", key, "command-prompt", "-p", "✉ message to this subagent:", template)


def send_from_prompt(pane):
    """The shortcut's prompt was submitted in `pane`: queue its text."""
    agent_id = tmux.pane_option(pane, AGENT_OPTION)
    state.ensure_dir()
    text = tmux.pane_option(pane, MESSAGE_OPTION)
    tmux.unset_pane_option(pane, MESSAGE_OPTION)
    if not text.strip():
        return
    if not agent_id:
        tmux.run("display-message", "This pane isn't a subagent pane.")
        return
    error, how = queue(state.Agent(agent_id), text)
    tmux.run("display-message", error or (QUEUED_FOR_RELAY if how == "relay" else QUEUED_FOR_STOP))
