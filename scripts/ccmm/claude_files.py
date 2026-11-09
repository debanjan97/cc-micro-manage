"""Where Claude Code keeps subagent transcripts and meta files.

  <project>/<session>.jsonl                              the session transcript
  <project>/<session>/subagents/agent-<id>.jsonl         a subagent's transcript
  <project>/<session>/subagents/agent-<id>.meta.json     description, parentAgentId,
                                                         spawnDepth, requestShape,
                                                         toolUseId, finished
and, for background shells (Bash with run_in_background):
  <tmp>/claude-<uid>/<project>/<session>/tasks/<task>.output
      the command's output as it runs; Claude Code appends "[exited with code N]"
      or "[killed]" on a line of its own when it ends
These are undocumented, so every reader here fails soft.
"""
import glob
import json
import os
import time

META_WAIT = 2.5


def bare_id(agent_id):
    return agent_id[len("agent-"):] if agent_id.startswith("agent-") else agent_id


def subagents_dir(session_transcript):
    base = session_transcript[:-len(".jsonl")] if session_transcript.endswith(".jsonl") else session_transcript
    return os.path.join(base, "subagents")


def meta_path(session_transcript, agent_id):
    return os.path.join(subagents_dir(session_transcript), f"agent-{bare_id(agent_id)}.meta.json")


def read_meta(session_transcript, agent_id, wait=0.0):
    """An agent's meta file as a dict ({} if missing). It can land just after
    SubagentStart, so placement waits for it briefly."""
    if not session_transcript:
        return {}
    file_path = meta_path(session_transcript, agent_id)
    deadline = time.time() + wait
    while True:
        try:
            with open(file_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            if time.time() >= deadline:
                return {}
            time.sleep(0.05)


def transcript_candidates(agent_id, session_transcript=None, session_id=None):
    """Paths where this agent's transcript may appear."""
    names = {f"agent-{bare_id(agent_id)}.jsonl", f"{agent_id}.jsonl"}
    out = []
    if session_transcript:
        out += [os.path.join(subagents_dir(session_transcript), n) for n in names]
    if session_id:
        root = os.path.expanduser("~/.claude/projects")
        for n in names:
            out += glob.glob(os.path.join(root, "*", session_id, "subagents", n))
    return out


def shell_output_candidates(session_id, task_id):
    """Paths where a background shell's output file may be. Claude Code puts it
    under $CLAUDE_CODE_TMPDIR, else /tmp (on macOS, /private/tmp)."""
    if not session_id or not task_id:
        return []
    bases = [os.environ.get("CLAUDE_CODE_TMPDIR"), "/tmp", "/private/tmp"]
    out = []
    for base in dict.fromkeys(b for b in bases if b):
        pattern = os.path.join(base, f"claude-{os.getuid()}", "*", session_id, "tasks", f"{task_id}.output")
        out += glob.glob(pattern)
    return out


def find_shell_output(session_id, task_id, wait=0.0):
    deadline = time.time() + wait
    while True:
        found = shell_output_candidates(session_id, task_id)
        if found or time.time() >= deadline:
            return found[0] if found else ""
        time.sleep(0.1)


def agent_for_tool_use(session_transcript, tool_use_id):
    """The id of the agent an Agent/Task tool call started, or None."""
    for file_path in glob.glob(os.path.join(subagents_dir(session_transcript), "*.meta.json")):
        try:
            with open(file_path) as f:
                if json.load(f).get("toolUseId") != tool_use_id:
                    continue
        except (OSError, ValueError):
            continue
        return bare_id(os.path.basename(file_path)[: -len(".meta.json")])
    return None
