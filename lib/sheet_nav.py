# -*- coding: utf-8 -*-
"""
sheet_nav.py - shared helper for Previous Sheet / Next Sheet tools.

Returns the list of sheets in the order AND with the filter that
the current Project Browser organization has active, read from the
public BrowserOrganization API (checked in the 2022 to 2026 API):
  - AreFiltersSatisfied(id)      -> is the sheet shown by the browser filter
  - GetFolderItems(id)           -> the folder path the sheet sits in
  - SortingParameterId / SortingOrder -> the sort inside each folder

Folders are ordered ascending by name and every text compares in natural
order (A-2 before A-10). Both are the closest guess to what the browser
draws; they still need to be checked against the browser in Revit.
"""
import re
import traceback

import clr
clr.AddReference('RevitAPI')
from Autodesk.Revit.DB import (
    FilteredElementCollector, ViewSheet, Viewport,
    BrowserOrganization, SortingOrder, StorageType,
)

_DIGITS = re.compile(r'([0-9]+)')


def _id_val(eid):
    # ElementId value compat Revit 2022-2026 (IntegerValue removed in 2026)
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def _natural_key(text):
    """Sort key where digit runs compare as numbers: A-2 before A-10."""
    # split() with a capturing group puts the digit runs at the odd indexes
    return tuple((0, int(c), u"") if i % 2 else (1, 0, c.lower())
                 for i, c in enumerate(_DIGITS.split(text or u"")) if c)


def _param_text(sheet, param_id):
    """Value of the browser's sorting parameter on this sheet, as text.
    Matched by Parameter.Id so built-in, project and shared parameters
    all work the same way."""
    target = _id_val(param_id)
    for p in sheet.Parameters:
        if _id_val(p.Id) == target:
            if p.StorageType == StorageType.String:
                return p.AsString() or u""
            return p.AsValueString() or u""
    return u""


def _folder_key(bo, sheet):
    return tuple(_natural_key(f.Name) for f in bo.GetFolderItems(sheet.Id))


def _browser_order(bo, sheets):
    """Filter and sort `sheets` (already in sheet number order) like the browser."""
    shown = [s for s in sheets if bo.AreFiltersSatisfied(s.Id)]

    # Stable sorts, least significant first: sheet number (already applied),
    # then the sorting parameter, then the folder path.
    # Ties keep sheet number order explicitly (the position goes in the key),
    # so a descending sort does not depend on reverse=True being stable.
    param_id = bo.SortingParameterId
    if param_id is not None and _id_val(param_id) != -1:
        pos = dict((_id_val(s.Id), i) for i, s in enumerate(shown))
        if bo.SortingOrder == SortingOrder.Descending:
            shown.sort(key=lambda s: (_natural_key(_param_text(s, param_id)),
                                      -pos[_id_val(s.Id)]), reverse=True)
        else:
            shown.sort(key=lambda s: (_natural_key(_param_text(s, param_id)),
                                      pos[_id_val(s.Id)]))
    shown.sort(key=lambda s: _folder_key(bo, s))
    return shown


def get_browser_sheets(doc):
    """
    Return the list of non-placeholder sheets visible in the Project Browser,
    in the order the browser shows them, with the active filter applied.

    If the browser organization cannot be read, falls back to all sheets
    in sheet number order.
    """
    sheets = sorted(
        [s for s in FilteredElementCollector(doc)
             .OfClass(ViewSheet)
             .WhereElementIsNotElementType()
             .ToElements()
         if not s.IsPlaceholder],
        key=lambda s: _natural_key(s.SheetNumber),
    )

    try:
        bo = BrowserOrganization.GetCurrentBrowserOrganizationForSheets(doc)
        if bo is None:
            return sheets
        return _browser_order(bo, sheets)
    except Exception:
        # All or nothing: a half-read organization would give a mixed order.
        from pyrevit import script
        script.get_logger().debug(traceback.format_exc())
        return sheets


def get_current_sheet(doc, uidoc):
    """
    Return the ViewSheet that is currently active (or the sheet that
    contains the active view if the active view is not itself a sheet).
    Returns None if the active view is not placed on any sheet.
    """
    active_view = uidoc.ActiveView

    if isinstance(active_view, ViewSheet):
        return active_view

    for vp in FilteredElementCollector(doc) \
            .OfClass(Viewport) \
            .WhereElementIsNotElementType() \
            .ToElements():
        if vp.ViewId == active_view.Id:
            return doc.GetElement(vp.SheetId)

    return None
