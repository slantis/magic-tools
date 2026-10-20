__title__ = "Parent"
__doc__ = "Navigate to the sheet where the active view is placed. Works for views, legends, schedules and panel schedules; when the view is on more than one sheet, pick which one to open."
__author__ = 'slantis'


import re

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
from Autodesk.Revit.DB import (
    FilteredElementCollector, ViewSheet, Viewport, ViewSchedule,
    ScheduleSheetInstance,
)
from Autodesk.Revit.DB.Electrical import PanelScheduleView, PanelScheduleSheetInstance
from pyrevit import revit, script
from slantisui import ui
import usage
import traceback

doc = revit.doc
uidoc = revit.uidoc

_DIGITS = re.compile(r'([0-9]+)')


def _id_val(eid):
    # ElementId value compat Revit 2022-2026 (IntegerValue removed in 2026)
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def _natural_key(text):
    """A-2 before A-10: split() puts the digit runs at the odd indexes."""
    return tuple((0, int(c), u"") if i % 2 else (1, 0, c.lower())
                 for i, c in enumerate(_DIGITS.split(text or u"")) if c)


def _sheets_showing(view):
    """Every sheet the view is placed on. A plan or section sits on one sheet;
    a legend or a schedule can sit on many. Schedules are placed as
    ScheduleSheetInstance and panel schedules as PanelScheduleSheetInstance,
    not Viewport; revision schedules inside title blocks are not placements
    of the view and are skipped."""
    vid = _id_val(view.Id)
    sheet_ids = []

    for vp in FilteredElementCollector(doc) \
            .OfClass(Viewport) \
            .WhereElementIsNotElementType():
        if _id_val(vp.ViewId) == vid:
            sheet_ids.append(vp.SheetId)

    if isinstance(view, ViewSchedule):
        for si in FilteredElementCollector(doc) \
                .OfClass(ScheduleSheetInstance) \
                .WhereElementIsNotElementType():
            if not si.IsTitleblockRevisionSchedule and _id_val(si.ScheduleId) == vid:
                sheet_ids.append(si.OwnerViewId)

    if isinstance(view, PanelScheduleView):
        for si in FilteredElementCollector(doc) \
                .OfClass(PanelScheduleSheetInstance) \
                .WhereElementIsNotElementType():
            if _id_val(si.ScheduleId) == vid:
                sheet_ids.append(si.OwnerViewId)

    sheets, seen = [], set()
    for sid in sheet_ids:
        if _id_val(sid) in seen:
            continue
        seen.add(_id_val(sid))
        sheet = doc.GetElement(sid)
        if isinstance(sheet, ViewSheet):
            sheets.append(sheet)
    return sorted(sheets, key=lambda s: _natural_key(s.SheetNumber))


with usage.tool_run(__file__) as run:
    try:
        active_view = uidoc.ActiveView

        if isinstance(active_view, ViewSheet):
            ui.alert("Already on a sheet.", title="Parent Sheet")
            script.exit()

        sheets = _sheets_showing(active_view)

        if not sheets:
            ui.alert(
                u"'{}' is not placed on any sheet.".format(active_view.Name),
                title="Parent Sheet"
            )
            script.exit()

        if len(sheets) == 1:
            target = sheets[0]
        else:
            target = ui.pick_list(
                sheets,
                title="Parent Sheet",
                subtitle=u"'{}' is on {} sheets. Pick the one to open.".format(
                    active_view.Name, len(sheets)),
                button_name="Open",
                multiselect=False,
                columns=[("Number", 110), ("Name", "*")],
                context=u"The view is placed on several sheets. Pick the one to open.",
                name_fn=lambda s: (s.SheetNumber, s.Name),
            )
            if target is None:
                script.exit()

        uidoc.RequestViewChange(target)

    except Exception:
        run.error()
        traceback.print_exc()
        ui.alert(u"Parent Sheet failed:\n{}".format(traceback.format_exc()),
                 title="Parent Sheet")
