# Release Safety

Use this skill for deployments, destructive changes, credential-sensitive operations, and other consequential external side effects.

1. Identify the exact environment, target, version, and rollback path.
2. Confirm tests, build artifacts, configuration changes, and dependency locks are current.
3. Keep credentials out of plans, events, prompts, and artifacts; use only opaque runtime handles.
4. Require explicit approval for high-risk or production mutations and attach an idempotency key.
5. After execution, verify health signals and record what changed, who approved it, and how to recover.

Never treat model intent as authorization. Policy enforcement and approval happen outside the model boundary.
