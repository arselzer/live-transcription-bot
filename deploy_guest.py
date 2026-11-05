#!/usr/bin/env python3
"""Compatibility entrypoint for the explicit guest source bundle."""
from _bootstrap import dispatch
dispatch(__name__, 'live_transcript.runtime.deployment')
