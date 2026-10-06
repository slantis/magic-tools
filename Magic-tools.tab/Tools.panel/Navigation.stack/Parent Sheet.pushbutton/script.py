__title__ = "Parent"
__doc__ = "Navigate to the sheet where the active view is placed as a viewport."
__author__ = 'slantis'


import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
from Autodesk.Revit.DB import FilteredElementCollector, ViewSheet, Viewport
from pyrevit import revit, script
from slantisui import ui
import traceback

doc = revit.doc
uidoc = revit.uidoc

try:
    active_view = uidoc.ActiveView

    if isinstance(active_view, ViewSheet):
        ui.alert("Already on a sheet.", title="Parent Sheet")
        script.exit()

    # Find the sheet that has this view placed as a viewport
    parent_sheet = None
    for vp in FilteredElementCollector(doc) \
            .OfClass(Viewport) \
            .WhereElementIsNotElementType() \
            .ToElements():
        if vp.ViewId == active_view.Id:
            parent_sheet = doc.GetElement(vp.SheetId)
            break

    if parent_sheet is None:
        ui.alert(
            "'{}' is not placed on any sheet.".format(active_view.Name),
            title="Parent Sheet"
        )
        script.exit()

    uidoc.RequestViewChange(parent_sheet)

except Exception:
    traceback.print_exc()
