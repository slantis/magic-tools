# -*- coding: utf-8 -*-
"""
sheet_nav.py - shared helper for Previous Sheet / Next Sheet tools.

Returns the list of sheets in the order AND with the filter that
the current Project Browser organization has active.

The Revit API surface on BrowserOrganization is limited and undocumented
in parts, so every step tries the most likely API and falls back gracefully.
"""
import clr
clr.AddReference('RevitAPI')
from Autodesk.Revit.DB import (
    FilteredElementCollector, ViewSheet, Viewport,
    BrowserOrganization, StorageType,
)

def _id_val(eid):
    # ElementId value compat Revit 2022-2026 (IntegerValue removed in 2026)
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue



def _try_apply_filter(doc, bo, sheets):
    """
    Attempt to read the ElementFilter stored in the active browser
    organization and use it to drop sheets that the browser hides.

    BrowserOrganization may expose filter ids via:
      - bo.GetFilters()               -> IList<ElementId>
      - bo.Filter                     -> single ElementId
      - a parameter named 'Filter'    -> ElementId value

    Each filter element (typically ParameterFilterElement) exposes
    GetElementFilter() -> ElementFilter, which we then apply.

    If none of these work we return the original list unchanged.
    """
    try:
        filter_ids = []

        if hasattr(bo, 'GetFilters'):
            ids = bo.GetFilters()
            if ids:
                filter_ids = list(ids)

        if not filter_ids and hasattr(bo, 'Filter'):
            fid = bo.Filter
            if fid and _id_val(fid) > 0:
                filter_ids = [fid]

        if not filter_ids:
            # Last resort: scan parameters for one whose value is an ElementId
            # pointing to a ParameterFilterElement
            from Autodesk.Revit.DB import ParameterFilterElement, StorageType as ST
            for p in bo.Parameters:
                if p.StorageType == ST.ElementId:
                    eid = p.AsElementId()
                    if eid and _id_val(eid) > 0:
                        candidate = doc.GetElement(eid)
                        if isinstance(candidate, ParameterFilterElement):
                            filter_ids = [eid]
                            break

        if not filter_ids:
            return sheets  # No filter found - show all

        # Apply each filter (AND logic, same as Revit)
        result = list(sheets)
        for fid in filter_ids:
            fe = doc.GetElement(fid)
            if fe is None or not hasattr(fe, 'GetElementFilter'):
                continue
            ef = fe.GetElementFilter()
            if ef is None:
                continue
            # PassesFilter accepts an Element directly
            try:
                result = [s for s in result if ef.PassesFilter(s)]
            except Exception:
                # Fallback: some ElementFilters require (Document, ElementId)
                try:
                    result = [s for s in result if ef.PassesFilter(doc, s.Id)]
                except Exception:
                    pass  # Skip this filter if both signatures fail

        return result

    except Exception:
        return sheets  # Any failure -> return unfiltered


def _try_apply_sort(bo, sheets):
    """
    Attempt to sort the sheet list using the primary sort criterion from the
    active browser organization.

    BrowserOrganization.GetSortingAndGroupingCriteria() should return
    IList<FolderItemInfo> where each item has:
      - ParameterId : ElementId of the parameter used for sorting/grouping
      - IsAscending : bool

    Falls back to SheetNumber (string) sort if the API is not available.
    """
    try:
        criteria = bo.GetSortingAndGroupingCriteria()
        if criteria is None or criteria.Count == 0:
            raise AttributeError("No criteria")

        primary = criteria[0]
        param_id  = primary.ParameterId
        ascending = getattr(primary, 'IsAscending', True)

        def sort_key(sheet):
            p = sheet.get_Parameter(param_id)
            if p is None:
                return ""
            if p.StorageType == StorageType.String:
                return p.AsString() or ""
            if p.StorageType == StorageType.Integer:
                # Return as zero-padded string so numeric order is preserved
                return "{:020d}".format(p.AsInteger())
            if p.StorageType == StorageType.Double:
                return "{:030.10f}".format(p.AsDouble())
            return p.AsValueString() or ""

        return sorted(sheets, key=sort_key, reverse=not ascending)

    except Exception:
        # Default: sort by SheetNumber (matches Revit's default browser order)
        return sorted(sheets, key=lambda s: s.SheetNumber)


def get_browser_sheets(doc):
    """
    Return the list of non-placeholder sheets visible in the Project Browser,
    in the order the browser shows them, with the active filter applied.

    If the BrowserOrganization API does not expose the needed information,
    falls back to all sheets sorted by SheetNumber.
    """
    all_sheets = [
        s for s in FilteredElementCollector(doc)
            .OfClass(ViewSheet)
            .WhereElementIsNotElementType()
            .ToElements()
        if not s.IsPlaceholder
    ]

    try:
        bo = BrowserOrganization.GetCurrentBrowserOrganizationForSheets(doc)
        sheets = _try_apply_filter(doc, bo, all_sheets)
        sheets = _try_apply_sort(bo, sheets)
        return sheets
    except Exception:
        return sorted(all_sheets, key=lambda s: s.SheetNumber)


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
