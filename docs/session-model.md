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
