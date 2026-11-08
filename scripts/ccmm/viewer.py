"""The program running in each agent pane: follows the agent's transcript,
shows your messages and their delivery, and closes when the agent is done."""
import os
import subprocess
import sys
import time

from . import claude_files, ghostty, layout, messaging, panes, program, state
from .relay import Relay
from . import render
from .render import agent_state, render_record
from .text import paint
from .screen import DEFAULT_STATUS, Screen
from .stats import Stats

FIND_TRANSCRIPT_FOR = 60      # seconds
CHECK_EVERY = 3               # seconds between the slower liveness checks
QUIET_AFTER_DONE = 1.0        # seconds of no new lines after SubagentStop before closing
FINISHED_GRACE = 2            # seconds of no new lines before trusting meta.finished
GIVE_UP_IDLE = 30 * 60        # seconds with no activity at all
CLAUDE_GONE_LINGER = 2
DEFAULT_LINGER = 10.0
RELAY_STATUS = ""             # background panes: a plain rule, no hint


def parse_args(argv):
    """--agent-id X --agent-type Y ... -> {"agent_id": "X", "agent_type": "Y", ...}"""
    args, items = {}, iter(argv)
    for item in items:
        if item.startswith("--"):
            args[item[2:].replace("-", "_")] = next(items, "")
    return args


def view_command(agent_id, agent_type, session_id, session_transcript, linger, claude_pane, purpose):
    """The command a new agent pane runs. Panes don't inherit Claude's
    environment, so settings they need travel as arguments."""
    return program.shell_command(
        "view", "--agent-id", agent_id, "--agent-type", agent_type, "--session", session_id or "",
        "--transcript", session_transcript or "", "--linger", linger, "--parent-pane", claude_pane,
        "--title", purpose)


def _linger_seconds(text):
    try:
        return float(text or DEFAULT_LINGER)
    except ValueError:
        return DEFAULT_LINGER


