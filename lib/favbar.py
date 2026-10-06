# -*- coding: utf-8 -*-
"""favbar -- the veil over the Favorites panel: which of its buttons are shown.

The ribbon of Magic Tools is two panels (2026-09-04): Tools, the buttons that
are always there (All Magic Tools and the Navigation and Selection stacks), and
Favorites, where every other tool is a pushbutton (the design: the tools live
in a panel called Favorites and are just unhidden). Nothing is created at
runtime and nothing is built from the store: pyRevit draws every Favorites
button off the disk as it always has, and this module sets `Visible` on each
one -- True for what the user starred (or for the whole group the user put on
the bar as a preset), False for the rest. A hidden RibbonItem takes no width,
so the bar is exactly the favorites. The Tools panel is never touched. Since
2026-09-24 the True/False goes through a binding on each window button rather
than through Visible, so a Reload cannot unveil the panel: see veil().

The panel's bundle.yaml also draws a separator before every group of
lib/groups.json, so the bar comes signposted instead of as one undivided run
of icons (2026-09-07: a thin line between groups and nothing more). Those
lines are not RibbonItems and the veil cannot reach them, so seams() sets them
through the window's own ribbon: see adw_panel(). Clicked the same day, and it
settled the two things that could not be checked offline: an Autodesk.Windows
item's IsVisible does track the Visible the Revit API just set on that button,
and switching a RibbonSeparator off does take it out of the bar.

TWO MOMENTS call sync(). The first Idling after a load (lib/lab_startup.py),
because pyRevit's loader activates every button it draws, on boot and on every
Reload (ribbon.py create_push_button -> activate()), so the veil has to be put
back each time: persistence is this call running, not Revit remembering. And
the star click in the gallery, through its own ExternalEvent
(toolpane.FavHandler), so a starred tool appears on the bar with no Reload.

Between the load and that first Idling the whole panel is on screen for a
moment. Known, measured, accepted; the fallback if it ever grates is a
slideout in the panel's bundle.yaml.

NEVER HOST_APP.uiapp in here. Every function takes the UIApplication Revit
handed the caller -- the Idling sender, the Execute argument -- because the one
from the engine can be None in a startup script and poisoned the engine pool
once already (toolpane.run_command, 2026-09-02).

FAILURE = EVERYTHING VISIBLE, never everything hidden. If this module cannot
run, the panel shows every tool: ugly, but every tool stays reachable and
the symptom is on screen. Nothing here raises past its own function; every
miss is printed so the pyRevit output says why.
"""
import os
import traceback

import favorites


def _tab_name():
    """The title of this extension's ribbon tab, read from the disk.

    pyRevit titles a tab after its FOLDER: uimaker.py:1135-1136 passes
    tab.name to create_ribbon_tab, and the bundle's 'title' key is not used
    for tabs. So the folder is the only source of truth, and hardcoding the
    string here would break the moment the same tree loads under another name.
    It does since 2026-09-07: two copies of the extension can be loaded side
    by side, each under its own tab name. Falls back to a default name if the
    walk finds nothing, which keeps the failure mode of this module: no tab
    read, no veil, every button visible.
    """
    try:
        ext = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for entry in sorted(os.listdir(ext)):
            if entry.endswith('.tab'):
                return entry[:-4]
        print("favbar: no .tab folder next to '{0}'".format(ext))
    except Exception as ex:
        print("favbar: could not read the tab name off the disk -> {0}"
              .format(ex))
    return "Magic Tools"


TAB = _tab_name()
PANEL = "Favorites"
# The doors' panel: left alone by sync() and by hide_strays().
DOORS = "Tools"
# The sheet stack (Next, Parent, Previous), fixed on the ribbon since
# 2026-09-08 and outside All Magic Tools; left alone the same way.
NAV = "Navigation"
KEEP = (PANEL, DOORS, NAV)
# Shown whatever the store says. Empty since the doors moved to their own
# panel; kept so a button that must never be veiled is one name away.
ALWAYS = ()


def panels(uiapp):
    """The ribbon panels of this extension's tab, or [] when there is none."""
    try:
        return list(uiapp.GetRibbonPanels(TAB))
    except Exception as ex:
        print("favbar: no '{0}' tab to read -> {1}".format(TAB, ex))
        return []


def panel(uiapp):
    for candidate in panels(uiapp):
        if candidate.Name == PANEL:
            return candidate
    return None


def wanted(name, favs):
    """Should the button called `name` be on the bar? Pure, so it is testable."""
    return name in ALWAYS or name in favs


