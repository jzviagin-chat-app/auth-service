---
description: Check whether this task changed the system design, and update the shared docs if so
---
Review what changed in this task (`git diff` against the default branch, plus anything decided in this
conversation). Then decide whether it affects the **shared system design**:

- services, what each owns, or how they are deployed
- a contract between services (tokens/claims, JWKS, WebSocket messages, Redis keys/channels, API conventions)
- databases, tables, buckets, secrets, network rules
- conventions every service follows (logging, retries, IDs, stack)
- a decision, phase or step status in PLAN.md

The shared docs are `docs/ARCHITECTURE.md` and `docs/PLAN.md` in **chat-infra**: in this repo if you are in
chat-infra, otherwise in `.context/chat-infra/` (a clone kept up to date by the SessionStart hook).

If nothing is affected, say so in one line and stop.

Otherwise:
1. In the chat-infra checkout, create a branch `design/<short-topic>` (if not already on one).
2. Update the relevant sections (keep them accurate and short), and add a row to the change log at the
   end of `PLAN.md` (date, what changed, which repo).
3. Commit with a clear message. Show the owner the diff and the exact commands to push and open the PR,
   e.g. `git -C .context/chat-infra push -u origin design/<topic>`. Do not merge it yourself unless asked.
