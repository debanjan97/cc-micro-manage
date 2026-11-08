"""Small per-agent files in the state directory.

The hook (Claude's environment) and the panes (the tmux server's environment)
share nothing but this directory. It's a fixed path because $TMPDIR can differ
between the two on macOS.

Per agent, keyed by a filesystem-safe agent id:
  <key>.pane       the agent's tmux pane
  <key>.shape      "foreground" or "background" (how Claude launched it)
  <key>.norelay    present when messages can't be relayed to a background agent (holds why)
  <key>.inbox      messages you typed for a foreground agent (JSON lines)
  <key>.delivered  how many inbox messages were handed over at SubagentStop
  <key>.relay      messages you typed for a background agent (JSON lines)
  <key>.done       written at SubagentStop; holds the agent's transcript path
"""
import glob
import json
import os
import re
import shutil
import time

STATE_DIR = os.environ.get("CC_MICRO_MANAGE_STATE_DIR") or os.path.join("/tmp", "cc-micro-manage-%d" % os.getuid())

AGENT_FILES = (".done", ".pane", ".inbox", ".delivered", ".shape", ".relay", ".norelay")
STALE_AFTER = 24 * 60 * 60


def ensure_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def path(name):
    return os.path.join(STATE_DIR, name)


def safe_key(agent_id):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", agent_id or "")


def read(file_path):
    try:
        with open(file_path) as f:
            return f.read().strip()
    except OSError:
        return ""


def write(file_path, text):
    with open(file_path, "w") as f:
        f.write(text)


def remove(file_path):
    try:
        os.remove(file_path)
    except OSError:
        pass


def prune_stale():
    """Drop state left by agents that never got (or outlived) a pane."""
    cutoff = time.time() - STALE_AFTER
    for f in glob.glob(path("*")):
        try:
            if os.path.isfile(f) and os.path.getmtime(f) < cutoff and not f.endswith(".jsonl"):
                os.remove(f)
        except OSError:
            pass


class Agent:
    """The state files of one agent."""

    def __init__(self, agent_id):
        self.key = safe_key(agent_id)

    def file(self, suffix):
        return path(self.key + suffix)

    # pane and shape
    @property
    def pane(self):
        return read(self.file(".pane"))

    @pane.setter
    def pane(self, pane_id):
        write(self.file(".pane"), pane_id)

    @property
    def shape(self):
        return read(self.file(".shape"))

    @shape.setter
    def shape(self, value):
        write(self.file(".shape"), value)

    @property
    def is_background(self):
        return self.shape == "background"

    @property
    def relay_allowed(self):
        return not os.path.exists(self.file(".norelay"))

    def forbid_relay(self, reason=""):
        write(self.file(".norelay"), reason or "1")

    @property
    def relay_off_reason(self):
        """Why a background pane is read-only, if placement said."""
        reason = read(self.file(".norelay"))
        return "" if reason == "1" else reason

    # lifecycle
    @property
    def is_done(self):
        return os.path.exists(self.file(".done"))

    def mark_done(self, transcript_path):
        write(self.file(".done"), transcript_path or "")

    @property
    def transcript_hint(self):
        """The transcript path SubagentStop passed on, if any."""
        return read(self.file(".done"))

    def clear_done(self):
        remove(self.file(".done"))

    def remove_all(self):
        for suffix in AGENT_FILES:
            remove(self.file(suffix))

    # messages you typed
    def append_message(self, box, text):
        with open(self.file(box), "a") as f:
            f.write(json.dumps({"ts": time.time(), "text": text}) + "\n")

    def messages(self, box):
        out = []
        try:
            with open(self.file(box)) as f:
                for line in f:
                    try:
                        out.append(json.loads(line)["text"])
                    except (ValueError, KeyError):
                        pass
        except OSError:
            pass
        return out

    @property
    def inbox(self):
        return self.messages(".inbox")

    @property
    def relay_queue(self):
        return self.messages(".relay")

    @property
    def delivered(self):
        try:
            return int(read(self.file(".delivered")) or 0)
        except ValueError:
            return 0

    @delivered.setter
    def delivered(self, count):
        write(self.file(".delivered"), str(count))

    @property
    def undelivered(self):
        return self.inbox[self.delivered:]


def forget_dead_panes(is_alive):
    """Drop the state of agents whose pane is gone (tmux reuses pane ids)."""
    for pane_file in glob.glob(path("*.pane")):
        if not is_alive(read(pane_file)):
            stem = pane_file[: -len(".pane")]
            for suffix in (".pane", ".inbox", ".delivered", ".done"):
                remove(stem + suffix)


class Lock:
    """Portable mkdir lock (flock isn't on stock macOS)."""

    TIMEOUT = 5

    def __init__(self, name):
        self.path = path(name + ".lock")

    def __enter__(self):
        deadline = time.time() + self.TIMEOUT
        while True:
            try:
                os.mkdir(self.path)
                return self
            except FileExistsError:
                if time.time() > deadline:  # stale lock
                    shutil.rmtree(self.path, ignore_errors=True)
                    deadline = time.time() + self.TIMEOUT
                time.sleep(0.05)

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)
