"""The tmux command line."""
import subprocess


class TmuxError(RuntimeError):
    pass


def run(*args):
    """Run a tmux command and return its stdout. Raises TmuxError on failure."""
    result = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise TmuxError(result.stderr.strip())
    return result.stdout.strip()


def try_run(*args, default=""):
    """Run a tmux command; on failure return `default` instead of raising."""
    try:
        return run(*args)
    except TmuxError:
        return default


def is_alive(target):
    # display-message exits 0 with empty output for a missing target, so check the output
    return bool(try_run("display-message", "-p", "-t", target, "#{pane_id}"))


def show(target, fmt):
    """Expand a tmux format for a pane or window, e.g. show(pane, "#{window_id}")."""
    return run("display-message", "-p", "-t", target, fmt)


def window_of(pane):
    return show(pane, "#{window_id}")


def panes_in(window, fmt="#{pane_id}"):
    return run("list-panes", "-t", window, "-F", fmt).split("\n")


# ── user options on panes and windows ──────────────────────────────────

def pane_option(pane, name):
    return try_run("show-options", "-p", "-v", "-t", pane, name)


def set_pane_option(pane, name, value):
    try_run("set-option", "-p", "-t", pane, name, value)


def unset_pane_option(pane, name):
    try_run("set-option", "-p", "-u", "-t", pane, name)


def window_option(window, name):
    return try_run("show-options", "-w", "-v", "-t", window, name)


def set_window_option(window, name, value):
    run("set-option", "-w", "-t", window, name, value)


def unset_window_option(window, name):
    try_run("set-option", "-w", "-u", "-t", window, name)


def global_option(name):
    return try_run("show-options", "-g", "-v", name)
