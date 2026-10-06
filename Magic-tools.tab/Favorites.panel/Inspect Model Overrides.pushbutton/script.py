# -*- coding: utf-8 -*-
__title__ = "Inspect\nModel Overrides"
__author__ = 'slantis'
__doc__ = "Reports which views carry local element-level overrides or hidden elements -- the stray ones a view template does not control, so this is how you find them. Opens on a scope picker: pick the views by hand, narrow them to a print set, or tick 'only views placed on sheets' so you are not scanning what nobody prints. Then a sortable grid (click the headers): double-click a row to open that view, or press Inspect view to jump to it and open Inspect View on it in one step; high-count rows are highlighted."
# The window and every handler live in lib/inspectmodel.py, not here: under
# rocket mode Magic Tools shares one engine across clicks and tears it down
# when this script returns, which would kill every handler a modeless window
# relies on.
# Hosting the window in a lib module keeps it alive in sys.modules for as long
# as Revit runs, the same shape lib/findroom.py uses -- so this door stays a
# persistent engine WITHOUT __cleanengine__ (that flag reimports
# lib/inspectmodel.py and lib/modeless.py on every click, which made a second
# click reopen the scope dialog instead of focusing the open table).
__persistentengine__ = True

import os
import traceback

from pyrevit import EXEC_PARAMS

import inspectmodel

# The tool this one chains into (its "Inspect view" button): same panel, one
# folder over. Resolved here, from this bundle's own path, so each copy of the
# extension that Revit loads chains into its own Inspect View (lib/launch.py).
try:
    _HERE = EXEC_PARAMS.command_path
except Exception:
    _HERE = os.path.dirname(os.path.abspath(__file__))
INSPECT_VIEW_BUNDLE = os.path.join(os.path.dirname(_HERE), "Inspect View Overrides.pushbutton")

try:
    inspectmodel.open_window(INSPECT_VIEW_BUNDLE)
except Exception:
    traceback.print_exc()
