#!/usr/bin/env python3
"""
cc-micro-manage — give every Claude Code subagent its own live tmux pane.

Claude Code plugin hooks (hooks/hooks.json):
  SubagentStart -> `cc_micro_manage.py start`  (returns instantly; a detached
                   `place` process opens the pane)
  SubagentStop  -> `cc_micro_manage.py stop`   (tells the viewer to wrap up)

Each pane runs `cc_micro_manage.py view`, which tails the subagent's transcript
(<session>/subagents/agent-<id>.jsonl) and pretty-prints it. Subagents still run
natively inside Claude Code; panes are read-only views.

Messaging: focus a subagent pane, press prefix + a, type. The message is queued
and delivered when the subagent tries to finish (SubagentStop block + reason),
which models treat as legitimate steering; mid-task injection is (rightly) read
as prompt injection. The main agent is told what you sent (PostToolUse note).
Background subagents ignore that, so for them the message is relayed: once
Claude's input box is empty, the viewer types "pass this on with SendMessage"
into it for you.

Layout ("split", default): Claude stays on the left, subagents stack in a column
on the right, oldest on top (FIFO). A nested subagent splits its parent's pane the
same way. Parent links come from agent-<id>.meta.json (parentAgentId).

Settings: /config (plugin options, see .claude-plugin/plugin.json userConfig),
or these env vars, which win over the plugin options (settings.json "env" or your shell):
  CC_MICRO_MANAGE_LAYOUT  split (default) | window (tile in a separate "agents" window)
  CC_MICRO_MANAGE_LINGER  seconds a pane stays after its subagent finishes (default 10; -1 = until Enter)
  CC_MICRO_MANAGE_SKIP    comma-separated agent types to ignore
  CC_MICRO_MANAGE_OFF     1 = disable
  CC_MICRO_MANAGE_SHORTCUT  tmux key (after your prefix) to message the focused subagent (default a)
  CC_MICRO_MANAGE_RELAY   0 = don't relay messages for background subagents through main Claude
  CC_MICRO_MANAGE_BORDERS  0 = don't turn on pane-border titles in agent windows
  CC_MICRO_MANAGE_DEBUG   1 = log hook payloads to /tmp/cc-micro-manage-<uid>/debug.jsonl
  CC_MICRO_MANAGE_STATE_DIR  where state files live (default /tmp/cc-micro-manage-<uid>; the tests use their own)
"""
import codecs
import collections
import glob
import json
import os
import re
import shlex
import select
import shutil
import signal
import subprocess
import sys
import time

# Fixed path: the hook (Claude's env) and the pane (tmux server's env) can see
# different $TMPDIR values on macOS, so don't derive this from tempfile.
STATE_DIR = os.environ.get("CC_MICRO_MANAGE_STATE_DIR") or os.path.join("/tmp", "cc-micro-manage-%d" % os.getuid())
SELF = os.path.abspath(__file__)


# ───────────────────────────── helpers ──────────────────────────────

# Every variable this plugin reads is spelled out here, never built at run time.
_SETTINGS = {
    "LAYOUT": lambda: (os.environ.get("CC_MICRO_MANAGE_LAYOUT"), os.environ.get("CLAUDE_PLUGIN_OPTION_LAYOUT")),
    "LINGER": lambda: (os.environ.get("CC_MICRO_MANAGE_LINGER"), os.environ.get("CLAUDE_PLUGIN_OPTION_LINGER")),
    "SHORTCUT": lambda: (os.environ.get("CC_MICRO_MANAGE_SHORTCUT"), os.environ.get("CLAUDE_PLUGIN_OPTION_MESSAGE_SHORTCUT")),
    "RELAY": lambda: (os.environ.get("CC_MICRO_MANAGE_RELAY"), os.environ.get("CLAUDE_PLUGIN_OPTION_RELAY")),
    "BORDERS": lambda: (os.environ.get("CC_MICRO_MANAGE_BORDERS"), os.environ.get("CLAUDE_PLUGIN_OPTION_BORDERS")),
    "SKIP": lambda: (os.environ.get("CC_MICRO_MANAGE_SKIP"), os.environ.get("CLAUDE_PLUGIN_OPTION_SKIP")),
}


def _opt(name, default, legacy=None):
    """A setting: CC_MICRO_MANAGE_<NAME> env (explicit override) wins, then the
    plugin option the user set in /config (CLAUDE_PLUGIN_OPTION_<NAME>), then
    the default. Only hook processes see these; panes get what they need as
    arguments or state files."""
    env_value, option_value = _SETTINGS[name]()
    v = env_value
    if v is None or v == "":
        v = option_value
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()


def _flag(name, default=True, legacy=None):
    v = _opt(name, None, legacy)
    if v is None:
        return default
    return v.lower() not in ("0", "false", "no", "off")


def _safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s or "")


def _tmux(*args):
    r = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def _alive(target):
    # display-message exits 0 with empty output for a missing target, so check the output.
    try:
        return bool(_tmux("display-message", "-p", "-t", target, "#{pane_id}"))
    except Exception:
        return False


class _Lock:
    """Portable mkdir lock (flock isn't on stock macOS)."""

    def __init__(self, name):
        self.path = os.path.join(STATE_DIR, name + ".lock")

    def __enter__(self):
        deadline = time.time() + 5
        while True:
            try:
                os.mkdir(self.path)
                return self
            except FileExistsError:
                if time.time() > deadline:  # stale lock
                    shutil.rmtree(self.path, ignore_errors=True)
                    deadline = time.time() + 5
                time.sleep(0.05)

    def __exit__(self, *a):
        shutil.rmtree(self.path, ignore_errors=True)


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _write(path, s):
    with open(path, "w") as f:
        f.write(s)


# ─────────────────────── split-layout column ────────────────────────
# Agent panes for one Claude pane live in a right-hand column, oldest on top
# (FIFO). The list is kept in col-<claude pane>.

