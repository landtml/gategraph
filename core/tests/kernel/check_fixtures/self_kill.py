#!/usr/bin/env python3
"""Fixture: kill itself with SIGKILL, so the check dies by signal."""
import os
import signal

os.kill(os.getpid(), signal.SIGKILL)
