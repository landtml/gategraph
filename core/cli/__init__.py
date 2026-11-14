"""Command layers over the kernel and the graph (plan section 1.3; brief G8).

``gate`` lives in ``core/cli/gate.py``. Each command parses its arguments, calls the runner
or the kernel, and prints. The graph decides; this package does not.
"""
