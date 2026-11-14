# GateGraph project targets. `make check` is the standard "done" entry (B35).
.PHONY: check venv maintenance

PY := .venv/bin/python
UNIT_DIR := {{FH_UNIT_DIR}}
VENV_STAMP := .venv/.lock-installed

# venv: .venv built from the hash-locked set. The stamp file makes a second `make venv` a
# no-op unless the lock changed. The first run needs network (pip installs the locked wheels).
venv: $(VENV_STAMP)

$(VENV_STAMP): harness/core/requirements.lock
	python3 -m venv .venv
	$(PY) -m pip install --require-hashes -r harness/core/requirements.lock
	touch $@

# Exit 0 only when every step passes:
#   1. self-lint, gating tier: stale docs, orphan references, brand literals, instance
#      leaks, committed state and hook registration
#   2. gate verify over every unit: the evidence chain of each unit
#   3. policy scan over every unit: the deny policies of harness/adapter/adapter.yaml
#   4. selftest: the kernel self-test (under three seconds)
check: venv
	@set -e; \
	PYTHONPATH=harness $(PY) -m core.kernel.selflint --tier gating --root .; \
	for dir in $$(find $(UNIT_DIR) -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort); do \
	  if [ -f "$$dir/manifest.yaml" ]; then \
	    $(PY) harness/core/cli/gate.py verify --unit "$$(basename "$$dir")"; \
	  fi; \
	done; \
	$(PY) harness/adapter/checks/policy_scan.py; \
	$(PY) harness/core/tests/selftest.py
	@echo "check: OK"

# T13 ritual checklist: what maintenance is due. It informs and never gates.
maintenance:
	python3 harness/core/maintenance.py status
