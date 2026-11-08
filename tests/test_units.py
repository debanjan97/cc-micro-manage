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

from ccmm import ghostty, ghostty_layout, messaging, panes, relay, render, settings, state, text  # noqa: E402
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
    def setUp(self):
        text.width_override = 60
        render._renderer = render.Renderer()

    def tearDown(self):
        text.width_override = None

    def md(self, source, **kw):
        return [plain(line) for line in render.markdown(source, **kw)]

    def test_markdown(self):
        self.assertEqual(self.md("# Title\n**bold** and `code`\n- item\n1. first\n> quoted\n---\n"
                                 "see [docs](https://x.y)"),
                         ["Title", "bold and code", "- item", "1. first", "▎ quoted", "─" * 30, "see docs"])

    def test_inline_code_uses_claude_codes_blue(self):
        self.assertIn("\x1b[38;5;153mcode", render.inline("`code`"))

    def test_code_fence_and_max_lines(self):
        self.assertEqual(self.md("```go\nfunc main() {}\n```\nmore\nlines\nhere", max_lines=3),
                         ["func main() {}", "more", "lines", "… +1 lines"])

    def test_table_is_box_drawn(self):
        out = self.md("| File | What |\n|---|---|\n| a.go | main |\n| bb.go | helper |")
        self.assertEqual(out[0], "┌───────┬────────┐")
        self.assertEqual(out[1], "│ File  │  What  │")
        self.assertEqual(out[3], "│ a.go  │ main   │")
        self.assertEqual(out[-1], "└───────┴────────┘")

    def test_too_wide_table_falls_back_to_rows(self):
        out = self.md("| a | b |\n|---|---|\n| " + "x" * 80 + " | y |")
        self.assertEqual(out[1], "x" * 80 + " │ y")

    def test_snake_case_is_not_italic(self):
        self.assertEqual(self.md("my_var_name and 2*3*4"), ["my_var_name and 2*3*4"])

    def test_task_preview(self):
        long_task = "word " * 200
        self.assertLessEqual(len(render.task_preview(long_task, first=True)), 240)
        self.assertTrue(render.task_preview(long_task, first=True).endswith("…"))
        self.assertEqual(render.task_preview("short\n  task", first=True), "short task")

    def test_tool_summary(self):
        self.assertEqual(render.tool_summary({"command": "ls\n-la", "description": "list"}), "ls -la")
        self.assertEqual(render.tool_summary({"file_path": "/a/b.go"}), "/a/b.go")
        self.assertEqual(render.tool_summary({"other": 1}), '{"other": 1}')

    def test_tool_titles_match_claude_code(self):
        t = render.tool_title
        self.assertEqual(t("Bash", {"command": "ls"}), ("Bash", "ls"))
        self.assertEqual(t("Read", {"file_path": "/repo/a/b.go"}, "/repo"), ("Read", "a/b.go"))
        self.assertEqual(t("Edit", {"file_path": "/repo/x.py"}, "/repo"), ("Update", "x.py"))
        self.assertEqual(t("Grep", {"pattern": "TODO", "path": "/repo/src"}, "/repo"),
                         ("Search", 'pattern: "TODO", path: "src"'))
        self.assertEqual(t("WebSearch", {"query": "q"}), ("Web Search", '"q"'))
        self.assertEqual(t("TodoWrite", {"todos": []}), ("Update Todos", ""))
        self.assertEqual(t("mcp__github__get_issue", {"number": 3}), ("github - get_issue (MCP)", '{"number": 3}'))

    def test_agent_state(self):
        ok = json.dumps({"message": {"content": [{"type": "tool_result", "content": "x"}]}})
        bad = json.dumps({"message": {"content": [{"type": "tool_result", "is_error": True}]}})
        txt = json.dumps({"message": {"content": [{"type": "text", "text": "hi"}]}})
        self.assertEqual(render.agent_state(ok), "running")
        self.assertEqual(render.agent_state(bad), "error")
        self.assertIsNone(render.agent_state(txt))
        self.assertIsNone(render.agent_state("not json"))

    def rendered(self, record, first=False):
        out = io.StringIO()
        with redirect_stdout(out):
            render.render_record(json.dumps(record), first)
        return plain(out.getvalue())

    def test_render_task_text_and_tools(self):
        self.assertIn("❯ Describe", self.rendered({"type": "user", "message": {"content": "Describe"}}, True))
        out = self.rendered({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Found **it**"},
            {"type": "thinking", "thinking": "hmm\nok"},
            {"type": "tool_use", "id": "t1", "name": "Grep", "input": {"pattern": "TODO"}}]}})
        self.assertIn("⏺ Found it", out)
        self.assertIn("✻ Thinking…\n  hmm ok", out)
        self.assertIn('⏺ Search(pattern: "TODO")', out)

    def test_text_wraps_with_a_hanging_indent(self):
        out = self.rendered({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "word " * 30}]}})
        rows = [r for r in out.splitlines() if r]
        self.assertTrue(rows[0].startswith("⏺ word"))
        self.assertTrue(all(r.startswith("  word") for r in rows[1:]))
        self.assertTrue(all(len(r) <= 60 for r in rows))

    def test_render_tool_result_is_capped(self):
        out = self.rendered({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "1\n2\n3\n4\n5"}]}})
        self.assertEqual(out.splitlines(), ["  ⎿  1", "     2", "     3", "     … +2 lines"])

    def test_results_summarised_like_claude_code(self):
        call = lambda name, inp: {"type": "assistant", "message": {"content": [   # noqa: E731
            {"type": "tool_use", "id": name, "name": name, "input": inp}]}}
        result = lambda name, content: {"type": "user", "message": {"content": [   # noqa: E731
            {"type": "tool_result", "tool_use_id": name, "content": content}]}}
        self.rendered(call("Read", {"file_path": "a.go"}))
        self.assertEqual(self.rendered(result("Read", "1\n2\n3\n4")).strip(), "⎿  Read 4 lines")
        self.rendered(call("Edit", {"file_path": "a.go", "old_string": "x = 1", "new_string": "x = 2\ny = 3"}))
        out = self.rendered(result("Edit", "ok")).splitlines()
        self.assertEqual(out[0], "  ⎿  Updated a.go (+2 -1)")
        self.assertEqual([r.strip() for r in out[1:]], ["- x = 1", "+ x = 2", "+ y = 3"])
        self.rendered(call("TodoWrite", {"todos": [{"content": "plan", "status": "completed"},
                                                   {"content": "build", "status": "in_progress"}]}))
        self.assertEqual(self.rendered(result("TodoWrite", "ok")).splitlines(), ["  ⎿  ☒ plan", "     ☐ build"])

    def test_tool_dot_turns_green_or_red_when_its_result_arrives(self):
        class CountingOut(io.StringIO):
            emitted, repaints = 0, []

            def write(self, data):
                self.emitted += data.count("\n")
                return super().write(data)

            def repaint(self, number, change):
                self.repaints.append((number, change(render.paint("⏺", "grey") + " Bash(ls)")))

        out = CountingOut()
        with redirect_stdout(out):
            render.render_record(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}}), False)
            render.render_record(json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "a"}]}}), False)
        self.assertEqual(out.repaints, [(1, render.paint("⏺", "ok") + " Bash(ls)")])   # line 0 is the blank

    def test_task_notification_collapses_to_its_summary(self):
        out = self.rendered({"type": "user", "message": {"content":
            "[SYSTEM NOTIFICATION]\n<task-notification><status>failed</status>"
            "<summary>Background command \"wait\" failed</summary></task-notification>"}})
        self.assertEqual(out.strip(), '⏺ Background command "wait" failed')

    def test_render_skips_reminders_meta_and_attachments(self):
        reminder = {"type": "user", "message": {"content": "<system-reminder>secret</system-reminder>"}}
        self.assertEqual(self.rendered(reminder), "")
        self.assertEqual(self.rendered({"type": "user", "isMeta": True, "message": {"content": "x"}}), "")
        self.assertEqual(self.rendered({"type": "attachment", "attachment": {"type": "other"}}), "")

    def test_render_forwarded_message_and_handback(self):
        out = self.rendered({"type": "attachment", "attachment": {
            "type": "queued_command", "prompt": "also X", "origin": {"kind": "coordinator"}}})
        self.assertIn("❯ also X\n  ⎿  received from Claude", out)
        out = self.rendered({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "SubagentHandback", "input": {"message": "# Done\nall good"}}]}})
        self.assertIn("⏺ Report to caller", out)
        self.assertIn("  all good", out)


