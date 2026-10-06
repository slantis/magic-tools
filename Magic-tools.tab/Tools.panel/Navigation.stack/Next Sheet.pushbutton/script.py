__title__ = "Next"
__doc__ = "Navigate to the next sheet in Project Browser order. Wraps around from the last sheet back to the first."
__author__ = 'slantis'


from sheet_nav import get_browser_sheets, get_current_sheet
from slantisui import ui
import usage

from pyrevit import revit, script
import traceback

doc   = revit.doc
uidoc = revit.uidoc

with usage.tool_run(__file__) as run:
    try:
        sheets = get_browser_sheets(doc)

        if not sheets:
            ui.alert("No sheets found in the model.", title="Next Sheet")
            script.exit()

        current_sheet = get_current_sheet(doc, uidoc)

        if current_sheet is None:
            ui.alert("Active view is not placed on any sheet.", title="Next Sheet")
            script.exit()

        current_idx = next(
            (i for i, s in enumerate(sheets) if s.Id == current_sheet.Id), -1
        )

        if current_idx == -1:
            # Current sheet is not in the browser-visible list (filtered out).
            # Land on the first sheet in the visible list.
            uidoc.RequestViewChange(sheets[0])
            script.exit()

        next_sheet = sheets[(current_idx + 1) % len(sheets)]
        uidoc.RequestViewChange(next_sheet)

    except Exception:
        run.error()
        traceback.print_exc()