def _col_file(claude_pane):
    return os.path.join(STATE_DIR, "col-" + _safe(claude_pane))


def _col(claude_pane):
    return [p for p in _read(_col_file(claude_pane)).split() if _alive(p)]


# Border label for agent panes: coloured dot (state) + the agent's purpose.
# Other panes keep a neutral label. Only applied to windows we put agent panes
# in, and only if the user hasn't turned on pane borders there themselves.
BORDER_FORMAT = (" #{?#{@ccmm_title},#[fg=#{@ccmm_color}]●#[default] #{@ccmm_title},"
                 "#{@ccmm_label}} ")
STATE_COLOR = {"running": "yellow", "finished": "green", "error": "red"}


def _borders_on(pane):
    if not _flag("BORDERS"):
        return
    win = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
    try:
        ours = _tmux("show-options", "-w", "-v", "-t", win, "@ccmm_borders")
    except Exception:
        ours = ""
    if ours:
        return
    try:
        current = _tmux("show-options", "-w", "-v", "-t", win, "pane-border-status")
    except Exception:
        current = ""
    if not current:   # not set on this window: inherits the global value
        current = _tmux("show-options", "-g", "-v", "pane-border-status")
    if current not in ("", "off"):
        return        # user already shows borders: respect their format
    _tmux("set-option", "-w", "-t", win, "pane-border-status", "top")
    _tmux("set-option", "-w", "-t", win, "pane-border-format", BORDER_FORMAT)
    _tmux("set-option", "-w", "-t", win, "@ccmm_borders", "1")
    claude = os.environ.get("TMUX_PANE")
    if claude:
        try:
            _tmux("set-option", "-p", "-t", claude, "@ccmm_label", "claude")
        except Exception:
            pass


def _borders_off_if_idle(win):
    """Restore the window once its last agent pane is gone."""
    try:
        if not _tmux("show-options", "-w", "-v", "-t", win, "@ccmm_borders"):
            return
        titles = _tmux("list-panes", "-t", win, "-F", "#{@ccmm_title}").split("\n")
    except Exception:
        return
    if any(t.strip() for t in titles):
        return
    for opt in ("pane-border-status", "pane-border-format", "@ccmm_borders"):
        try:
            _tmux("set-option", "-w", "-u", "-t", win, opt)
        except Exception:
            pass
    try:
        for pid in _tmux("list-panes", "-t", win, "-F", "#{pane_id}").split():
            _tmux("set-option", "-p", "-u", "-t", pid, "@ccmm_label")
    except Exception:
        pass


def _set_state(pane, state):
    try:
        _tmux("set-option", "-p", "-t", pane, "@ccmm_color", STATE_COLOR.get(state, "yellow"))
    except Exception:
        pass


def _relayout(claude_pane):
    """Claude on the left, agent panes stacked evenly on the right — but only
    when the window holds nothing except Claude + its agent panes, so we never
    rearrange panes you created yourself."""
    agents = _col(claude_pane)
    _write(_col_file(claude_pane), "\n".join(agents))
    if not agents and _alive(claude_pane):
        _borders_off_if_idle(_tmux("display-message", "-p", "-t", claude_pane, "#{window_id}"))
    if not agents or not _alive(claude_pane):
        return
    win = _tmux("display-message", "-p", "-t", claude_pane, "#{window_id}")
    panes = _tmux("list-panes", "-t", win, "-F", "#{pane_id}").split()
    if panes != [claude_pane] + agents:
        return
    _tmux("set-window-option", "-t", win, "main-pane-width", "55%")
    _tmux("select-layout", "-t", win, "main-vertical")


# ───────────────────────────── messaging ────────────────────────────
# <key>.inbox      one JSON line per message you typed
# <key>.delivered  how many inbox lines have been handed to the subagent
# <key>.relay      background agents: messages to relay through main Claude

def _inbox(key):
    return _lines(key, ".inbox")


def _delivered(key):
    try:
        return int(_read(os.path.join(STATE_DIR, key + ".delivered")) or 0)
    except ValueError:
        return 0


def _bind_key():
    """Register prefix+<key> -> tmux prompt -> `send`. Never clobbers a binding
    that isn't ours."""
    key = _opt("SHORTCUT", "a")
    if not key or key.lower() == "none":
        return
    try:
        existing = _tmux("list-keys", "-T", "prefix", key)
    except Exception:
        existing = ""
    if existing and "cc_micro_manage" not in existing and "subagent_panes" not in existing:  # old name
        return
    run = " ".join(shlex.quote(x) for x in [sys.executable, SELF, "send"]) + " '#{pane_id}'"
    # the text goes through a pane option, so quotes/$()/# in it are never shell- or format-parsed
    template = 'set-option -p @cc_micro_manage_msg "%%%" ; run-shell -b "' + run.replace('"', '\\"') + '"'
    _tmux("bind-key", "-T", "prefix", key, "command-prompt", "-p", "✉ message to this subagent:", template)


def cmd_send(pane):
    try:
        agent_key = _safe(_tmux("show-options", "-p", "-v", "-t", pane, "@cc_micro_manage_agent")) or None
    except Exception:
        agent_key = None
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        text = _tmux("show-options", "-p", "-v", "-t", pane, "@cc_micro_manage_msg")
        _tmux("set-option", "-p", "-u", "-t", pane, "@cc_micro_manage_msg")
    except Exception:
        text = ""
    if not text.strip():
        return
    if not agent_key:
        _tmux("display-message", "This pane isn't a subagent pane.")
        return
    err, how = _queue(agent_key, text)
    _tmux("display-message", err or ("✉ relaying via Claude" if how == "relay"
                                     else "✉ queued, delivered when the subagent wraps up"))


BACKGROUND_NOTE = "read-only · background subagent (relay is turned off)"
RELAY_STATUS = ""   # background panes: plain separator, no hint


