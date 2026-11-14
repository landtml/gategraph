# GateGraph template targets. Projects get their own Makefile from seed/.
.PHONY: test test-core eval install-hooks maintenance \
	venv test-kernel test-graph test-compat test-tools test-agent test-hosts test-packs test-search \
	test-eval eval-new phase0 phase1 check

# test-core: the top-level core/tests files, then every layer directory. unittest discover does not
# recurse into directories without __init__.py, so each layer runs through its own target below.
# test-core also runs test_no_legacy.py (the X1 gate: only the allowed shims remain).
test-core: test-kernel test-hosts test-graph test-compat test-tools test-agent test-packs test-search test-eval
	python3 -m unittest discover -s core/tests -p 'test_*.py'

test: test-core
	python3 -m unittest discover -s tests

# §13: deterministic corpus × 5 seeds on the new engine (runner_v2, eval/pack_v2).
# Prints "overall: N/N passed".
eval: venv
	.venv/bin/python core/eval/runner_v2.py --pack eval/pack_v2 --corpus eval/corpus

# T13: what maintenance is due (corpus rotation, ledger triage, FLAGS pin).
maintenance:
	python3 -m core.kernel.maintenance --root . status

# Part 1 item 2 (local half): block a red push before CI exists.
# (Kept below test-core so bare `make` still runs the core suite.)
install-hooks:
	install -m 755 scripts/pre-push .git/hooks/pre-push
	@echo "installed: .git/hooks/pre-push (runs make test-core eval)"

# ---- LangGraph layer ----

# venv: .venv built from the hash-locked set. The stamp file makes a second
# `make venv` a no-op unless the lock changed.
VENV_STAMP := .venv/.lock-installed

venv: $(VENV_STAMP)

$(VENV_STAMP): core/requirements.lock
	python3 -m venv .venv
	.venv/bin/python -m pip install --require-hashes -r core/requirements.lock
	touch $@

# RUN_TESTS: run every test_*.py under directory $(1) with interpreter $(2),
# printing $(3) as the target name. A missing or empty directory is OK (no
# tests yet). On failure, print the failing file's output and exit 1.
define RUN_TESTS
	@files=$$(find $(1) -name 'test_*.py' 2>/dev/null | sort); \
	if [ -z "$$files" ]; then echo "$(3): OK (no tests)"; exit 0; fi; \
	for f in $$files; do \
		out=$$($(2) "$$f" 2>&1) || { echo "$$out"; echo "$(3): FAIL $$f"; exit 1; }; \
	done; \
	echo "$(3): OK"
endef

# Kernel layer: standard library and PyYAML only, so system python3.
test-kernel:
	$(call RUN_TESTS,core/tests/kernel,python3,test-kernel)

test-hosts:
	$(call RUN_TESTS,core/tests/hosts,python3,test-hosts)

# Graph, agent, tool and pack layers: the pinned venv.
test-graph: venv
	$(call RUN_TESTS,core/tests/graph,.venv/bin/python,test-graph)

test-compat: venv
	$(call RUN_TESTS,core/tests/compat,.venv/bin/python,test-compat)

test-tools: venv
	$(call RUN_TESTS,core/tests/tools,.venv/bin/python,test-tools)

# Phase 4 exit: tools and agent together.
test-agent: venv test-tools
	$(call RUN_TESTS,core/tests/agent,.venv/bin/python,test-agent)

test-packs: venv
	$(call RUN_TESTS,core/tests/packs,.venv/bin/python,test-packs)

test-search: venv
	$(call RUN_TESTS,core/tests/search,.venv/bin/python,test-search)

# phase0 (Foundation): the golden snapshots verify and the import boundary
# holds on the pinned venv.
phase0: venv
	@step() { out=$$("$$@" 2>&1) || { echo "$$out"; echo "phase0: FAIL $$*"; exit 1; }; }; \
	step python3 tests/golden/verify_golden.py && \
	step .venv/bin/python core/tests/test_boundary_imports.py && \
	echo "phase0: OK"

# phase1 (Contracts): every test under core/tests/contracts runs on the venv.
# No tests at all is a failure, so this never reads as passing by default.
phase1: venv
	@files=$$(find core/tests/contracts -name 'test_*.py' 2>/dev/null | sort); \
	if [ -z "$$files" ]; then echo "phase1: FAIL no tests under core/tests/contracts"; exit 1; fi; \
	for f in $$files; do \
		out=$$(.venv/bin/python "$$f" 2>&1) || { echo "$$out"; echo "phase1: FAIL $$f"; exit 1; }; \
	done; \
	echo "phase1: OK"

# Phases and gates not built yet. They fail on purpose so that nothing reads
# as passing before its tasks land.

test-eval: venv
	$(call RUN_TESTS,core/tests/eval,.venv/bin/python,test-eval)

eval-new: test-eval
	.venv/bin/python core/eval/runner_v2.py --pack eval/pack_v2 --corpus eval/corpus

# check (X3): the template's release check. Runs the whole test suite (make test), the phase-1
# contract tests, the gating self-lint at the template root, the no-legacy gate, the golden
# verifier and the deterministic eval, in that order. It never calls
# a model. It prints `check: OK` only when every step passes.
check: venv
	@run() { name="$$1"; shift; echo "check: $$name"; "$$@" || { echo "check: FAIL $$name"; exit 1; }; }; \
	run "make test" $(MAKE) --no-print-directory test && \
	run "make phase1" $(MAKE) --no-print-directory phase1 && \
	run "selflint gating" python3 core/kernel/selflint.py --tier gating --root . && \
	run "test_no_legacy" python3 core/tests/test_no_legacy.py && \
	run "verify_golden" python3 tests/golden/verify_golden.py && \
	run "make eval" $(MAKE) --no-print-directory eval && \
	echo "check: OK"
