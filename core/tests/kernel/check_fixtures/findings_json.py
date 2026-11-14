#!/usr/bin/env python3
"""Fixture: one JSON object per finding on standard output, exit 1."""
import json
import sys

findings = [
    {"rule": "demo.one", "severity": "fatal", "path": "a.md", "line": 3,
     "message": "first finding"},
    {"rule": "demo.two", "severity": "warn", "path": None, "line": None,
     "message": "second finding"},
]
for finding in findings:
    print(json.dumps(finding))
sys.exit(1)
