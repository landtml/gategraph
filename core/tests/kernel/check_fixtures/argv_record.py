#!/usr/bin/env python3
"""Fixture: record argv[2:] as a JSON list in the file named by argv[1], exit 0."""
import json
import sys

with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(sys.argv[2:], handle)
