"""Settings: plugin options (/config) and CC_MICRO_MANAGE_* environment variables.

An environment variable wins over the plugin option, which wins over the
default. Only hook processes see these; panes are told what they need through
arguments and state files, because panes don't inherit Claude's
environment.

Every variable is spelled out literally (never built at run time).
"""
import os
import sys

DEFAULT_LINGER = "10"
DEFAULT_SHORTCUT = "a"
LAYOUTS = ("split", "window")

# name -> reader returning (environment variable, plugin option)
_SOURCES = {
    "LAYOUT": lambda: (os.environ.get("CC_MICRO_MANAGE_LAYOUT"), os.environ.get("CLAUDE_PLUGIN_OPTION_LAYOUT")),
    "LINGER": lambda: (os.environ.get("CC_MICRO_MANAGE_LINGER"), os.environ.get("CLAUDE_PLUGIN_OPTION_LINGER")),
    "SHORTCUT": lambda: (os.environ.get("CC_MICRO_MANAGE_SHORTCUT"),
                         os.environ.get("CLAUDE_PLUGIN_OPTION_MESSAGE_SHORTCUT")),
    "RELAY": lambda: (os.environ.get("CC_MICRO_MANAGE_RELAY"), os.environ.get("CLAUDE_PLUGIN_OPTION_RELAY")),
    "BORDERS": lambda: (os.environ.get("CC_MICRO_MANAGE_BORDERS"), os.environ.get("CLAUDE_PLUGIN_OPTION_BORDERS")),
    "SKIP": lambda: (os.environ.get("CC_MICRO_MANAGE_SKIP"), os.environ.get("CLAUDE_PLUGIN_OPTION_SKIP")),
}


def _blank(value):
    return value is None or str(value).strip() == ""


def _value(name, default):
    env_value, option_value = _SOURCES[name]()
    if not _blank(env_value):
        return str(env_value).strip()
    if not _blank(option_value):
        return str(option_value).strip()
    return default


def _enabled(name):
    value = _value(name, None)
    return value is None or value.lower() not in ("0", "false", "no", "off")


def layout():
    value = _value("LAYOUT", "split").lower()
    return value if value in LAYOUTS else "split"


def linger():
    """Seconds a finished pane stays open, as text (it's passed on as an argument)."""
    return _value("LINGER", DEFAULT_LINGER)


def shortcut():
    """The key after the tmux prefix that opens the message prompt, or None."""
    key = _value("SHORTCUT", DEFAULT_SHORTCUT)
    return None if not key or key.lower() == "none" else key


def relay_enabled():
    return _enabled("RELAY")


def borders_enabled():
    return _enabled("BORDERS")


def skipped_agent_types():
    return {t.strip() for t in _value("SKIP", "").split(",") if t.strip()}


def plugin_disabled():
    return os.environ.get("CC_MICRO_MANAGE_OFF") == "1"


def debug_enabled():
    return os.environ.get("CC_MICRO_MANAGE_DEBUG") == "1"


def inside_tmux():
    return bool(os.environ.get("TMUX"))


def color_depth():
    """ "24bit" when Claude Code draws in truecolor, else "256". Claude decides
    from its own environment (as the supports-color library does), and panes
    don't inherit that environment, so this is worked out here and passed on."""
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return "24bit"
    if os.environ.get("TERM", "") in ("xterm-ghostty", "xterm-kitty", "wezterm") or os.environ.get("TERM", "").endswith("-direct"):
        return "24bit"
    if os.environ.get("TERM_PROGRAM") == "iTerm.app":
        return "24bit"
    return "256"


def terminal():
    """Where Claude runs: "tmux", "ghostty" (macOS, without tmux) or None.
    tmux wins, so Claude in tmux inside Ghostty uses tmux panes."""
    if inside_tmux():
        return "tmux"
    if os.environ.get("TERM_PROGRAM") == "ghostty" and sys.platform == "darwin":
        return "ghostty"
    return None


def claude_pane():
    """The tmux pane Claude Code runs in (hooks inherit it)."""
    return os.environ.get("TMUX_PANE")


def snapshot():
    """Every setting as [environment variable, plugin option], for the debug log."""
    return {name: list(read()) for name, read in _SOURCES.items()}
