# -*- coding: utf-8 -*-
__title__ = "Print Set\nManager"
__doc__ = ("Shows which sheets AND views belong to each Print Set and lets "
           "you edit that from one window: add or remove sheets, keep or "
           "drop any non-sheet view (floor plan, 3D view...) already in the "
           "set, create, rename or delete sets, and sort the sheet list by "
           "any sheet parameter, without opening the Print dialog.")
# The window and every handler live in lib/printsetmanager.py, not here:
# under rocket mode Magic Tools shares one engine across clicks and tears it
# down when this script returns, which would kill every handler a modeless
# window relies on. Hosting the window in a lib module keeps it alive in
# sys.modules for as long as Revit runs, the same shape lib/vtm.py uses for
# View Template Manager -- so this door stays a persistent engine WITHOUT
# __cleanengine__. That flag used to be here (until 2026-09-25) to keep the
# handlers alive; dropping it fixes a side effect it had instead:
# __cleanengine__ reimports every lib/ module -- modeless.py included -- on
# every click, so modeless._OPEN was always empty and a second click on the
# ribbon button opened a new window rather than focusing the one already
# open (QA, 2026-09-24). See printsetmanager.open_window() for the
# full explanation.
__persistentengine__ = True

import traceback

import printsetmanager

try:
    printsetmanager.open_window()
except Exception:
    traceback.print_exc()
    try:
        from slantisui import ui
        ui.alert(traceback.format_exc(), title=u"Print Set Manager - Error")
    except Exception:
        pass
