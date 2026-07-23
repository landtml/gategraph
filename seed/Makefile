# Gatebench project targets. `make check` is the standard "done" entry.
.PHONY: check loop eval maintenance

check:
	python3 harness/core/harness_lint.py --tier gating
	bash harness/core/tests/run_checks.sh
	python3 harness/core/loop/cli.py validate

# T13 ritual checklist: what maintenance is due; record with
# `python3 harness/core/maintenance.py done <ritual>`.
maintenance:
	python3 harness/core/maintenance.py status

loop:
	python3 harness/core/loop/cli.py run

# §13 regression corpus: cases live under harness/eval/corpus/*.yaml.
eval:
	python3 harness/core/eval/cli.py run
