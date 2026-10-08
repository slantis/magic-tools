# -*- coding: utf-8 -*-
__title__ = "Select\nSame Family"
__doc__ = "Grows the current selection to every element of the same family visible in the active view, whatever its type: select one window and get every window of that family on this view, in all its sizes. Works for loadable families and for system families (walls, floors, ducts) alike. With nothing selected it asks you to pick elements first."
__author__ = 'slantis'

# See Select Same Type: same quick action, one level up. A FamilyInstance has a
# Family; a system family element (wall, floor, pipe...) has none, so for those
# the family is the FamilyName of its type within its category, which is how
# Revit itself names them in the type selector.
import traceback

from Autodesk.Revit.DB import (ElementId, FamilyInstance,
                               FilteredElementCollector)
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


TITLE = "Select Same Family"

doc = revit.doc
uidoc = revit.uidoc


def family_key(element):
    """What 'same family' means for this element, or None."""
    try:
        if isinstance(element, FamilyInstance):
            fam = element.Symbol.Family
            return ('fam', _id_val(fam.Id))
        tid = element.GetTypeId()
        if tid is None or tid == ElementId.InvalidElementId:
            return None
        etype = doc.GetElement(tid)
        if etype is None or element.Category is None:
            return None
        return ('sys', _id_val(element.Category.Id), etype.FamilyName)
    except Exception:
        return None


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
            "Pick elements to select their family. Click adds or removes, "
            "Finish to confirm, Esc to cancel.")
    except OperationCanceledException:
        return []              # Esc: nothing to do
    return [doc.GetElement(r.ElementId) for r in refs]


with usage.tool_run(__file__) as run:
    try:
        view = doc.ActiveView
        seeds = [e for e in seed_elements() if e is not None]
        if not seeds:
            script.exit()

        keys = set(k for k in (family_key(e) for e in seeds) if k is not None)
        if not keys:
            ui.alert("None of the selected elements belongs to a family "
                     "(elements with no type at all, like a curtain grid "
                     "line, do not).", title=TITLE)
            script.exit()

        found = []
        for e in (FilteredElementCollector(doc, view.Id)
                  .WhereElementIsNotElementType()):
            if family_key(e) in keys:
                found.append(e.Id)

        if not found:
            ui.alert("No element of that family is visible in this view.",
                     title=TITLE)
            script.exit()
        uidoc.Selection.SetElementIds(List[ElementId](found))
    except Exception as ex:
        run.error()
        traceback.print_exc()
        try:
            ui.alert("Select Same Family ran into an error:\n{}".format(ex),
                     title=TITLE)
        except Exception:
            pass