def sync(uiapp):
    """Show the starred buttons of the Favorites panel, hide every other one.

    Returns (shown, hidden) or None when the panel was not found. The button's
    Name is the bundle's folder name, which is the favorites key (see
    favorites.py), so no lookup table sits between the store and the ribbon.
    """
    target = panel(uiapp)
    if target is None:
        print("favbar: no '{0}' panel under '{1}'; nothing veiled".format(
            PANEL, TAB))
        return None
    # The active preset: the stars, or one whole group (favorites.bar_keys).
    favs = set(favorites.bar_keys())
    windows = adw_buttons()
    shown = hidden = 0
    for item in target.GetItems():
        try:
            show = wanted(item.Name, favs)
            if not veil(windows.get(item.Name), show) and item.Visible != show:
                item.Visible = show
            if show:
                shown += 1
            else:
                hidden += 1
        except Exception as ex:
            print("favbar: could not set '{0}' -> {1}".format(
                getattr(item, 'Name', '?'), ex))
    seams()
    unfold()
    return shown, hidden


def adw_panel():
    """The Autodesk.Windows panel behind PANEL, or None if it is not up yet.

    The Revit API hands out the buttons (RibbonPanel.GetItems) but says nothing
    about the panel as the window draws it: whether it is folded, and where the
    separators sit -- AddSeparator() returns no RibbonItem at all. Both live in
    the WPF ribbon, so both are asked of it here. Raises on its own; every
    caller decides what a miss means for itself.
    """
    import clr
    clr.AddReference('AdWindows')
    from Autodesk.Windows import ComponentManager
    for tab in ComponentManager.Ribbon.Tabs:
        if tab.Title != TAB:
            continue
        for candidate in tab.Panels:
            source = candidate.Source
            if source is not None and source.Title == PANEL:
                return candidate
    return None


# ---------------------------------------------------------------------------
# The veil as a binding (2026-09-24)
# ---------------------------------------------------------------------------
# Setting Visible=False is not enough: on every Reload pyRevit's activate()
# puts Visible=True on every button, the tab gets wider than the window and
# Revit folds this panel into a drop-down, and the fold sticks even after the
# veil comes back (diagnosed 2026-09-12). So each button's IsVisibleBinding is
# pointed at a CheckBox of ours: from then on the window ribbon shows what the
# CheckBox says, and pyRevit's Visible=True -- or anyone else's -- is inert
# (probed in Revit 2024).
#
# GOTCHA: a button takes ONE binding per Revit session. Assigning a second one
# locks it in whatever state it had until Revit restarts. So the CheckBoxes
# are kept in the AppDomain, which outlives a Reload (the engine and this
# module do not), and a button that already has one only gets its IsChecked
# changed. Back to factory is IsVisibleBinding = None, never another binding.

VEILS_KEY = "magictools.favbar.veils"


def _veils():
    """{button name: CheckBox}, one per process, shared by every engine."""
    from System import AppDomain, String, Object
    from System.Collections.Generic import Dictionary
    domain = AppDomain.CurrentDomain
    store = domain.GetData(VEILS_KEY)
    if store is None:
        store = Dictionary[String, Object]()
        domain.SetData(VEILS_KEY, store)
    return store


def adw_buttons():
    """{button name: Autodesk.Windows item} for PANEL, or {} if unreadable.

    The window item's Id ends in '%<Name>', the same Name the Revit API item
    carries (Id = CustomCtrl_%...%Tab%Panel%Folder).
    """
    try:
        holder = adw_panel()
        source = None if holder is None else holder.Source
        if source is None:
            return {}
        found = {}
        for item in source.Items:
            if is_separator(item):
                continue
            ident = getattr(item, 'Id', None) or ''
            if '%' in ident:
                found[ident.split('%')[-1]] = item
        return found
    except Exception as ex:
        print("favbar: could not read the window buttons -> {0}".format(ex))
        return {}


def _factory(item):
    """Does this button still carry Revit's own binding (Source = the item)?

    Only a sure yes counts: if the binding cannot be read, the answer is no,
    because re-binding a button that is already ours locks it.
    """
    try:
        current = item.IsVisibleBinding
        return current is not None and current.Source is item
    except Exception:
        return False


