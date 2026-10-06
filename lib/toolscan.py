# -*- coding: utf-8 -*-
"""One number per tile: what the model holds, before the tool is opened.

Requested 2026-09-04: a scan button that gives the values of the things the
cleaning tools act on, so nobody goes in blind. Sixteen identical tiles say
nothing about which one has work in it, and finding out costs opening the tool,
running it and reading its dialog. This module is the number that saves that.

THE RULE THAT SHAPES EVERY COUNTER IN HERE:

    Only how many there are, not how many should be cleaned up. The user
    decides whether 30 line styles deserve a merge or not.

So the default is a CENSUS -- how many of the thing exist -- and not an
analysis. The exception carved out the same day is the purges: showing the
purgeable ones is worth it, but not the complicated ones. Which gives
the one line that decides what each counter returns:

    the number is what is PURGEABLE when that comes out of one pass;
    it is the CENSUS when deciding would need the user's criteria, or a sweep
    of the whole model.

That is not a style preference, it is what keeps this file honest. The merge
tools (filled regions, dimension styles, text types) all decide what counts as a
duplicate from checkboxes the user ticks in the wizard (use_back / use_lw /
use_mask, compare_params), so any "duplicates" number computed here would be an
answer to a question the user has not been asked yet -- and would go stale the
day the wizard's defaults move. A census cannot lie that way.

The scope box counter is the purge that stayed a census, and its reason is
cost: the tool finds the unused ones by walking EVERY non-type element in the
document calling LookupParameter("Scope Box") on each. That is
seconds on a real model, for one badge.

WHY IT DOES NOT IMPORT THE TOOLS. Every script.py of the tools counted here runs
its work at import time (doc = revit.doc at module level, collectors right
after, dialogs and script.exit() in the body), so there is nothing importable in
them: asking a tool for its count would RUN it. The universes below are
therefore re-read from the same API the tools read, and the docstring of each
counter says what the tool looks at, so a tool and its counter can be matched
by that description the day the tool changes what it looks at. A counter that
quietly drifts from its tool is the one failure mode of this design.

KEYED BY THE MASTER NAME of the tool (the name toolpane.pane_groups uses), never
by the folder name, which differs between extensions -- toolpane.master_key()
maps one to the other through the ALIASES table that already exists.
"""
from pyrevit import DB

def _id_val(eid):
    # ElementId value compat Revit 2022-2026 (IntegerValue removed in 2026)
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue



def _name(element):
    """An element's name, the way it survives classes that hide the property."""
    try:
        return DB.Element.Name.__get__(element)
    except Exception:
        try:
            return element.Name
        except Exception:
            return u""


def _line_styles(doc):
    """The editable line styles: Lines' subcategories, minus the system ones.

    Same universe as BOTH line style tools, which read it identically:
    the projection graphics style of every subcategory of OST_Lines, with the
    names in angle brackets left out -- <Sketch>, <Hidden> and friends cannot be
    merged or renamed, so counting them would inflate the badge with styles no
    line style tool can touch.
    """
    category = doc.Settings.Categories.get_Item(DB.BuiltInCategory.OST_Lines)
    if category is None:
        return 0
    found = 0
    for subcat in category.SubCategories:
        try:
            style = subcat.GetGraphicsStyle(DB.GraphicsStyleType.Projection)
        except Exception:
            continue
        if not style:
            continue
        if _name(style).startswith(u"<"):
            continue
        found += 1
    return found


def _line_patterns(doc):
    """Line pattern elements, as the line pattern tools read them.

    The built-in Solid is not one of them (it has no LinePatternElement), which
    is also why the merge tool adds a synthetic "<Solid>" entry to its own
    list: that entry is a UI affordance, not a thing in the model, so it is not
    counted here.
    """
    return (DB.FilteredElementCollector(doc)
            .OfClass(DB.LinePatternElement)
            .GetElementCount())


