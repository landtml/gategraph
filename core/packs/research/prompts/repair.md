# Repair prompt fragment (research pack)

A claim check failed. Read the finding's rule and line, then repair it:

- `claims.no_source`: add `| source: sources/<file>` to the claim line.
- `claims.source_missing` or `sources.unresolved`: the cited file is not under
  `sources/`. Add the source or correct the path.
- `claims.not_in_source`: the number is not in the cited source. Correct the
  claim to the source's value, or cite the source that states it.
- `claims.malformed`: the line does not match the claim form. Fix the syntax.
- `claims.no_claims` or `claims.missing_required`: a claim line is missing.
  Restore it. Do not delete a claim to make a check pass; a deleted claim
  fails the gate.

Change the claim line, not the check. Do not edit files under `checks/`.
