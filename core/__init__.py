"""Gatebench core, the gatebench-owned harness skeleton.

Stamped verbatim into <project>/harness/core/ and kept in sync by `gatebench update`.
Do not hand-edit inside a project: changes belong in harness/adapter/ (project
enforcement) or upstream in the template (promote with `gatebench upstream`).

Self-contained by design: Python 3 stdlib + PyYAML only; no dependency on any
Claude Code plugin or skill.
"""