def _scope_boxes(doc):
    """Census. See the note at the top for why this one is not the purgeable."""
    return (DB.FilteredElementCollector(doc)
            .OfCategory(DB.BuiltInCategory.OST_VolumeOfInterest)
            .WhereElementIsNotElementType()
            .GetElementCount())


def _unused_filters(doc):
    """Filters applied to no view."""
    used = set()
    for view in DB.FilteredElementCollector(doc).OfClass(DB.View).ToElements():
        try:
            for fid in view.GetFilters():
                used.add(fid)
        except Exception:
            pass
    total = 0
    for f in (DB.FilteredElementCollector(doc)
              .OfClass(DB.ParameterFilterElement).ToElements()):
        if f.Id not in used:
            total += 1
    return total


def _unused_templates(doc):
    """Templates assigned to no view.

    The tool collects every View and splits it in the same pass -- IsTemplate
    ones are the candidates, the rest are read for their ViewTemplateId -- so
    one collector serves both halves here too.
    """
    views = DB.FilteredElementCollector(doc).OfClass(DB.View).ToElements()
    used = set()
    templates = []
    for view in views:
        if view.IsTemplate:
            templates.append(view)
            continue
        try:
            tid = view.ViewTemplateId
            if tid and tid != DB.ElementId.InvalidElementId:
                used.add(tid)
        except Exception:
            pass
    return len([t for t in templates if t.Id not in used])


def _lonely_groups(doc):
    """Group types with exactly one placed instance.

    Model and detail together, which is what the tool ungroups: it buckets every
    DB.Group instance by its type id and keeps the buckets of one.
    """
    by_type = {}
    for group in (DB.FilteredElementCollector(doc).OfClass(DB.Group)
                  .WhereElementIsNotElementType().ToElements()):
        try:
            tid = group.GetTypeId()
            if tid is None or tid == DB.ElementId.InvalidElementId:
                continue
            by_type[_id_val(tid)] = by_type.get(_id_val(tid), 0) + 1
        except Exception:
            pass
    return len([1 for count in by_type.values() if count == 1])


def _filled_region_types(doc):
    """Census. The filled region merge tool uses the same collector."""
    return (DB.FilteredElementCollector(doc)
            .OfClass(DB.FilledRegionType).GetElementCount())


def _dim_types(doc):
    """Census. The dimension style merge tool uses the same collector."""
    return (DB.FilteredElementCollector(doc)
            .OfClass(DB.DimensionType).GetElementCount())


def _text_types(doc):
    """Census. The text type merge tool uses the same collector."""
    return (DB.FilteredElementCollector(doc)
            .OfClass(DB.TextNoteType)
            .WhereElementIsElementType().GetElementCount())


def _in_place(doc):
    """In-place family instances.

    Instances and not families, deliberately: that is what the tool lists, and
    two instances of one in-place family are two things to look at.

    The heaviest of the cheap counters, because there is no collector filter for
    "is in place" -- it walks every FamilyInstance in the document asking its
    symbol's family.
    """
    total = 0
    for fi in (DB.FilteredElementCollector(doc)
               .WhereElementIsNotElementType().OfClass(DB.FamilyInstance)):
        try:
            if fi.Symbol.Family.IsInPlace:
                total += 1
        except Exception:
            pass
    return total


def _unplaced_rooms(doc):
    """Unplaced PLUS not-enclosed, which is the total the tool reports.

    A room with no Location is unplaced, a placed room with Area 0 is not
    enclosed, and an unreadable one is counted as unplaced. Two problems, one
    badge, because the tool treats them as one list.
    """
    total = 0
    for room in (DB.FilteredElementCollector(doc)
                 .OfCategory(DB.BuiltInCategory.OST_Rooms)
                 .WhereElementIsNotElementType()):
        try:
            if room.Location is None or room.Area == 0:
                total += 1
        except Exception:
            total += 1
    return total


def _ref_planes(doc):
    """Every reference plane.

    The one tool whose census IS its output: it reports the whole set with a
    named / unnamed / pinned summary, so the badge is the size of what it will
    show rather than a count of anything wrong.
    """
    return (DB.FilteredElementCollector(doc)
            .OfClass(DB.ReferencePlane)
            .WhereElementIsNotElementType().GetElementCount())


