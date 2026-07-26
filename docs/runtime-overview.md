# Runtime Overview

NovaMind is a transparent sandboxed agent runtime.

Core runtime properties:

- Every significant step should be auditable through structured events.
- Tool execution is explicit and observable.
- Thread state is isolated by thread_id.
- Long conversations are trimmed into summaries to keep context bounded.
- The runtime is designed to expose what the agent did, why it did it, and under which boundaries.

Harness implications:

- Context should come from structured docs instead of one oversized prompt.
- Runtime decisions should be grounded in the narrowest relevant reference.
- Operators should be able to inspect both actions and the context pack used for those actions.
