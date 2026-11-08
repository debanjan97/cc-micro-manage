"""How to run this program again: from tmux (viewer panes, the message
shortcut, relayout) and as a detached helper."""
import os
import shlex
import sys

ENTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cc_micro_manage.py")


def argv(*args):
    return [sys.executable, ENTRY, *args]


def shell_command(*args):
    """A shell-quoted command line, for tmux to run."""
    return " ".join(shlex.quote(a) for a in argv(*args))
