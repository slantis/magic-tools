# -*- coding: utf-8 -*-
__title__ = "Selection\nManager"
__doc__ = "The current selection broken down by category, family and type, with a count and a checkbox on each row, grouped under a foldable, tri-state category header. Uncheck a type to drop it from the selection and keep the rest of its category, keep only one row within its own category with a double click, invert, or type to filter the rows (All / None / Invert, and each header's own mark, act on whatever the filter shows); Apply rewrites the selection and the window stays open, so you can keep carving it while you work. Refresh re-reads whatever is selected now."
__author__ = 'slantis'
# The window and every handler live in lib/selmgr.py, not here: see that
# module's docstring for why (QA 2026-09-24, bug 1 -- a second click
# opened a second window instead of bringing the first one to front).
# `__persistentengine__` alone is enough for a window hosted in lib/: no
# `__cleanengine__` here on purpose, unlike most other modeless tools --
# a clean engine reimports lib/modeless.py from scratch on every click,
# which is exactly what broke the second-click behavior in the first place.
__persistentengine__ = True

import traceback

import selmgr
import usage

# Hot-reload on every click: this door is the ONLY caller of selmgr.py, and
# unlike modeless.py (which several tools share and which owns state that a
# reload would wipe -- _OPEN, _EVENT, _HANDLER) selmgr.py holds nothing at
# module level that a reload could corrupt. Without this, an edit to
# lib/selmgr.py during testing would need a full Revit restart to show up
# (same "shared engine does not reread lib/" gotcha as lib/vgrow.py, which
# every one of its callers reloads for the same reason).
try:
    reload(selmgr)
except Exception:
    pass

with usage.tool_run(__file__) as run:
    try:
        selmgr.open_window()
    except Exception:
        run.error()
        traceback.print_exc()
        try:
            from slantisui import ui
            ui.alert(traceback.format_exc(), title=u"Selection Manager - Error")
        except Exception:
            pass
