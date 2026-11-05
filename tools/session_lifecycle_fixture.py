#!/usr/bin/env python3
"""Idle-only supervised-process fixture; never opens or joins a meeting."""

import time

print("SESSION FIXTURE READY", flush=True)
time.sleep(15)
print("SESSION FIXTURE COMPLETE", flush=True)
