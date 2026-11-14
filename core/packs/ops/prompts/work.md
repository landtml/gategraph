# ops work fragment

Write the unit's plan.yaml: declare every target under the sandbox, then one
write step per target. Run the dry run and write dry_run.log with one OK or
ERROR line per step. Do not call apply_plan. The approval gate runs it after a
human approves the unit.
