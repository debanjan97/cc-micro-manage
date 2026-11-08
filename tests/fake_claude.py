"""Stands in for Claude Code's TUI in the e2e tests.

Draws an input box the way Claude Code does (a rule, then "❯ "), and appends
every submitted line to the log file given as argv[1], so tests can see what
the plugin typed into "Claude".
"""
import sys

log = sys.argv[1]
print("fake claude ready")
while True:
    print("─" * 40)
    try:
        line = input("❯ ")
    except EOFError:
        break
    with open(log, "a") as f:
        f.write(line + "\n")
    print("⏺ ok")
