# -*- coding: utf-8 -*-
__title__ = "Select\nSame Type"
__doc__ = "Grows the current selection to every element of the same type visible in the active view. Select one door and get every door of that type on this view; select several elements of several types and get them all. With nothing selected it asks you to pick elements first."
__author__ = 'slantis'

# THE SELECTION QUICK ACTIONS (2026-09-05). Design decision: Magic Tools had no
# arrow menu of "selection tools", so it gets one: this and Select Same Family
# are the two one-click ones, Selection Manager is the pro one. Scope is the
# ACTIVE VIEW on purpose: a quick action that grabs the whole model selects
# things you cannot see and then you move them.
import traceback

from Autodesk.Revit.DB import ElementId, FilteredElementCollector
from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.UI.Selection import ObjectType
from System.Collections.Generic import List

from pyrevit import revit, script
from slantisui import ui
import usage

def _id_val(eid):
    # ElementId value compat Revit 2022-2026 (IntegerValue removed in 2026)
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


TITLE = "Select Same Type"

doc = revit.doc
uidoc = revit.uidoc


def seed_elements():
    """The elements to match: the selection, or elements picked in one
    multi-pick session (click adds or removes, Tab cycles overlaps, Finish
    confirms, Esc cancels: native PickObjects; Enter does NOT confirm in
    Revit 2025)."""
    ids = list(uidoc.Selection.GetElementIds())
    if ids:
        return [doc.GetElement(i) for i in ids]
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element,
            "Pick elements to select their type. Click adds or removes, "
            "Finish to confirm, Esc to cancel.")
    except OperationCanceledException:
        return []              # Esc in the pick: nothing to do
    return [doc.GetElement(r.ElementId) for r in refs]


with usage.tool_run(__file__) as run:
    try:
        view = doc.ActiveView
        seeds = [e for e in seed_elements() if e is not None]
        if not seeds:
            script.exit()

        type_ids = set()
        for e in seeds:
            try:
                tid = e.GetTypeId()
            except Exception:
                continue
            if tid is not None and tid != ElementId.InvalidElementId:
                type_ids.add(_id_val(tid))
        if not type_ids:
            ui.alert("None of the selected elements has a type to match "
                     "(elements with no type at all, like a curtain grid "
                     "line, do not).", title=TITLE)
            script.exit()

        found = []
        for e in (FilteredElementCollector(doc, view.Id)
                  .WhereElementIsNotElementType()):
            try:
                tid = e.GetTypeId()
            except Exception:
                continue
            if tid is not None and _id_val(tid) in type_ids:
                found.append(e.Id)

        if not found:
            ui.alert("No element of that type is visible in this view.",
                     title=TITLE)
            script.exit()
        # Quiet on success: Revit's own status bar shows the count, and a dialog
        # after every quick action would defeat the point of a quick action.
        uidoc.Selection.SetElementIds(List[ElementId](found))
    except Exception as ex:
        run.error()
        traceback.print_exc()
        try:
            ui.alert("Select Same Type ran into an error:\n{}".format(ex),
                     title=TITLE)
        except Exception:
            pass