def _unpinned_links(doc):
    """Links not pinned yet, which is exactly what the tool would change.

    The pin links tool pins every RevitLinkInstance that is not pinned
    already and counts the two groups; this is that first group.
    """
    total = 0
    for link in (DB.FilteredElementCollector(doc)
                 .OfClass(DB.RevitLinkInstance).ToElements()):
        try:
            if not link.Pinned:
                total += 1
        except Exception:
            pass
    return total


# key -> (counter, sentence). {0} is the number, {1} the plural s and {2} the
# y/ies ending, so the detail panel never prints "1 line styles".
#
# NOT IN HERE ON PURPOSE: a phase check. Its subject is not a kind of element
# (it compares the phase of elements against the phase of their view), so there
# is nothing to count that would mean anything. Such a tile has no badge, and
# it is left visible as an exception rather than given a number that says
# nothing.
COUNTERS = {
    "Scope Boxes":
        (_scope_boxes, u"{0} scope box{2} in the model"),
    "Purge Unused Filters":
        (_unused_filters, u"{0} filter{1} applied to no view"),
    "Purge Unused VT":
        (_unused_templates, u"{0} view template{1} assigned to no view"),
    "Purge Lonely Groups":
        (_lonely_groups, u"{0} group type{1} with a single instance"),
    "Merge Filled Regions":
        (_filled_region_types, u"{0} filled region type{1} in the model"),
    "Merge Dim Styles":
        (_dim_types, u"{0} dimension type{1} in the model"),
    "Merge Text Types":
        (_text_types, u"{0} text type{1} in the model"),
    "Line Style Cleaner":
        (_line_styles, u"{0} line style{1} in the model"),
    "Merge Line Styles":
        (_line_styles, u"{0} line style{1} in the model"),
    "Analyze and Delete Line Patterns":
        (_line_patterns, u"{0} line pattern{1} in the model"),
    "Search and Merge Line Patterns":
        (_line_patterns, u"{0} line pattern{1} in the model"),
    "Family In-Place":
        (_in_place, u"{0} in-place famil{3} in the model"),
    "Replace In-Place":
        (_in_place, u"{0} in-place famil{3} in the model"),
    "Unplaced Rooms":
        (_unplaced_rooms, u"{0} room{1} unplaced or not enclosed"),
    "Audit Ref Planes":
        (_ref_planes, u"{0} reference plane{1} in the model"),
    "Pin All Links":
        (_unpinned_links, u"{0} link{1} not pinned yet"),
}


def sentence(key, count):
    """The tool's number as a line of English, or None if it has no counter."""
    entry = COUNTERS.get(key)
    if entry is None or count is None:
        return None
    one = (count == 1)
    return entry[1].format(count,
                           u"" if one else u"s",       # plural s
                           u"" if one else u"es",      # box / boxes
                           u"y" if one else u"ies")    # family / families


def scan(doc, keys):
    """(counts, failed): one number per key that has a counter.

    A counter that throws lands in `failed` and NOT in `counts` with a zero: a
    badge saying 0 where the truth is "could not read" is the one thing worse
    than no badge. The caller shows the failures instead of hiding them.

    The counters that share a universe (the two line style tools, the two line
    pattern ones) are computed ONCE and copied, which is both cheaper and the
    only way the two tiles cannot disagree. The number was asked for on each of
    the four even though it repeats, to favour consistency, and a repeated
    number computed twice is a number that can differ.
    """
    counts, failed, cache = {}, [], {}
    for key in keys:
        entry = COUNTERS.get(key)
        if entry is None:
            continue
        fn = entry[0]
        if fn in cache:
            counts[key] = cache[fn]
            continue
        try:
            value = int(fn(doc))
        except Exception:
            failed.append(key)
            continue
        cache[fn] = value
        counts[key] = value
    return counts, failed
