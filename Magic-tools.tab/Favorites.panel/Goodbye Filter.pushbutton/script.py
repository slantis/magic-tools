# -*- coding: utf-8 -*-
__title__ = "Goodbye\nFilter"
__doc__ = "Turns off every filter in the active view through Temporary View Properties mode, so nothing is permanently changed. Click again on the same view to restore. Views that can't use the temporary mode (sheets, schedules) are left untouched, with a clear message."
__author__ = 'slantis'

from pyrevit import revit, DB, script
from slantisui import ui

doc = revit.doc
view = doc.ActiveView

if not view:
    ui.alert("No active view.", title="Goodbye Filter")
    script.exit()

# --- Views that can't use Temporary View Properties (sheets, schedules...) ---
# View.CanEnableTemporaryViewPropertiesMode() is the API's own answer to
# "does this view support the mode" -- no need to branch on ViewType by hand,
# and it never opens a Transaction that would need rolling back.
if not view.CanEnableTemporaryViewPropertiesMode():
    ui.alert(
        "This view can't use temporary filters.",
        title="Goodbye Filter",
        context=u"Sheets and schedules don't support the temporary view "
                u"properties mode. Nothing was changed."
    )
    script.exit()

# --- Second click on a view already in temporary mode: act as a restore ---
if view.IsTemporaryViewPropertiesModeEnabled():
    t = DB.Transaction(doc, "Goodbye Filter - Restore View Properties")
    t.Start()
    try:
        view.DisableTemporaryViewMode(DB.TemporaryViewMode.TemporaryViewProperties)
        t.Commit()
    except Exception as ex:
        t.RollBack()
        ui.alert(
            "Could not restore this view.",
            title="Goodbye Filter",
            context=u"Error: {}".format(str(ex))
        )
        script.exit()
    ui.alert("Filters restored.", title="Goodbye Filter")
    script.exit()

# --- Nothing to turn off ---
try:
    filter_ids = list(view.GetFilters())
except Exception as ex:
    ui.alert(
        "Could not read the filters of this view.",
        title="Goodbye Filter",
        context=u"Error: {}".format(str(ex))
    )
    script.exit()

if not filter_ids:
    ui.alert("This view has no filters.", title="Goodbye Filter")
    script.exit()

t = DB.Transaction(doc, "Goodbye Filter - Disable All Filters")
t.Start()
try:
    vt_id = view.ViewTemplateId
    if vt_id and vt_id != DB.ElementId.InvalidElementId:
        # View has a template -> pass it: same appearance, now temporarily editable
        entered = view.EnableTemporaryViewPropertiesMode(vt_id)
    else:
        # View has no template -> its own current settings become the baseline
        entered = view.EnableTemporaryViewPropertiesMode(DB.ElementId.InvalidElementId)

    # CRITICAL BUG fix: EnableTemporaryViewPropertiesMode returns a bool and
    # can fail without raising -- the old code never checked it and went on to
    # disable filters on the REAL view. Never touch a filter unless the
    # temporary mode is confirmed on.
    if not entered or not view.IsTemporaryViewPropertiesModeEnabled():
        t.RollBack()
        ui.alert(
            "Could not enable temporary filters for this view.",
            title="Goodbye Filter",
            context=u"Nothing was changed. Try Enable Temporary View "
                    u"Properties from the Properties palette to check "
                    u"whether this view supports it."
        )
        script.exit()

    count = 0
    failed = 0
    for fid in filter_ids:
        try:
            view.SetIsFilterEnabled(fid, False)
            count += 1
        except Exception:
            failed += 1

    t.Commit()
except Exception as ex:
    t.RollBack()
    ui.alert(
        "Goodbye Filter could not finish.",
        title="Goodbye Filter",
        context=u"Error: {}".format(str(ex))
    )
    script.exit()

if failed:
    context_msg = (
        u"Click Goodbye Filter again on this view to restore them. "
        u"{} filters could not be turned off."
    ).format(failed)
else:
    context_msg = u"Click Goodbye Filter again on this view to restore them."

ui.alert(
    "{} filters turned off (temporary).".format(count),
    title="Goodbye Filter",
    context=context_msg
)