class Viewer:
    def __init__(self, args):
        self.agent_id = args.get("agent_id", "")
        self.agent_type = args.get("agent_type", "")
        self.purpose = args.get("title") or ""
        self.session_id = args.get("session")
        self.session_transcript = args.get("transcript")
        self.claude_pane = args.get("parent_pane")
        self.linger = _linger_seconds(args.get("linger"))
        self.agent = state.Agent(self.agent_id)
        self.my_pane = os.environ.get("TMUX_PANE")
        self.stats = Stats()
        self.relay = Relay(self.claude_pane, self.agent_id, self.purpose)
        self.screen = Screen(self._on_send)
        self.shown_state = "running"
        self.seen_inbox = self.seen_delivered = self.seen_relay = 0
        self.relay_note = ""
        self.shape_known = False

    # ── the whole life of a pane ───────────────────────────────────────
    def run(self):
        try:
            self._print_header()
            transcript = self._find_transcript()
            if transcript:
                self._follow(transcript)
            self._finish(followed=bool(transcript))
            self._linger()
        finally:
            self.screen.close()
        self._request_relayout()

    def _print_header(self):
        title = self.purpose or self.agent_type
        detail = " · ".join(p for p in (self.agent_type if self.purpose else "", self.agent_id[:8]) if p)
        render.header(title, detail)
        sys.stdout.flush()

    def _find_transcript(self):
        started = time.time()
        while True:
            for candidate in claude_files.transcript_candidates(self.agent_id, self.session_transcript,
                                                                self.session_id):
                if os.path.exists(candidate):
                    return candidate
            hinted = self.agent.transcript_hint if self.agent.is_done else ""
            if hinted and os.path.exists(hinted):
                return hinted
            if time.time() - started > FIND_TRANSCRIPT_FOR:
                render.note("Couldn't find this subagent's transcript.", "err")
                return None
            self.screen.poll(0.25)

    # ── following the transcript ───────────────────────────────────────
    def _follow(self, transcript):
        first, partial = True, ""
        last_data = last_check = time.time()
        last_footer = 0.0
        done_since = None
        with open(transcript) as f:
            while True:
                chunk = f.readline()
                if chunk:
                    partial += chunk
                    if partial.endswith("\n"):
                        self._on_record(partial, first)
                        first, partial = False, ""
                    done_since, last_data = None, time.time()
                    continue
                now = time.time()
                if now - last_footer >= 1:            # keep the elapsed time ticking
                    last_footer = now
                    self._refresh_footer()
                self._show_messages()
                self._relay_step(now)
                sys.stdout.flush()
                if self.agent.is_done:
                    done_since = done_since or now
                    if now - done_since > QUIET_AFTER_DONE:
                        return
                self._learn_shape()
                if now - last_check > CHECK_EVERY:
                    last_check = now
                    if self._should_stop(now - last_data):
                        return
                self.screen.poll(0.15)

    def _on_record(self, line, first):
        new_state = agent_state(line)
        if new_state and new_state != self.shown_state and self.my_pane:
            self.shown_state = new_state
            layout.show_state(self.my_pane, new_state)
        self.stats.update(line)
        self.screen.set_footer(self.stats.footers())
        render_record(line, first)

    def _refresh_footer(self):
        self.screen.set_footer(self.stats.footers())
        self.screen.redraw()

    def _should_stop(self, idle):
        """The slower checks for an agent that ends without a SubagentStop we can see."""
        if idle > FINISHED_GRACE and claude_files.read_meta(self.session_transcript, self.agent_id).get("finished"):
            return True     # Claude Code marks background agents finished in their meta file
        if self.claude_pane and not panes.is_alive(self.claude_pane):
            render.done_line("Claude pane closed; stopping")
            self.linger = min(self.linger, CLAUDE_GONE_LINGER) if self.linger >= 0 else CLAUDE_GONE_LINGER
            return True
        if idle > GIVE_UP_IDLE:
            render.done_line("No activity for 30 min; stopping")
            return True
        return False

    def _learn_shape(self):
        """Once placement records how the agent was launched, set up the message box."""
        if self.shape_known or not self.agent.shape:
            return
        self.shape_known = True
        if not self.agent.is_background:
            return
        if self.agent.relay_allowed and self.claude_pane:
            self.screen.set_status(RELAY_STATUS)
        else:
            self.screen.input_enabled = False
            self.screen.set_status(self.agent.relay_off_reason or messaging.RELAY_OFF)

    # ── your messages ──────────────────────────────────────────────────
    def _on_send(self, text):
        error, how = messaging.queue(self.agent, text)
        if error:
            self.screen.set_status(error)
        elif how == "relay":
            self.screen.set_status("relaying via Claude · type another")
        else:
            self.screen.set_status("queued · type another, or wait for it to wrap up")

    def _show_messages(self):
        inbox, delivered = self.agent.inbox, self.agent.delivered
        for text in inbox[self.seen_inbox:]:
            print()
            render.prompt(text)
            render.note("queued · delivered when it wraps up")
        if delivered > self.seen_delivered:
            render.note(f"delivered {delivered - self.seen_delivered} message(s)", "ok")
            self.screen.set_status(DEFAULT_STATUS)
        self.seen_inbox, self.seen_delivered = len(inbox), max(self.seen_delivered, delivered)

        to_relay = self.agent.relay_queue
        for text in to_relay[self.seen_relay:]:
            print()
            render.prompt(text)
            render.note("relaying via Claude")
            self.relay.add(text)
        self.seen_relay = len(to_relay)

    def _relay_step(self, now):
        event = self.relay.step(now)
        if event == "sent":
            render.note("handed to Claude · it lands at the agent's next tool call")
        elif event == "failed":
            render.note("typed into Claude's input but couldn't send it; check that pane", "err")
        if event in ("sent", "failed"):
            self.relay_note = ""
            self.screen.set_status(RELAY_STATUS)
        elif event in ("waiting", "waiting-draft"):
            note = ("waiting: Claude's input box has a draft" if event == "waiting-draft"
                    else "waiting for Claude's input box")
            if note != self.relay_note:
                self.relay_note = note
                self.screen.set_status(note)

    # ── the end ────────────────────────────────────────────────────────
    def _finish(self, followed):
        self.stats.ended = time.time()
        if self.my_pane:
            layout.show_state(self.my_pane, "finished")
        self.screen.set_footer(self.stats.footers())
        worked = self.stats.elapsed()
        render.done_line(f"{self.purpose or self.agent_type} finished" + (f" · worked for {worked}" if worked else ""))
        undelivered = len(self.agent.undelivered) + (len(self.relay.waiting) if followed else 0)
        if undelivered > 0:
            render.note(f"{undelivered} message(s) arrived too late to deliver", "err")
        sys.stdout.flush()
        self.agent.remove_all()

    def _linger(self):
        self.screen.finished = True
        if self.linger < 0:
            self._wait_for_enter()
            return
        end = time.time() + self.linger
        while time.time() < end:
            self.screen.set_status(f"finished · closing in {int(end - time.time()) + 1}s")
            self.screen.poll(min(1.0, max(end - time.time(), 0.05)))

    def _wait_for_enter(self):
        if self.screen.active:
            self.screen.set_status("finished · press Enter to close")
            while not self.screen.enter_pressed:
                self.screen.poll(0.5)
            return
        try:
            input(paint("press Enter to close", "grey"))
        except EOFError:
            pass

    def _request_relayout(self):
        """Once this pane is gone, re-even the column (tmux runs it after we exit)."""
        if not self.claude_pane:
            return
        if panes.is_tmux(self.claude_pane):
            command = program.shell_command("relayout", self.claude_pane)
            subprocess.run(["tmux", "run-shell", "-b", f"sleep 0.4; {command}"], capture_output=True)
            return
        # Ghostty leaves a split open when its command exits, so a helper that
        # outlives us closes it, then evens out the rest
        try:
            me = ghostty.terminal_for_tty(os.ttyname(sys.stdin.fileno()))
        except (OSError, ghostty.GhosttyError):
            me = ""
        subprocess.Popen(program.argv("ghostty-closed", me, self.claude_pane), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def main(argv):
    Viewer(parse_args(argv)).run()