class WrapTest(unittest.TestCase):
    def test_hanging_indent_and_colours_carry_over(self):
        rows = text.wrap("aa " + text.C["b"] + "bb cc dd" + text.C["r"] + " ee", 10, "⏺ ", "  ")
        self.assertEqual([plain(r) for r in rows], ["⏺ aa bb cc", "  dd ee"])
        self.assertTrue(rows[1].startswith("  " + text.C["b"]), "bold continues on the next row")

    def test_long_words_are_split(self):
        self.assertEqual([plain(r) for r in text.wrap("x" * 12, 10, "  ")], ["  xxxxxxxx", "  xxxx"])

    def test_wide_characters_count_double(self):
        self.assertEqual(text.visible_width("日本"), 4)


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

    def test_relay_refused_with_a_reason(self):
        agent = state.Agent("bg4")
        agent.shape = "background"
        agent.forbid_relay(messaging.RELAY_NEEDS_TMUX)
        self.assertEqual(messaging.queue(agent, "x"), (messaging.RELAY_NEEDS_TMUX, None))


class FakeGhostty:
    """Stands in for osascript: records scripts, answers from a table."""

    def __init__(self):
        self.scripts, self.answers = [], []
        self.ids = 0

    def run(self, cmd, capture_output=True, text=True):
        if cmd[0] == "ps":
            return mock.Mock(returncode=0, stdout=self.ps.pop(0), stderr="")
        script = cmd[2]
        self.scripts.append(script)
        out = ""
        if "split terminal id" in script or "new tab in" in script:
            self.ids += 1
            out = f"T{self.ids}"
        for needle, answer in self.answers:
            if needle in script:
                out = answer
        return mock.Mock(returncode=0, stdout=out + "\n", stderr="")


class GhosttyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ccmm-unit-")
        self.fake = FakeGhostty()
        for patcher in (mock.patch.object(state, "STATE_DIR", self.dir),
                        mock.patch.object(ghostty.subprocess, "run", self.fake.run)):
            patcher.start()
            self.addCleanup(patcher.stop)
        ghostty._changed()

    def test_terminal_detection(self):
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TERM_PROGRAM")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(settings.sys, "platform", "darwin"):
            self.assertIsNone(settings.terminal())
            os.environ["TERM_PROGRAM"] = "ghostty"
            self.assertEqual(settings.terminal(), "ghostty")
            os.environ["TMUX"] = "/tmp/tmux-1/default,1,0"
            self.assertEqual(settings.terminal(), "tmux", "tmux inside Ghostty uses tmux")
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "ghostty"}, clear=True), \
                mock.patch.object(settings.sys, "platform", "linux"):
            self.assertIsNone(settings.terminal(), "Ghostty's scripting is macOS-only")

    def test_quote(self):
        self.assertEqual(ghostty.quote('say "hi" \\ bye'), '"say \\"hi\\" \\\\ bye"')

    def test_controlling_tty_walks_up_to_claude(self):
        self.fake.ps = ["?? 500\n", "ttys004 400\n"]
        self.assertEqual(ghostty.controlling_tty(600), "/dev/ttys004")
        self.fake.ps = ["?? 1\n"]
        self.assertEqual(ghostty.controlling_tty(600), "")

    def test_split_sets_command_direction_and_title(self):
        new = ghostty.split("CLAUDE", "right", "python3 'x y'", 'Scout "1"')
        self.assertEqual(new, "T1")
        script = self.fake.scripts[-1]
        self.assertIn('set command of cfg to "python3 \'x y\'"', script)
        self.assertIn('split terminal id "CLAUDE" direction right', script)
        self.assertIn('set name override of t to "Scout \\"1\\""', script)

    def test_liveness_is_one_call_for_many_panes(self):
        self.fake.answers = [("return (id of terminals)", "A\nB")]
        self.assertTrue(panes.is_alive("A"))
        self.assertTrue(panes.is_alive("B"))
        self.assertFalse(panes.is_alive("C"))
        self.assertFalse(panes.is_alive(""))
        self.assertEqual(len(self.fake.scripts), 1)

    def test_pane_ids_tell_the_terminals_apart(self):
        self.assertTrue(panes.is_tmux("%12"))
        self.assertTrue(panes.is_tmux("@3"))
        self.assertFalse(panes.is_tmux("59EBD1C9-9081-4E0D-8169-2A2C9A7D3FA6"))

    def test_column_splits_right_then_stacks_down(self):
        self.fake.answers = [("return (id of terminals)", "CLAUDE\nT1\nT2")]
        column = ghostty_layout.Column("CLAUDE")
        self.assertEqual(column.add("view a", "a"), "T1")
        self.assertEqual(column.add("view b", "b"), "T2")
        self.assertIn('split terminal id "CLAUDE" direction right', self.fake.scripts[-3])
        self.assertIn('split terminal id "T1" direction down', self.fake.scripts[-1])
        self.assertEqual(column.panes(), ["T1", "T2"])

    def test_even_out_leaves_your_own_splits_alone(self):
        state.Agent("a1").pane = "T1"
        self.fake.answers = [("ids as text", "CLAUDE\nT1\nT2")]
        ghostty_layout.even_out("CLAUDE", new_pane="T2")
        self.assertIn("equalize_splits", self.fake.scripts[-1])
        self.fake.answers = [("ids as text", "CLAUDE\nT1\nMINE")]
        ghostty_layout.even_out("CLAUDE")
        self.assertNotIn("equalize_splits", self.fake.scripts[-1])

    def test_agents_tab_opens_once_then_splits(self):
        self.fake.answers = [("return (id of terminals)", "CLAUDE\nT1")]
        tab = ghostty_layout.AgentsTab("CLAUDE")
        self.assertEqual(tab.add("view a", "a"), "T1")
        self.assertIn("new tab in w", self.fake.scripts[-1])
        self.assertIn('set name override of newTab to "agents"', self.fake.scripts[-1])
        self.assertIn("select tab tb", self.fake.scripts[-1], "focus stays on Claude's tab")
        self.assertEqual(tab.add("view b", "b"), "T2")
        self.assertIn('split terminal id "T1" direction right', self.fake.scripts[-2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
