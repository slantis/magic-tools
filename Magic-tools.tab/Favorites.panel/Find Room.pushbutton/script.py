# -*- coding: utf-8 -*-
__title__ = "Find\nRoom"
__doc__ = ("Search rooms by name, number, or level with live filtering. Selects "
           "matching rooms in the model and zooms the active view to fit. Flags "
           "open (not enclosed) and redundant rooms with a status icon, and a "
           "Refresh button re-reads the model without losing the search text.")
__author__ = 'slantis'
# The window and every handler live in lib/findroom.py, not here: under
# rocket mode Magic Tools shares one engine across clicks and tears it down
# when this script returns, which would kill every handler a modeless window
# relies on.
# Hosting the window in a lib module keeps it alive in sys.modules for as
# long as Revit runs, the same shape lib/vtm.py uses for View Template
# Manager -- so this door stays a persistent engine WITHOUT __cleanengine__
# (that flag reimports lib/findroom.py and lib/modeless.py on every click,
# which was making a second click open a second window instead of focusing
# the first).
__persistentengine__ = True

import traceback

import findroom

try:
    findroom.open_window()
except Exception:
    traceback.print_exc()
