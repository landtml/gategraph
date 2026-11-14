# Rubric: ops plan review

Review the unit's plan.yaml and dry_run.log. One verdict per run: pass or fail.

- Every write step targets a declared target, and every target stays inside the sandbox.
- The dry-run log has an OK line for each step and no ERROR line.
- The plan changes only what the unit's task asks for; nothing else is named.
- Each finding names its rule (for example plan.target-undeclared) and the line or step it concerns.

A fail is a finding a human can act on without rereading the whole plan.