def _relay_on(key):
    # decided in the hook (Claude's env) at place time; panes don't see that env
    return not os.path.exists(os.path.join(STATE_DIR, key + ".norelay"))


def _queue(key, text):
    """Queue a message for an agent. Foreground agents get it at SubagentStop
    (<key>.inbox); background agents get it relayed through main Claude
    (<key>.relay, picked up by the viewer). Returns (error, how)."""
    if os.path.exists(os.path.join(STATE_DIR, key + ".done")):
        return "That subagent has already finished; message not sent.", None
    if _read(os.path.join(STATE_DIR, key + ".shape")) == "background":
        if not _relay_on(key):
            return BACKGROUND_NOTE, None
        box, how = ".relay", "relay"
    else:
        box, how = ".inbox", "inbox"
    with open(os.path.join(STATE_DIR, key + box), "a") as f:
        f.write(json.dumps({"ts": time.time(), "text": text}) + "\n")
    return None, how


def _lines(key, suffix):
    out = []
    try:
        with open(os.path.join(STATE_DIR, key + suffix)) as f:
            for line in f:
                try:
                    out.append(json.loads(line)["text"])
                except (ValueError, KeyError):
                    pass
    except OSError:
        pass
    return out


# ── relay through main Claude ─────────────────────────────────────────
# Background subagents ignore SubagentStop blocks, but main Claude can message
# them with SendMessage. So we type the request into Claude's own input box,
# as if you had, but only while that box is empty: never clobber a draft.

_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _claude_input(pane):
    """Read Claude Code's input box from its pane.
    Returns "empty", "draft", or None (no box visible: dialog open, copy mode,
    not Claude...)."""
    try:
        if _tmux("display-message", "-p", "-t", pane, "#{pane_in_mode}") == "1":
            return None
        raw = _tmux("capture-pane", "-e", "-p", "-t", pane).split("\n")
    except Exception:
        return None
    for i in range(len(raw) - 1, 0, -1):
        if not _ANSI.sub("", raw[i]).startswith("❯"):
            continue
        if not _ANSI.sub("", raw[i - 1]).startswith("──"):    # the box's top rule
            continue
        rest = raw[i][raw[i].index("❯") + 1:]
        if not _ANSI.sub("", rest).strip():
            return "empty"
        # placeholder / suggestion text is drawn dim (SGR 2); your draft isn't
        m = re.match(r"\s*((?:\x1b\[[0-9;]*m\s*)+)", rest)
        if m and any("2" in g.split(";") for g in _SGR.findall(m.group(1))):
            return "empty"
        return "draft"
    return None


def _relay_prompt(agent_id, purpose, text):
    text = " ".join(text.split())
    who = f"{agent_id} ({purpose})" if purpose else agent_id
    return (f"[cc-micro-manage] I'm messaging background subagent {who} from its tmux pane. "
            f"Pass this on to it with SendMessage, verbatim, then confirm in one line: {text}")


def _type_into(pane, prompt):
    """Type prompt into Claude's (empty) input box and submit it. True if sent."""
    _tmux("send-keys", "-t", pane, "-l", prompt)
    probe = prompt[:24]
    for _ in range(10):                        # wait for Claude to echo it
        time.sleep(0.15)
        try:
            if probe in _ANSI.sub("", _tmux("capture-pane", "-p", "-t", pane)).replace("\n", ""):
                _tmux("send-keys", "-t", pane, "Enter")
                return True
        except Exception:
            pass
    return False


def cmd_stop_hook(data):
    """SubagentStop: if you sent messages it hasn't seen, keep it running and
    hand them over as the block reason. Otherwise mark it done."""
    key = _safe(data["agent_id"])
    msgs = _inbox(key)
    n = _delivered(key)
    new = msgs[n:]
    background = _meta_now(data.get("transcript_path"), data["agent_id"]).get("requestShape") == "background"
    if new and not background:
        _write(os.path.join(STATE_DIR, key + ".delivered"), str(len(msgs)))
        lines = "\n".join(f"- {m}" for m in new)
        print(json.dumps({
            "decision": "block",
            "reason": ("Before you finish: the user is watching you live in a cc-micro-manage tmux pane "
                       f"and sent you {len(new)} message(s):\n{lines}\n"
                       "Address them, then finish as normal and mention in your final reply what you did about them."),
        }))
        return
    _write(os.path.join(STATE_DIR, key + ".done"), data.get("agent_transcript_path") or "")


def cmd_post_agent(data):
    """PostToolUse on Agent/Task: tell the caller what the user sent the subagent,
    so its extra work isn't mistaken for going off-brief."""
    tid = data.get("tool_use_id")
    t = data.get("transcript_path") or ""
    if not tid or not t:
        return
    sub = os.path.join(t[:-6] if t.endswith(".jsonl") else t, "subagents")
    for mf in glob.glob(os.path.join(sub, "*.meta.json")):
        try:
            with open(mf) as f:
                if json.load(f).get("toolUseId") != tid:
                    continue
        except (OSError, ValueError):
            continue
        aid = os.path.basename(mf)[: -len(".meta.json")]
        aid = aid[len("agent-"):] if aid.startswith("agent-") else aid
        msgs = _inbox(_safe(aid))[: _delivered(_safe(aid))]
        if msgs:
            lines = "\n".join(f"- {m}" for m in msgs)
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": ("cc-micro-manage: while this subagent ran, the user messaged it directly "
                                      f"from its tmux pane:\n{lines}\nIts result includes work done in response."),
            }}))
        return


# ───────────────────────────── start ────────────────────────────────

def _debug(event, data):
    if os.environ.get("CC_MICRO_MANAGE_DEBUG") == "1":
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "debug.jsonl"), "a") as f:
            opts = {n: list(read()) for n, read in _SETTINGS.items()}   # [env var, plugin option]
            f.write(json.dumps({"event": event, "payload": data, "TMUX_PANE": os.environ.get("TMUX_PANE"),
                                "options": opts}) + "\n")


