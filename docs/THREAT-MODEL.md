# THREAT-MODEL, what the policy layer defends against, and what it does not

One page, written down so no project inherits this implicitly (Part 1 item 4).
The harness's guardrails are **cooperative**: they exist to catch an
honest-but-fallible agent drifting off protocol, not to stop an adversary.

## Defended: the cooperative threat model

- **Accidental writes of denied content** by a cooperating agent: deny
  policies (adapter.yaml `policies.deny`) scan every Write/Edit through the
  pretool guard and every landed file through the posttool lint.
- **Scanning is NFKC-normalized and whole-text** (core/policy.py): fullwidth
  or superscript homoglyph digits are folded to ASCII, zero-width characters
  (ZWSP/ZWNJ/ZWJ/word-joiner/BOM) are stripped, and a match spanning a soft
  line wrap still fires. This closes the accidental-paste holes (F14), a
  copy-pasted account number with a line break in the middle is caught.
- **Protected paths** (`protected_paths`): harness-owned state files refuse
  agent edits.
- **Ask paths** (`policies.ask`): convention-read-only trees require explicit
  confirmation.

## Not defended: the adversarial cases (by design)

The policy layer **does not defend** against a deliberately evasive writer.
Known bypass classes, listed so nobody discovers them in production:

- **Encoding**: base64, rot13, hex, URL-encoding, or any transform a regex
  cannot see through.
- **Out-of-band tools**: content written by a tool the hooks do not cover
  (e.g. arbitrary shell redirection is only caught when the posttool
  bash-relint pass happens to re-scan the touched file).
- **Novel homoglyphs**: NFKC folds compatibility characters only; a Cyrillic
  'о' for Latin 'o' survives normalization.
- **Semantic smuggling**: paraphrase, translation into another language, or
  splitting content across many files/commits.

## Failure direction of each enforcement point

- **pretool guard: fail CLOSED**, a crashing guard blocks the write.
- **posttool lint, stop gate, session start: fail OPEN**, a crashing hook
  never bricks the session; enforcement is re-earned at gate time by
  `make check` (harness_lint), which is the load-bearing pass.

## If your project holds real secrets or PII

Bring real tooling; the template deliberately does not grow a secrets engine
(stdlib + PyYAML constraint). Wire a scanner as an advisory doctor check,
seed/adapter.yaml ships a commented `gitleaks` example, and consider making
it a fatal adapter check on your unit tree.
