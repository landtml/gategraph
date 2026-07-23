# MEMORY, router (read this first, then open at most 2-5 files)

Four typed stores with distinct read/write discipline. Conflating them is the
classic failure: the episodic log is an audit trail, **not** a fact source.

| Store | File | Discipline | Read when |
|---|---|---|---|
| Working | `harness/state/now.md` | current task + in-flight state; resets per session; compressed at stop | session start |
| Episodic | `harness/state/decisions.jsonl` | append-only audit trail; never overwritten, never used as a fact source | on demand / forensics |
| Semantic | `harness/state/facts.yaml` | consolidated facts/rules; promoted from episodic on an explicit "commit fact", not automatically | session start |
| Procedural | `.claude/skills/`, `harness/adapter/checks/` | reusable procedures | when the task matches |

Handoff (`HANDOFF.md`) is a consolidation event: at stop, working memory is
compressed, a decision is appended to the episodic log, and durable facts are
promoted to `facts.yaml`.
