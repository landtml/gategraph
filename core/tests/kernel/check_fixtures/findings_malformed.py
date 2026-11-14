#!/usr/bin/env python3
"""Fixture: standard output is not JSON; standard error has five lines, exit 1."""
import sys

print("this is not json")
for n in range(1, 6):
    print(f"stderr line {n}", file=sys.stderr)
sys.exit(1)
