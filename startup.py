# -*- coding: utf-8 -*-
"""Boot of Magic Tools: on the first Idling, arm the launcher and hide the
Favorites tools that are not starred (lib/lab_startup.py does the work).

Boot changes no pyRevit settings. It sends usage data only for a user who
opted in to it (lib/telemetry.py, TELEMETRY.md), and then on a background
thread; it never asks, since the question waits for the first click of a tool.
"""
import os

try:
    import lab_startup  # noqa: F401
except ImportError:
    # An ImportError raised INSIDE a lab_startup that does exist is a broken
    # boot, not an absence: say so.
    if os.path.isfile(os.path.join(os.path.dirname(__file__), 'lib', 'lab_startup.py')):
        import traceback
        traceback.print_exc()
except Exception:
    import traceback
    traceback.print_exc()
