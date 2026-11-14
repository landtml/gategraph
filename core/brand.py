"""Product name and the spellings this code writes and reads.

The product name appears in code only in this file; every other module
imports it from here. Legacy spellings are read for compatibility and are
never written.
"""

NAME = "GateGraph"
SLUG = "gategraph"

# Project state directory. Written: STATE_DIRNAME. Read: LEGACY_STATE_DIRNAMES.
STATE_DIRNAME = ".gategraph"
LEGACY_STATE_DIRNAMES = (".fh",)

# Generated-section marker. Written: GEN_MARKER. Read: LEGACY_GEN_MARKERS.
GEN_MARKER = "gategraph:gen"
LEGACY_GEN_MARKERS = ("fh:gen", "fh:auto-handoff")

# Environment variable prefix. Written: ENV_PREFIX. Read: LEGACY_ENV_PREFIXES.
ENV_PREFIX = "GATEGRAPH_"
LEGACY_ENV_PREFIXES = ("FH_",)

# Schema key in state files. Read only: LEGACY_SCHEMA_KEYS.
LEGACY_SCHEMA_KEYS = ("fh_schema",)

# Python floor (B51). The doctor and the CI matrix both read it.
MIN_PYTHON = (3, 11)
# Newest interpreter the CI matrix covers.
CI_MAX_PYTHON = (3, 14)


def python_matrix():
    """CI interpreter versions from MIN_PYTHON up to CI_MAX_PYTHON, as "M.m"."""
    return [f"{MIN_PYTHON[0]}.{minor}"
            for minor in range(MIN_PYTHON[1], CI_MAX_PYTHON[1] + 1)]
