"""The pane footer: model · effort · context · output tokens · elapsed time."""
import datetime
import json
import re
import time


def pretty_model(model_id):
    """claude-haiku-5-5 -> Haiku 5.5"""
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)-(\d+)(?:-\d{8})?", model_id or "")
    return f"{m.group(1).capitalize()} {m.group(2)}.{m.group(3)}" if m else (model_id or "")


def thousands(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _timestamp(value):
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _usage_context(usage):
    return sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


class Stats:
    def __init__(self):
        self.model = self.effort = ""
        self.context = self.output = 0
        self.started = self.ended = None

    def update(self, record_line):
        try:
            record = json.loads(record_line)
        except ValueError:
            return
        if self.started is None:
            self.started = _timestamp(record.get("timestamp"))
        if record.get("effort"):
            self.effort = str(record["effort"])
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        if record.get("type") != "assistant":
            return
        if message.get("model"):
            self.model = message["model"]
        usage = message.get("usage") or {}
        if usage:
            self.context = _usage_context(usage)     # the latest reply's context size
            self.output += usage.get("output_tokens") or 0

    def elapsed(self):
        if not self.started:
            return ""
        secs = int((self.ended or time.time()) - self.started)
        return f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s"

    def footers(self):
        """Full, medium and tiny footers; the screen shows the widest that fits."""
        return [self._full(), self._medium(), self._tiny()]

    def _full(self):
        return _join(pretty_model(self.model),
                     self.effort and f"{self.effort} effort",
                     self.context and f"{thousands(self.context)} context",
                     self.output and f"{thousands(self.output)} out",
                     self.elapsed())

    def _medium(self):
        return _join(pretty_model(self.model),
                     self.context and f"{thousands(self.context)} ctx",
                     self.output and f"{thousands(self.output)} out",
                     self.elapsed())

    def _tiny(self):
        return _join(self.context and thousands(self.context), self.elapsed())


def _join(*parts):
    return " · ".join(p for p in parts if p)
