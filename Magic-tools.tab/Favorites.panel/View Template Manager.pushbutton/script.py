# -*- coding: utf-8 -*-
__title__ = "View Template\nManager"
__author__ = 'slantis'
__doc__ = ("Visibility/Graphics for many view templates at once, as a matrix: "
           "tick view templates on the left and they become columns; the rows "
           "are categories (Model and Annotation tabs) or filters (Filters tab). "
           "Each cell shows Visible / Hidden / N/A plus its overrides; click a "
           "chip to stage a change on one cell, or open Selected rows for bulk "
           "actions on the ticked rows, then Apply writes everything in one "
           "transaction. Replaces Category Manager and Batch Filter Override "
           "(2026-09-19).")
# The window and every handler live in lib/vtm.py, not here: under rocket
# mode Magic Tools shares one engine across clicks and tears it down when
# this script returns, which would kill every handler a modeless window
# relies on. Hosting the window in a lib module keeps it alive in sys.modules
# for as long as Revit runs, the same shape lib/toolwindow.py uses for All
# Magic Tools -- so this door stays a persistent engine WITHOUT
# __cleanengine__.
__persistentengine__ = True

import traceback

import vtm

try:
    vtm.open_window()
except Exception:
    traceback.print_exc()
    try:
        from slantisui import ui
        ui.alert(traceback.format_exc(), title=u"View Template Manager - Error")
    except Exception:
        pass
