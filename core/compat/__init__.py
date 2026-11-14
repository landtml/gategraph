"""Version-1 compatibility layer (plan section 1.18, brief M3).

Stamped projects keep their version-1 adapter scripts for one release. Those scripts import the
old top-level names (``config``, ``manifest``, ``paths``, ``lint``, ``policy``, ``doctor``,
``failures``, ``gate``). The migration replaces ``harness/core/`` with this core and keeps the old
names as one-line shims over ``shims/``. ``v1`` collects the version-1 API in one place.

Kernel layer: standard library and PyYAML only. The graph is reached through the gate command
(a subprocess), never imported. Product name only via core/brand.py.
"""
