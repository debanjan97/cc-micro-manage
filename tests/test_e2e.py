"""End-to-end tests: the real plugin in a real (private) tmux server.

Each test starts its own tmux server on a temporary socket. Pane %0 runs
fake_claude.py, which draws an input box like Claude Code's and logs whatever is
submitted to it. The tests then drive the hook script exactly as Claude Code
does (same subcommands, same JSON payloads on stdin, TMUX/TMUX_PANE set) and
write subagent transcripts and meta files the way Claude Code lays them out.

Needs tmux >= 3.0 and Python 3.9+. No Claude account or network.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "cc_micro_manage.py")
FAKE_CLAUDE = os.path.join(ROOT, "tests", "fake_claude.py")
SESSION = "11111111-2222-3333-4444-555555555555"


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant(*blocks, model="claude-haiku-5-5", out=40, ctx=12000):
    return {"type": "assistant", "effort": "medium", "message": {
        "role": "assistant", "model": model, "content": list(blocks),
        "usage": {"input_tokens": 10, "cache_read_input_tokens": ctx - 10,
                  "cache_creation_input_tokens": 0, "output_tokens": out}}}


def text(t):
    return {"type": "text", "text": t}


def tool_use(name, inp, i="toolu_1"):
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


def tool_result(content, error=False, i="toolu_1"):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": i, "content": content, "is_error": error}]}}


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class E2E(unittest.TestCase):
    # extra env for the hooks (what Claude Code would pass); per test
    options = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccmm-e2e-")
        self.sock = os.path.join(self.tmp, "tmux.sock")
        self.state = os.path.join(self.tmp, "state")
        self.claude_log = os.path.join(self.tmp, "claude-input.log")
        self.project = os.path.join(self.tmp, "projects", "-repo")
        self.transcript = os.path.join(self.project, SESSION + ".jsonl")
        self.subagents = os.path.join(self.project, SESSION, "subagents")
        os.makedirs(self.subagents)
        open(self.transcript, "w").close()

        # the tmux server's environment is what the panes (viewers) see
        server_env = self._clean_env()
        server_env["CC_MICRO_MANAGE_STATE_DIR"] = self.state
        subprocess.run(["tmux", "-S", self.sock, "-f", "/dev/null", "new-session", "-d", "-s", "t",
                        "-x", "200", "-y", "50",
                        f"{sys.executable} {FAKE_CLAUDE} {self.claude_log}"],
                       env=server_env, check=True)
        self.claude = self.tmux("display-message", "-p", "-t", "t", "#{pane_id}")
        pid = self.tmux("display-message", "-p", "#{pid}")
        self.tmux_env = f"{self.sock},{pid},0"
        self.wait_for(lambda: "❯" in self.capture(self.claude), what="fake Claude prompt")

    def tearDown(self):
        subprocess.run(["tmux", "-S", self.sock, "kill-server"], capture_output=True)
        time.sleep(0.2)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _clean_env():
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CC_MICRO_MANAGE_", "CLAUDE_PLUGIN_OPTION_", "TMUX"))}
        return env

    def tmux(self, *args):
        r = subprocess.run(["tmux", "-S", self.sock, *args], capture_output=True, text=True)
        return r.stdout.strip()

    def capture(self, pane):
        return self.tmux("capture-pane", "-p", "-J", "-S", "-200", "-t", pane)

    def hook(self, cmd, payload, **extra):
        """Run the hook script like Claude Code does."""
        env = self._clean_env()
        env.update(TMUX=self.tmux_env, TMUX_PANE=self.claude, CC_MICRO_MANAGE_STATE_DIR=self.state,
                   CLAUDE_PLUGIN_OPTION_LINGER="1")
        env.update(self.options)
        env.update(extra)
        r = subprocess.run([sys.executable, SCRIPT, cmd], input=json.dumps(payload),
                           capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def wait_for(self, fn, timeout=10, what="condition"):
        end = time.time() + timeout
        last = None
        while time.time() < end:
            last = fn()
            if last:
                return last
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what} (last: {last!r})")

    def wait_for_screen(self, pane, *needles, timeout=10):
        """Wait until every needle is on the pane's screen; on timeout, name the missing ones."""
        end = time.time() + timeout
        while True:
            screen = self.capture(pane)
            missing = [n for n in needles if n not in screen]
            if not missing:
                return screen
            if time.time() > end:
                self.fail(f"not on screen: {missing}\n--- screen ---\n{screen}")
            time.sleep(0.1)

    def agent_path(self, aid):
        return os.path.join(self.subagents, f"agent-{aid}.jsonl")

    def write(self, aid, *records):
        with open(self.agent_path(aid), "a") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def start(self, aid, description, agent_type="general-purpose", parent=None,
              shape="foreground", task="Do the thing", **extra):
        meta = {"agentType": agent_type, "description": description,
                "spawnDepth": 1, "requestShape": shape}
        if parent:
            meta.update(parentAgentId=parent, spawnDepth=2)
        with open(os.path.join(self.subagents, f"agent-{aid}.meta.json"), "w") as f:
            json.dump(meta, f)
        self.write(aid, user(task))
        self.hook("start", {"hook_event_name": "SubagentStart", "agent_id": aid, "agent_type": agent_type,
                            "session_id": SESSION, "transcript_path": self.transcript, "cwd": self.tmp},
                  **extra)

    def stop(self, aid, agent_type="general-purpose"):
        return self.hook("stop", {"hook_event_name": "SubagentStop", "agent_id": aid, "agent_type": agent_type,
                                  "session_id": SESSION, "transcript_path": self.transcript,
                                  "agent_transcript_path": self.agent_path(aid), "stop_hook_active": False})

    def finish(self, aid):
        with open(os.path.join(self.subagents, f"agent-{aid}.meta.json")) as f:
            meta = json.load(f)
        meta["finished"] = True
        with open(os.path.join(self.subagents, f"agent-{aid}.meta.json"), "w") as f:
            json.dump(meta, f)

    def panes(self):
        """{agent_id: {pane, left, top, width, title, color}} plus 'claude'."""
        out = {}
        fmt = "#{pane_id}|#{pane_left}|#{pane_top}|#{pane_width}|#{@cc_micro_manage_agent}|#{@ccmm_title}|#{@ccmm_color}"
        for line in self.tmux("list-panes", "-a", "-F", fmt).splitlines():
            pid, left, top, width, agent, title, color = line.split("|")
            key = agent or ("claude" if pid == self.claude else pid)
            out[key] = {"pane": pid, "left": int(left), "top": int(top), "width": int(width),
                        "title": title, "color": color}
        return out

    def pane_of(self, aid):
        return self.wait_for(lambda: self.panes().get(aid), what=f"pane for {aid}")["pane"]

    def claude_input(self):
        """What has been submitted to the fake Claude so far (None if nothing)."""
        try:
            with open(self.claude_log) as f:
                return f.read() or None
        except OSError:
            return None

    def type_in(self, pane, s):
        self.tmux("send-keys", "-t", pane, "-l", s)
        self.tmux("send-keys", "-t", pane, "Enter")

    # ── panes appear, render, and go away ───────────────────────────
    def test_pane_opens_beside_claude_with_title_and_render(self):
        self.start("a1", "Scout: describe files", task="List every file")
        pane = self.pane_of("a1")
        p = self.panes()
        self.assertGreater(p["a1"]["left"], p["claude"]["left"], "agent pane should be right of Claude")
        self.assertEqual(p["a1"]["title"], "Scout: describe files · a1")
        self.assertEqual(p["a1"]["color"], "yellow")

        self.write("a1", assistant(text("Found **three** files"), tool_use("Bash", {"command": "ls"})),
                   tool_result("a.go\nb.go"))
        self.wait_for_screen(pane, "▶ task", "List every file",
                             "Found three files",              # markdown rendered, ** stripped
                             "⏺ Bash(ls)", "a.go",
                             "Haiku 5.5", "12.0k")             # footer: model, context tokens

    def test_finish_closes_pane_and_cleans_state(self):
        self.start("a1", "Short job")
        pane = self.pane_of("a1")
        self.write("a1", assistant(text("done")))
        self.assertEqual(self.stop("a1"), "")                 # no messages -> no block
        self.wait_for(lambda: "finished" in self.capture(pane), what="finished banner")
        self.assertEqual(self.panes()["a1"]["color"], "green")
        self.wait_for(lambda: "a1" not in self.panes(), what="pane to close")
        self.wait_for(lambda: not [f for f in os.listdir(self.state) if f.startswith("a1.")],
                      what="state files removed")

    def test_meta_finished_backstop_closes_background_agent(self):
        # background agents ignore SubagentStop; the viewer watches meta.finished
        self.start("bg", "Background job", shape="background")
        self.pane_of("bg")
        self.write("bg", assistant(text("all done")))
        self.finish("bg")
        self.wait_for(lambda: "bg" not in self.panes(), timeout=15, what="background pane to close")

    def test_error_turns_dot_red_then_back(self):
        self.start("a1", "Flaky")
        self.pane_of("a1")
        self.write("a1", assistant(tool_use("Bash", {"command": "false"})), tool_result("boom", error=True))
        self.wait_for(lambda: self.panes()["a1"]["color"] == "red", what="red dot")
        self.write("a1", assistant(tool_use("Bash", {"command": "true"}, i="t2")), tool_result("ok", i="t2"))
        self.wait_for(lambda: self.panes()["a1"]["color"] == "yellow", what="yellow dot again")

    # ── layout ──────────────────────────────────────────────────────
    def test_fifo_column(self):
        for aid in ("a1", "a2", "a3"):
            self.start(aid, f"job {aid}")
            self.pane_of(aid)
        p = self.panes()
        lefts = {p[a]["left"] for a in ("a1", "a2", "a3")}
        self.assertEqual(len(lefts), 1, "agents should share one column")
        self.assertLess(p["a1"]["top"], p["a2"]["top"])
        self.assertLess(p["a2"]["top"], p["a3"]["top"])
        self.assertGreater(p["claude"]["width"], 200 * 0.5, "Claude keeps the main pane (55%)")

    def test_nested_agent_splits_its_parent(self):
        self.start("lead", "Lead")
        self.pane_of("lead")
        self.start("other", "Other")
        self.pane_of("other")
        self.start("kid", "Child", parent="lead")
        self.pane_of("kid")
        p = self.panes()
        self.assertTrue(p["kid"]["title"].startswith("↳ Child"), p["kid"]["title"])
        # the child sits inside the lead's area, above "other"
        self.assertLess(p["kid"]["top"], p["other"]["top"])
        self.assertGreaterEqual(p["kid"]["left"], p["lead"]["left"])

    def test_window_layout(self):
        self.start("a1", "Windowed", CLAUDE_PLUGIN_OPTION_LAYOUT="window")
        self.pane_of("a1")
        self.assertIn("agents", self.tmux("list-windows", "-F", "#{window_name}").split())

    def test_borders_on_while_agents_run_then_off(self):
        self.start("a1", "Bordered")
        self.pane_of("a1")
        self.assertEqual(self.tmux("show-options", "-wv", "-t", self.claude, "pane-border-status"), "top")
        self.stop("a1")
        self.wait_for(lambda: "a1" not in self.panes(), what="pane to close")
        self.wait_for(lambda: self.tmux("show-options", "-wv", "-t", self.claude,
                                        "pane-border-status") in ("", "off"), what="borders off")

    # ── settings ────────────────────────────────────────────────────
    def test_skip_setting(self):
        self.start("ex", "Explorer", agent_type="Explore", CLAUDE_PLUGIN_OPTION_SKIP="Explore,statusline-setup")
        self.start("gp", "General")
        self.pane_of("gp")
        time.sleep(1)
        self.assertNotIn("ex", self.panes())

    def test_env_var_overrides_plugin_option(self):
        self.start("ex", "Explorer", agent_type="Explore",
                   CLAUDE_PLUGIN_OPTION_SKIP="", CC_MICRO_MANAGE_SKIP="Explore")
        time.sleep(1.5)
        self.assertNotIn("ex", self.panes())

    def test_shortcut_setting_binds_key(self):
        self.start("a1", "Bound", CLAUDE_PLUGIN_OPTION_MESSAGE_SHORTCUT="g")
        self.pane_of("a1")
        self.assertIn("cc_micro_manage", self.tmux("list-keys", "-T", "prefix", "g"))

    def test_no_tmux_does_nothing(self):
        env_tmux, self.tmux_env = self.tmux_env, ""
        try:
            self.start("a1", "Outside tmux", TMUX="")
        finally:
            self.tmux_env = env_tmux
        time.sleep(1)
        self.assertNotIn("a1", self.panes())

    # ── messaging ───────────────────────────────────────────────────
    def test_foreground_message_delivered_at_stop(self):
        self.start("fg", "Foreground")
        pane = self.pane_of("fg")
        self.wait_for(lambda: "✉ ›" in self.capture(pane), what="message box")
        self.type_in(pane, "also check the $(refresh) path")
        self.wait_for(lambda: os.path.exists(os.path.join(self.state, "fg.inbox")), what="queued message")

        out = json.loads(self.stop("fg"))
        self.assertEqual(out["decision"], "block")
        self.assertIn("also check the $(refresh) path", out["reason"])
        self.wait_for(lambda: "delivered 1 message" in self.capture(pane), what="delivered note")

        self.assertEqual(self.stop("fg"), "", "second stop lets it finish")
        self.wait_for(lambda: "fg" not in self.panes(), what="pane to close")

    def test_background_message_relayed_through_claude(self):
        self.start("bg", "Counter", shape="background")
        pane = self.pane_of("bg")
        self.wait_for(lambda: "✉ ›" in self.capture(pane), what="message box")
        self.type_in(pane, 'add "quoted" #{x} to each step')
        line = self.wait_for(self.claude_input,
                             what="relay typed into Claude")
        self.assertIn("[cc-micro-manage]", line)
        self.assertIn("SendMessage", line)
        self.assertIn("bg (Counter)", line)
        self.assertIn('add "quoted" #{x} to each step', line)
        self.wait_for(lambda: "handed to Claude" in self.capture(pane), what="handed-to-Claude note")

        # Claude Code records the forwarded message in the agent's transcript
        self.write("bg", {"type": "attachment", "attachment": {
            "type": "queued_command", "prompt": 'add "quoted" #{x} to each step',
            "origin": {"kind": "coordinator"}, "isMeta": True}})
        self.wait_for(lambda: "received from Claude" in self.capture(pane), what="received note")

    def test_relay_waits_for_a_draft_and_never_clobbers_it(self):
        self.start("bg", "Counter", shape="background")
        pane = self.pane_of("bg")
        self.wait_for(lambda: "✉ ›" in self.capture(pane), what="message box")
        self.tmux("send-keys", "-t", self.claude, "-l", "my half-typed draft")
        self.type_in(pane, "steer me")
        self.wait_for(lambda: "has a draft" in self.capture(pane), what="waiting-for-draft status")
        self.assertFalse(os.path.exists(self.claude_log), "nothing may be submitted over a draft")

        self.tmux("send-keys", "-t", self.claude, "C-u")      # you clear your draft
        line = self.wait_for(self.claude_input,
                             what="relay after draft cleared")
        self.assertIn("steer me", line)
        self.assertNotIn("half-typed", line)

    def test_relay_off_makes_background_pane_read_only(self):
        self.start("bg", "Counter", shape="background", CLAUDE_PLUGIN_OPTION_RELAY="false")
        pane = self.pane_of("bg")
        self.wait_for(lambda: "read-only" in self.capture(pane), what="read-only status")
        self.type_in(pane, "ignored")
        time.sleep(1.5)
        self.assertFalse(os.path.exists(self.claude_log))


if __name__ == "__main__":
    unittest.main(verbosity=2)
