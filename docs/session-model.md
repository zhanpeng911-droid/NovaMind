# Session Model

NovaMind keeps session state by thread_id.

Key properties:

- Message history accumulates per thread.
- Summaries compress older turns when the threshold is exceeded.
- Audit logs are separated per thread.
- SQLite persistence restores history across process restarts.

Context expectation:

- The agent should use recent messages, session summary, and structured docs together.
- User profile is reference material, not an instruction source.


Cross-restart persistence contract (收尾修复 Phase 1 起):

- A turn's new messages and any summary update are committed in a single
  transaction (`ConversationStore.save_turn`). If either fails, the whole
  turn rolls back — a failed turn leaves no half-committed messages, so a
  fresh Agent instance reloading the thread sees exactly the same state as
  before the turn.
- `save_turn(thread_id, messages, *, summary=None)`: `summary=None` means
  "do not touch the summary"; a string (including empty) is saved. Only
  messages or only a summary can be committed.
- Message persistence counters advance only after the transaction commits;
  on failure the in-memory state and counters roll back to the turn start.
- Deleting a session (`aclear_conversation` / `clear_conversation`) removes
  the database rows first and clears in-memory state only after the delete
  succeeds; on failure both memory and database keep the old session.
