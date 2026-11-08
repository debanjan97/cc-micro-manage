"""Unit tests for the parts with no tmux or terminal: settings, parsing Claude's
input box, rendering, the footer, and message queueing.

    python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from ccmm import messaging, relay, render, settings, state  # noqa: E402
from ccmm.placement import describe  # noqa: E402
from ccmm.stats import Stats, pretty_model  # noqa: E402

DIM, RESET = "\x1b[2m", "\x1b[0m"
RULE = "─" * 40


def plain(text):
    return relay.strip_ansi(text)


class SettingsTest(unittest.TestCase):
    def env(self, **values):
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(("CC_MICRO_MANAGE_", "CLAUDE_PLUGIN_OPTION_"))}
        clean.update(values)
        return mock.patch.dict(os.environ, clean, clear=True)

    def test_defaults(self):
        with self.env():
            self.assertEqual(settings.layout(), "split")
            self.assertEqual(settings.linger(), "10")
            self.assertEqual(settings.shortcut(), "a")
            self.assertTrue(settings.relay_enabled())
            self.assertTrue(settings.borders_enabled())
            self.assertEqual(settings.skipped_agent_types(), set())

    def test_plugin_option_used_when_no_env_var(self):
        with self.env(CLAUDE_PLUGIN_OPTION_LAYOUT="window", CLAUDE_PLUGIN_OPTION_RELAY="false",
                      CLAUDE_PLUGIN_OPTION_MESSAGE_SHORTCUT="g", CLAUDE_PLUGIN_OPTION_SKIP=" Explore , x "):
            self.assertEqual(settings.layout(), "window")
            self.assertFalse(settings.relay_enabled())
            self.assertEqual(settings.shortcut(), "g")
            self.assertEqual(settings.skipped_agent_types(), {"Explore", "x"})

    def test_env_var_wins_over_plugin_option(self):
        with self.env(CLAUDE_PLUGIN_OPTION_LINGER="3", CC_MICRO_MANAGE_LINGER="7",
                      CLAUDE_PLUGIN_OPTION_RELAY="false", CC_MICRO_MANAGE_RELAY="1"):
            self.assertEqual(settings.linger(), "7")
            self.assertTrue(settings.relay_enabled())

    def test_blank_env_var_falls_through(self):
        with self.env(CC_MICRO_MANAGE_LAYOUT=" ", CLAUDE_PLUGIN_OPTION_LAYOUT="window"):
            self.assertEqual(settings.layout(), "window")

    def test_off_values_and_bad_layout(self):
        for off in ("0", "false", "No", "OFF"):
            with self.env(CC_MICRO_MANAGE_BORDERS=off):
                self.assertFalse(settings.borders_enabled(), off)
        with self.env(CC_MICRO_MANAGE_LAYOUT="diagonal"):
            self.assertEqual(settings.layout(), "split")
        with self.env(CC_MICRO_MANAGE_SHORTCUT="none"):
            self.assertIsNone(settings.shortcut())


class InputBoxTest(unittest.TestCase):
    def test_empty_box(self):
        self.assertEqual(relay.input_box_state(["out", RULE, "\x1b[39m❯ ", RULE]), relay.EMPTY)

    def test_dim_placeholder_counts_as_empty(self):
        line = f"\x1b[39m❯ {DIM}Try \"create a util logging.py that...\""
        self.assertEqual(relay.input_box_state(["out", RULE, line, RULE]), relay.EMPTY)

    def test_dim_suggestion_with_reset_counts_as_empty(self):
        line = f"\x1b[39m❯ \x1b[2;3myes, replace the Verify stub{RESET}"
        self.assertEqual(relay.input_box_state([RULE, line]), relay.EMPTY)

    def test_typed_text_is_a_draft(self):
        self.assertEqual(relay.input_box_state([RULE, "\x1b[39m❯ hello draft"]), relay.DRAFT)

    def test_no_box_while_a_dialog_is_open(self):
        screen = ["❯ an old prompt in the scrollback", "output", "Do you want to proceed?", "❯ 1. Yes"]
        self.assertIsNone(relay.input_box_state(screen))

    def test_uses_the_last_box_on_screen(self):
        screen = [RULE, "❯ earlier", "lots of output", RULE, "❯ "]
        self.assertEqual(relay.input_box_state(screen), relay.EMPTY)

    def test_request_text(self):
        text = relay.request("a1b2", "Scout: files", "also\n check   the $(cwd)")
        self.assertIn("background subagent a1b2 (Scout: files)", text)
        self.assertIn("SendMessage", text)
        self.assertTrue(text.endswith("<<< also check the $(cwd) >>>"), "the message, flattened and marked off")
        self.assertEqual(text.rsplit("<<<", 1)[1], " also check the $(cwd) >>>", "only the message is marked off")
        self.assertNotIn("\n", text)


class RenderTest(unittest.TestCase):
    def test_markdown(self):
        out = plain(render.markdown("# Title\n**bold** and `code`\n- item\n1. first\n> quoted\n---\n"
                                    "see [docs](https://x.y)"))
        self.assertEqual(out.splitlines(), ["Title", "bold and code", "• item", "1. first", "▎ quoted",
                                            "─" * 30, "see docs"])

    def test_code_fence_and_max_lines(self):
        out = plain(render.markdown("```go\nfunc main() {}\n```\nmore\nlines", max_lines=3))
        self.assertEqual(out.splitlines(), ["┌─ go", "│ func main() {}", "└─", "… +2 lines"])

    def test_snake_case_is_not_italic(self):
        self.assertEqual(plain(render.markdown("my_var_name and 2*3*4")), "my_var_name and 2*3*4")

    def test_task_preview(self):
        long_task = "word " * 200
        self.assertLessEqual(len(render.task_preview(long_task, first=True)), 240)
        self.assertTrue(render.task_preview(long_task, first=True).endswith("…"))
        self.assertEqual(render.task_preview("short\n  task", first=True), "short task")

    def test_tool_summary(self):
        self.assertEqual(render.tool_summary({"command": "ls\n-la", "description": "list"}), "ls -la")
        self.assertEqual(render.tool_summary({"file_path": "/a/b.go"}), "/a/b.go")
        self.assertEqual(render.tool_summary({"other": 1}), '{"other": 1}')

    def test_agent_state(self):
        ok = json.dumps({"message": {"content": [{"type": "tool_result", "content": "x"}]}})
        bad = json.dumps({"message": {"content": [{"type": "tool_result", "is_error": True}]}})
        text = json.dumps({"message": {"content": [{"type": "text", "text": "hi"}]}})
        self.assertEqual(render.agent_state(ok), "running")
        self.assertEqual(render.agent_state(bad), "error")
        self.assertIsNone(render.agent_state(text))
        self.assertIsNone(render.agent_state("not json"))

    def rendered(self, record, first=False):
        out = io.StringIO()
        with redirect_stdout(out):
            render.render_record(json.dumps(record), first)
        return plain(out.getvalue())

    def test_render_task_text_and_tools(self):
        self.assertIn("▶ task Describe", self.rendered({"type": "user", "message": {"content": "Describe"}}, True))
        out = self.rendered({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Found **it**"},
            {"type": "thinking", "thinking": "hmm\nok"},
            {"type": "tool_use", "name": "Grep", "input": {"pattern": "TODO"}}]}})
        self.assertIn("Found it", out)
        self.assertIn("✻ hmm ok", out)
        self.assertIn("⏺ Grep(TODO)", out)

    def test_render_tool_result_is_capped(self):
        out = self.rendered({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "1\n2\n3\n4\n5"}]}})
        self.assertIn("⎿ 1", out)
        self.assertIn("+2 lines", out)
        self.assertNotIn("4", out.replace("+2 lines", ""))

    def test_render_skips_reminders_meta_and_attachments(self):
        reminder = {"type": "user", "message": {"content": "<system-reminder>secret</system-reminder>"}}
        self.assertEqual(self.rendered(reminder), "")
        self.assertEqual(self.rendered({"type": "user", "isMeta": True, "message": {"content": "x"}}), "")
        self.assertEqual(self.rendered({"type": "attachment", "attachment": {"type": "other"}}), "")

    def test_render_forwarded_message_and_handback(self):
        out = self.rendered({"type": "attachment", "attachment": {
            "type": "queued_command", "prompt": "also X", "origin": {"kind": "coordinator"}}})
        self.assertIn("✉ received from Claude: also X", out)
        out = self.rendered({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "SubagentHandback", "input": {"message": "# Done\nall good"}}]}})
        self.assertIn("⏎ report to caller", out)
        self.assertIn("all good", out)


class StatsTest(unittest.TestCase):
    def test_footers(self):
        stats = Stats()
        for out in (1500, 2600):
            stats.update(json.dumps({"type": "assistant", "effort": "medium", "message": {
                "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 59000,
                          "cache_creation_input_tokens": 300, "output_tokens": out}}}))
        full, medium, tiny = stats.footers()
        self.assertEqual(full, "Haiku 5.5 · medium effort · 59.3k context · 4.1k out")
        self.assertEqual(medium, "Haiku 5.5 · 59.3k ctx · 4.1k out")
        self.assertEqual(tiny, "59.3k")

    def test_elapsed(self):
        stats = Stats()
        stats.started, stats.ended = 100.0, 175.0
        self.assertEqual(stats.elapsed(), "1m 15s")
        stats.ended = 108.0
        self.assertEqual(stats.elapsed(), "8s")

    def test_pretty_model(self):
        self.assertEqual(pretty_model("claude-opus-5-5"), "Opus 5.5")
        self.assertEqual(pretty_model("claude-sonnet-4-5-20250929"), "Sonnet 4.5")
        self.assertEqual(pretty_model("something-else"), "something-else")


class DescribeTest(unittest.TestCase):
    def test_purpose_title_and_nesting(self):
        purpose, title = describe("general-purpose", "abcdef123", {"description": "Scout:\n  files",
                                                                   "spawnDepth": 2})
        self.assertEqual(purpose, "Scout: files")
        self.assertEqual(title, "↳ Scout: files · abcdef")

    def test_falls_back_to_agent_type(self):
        self.assertEqual(describe("Explore", "abcdef123", {}), ("", "Explore · abcdef"))


class MessagingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ccmm-unit-")
        patcher = mock.patch.object(state, "STATE_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_foreground_messages_go_to_the_inbox_and_out_at_stop(self):
        agent = state.Agent("fg1")
        self.assertEqual(messaging.queue(agent, "first"), (None, "inbox"))
        messaging.queue(agent, "second")
        reply = messaging.stop_decision(agent, background=False)
        self.assertEqual(reply["decision"], "block")
        self.assertIn("2 message(s):\n- first\n- second", reply["reason"])
        self.assertIsNone(messaging.stop_decision(agent, background=False), "nothing new the second time")

    def test_background_messages_go_to_the_relay(self):
        agent = state.Agent("bg1")
        agent.shape = "background"
        self.assertEqual(messaging.queue(agent, "steer"), (None, "relay"))
        self.assertEqual(agent.relay_queue, ["steer"])
        self.assertEqual(agent.inbox, [])

    def test_relay_turned_off(self):
        agent = state.Agent("bg2")
        agent.shape = "background"
        agent.forbid_relay()
        self.assertEqual(messaging.queue(agent, "x"), (messaging.RELAY_OFF, None))

    def test_finished_agent_refuses_messages(self):
        agent = state.Agent("done1")
        agent.mark_done("/t.jsonl")
        self.assertEqual(messaging.queue(agent, "late"), (messaging.ALREADY_FINISHED, None))

    def test_unsafe_agent_ids_stay_inside_the_state_dir(self):
        agent = state.Agent("../../etc/passwd")
        self.assertEqual(os.path.dirname(agent.file(".pane")), self.dir)

    def test_stop_never_blocks_background_agents(self):
        agent = state.Agent("bg3")
        messaging.queue(agent, "x")
        self.assertIsNone(messaging.stop_decision(agent, background=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