def cmd_start():
    """Hook entry point: must return instantly, so placement runs detached."""
    data = json.load(sys.stdin)
    _debug("start", data)
    if os.environ.get("CC_MICRO_MANAGE_OFF") == "1" or not os.environ.get("TMUX"):
        return
    if not data.get("agent_id") or not data.get("agent_type"):  # internal agents (prompt suggestions, /btw)
        return
    skip = {s.strip() for s in _opt("SKIP", "").split(",") if s.strip()}
    if data["agent_type"] in skip:
        return
    p = subprocess.Popen([sys.executable, SELF, "place"], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    p.stdin.write(json.dumps(data).encode())
    p.stdin.close()


def _meta(data):
    """<session>/subagents/agent-<id>.meta.json carries parentAgentId/spawnDepth
    (the hook payload doesn't). It can land just after SubagentStart, so wait briefly."""
    t = data.get("transcript_path") or ""
    aid = data["agent_id"]
    bare = aid[len("agent-"):] if aid.startswith("agent-") else aid
    if not t:
        return {}
    path = os.path.join(t[:-6] if t.endswith(".jsonl") else t, "subagents", f"agent-{bare}.meta.json")
    deadline = time.time() + 2.5
    while time.time() < deadline:
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            time.sleep(0.05)
    return {}


def _meta_now(transcript, agent_id):
    t = transcript or ""
    bare = agent_id[len("agent-"):] if agent_id.startswith("agent-") else agent_id
    if not t:
        return {}
    try:
        with open(os.path.join(t[:-6] if t.endswith(".jsonl") else t, "subagents", f"agent-{bare}.meta.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _prune(max_age=86400):
    """Drop state left by agents that never got (or outlived) a pane."""
    cutoff = time.time() - max_age
    for f in glob.glob(os.path.join(STATE_DIR, "*")):
        try:
            if os.path.isfile(f) and os.path.getmtime(f) < cutoff and not f.endswith(".jsonl"):
                os.remove(f)
        except OSError:
            pass


def cmd_place():
    data = json.load(sys.stdin)
    agent_id, agent_type = data["agent_id"], data["agent_type"]
    os.makedirs(STATE_DIR, exist_ok=True)
    _prune()
    key = _safe(agent_id)
    state = os.path.join(STATE_DIR, key + ".pane")

    claude_pane = os.environ.get("TMUX_PANE") or _tmux("display-message", "-p", "#{pane_id}")
    layout = _opt("LAYOUT", "split").lower()
    if layout not in ("split", "window"):
        layout = "split"
    meta = _meta(data)
    parent_id = meta.get("parentAgentId") or ""
    depth = int(meta.get("spawnDepth") or 1)
    # what the agent is for (Claude's own description), e.g. "Scout: describe every .go file"
    purpose = re.sub(r"\s+", " ", str(meta.get("description") or "")).strip()
    title = ("↳ " * (depth - 1)) + (purpose or agent_type) + f" · {agent_id[:6]}"

    viewer = " ".join(shlex.quote(x) for x in [
        sys.executable, SELF, "view",
        "--agent-id", agent_id,
        "--agent-type", agent_type,
        "--session", data.get("session_id") or "",
        "--transcript", data.get("transcript_path") or "",
        # tmux panes don't inherit Claude's env, so forward the setting explicitly
        "--linger", _opt("LINGER", "10"),
        "--parent-pane", claude_pane,
        "--title", purpose,
    ])

    with _Lock("tmux"):
        for f in glob.glob(os.path.join(STATE_DIR, "*.pane")):
            if not _alive(_read(f)):
                for suf in (".pane", ".inbox", ".delivered", ".done"):
                    try:
                        os.remove(f[:-5] + suf)
                    except OSError:
                        pass
        # SubagentStart also fires on resume / per teammate message: don't duplicate.
        existing = _read(state)
        if existing and _alive(existing):
            return
        try:
            os.remove(os.path.join(STATE_DIR, key + ".done"))
        except OSError:
            pass

        if layout == "split":
            # Owner = the pane this agent's column hangs off: its parent agent's pane
            # for nested agents, otherwise Claude's pane. Same rule at every level:
            # first child splits the owner to the right, later ones stack below (FIFO).
            owner = claude_pane
            if parent_id:
                pp = _read(os.path.join(STATE_DIR, _safe(parent_id) + ".pane"))
                if pp and _alive(pp):
                    owner = pp
            col = _col(owner)
            if col:
                pane = _tmux("split-window", "-d", "-v", "-t", col[-1], "-P", "-F", "#{pane_id}", viewer)
            else:
                width = "45%" if owner == claude_pane else "50%"
                pane = _tmux("split-window", "-d", "-h", "-l", width, "-t", owner,
                             "-P", "-F", "#{pane_id}", viewer)
            _write(_col_file(owner), "\n".join(col + [pane]))
            if owner == claude_pane:
                try:
                    _relayout(claude_pane)
                except Exception:
                    pass
        else:
            win_file = os.path.join(STATE_DIR, "win-" + _safe(claude_pane))
            win = _read(win_file)
            if win and _alive(win):
                pane = _tmux("split-window", "-d", "-t", win, "-P", "-F", "#{pane_id}", viewer)
                _tmux("select-layout", "-t", win, "tiled")
            else:
                sess = _tmux("display-message", "-p", "-t", claude_pane, "#{session_id}")
                pane = _tmux("new-window", "-d", "-t", sess + ":", "-n", "agents",
                             "-P", "-F", "#{pane_id}", viewer)
                win = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
                _write(win_file, win)

        try:
            _tmux("select-pane", "-t", pane, "-T", title)
            _tmux("set-option", "-p", "-t", pane, "@ccmm_title", title)
            _set_state(pane, "running")
            _borders_on(pane)
        except Exception:
            pass
        _write(state, pane)
        if meta.get("requestShape"):
            _write(os.path.join(STATE_DIR, key + ".shape"), meta["requestShape"])
        if not _flag("RELAY"):
            _write(os.path.join(STATE_DIR, key + ".norelay"), "1")
        try:  # the pane itself says which agent it shows (tmux reuses pane ids across restarts)
            _tmux("set-option", "-p", "-t", pane, "@cc_micro_manage_agent", agent_id)
        except Exception:
            pass
        try:
            _bind_key()
        except Exception:
            pass


# ───────────────────────────── stop ─────────────────────────────────

def cmd_stop():
    data = json.load(sys.stdin)
    _debug("stop", data)
    if not data.get("agent_id"):
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    cmd_stop_hook(data)


# ───────────────────────────── viewer ───────────────────────────────

C = {
    "dim": "\033[2m", "it": "\033[3m", "b": "\033[1m", "cyan": "\033[36m",
    "red": "\033[31m", "green": "\033[32m", "yel": "\033[33m", "mag": "\033[35m", "r": "\033[0m",
    "blue": "\033[34m", "ul": "\033[4m",
}


# ── markdown → ANSI ────────────────────────────────────────────────
_CODE_SPAN = re.compile(r"(`[^`\n]+`)")
_BOLD = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*|__(?!\s)(.+?)(?<!\s)__")
_ITAL = re.compile(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])|(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUM = re.compile(r"^(\s*)(\d+[.)])\s+(.*)$")


def _inline(text):
    out = []
    for part in _CODE_SPAN.split(text):
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"{C['yel']}{part[1:-1]}{C['r']}")
            continue
        part = _LINK.sub(lambda m: f"{C['ul']}{C['blue']}{m.group(1)}{C['r']}", part)
        part = _BOLD.sub(lambda m: f"{C['b']}{m.group(1) or m.group(2)}{C['r']}", part)
        part = _ITAL.sub(lambda m: f"{C['it']}{m.group(1) or m.group(2)}{C['r']}", part)
        out.append(part)
    return "".join(out)


def _md(text, max_lines=None):
    """Render the markdown agents write (headings, emphasis, code, lists,
    quotes, rules) with ANSI styling. Plain text passes through unchanged."""
    lines, in_code = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            lang = stripped[3:].strip()
            lines.append(f"{C['dim']}{'┌─ ' + lang if in_code and lang else ('┌─' if in_code else '└─')}{C['r']}")
            continue
        if in_code:
            lines.append(f"{C['dim']}│{C['r']} {C['green']}{line}{C['r']}")
            continue
        m = _HEAD.match(line)
        if m:
            color = C["mag"] if len(m.group(1)) <= 2 else C["cyan"]
            lines.append(f"{C['b']}{color}{_inline(m.group(2))}{C['r']}")
            continue
        if re.fullmatch(r"\s*([-*_])(\s*\1){2,}\s*", line):
            lines.append(f"{C['dim']}{'─' * 30}{C['r']}")
            continue
        m = _BULLET.match(line)
        if m:
            lines.append(f"{m.group(1)}{C['cyan']}•{C['r']} {_inline(m.group(2))}")
            continue
        m = _NUM.match(line)
        if m:
            lines.append(f"{m.group(1)}{C['cyan']}{m.group(2)}{C['r']} {_inline(m.group(3))}")
            continue
        if stripped.startswith(">"):
            lines.append(f"{C['dim']}▎{C['r']} {C['it']}{_inline(stripped.lstrip('>').strip())}{C['r']}")
            continue
        lines.append(_inline(line))
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines] + [f"{C['dim']}… +{len(lines) - max_lines} lines{C['r']}"]
    return "\n".join(lines)


def _task(text, first):
    """The task prompt can be pages long; show its gist (~3 lines) and move on."""
    flat = re.sub(r"\s+", " ", text).strip()
    limit = 240 if first else 400
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _state_of(line):
    """running / error from a transcript line (None if it says nothing)."""
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    c = (ev.get("message") or {}).get("content") if isinstance(ev.get("message"), dict) else None
    if not isinstance(c, list):
        return None
    results = [b for b in c if isinstance(b, dict) and b.get("type") == "tool_result"]
    if results:
        return "error" if any(b.get("is_error") for b in results) else "running"
    return None


def _trunc(s, n):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool_summary(name, inp):
    if not isinstance(inp, dict):
        return _trunc(inp, 100)
    for k in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt", "skill"):
        if k in inp and inp[k]:
            v = str(inp[k]).replace("\n", " ")
            return _trunc(v, 110)
    return _trunc(inp, 110)


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    out.append(c.get("text", ""))
                elif c.get("type") == "image":
                    out.append("[image]")
        return "\n".join(out)
    return str(content)


_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def _clean(text):
    """Drop Claude Code's injected <system-reminder> blocks."""
    return _REMINDER.sub("", text or "").strip()


def _render(line, first):
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return
    att = ev.get("attachment") if ev.get("type") == "attachment" else None
    if isinstance(att, dict) and att.get("type") == "queued_command" and att.get("prompt"):
        # a SendMessage from Claude reached this agent (e.g. a relayed message)
        who = "Claude" if (att.get("origin") or {}).get("kind") == "coordinator" else "caller"
        print(f"{C['yel']}{C['b']}✉ received from {who}:{C['r']} {C['yel']}{_trunc(_clean(str(att['prompt'])), 300)}{C['r']}")
        return
    # Only real conversation turns: skip attachments, api-request records, and
    # isMeta entries (e.g. our own stop-hook feedback, already shown as ✉ delivered).
    if ev.get("type") not in ("user", "assistant") or ev.get("isMeta"):
        return
    msg = ev.get("message") or {}
    role = msg.get("role") or ev.get("type")
    content = msg.get("content")
    if content is None:
        return

    if isinstance(content, str):
        content = _clean(content)
        if not content:
            return
        if role == "user":
            label = "task" if first else "user"
            print(f"{C['mag']}{C['b']}▶ {label}{C['r']} {C['dim']}{_task(content, first)}{C['r']}\n")
        else:
            print(_md(content) + "\n")
        return

    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text" and _clean(block.get("text")):
            text = _clean(block["text"])
            if role == "user":
                label = "task" if first else "user"
                print(f"{C['mag']}{C['b']}▶ {label}{C['r']} {C['dim']}{_task(text, first)}{C['r']}\n")
            else:
                print(_md(text) + "\n")
        elif t == "thinking" and block.get("thinking", "").strip():
            print(f"{C['dim']}{C['it']}✻ {_trunc(block['thinking'].strip().replace(chr(10), ' '), 240)}{C['r']}")
        elif t == "tool_use" and block.get("name") == "SubagentHandback" and isinstance(block.get("input"), dict):
            report = str(block["input"].get("message") or "").strip()
            print(f"\n{C['green']}{C['b']}⏎ report to caller{C['r']}")
            if report:
                print(_md(report, max_lines=60) + "\n")
        elif t == "tool_use":
            name = block.get("name", "?")
            print(f"{C['cyan']}⏺ {C['b']}{name}{C['r']}{C['cyan']}({_tool_summary(name, block.get('input'))}){C['r']}")
        elif t == "tool_result":
            text = _result_text(block.get("content")).strip()
            lines = text.splitlines() or [""]
            color = C["red"] if block.get("is_error") else C["dim"]
            shown = lines[:3]
            for i, l in enumerate(shown):
                prefix = "  ⎿ " if i == 0 else "    "
                print(f"{color}{prefix}{_trunc(l, 160)}{C['r']}")
            if len(lines) > 3:
                print(f"{C['dim']}    … +{len(lines) - 3} lines{C['r']}")
    sys.stdout.flush()


def _candidates(args):
    aid = args["agent_id"]
    bare = aid[len("agent-"):] if aid.startswith("agent-") else aid
    names = {f"agent-{bare}.jsonl", f"{aid}.jsonl"}
    out = []
    t = args.get("transcript")
    if t:
        base = t[:-6] if t.endswith(".jsonl") else t
        out += [os.path.join(base, "subagents", n) for n in names]
    s = args.get("session")
    if s:
        root = os.path.expanduser("~/.claude/projects")
        for n in names:
            out += glob.glob(os.path.join(root, "*", s, "subagents", n))
    return out


# ── footer stats: model · effort · tokens · elapsed ───────────────────
def _pretty_model(m):
    mm = re.fullmatch(r"claude-([a-z]+)-(\d+)-(\d+)(?:-\d{8})?", m or "")
    return f"{mm.group(1).capitalize()} {mm.group(2)}.{mm.group(3)}" if mm else (m or "")


def _k(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _ts(v):
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


class _Stats:
    def __init__(self):
        self.model = self.effort = ""
        self.context = self.out = 0
        self.t0 = self.t_end = None

    def update(self, line):
        try:
            ev = json.loads(line)
        except ValueError:
            return
        if self.t0 is None:
            self.t0 = _ts(ev.get("timestamp"))
        if ev.get("effort"):
            self.effort = str(ev["effort"])
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        if ev.get("type") == "assistant":
            if msg.get("model"):
                self.model = msg["model"]
            u = msg.get("usage") or {}
            if u:
                self.context = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
                    + (u.get("cache_creation_input_tokens") or 0)
                self.out += u.get("output_tokens") or 0

    def variants(self):
        """Full, medium and tiny footers; the screen shows the widest that fits."""
        full = self.line()
        model = _pretty_model(self.model)
        el = self._elapsed()
        mid = " · ".join(x for x in (model, f"{_k(self.context)} ctx" if self.context else "",
                                     f"{_k(self.out)} out" if self.out else "", el) if x)
        tiny = " · ".join(x for x in (f"{_k(self.context)}" if self.context else "", el) if x)
        return [full, mid, tiny]

    def _elapsed(self):
        if not self.t0:
            return ""
        secs = int((self.t_end or time.time()) - self.t0)
        return f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s"

    def line(self):
        parts = []
        if self.model:
            parts.append(_pretty_model(self.model))
        if self.effort:
            parts.append(f"{self.effort} effort")
        if self.context:
            parts.append(f"{_k(self.context)} context")
        if self.out:
            parts.append(f"{_k(self.out)} out")
        if self.t0:
            secs = int((self.t_end or time.time()) - self.t0)
            parts.append(f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s")
        return " · ".join(parts)


class _Screen:
    """Pane UI: output scrolls in the top region; the last line is a message box.

    Uses a terminal scroll region (DECSTBM) so streamed output never touches what
    you're typing. Falls back to plain output if stdin isn't a TTY or the pane is
    too small.
    """

    def __init__(self, on_send):
        self.on_send = on_send
        self.real = sys.__stdout__
        self.active = self.finished = self.enter = False
        self.buf = ""       # what you're typing
        self.pending = ""   # partial output line
        self.esc = False
        self.history = collections.deque(maxlen=400)   # replayed on resize
        self.footer = ""
        self.status = "type a message for this agent · Enter to send"
        self.input_enabled = True
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            self.fd = sys.stdin.fileno() if sys.stdin and sys.stdin.isatty() else None
        except (ValueError, OSError):
            self.fd = None
        if self.fd is None or self._size()[1] < 7:
            return
        import termios
        self._termios = termios
        self.saved = termios.tcgetattr(self.fd)
        mode = termios.tcgetattr(self.fd)
        mode[3] &= ~(termios.ICANON | termios.ECHO)   # keystrokes one at a time, no echo
        mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
        termios.tcsetattr(self.fd, termios.TCSANOW, mode)
        self.active = True
        sys.stdout = self
        signal.signal(signal.SIGWINCH, lambda *a: self._resize())
        self._resize()

    # -- terminal plumbing
    def _size(self):
        # ask the pane itself; shutil.get_terminal_size() prefers $COLUMNS/$LINES,
        # which can be inherited and wrong
        for fd in (self.real.fileno(), self.fd):
            try:
                sz = os.get_terminal_size(fd)
                return sz.columns, sz.lines
            except (OSError, ValueError, TypeError):
                pass
        return 80, 24

    def _resize(self):
        self.cols, self.rows = self._size()
        # clear (tmux reflow leaves fragments of old lines), then replay the tail
        self.real.write(f"\033[r\033[2J\033[1;{self.rows - 3}r")
        for line in list(self.history)[-(self.rows - 3):]:
            self.real.write(f"\033[{self.rows - 3};1H\n{line}")
        self._chrome()

    def _chrome(self):
        if not self.active:
            return
        w, R, cols = self.real.write, self.rows, self.cols
        label = f"─ {self.status} " if self.status else ""
        w(f"\033[{R - 2};1H\033[2K{C['dim']}{(label + '─' * cols)[:cols]}{C['r']}")
        # footer first, input last, so the cursor ends up in the input box
        room = max(cols - 5, 0)
        options = self.footer if isinstance(self.footer, list) else [self.footer]
        text = next((v for v in options if len(v) <= room), (options[-1] if options else ""))[:room]
        foot = (f"─ {text} " if text else "") + "─" * cols   # a rule, like the status line
        w(f"\033[{R};1H\033[2K{C['dim']}{foot[:cols]}{C['r']}")
        if self.finished or not self.input_enabled:
            w(f"\033[{R - 1};1H\033[2K")
        else:
            room = max(cols - 5, 1)
            shown = self.buf[-room:]
            w(f"\033[{R - 1};1H\033[2K{C['yel']}✉ ›{C['r']} {shown}")
        self.real.flush()

    def _emit(self, line):
        self.history.append(line)
        # cursor to bottom of the scroll region, newline scrolls it, then the text
        self.real.write(f"\033[{self.rows - 3};1H\n{line}")
        self._chrome()

    # -- file-like, so print() works unchanged
    def write(self, data):
        self.pending += data
        while "\n" in self.pending:
            line, _, self.pending = self.pending.partition("\n")
            self._emit(line)
        return len(data)

    def flush(self):
        self.real.flush()

    def set_status(self, text):
        self.status = text
        self._chrome()

    # -- input
    def poll(self, timeout):
        if not self.active:
            time.sleep(timeout)
            return
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return
        try:
            data = os.read(self.fd, 1024)
        except OSError:
            return
        for ch in self._dec.decode(data):
            if self.esc:                      # swallow escape sequences (arrows etc.)
                if ch.isalpha() or ch == "~":
                    self.esc = False
                continue
            if ch == "\x1b":
                self.esc = True
            elif not self.input_enabled and not self.finished:
                continue
            elif ch in "\r\n":
                if self.finished:
                    self.enter = True
                else:
                    msg, self.buf = self.buf.strip(), ""
                    if msg:
                        self.on_send(msg)
            elif ch in "\x7f\x08":
                self.buf = self.buf[:-1]
            elif ch == "\x15":                # Ctrl-U clears the line
                self.buf = ""
            elif ch >= " ":
                self.buf += ch
        self._chrome()

    def close(self):
        if not self.active:
            return
        if self.pending:
            self._emit(self.pending)
            self.pending = ""
        self.real.write(f"\033[r\033[{self.rows};1H\033[2K")
        self.real.flush()
        try:
            self._termios.tcsetattr(self.fd, self._termios.TCSANOW, self.saved)
        except Exception:
            pass
        sys.stdout = self.real
        self.active = False


def cmd_view(argv):
    args = {}
    it = iter(argv)
    for a in it:
        if a.startswith("--"):
            args[a[2:].replace("-", "_")] = next(it, "")
    agent_id, agent_type = args.get("agent_id", ""), args.get("agent_type", "")
    key = _safe(agent_id)
    done_file = os.path.join(STATE_DIR, key + ".done")
    parent = args.get("parent_pane")
    try:
        linger = float(args.get("linger") or 10)
    except ValueError:
        linger = 10.0

    def on_send(text):
        err, how = _queue(key, text)
        screen.set_status(err or ("✉ relaying via Claude · type another" if how == "relay"
                                  else "✉ queued · type another, or wait for it to wrap up"))

    screen = _Screen(on_send)
    stats = _Stats()
    my_pane = os.environ.get("TMUX_PANE")
    pane_state = "running"
    shape_file = os.path.join(STATE_DIR, key + ".shape")
    try:
        purpose = args.get("title") or ""
        head = f"{purpose} {C['dim']}({agent_type}){C['r']}" if purpose else agent_type
        print(f"{C['b']}{C['yel']}● {C['r']}{C['b']}{head}{C['r']} {C['dim']}{agent_id[:8]}{C['r']}\n")
        sys.stdout.flush()

        path = None
        t0 = time.time()
        while not path:
            for c in _candidates(args):
                if os.path.exists(c):
                    path = c
                    break
            if path:
                break
            if os.path.exists(done_file):
                hinted = _read(done_file)
                if hinted and os.path.exists(hinted):
                    path = hinted
                    break
            if time.time() - t0 > 60:
                print(f"{C['red']}Couldn't find this subagent's transcript.{C['r']}")
                break
            screen.poll(0.25)

        if path:
            first = True
            buf = ""
            idle_since = None
            last_data = last_check = time.time()
            max_idle = 30 * 60  # no transcript activity for 30 min -> give up
            seen_q = seen_d = seen_r = 0
            to_relay = collections.deque()   # background: waiting for Claude's input box
            relay_try = relay_wait_since = 0.0
            relay_note = ""
            shape_checked = False
            last_footer = 0
            with open(path) as f:
                while True:
                    chunk = f.readline()
                    if chunk:
                        buf += chunk
                        if buf.endswith("\n"):
                            st = _state_of(buf)
                            if st and st != pane_state and my_pane:
                                pane_state = st
                                _set_state(my_pane, st)
                            stats.update(buf)
                            screen.footer = stats.variants()
                            _render(buf, first)
                            first = False
                            buf = ""
                        idle_since = None
                        last_data = time.time()
                        continue
                    if time.time() - last_footer >= 1:   # keep the elapsed time ticking
                        last_footer = time.time()
                        screen.footer = stats.variants()
                        screen._chrome()
                    q, d = _inbox(key), _delivered(key)
                    for m in q[seen_q:]:
                        print(f"{C['yel']}✉ you: {m}{C['r']} {C['dim']}(queued — delivered when it wraps up){C['r']}")
                    if d > seen_d:
                        print(f"{C['yel']}{C['b']}✉ delivered {d - seen_d} message(s){C['r']}\n")
                        screen.set_status("type a message for this agent · Enter to send")
                    seen_q, seen_d = len(q), max(seen_d, d)
                    r = _lines(key, ".relay")
                    for m in r[seen_r:]:
                        print(f"{C['yel']}✉ you: {m}{C['r']} {C['dim']}(relaying via Claude){C['r']}")
                        to_relay.append(m)
                    seen_r = len(r)
                    if to_relay and parent and time.time() - relay_try > 0.5:
                        relay_try = time.time()
                        relay_wait_since = relay_wait_since or relay_try
                        box = _claude_input(parent)
                        if box == "empty":
                            m = to_relay[0]
                            if _type_into(parent, _relay_prompt(agent_id, args.get("title"), m)):
                                to_relay.popleft()
                                print(f"{C['yel']}{C['b']}✉ handed to Claude{C['r']} "
                                      f"{C['dim']}(it lands at the agent's next tool call){C['r']}\n")
                            else:
                                print(f"{C['red']}✉ typed into Claude's input but couldn't send it; "
                                      f"check that pane{C['r']}")
                                to_relay.popleft()
                            relay_wait_since, relay_note = 0.0, ""
                            screen.set_status(RELAY_STATUS)
                        elif time.time() - relay_wait_since > 2:
                            note = ("✉ waiting: Claude's input box has a draft" if box == "draft"
                                    else "✉ waiting for Claude's input box")
                            if note != relay_note:
                                relay_note = note
                                screen.set_status(note)
                    sys.stdout.flush()
                    if os.path.exists(done_file):
                        idle_since = idle_since or time.time()
                        if time.time() - idle_since > 1.0:
                            break
                    if not shape_checked and _read(shape_file):
                        shape_checked = True
                        if _read(shape_file) == "background":
                            if _relay_on(key) and parent:
                                screen.set_status(RELAY_STATUS)
                            else:
                                screen.input_enabled = False
                                screen.set_status(BACKGROUND_NOTE)
                    now = time.time()
                    if now - last_check > 3:
                        last_check = now
                        # backstop: Claude Code marks the agent finished in its meta file
                        if _meta_now(args.get("transcript"), agent_id).get("finished") and now - last_data > 2:
                            break
                        if parent and not _alive(parent):
                            print(f"\n{C['dim']}Claude pane closed; stopping.{C['r']}")
                            linger = min(linger, 2) if linger >= 0 else 2
                            break
                        if now - last_data > max_idle:
                            print(f"\n{C['dim']}No activity for 30 min; stopping.{C['r']}")
                            break
                    screen.poll(0.15)

        stats.t_end = time.time()
        if my_pane:
            _set_state(my_pane, "finished")
        screen.footer = stats.variants()
        print(f"\n{C['green']}{C['b']}✓ {(args.get('title') or agent_type)} finished{C['r']}")
        pending = len(_inbox(key)) - _delivered(key)
        if path:
            pending += len(to_relay)
        if pending > 0:
            print(f"{C['red']}✉ {pending} message(s) arrived too late to deliver{C['r']}")
        sys.stdout.flush()
        for suffix in (".done", ".pane", ".inbox", ".delivered", ".shape", ".relay", ".norelay"):
            try:
                os.remove(os.path.join(STATE_DIR, key + suffix))
            except OSError:
                pass

        screen.finished = True
        if linger < 0:
            if screen.active:
                screen.set_status("finished · press Enter to close")
                while not screen.enter:
                    screen.poll(0.5)
            else:
                try:
                    input(f"{C['dim']}press Enter to close{C['r']}")
                except EOFError:
                    pass
        else:
            end = time.time() + linger
            while time.time() < end:
                screen.set_status(f"finished · closing in {int(end - time.time()) + 1}s")
                screen.poll(min(1.0, max(end - time.time(), 0.05)))
    finally:
        screen.close()

    if parent:  # once this pane is gone, re-even the column (runs in the tmux server)
        cmd = " ".join(shlex.quote(x) for x in [sys.executable, SELF, "relayout", parent])
        subprocess.run(["tmux", "run-shell", "-b", f"sleep 0.4; {cmd}"], capture_output=True)


# ───────────────────────────── main ─────────────────────────────────

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if cmd == "start":
            cmd_start()
        elif cmd == "send":
            cmd_send(sys.argv[2])
        elif cmd == "post-agent":
            cmd_post_agent(json.load(sys.stdin))
        elif cmd == "place":
            cmd_place()
        elif cmd == "stop":
            cmd_stop()
        elif cmd == "relayout":
            with _Lock("tmux"):
                _relayout(sys.argv[2])
        elif cmd == "view":
            cmd_view(sys.argv[2:])
        else:
            print(__doc__)
    except Exception as e:  # a hook must never break the session
        if cmd in ("start", "stop", "relayout", "place", "send", "post-agent"):
            print(f"cc_micro_manage: {e}", file=sys.stderr)
            sys.exit(0)
        raise


if __name__ == "__main__":
    main()
