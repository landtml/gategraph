# Gatebench template targets. Projects get their own Makefile from seed/.
.PHONY: test test-core eval eval-ab chaos fuzz install-hooks maintenance

test-core:
	python3 -m unittest discover -s core/tests -p 'test_*.py'

test: test-core
	python3 -m unittest discover -s tests

# T7 chaos suite (mock legs) + T9 boundary fuzz, the fault-injection detectors.
# Both are already inside test-core; these targets run them alone (CI nightly lane).
chaos:
	cd core/tests && python3 -m unittest test_chaos -v

fuzz:
	cd core/tests && python3 -m unittest test_boundary -v

# §13: deterministic corpus × 5 seeds, pass-rate+CI, ICC, gate histogram.
eval:
	python3 core/eval/cli.py run --root . --corpus eval/corpus --pack eval/pack

# The paired McNemar A/B on the live cases. Spends real worker time, never
# run implicitly; pre-register the criterion (docs/DESIGN.md) first.
eval-ab:
	python3 core/eval/cli.py ab --root . --corpus eval/corpus --pack eval/pack \
		--out eval/ab_report.json

# T13: what maintenance is due (corpus rotation, ledger triage, FLAGS pin).
maintenance:
	python3 core/maintenance.py --root . status

# Part 1 item 2 (local half): block a red push before CI exists.
# (Kept below test-core so bare `make` still runs the core suite.)
install-hooks:
	install -m 755 scripts/pre-push .git/hooks/pre-push
	@echo "installed: .git/hooks/pre-push (runs make test-core eval)"