def veil(item, show):
    """Drive one window button through its bound CheckBox. True if it did.

    False means the binding path is out (no window item, no WPF) and the caller
    falls back to the Revit API's Visible, the veil as it was before the
    binding.
    """
    if item is None:
        return False
    try:
        import clr
        clr.AddReference('PresentationFramework')
        clr.AddReference('WindowsBase')
        from System.Windows.Controls import CheckBox
        from System.Windows.Data import Binding, BindingMode
        store = _veils()
        key = item.Id      # the whole Id: two copies share names
        box = store[key] if store.ContainsKey(key) else None
        if box is not None and _factory(item):
            # Same Id, but a window item pyRevit built anew: ours is gone.
            box = None
        if box is None:
            box = CheckBox()
            box.IsChecked = show
            link = Binding("IsChecked")
            link.Source = box
            link.Mode = BindingMode.OneWay
            item.IsVisibleBinding = link     # once per button and session
            store[key] = box
        elif bool(box.IsChecked) != show:
            box.IsChecked = show
        return True
    except Exception as ex:
        print("favbar: could not bind '{0}' -> {1}".format(
            getattr(item, 'Id', '?'), ex))
        return False


def is_separator(item):
    """Is this window-ribbon item one of the lines, rather than a button?

    By type name because the class is not imported at module level: this file
    has to import on any machine, and AdWindows only exists inside Revit.
    """
    try:
        return 'Separator' in item.GetType().Name
    except Exception:
        return False


def seams():
    """Light one separator per gap between visible groups, switch the rest off.

    The bundle draws a line before each group but the first. A bar of three
    favorites from three far-apart groups would carry every line, most of them
    hanging off nothing, so a line earns its place only with a visible button
    on BOTH sides of it, and a run of empty groups collapses into a single
    line: the last separator of the run, the one that opens the group which
    does have something up. No line at either end of the bar.

    Returns (lit, off), or None when the gaps could not be read -- and the
    reading happens whole, before anything is written. FAILURE = AS DRAWN: if
    the gaps cannot be read at all, every separator stays exactly where pyRevit
    put it (lines around nothing, which is only ugly) rather than a bar whose
    groups run into each other. A bar with NO visible button is not a failure:
    it is Clear stars, or Mine with nothing starred yet, and then every line
    goes off, quietly. It used to print a notice, and a print from the Idling
    handler opens the pyRevit output window over Revit (reported 2026-09-08:
    clearing every favorite popped up a notice).
    """
    try:
        holder = adw_panel()
        source = None if holder is None else holder.Source
        if source is None:
            print("favbar: no '{0}' panel in the window ribbon; seams left as"
                  " drawn".format(PANEL))
            return None
        items = list(source.Items)
    except Exception as ex:
        print("favbar: could not read the seams -> {0}".format(ex))
        return None

    seen = False    # is there a visible button to the left of here?
    pending = -1    # the last separator crossed since that button
    keep = set()    # indexes of the separators that earned their line
    try:
        for idx, item in enumerate(items):
            if is_separator(item):
                if seen:
                    pending = idx
            elif item.IsVisible:
                if pending >= 0:
                    keep.add(pending)
                    pending = -1
                seen = True
    except Exception as ex:
        print("favbar: could not walk the seams -> {0}".format(ex))
        return None
    # No visible button: an empty bar. keep stays empty and the loop below
    # switches every separator off.

    lit = off = 0
    for idx, item in enumerate(items):
        if not is_separator(item):
            continue
        show = idx in keep
        try:
            if item.IsVisible != show:
                item.IsVisible = show
            if show:
                lit += 1
            else:
                off += 1
        except Exception as ex:
            print("favbar: could not set the seam at {0} -> {1}".format(
                idx, ex))
    return lit, off


def unfold():
    """Ask Revit to draw the Favorites panel open, not folded into a button.

    Revit folds a panel into a drop-down (Autodesk.Windows.RibbonPanel
    .IsCollapsed) when the tab is wider than the window, and the rightmost
    panel goes first: that is this one. It was seen on 2026-09-05 with a
    preset up: the Favorites panel turned into a pulldown. Best
    effort: if the bar really does not fit, Revit folds it again on the next
    layout pass and nothing is lost; if it was folded by the flash of every
    button before the veil, this is what opens it back up.
    """
    try:
        holder = adw_panel()
        if holder is not None and holder.IsCollapsed:
            holder.IsCollapsed = False
    except Exception as ex:
        print("favbar: could not unfold the panel -> {0}".format(ex))


def hide_strays(uiapp):
    """Any panel of the tab that is not Tools, Navigation or Favorites goes away.

    Covers a Community.panel kept in a shared clone, and any
    panel folder left behind by a partial pull: neither may become a third
    bar. Hidden, not deleted, so their tools stay reachable.
    """
    for candidate in panels(uiapp):
        try:
            if candidate.Name not in KEEP and candidate.Visible:
                candidate.Visible = False
        except Exception:
            traceback.print_exc()
