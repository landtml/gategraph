#!/usr/bin/env python3
"""Fixture: fork a child that outlives the parent's timeout.

argv[1] is a file that receives the child's pid. The child inherits the
stdout pipe, so only a process-group kill can unblock the harness.
"""
import os
import sys
import time

pid = os.fork()
if pid == 0:
    time.sleep(60)
    os._exit(0)
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    handle.write(str(pid))
time.sleep(60)
