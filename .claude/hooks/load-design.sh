#!/usr/bin/env bash
# SessionStart hook: keep a clone of chat-infra (the shared system design) in .context/chat-infra
# and tell Claude where it is. CLAUDE.md imports the docs from there.
# Never fails the session: on any problem it prints a note and exits 0.
# Output is kept short (Claude Code caps hook output added to context at 10,000 characters).

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
DEST="$ROOT/.context/chat-infra"
URL="https://github.com/jzviagin-chat-app/chat-infra.git"
export GIT_TERMINAL_PROMPT=0 # public repo: never wait for a password prompt

say() { printf '%s\n' "$*"; }

if [ -d "$DEST/.git" ]; then
  branch="$(git -C "$DEST" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  dirty="$(git -C "$DEST" status --porcelain 2>/dev/null)"
  if [ "$branch" = "main" ] && [ -z "$dirty" ]; then
    if git -C "$DEST" pull --ff-only --quiet 2>/dev/null; then
      state="updated to the latest main"
    else
      state="could NOT be updated (offline?); using the existing copy"
    fi
  else
    state="left as is: it is on branch '$branch' with your design edits"
  fi
else
  mkdir -p "$ROOT/.context"
  if git clone --quiet --depth 50 "$URL" "$DEST" 2>/dev/null; then
    state="cloned (first run)"
  else
    say "System design: could NOT clone $URL (offline?). Design docs unavailable in this session."
    exit 0
  fi
fi

say "System design (chat-infra) at .context/chat-infra: $state."
say "CLAUDE.md imports .context/chat-infra/docs/ARCHITECTURE.md and docs/PLAN.md. If they are not in your"
say "context yet (first run), read both files before starting work."

# Design edits that would otherwise be forgotten
pending="$(git -C "$DEST" status --porcelain 2>/dev/null)"
unpushed="$(git -C "$DEST" log --branches --not --remotes --oneline 2>/dev/null)"
if [ -n "$pending" ] || [ -n "$unpushed" ]; then
  say "WARNING: .context/chat-infra has design changes not yet pushed. Remind the owner to push them and"
  say "open a chat-infra PR (git -C .context/chat-infra push -u origin <branch>)."
  [ -n "$pending" ] && say "Uncommitted:" && say "$pending" | head -10
  [ -n "$unpushed" ] && say "Unpushed commits:" && say "$unpushed" | head -10
fi
exit 0
