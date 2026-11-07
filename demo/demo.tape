# Regenerate the README GIF:  ./demo/setup-demo-repo.sh && vhs demo/demo.tape
# Records a real Claude Code session (Haiku) in an isolated tmux server with default settings.

Output demo/demo.gif

Set Shell "bash"
Set FontSize 13
Set Width 1280
Set Height 720
Set Padding 10
Set Theme "Catppuccin Mocha"
Set TypingSpeed 10ms

Hide
Type "cd /tmp/acme-api && clear"
Enter
Type `tmux -L ccmm -f /dev/null new-session -s demo "env -u ANTHROPIC_API_KEY COLORTERM=truecolor CC_MICRO_MANAGE_LINGER=3 claude --model haiku --allowedTools Bash Read Grep Glob Agent" \; set -g status off \; set -g pane-border-status top \; set -g pane-border-format " #{?#{m:*·*,#{pane_title}},#{pane_title},claude} "`
Enter
Sleep 7s
# folder-trust prompt defaults to "No, exit": pick "Yes, I trust this folder"
Down
Enter
Sleep 5s
Show

Sleep 1s
Type "Launch 3 general-purpose subagents in parallel: (1) Scout: read every .go file one at a time and describe each in one line. (2) Lead: launch 2 subagents in parallel, one summarizes the README, one lists TODO/FIXME comments as file:line; combine their answers. (3) Deps: for each dependency in go.mod, find where the code uses it. Then give me a 3-line summary."
Sleep 500ms
Enter

Sleep 48s
Hide
Type "tmux -L ccmm kill-server"
Enter
