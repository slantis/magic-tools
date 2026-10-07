# -*- coding: utf-8 -*-
__title__ = "All Magic\nTools"
__doc__ = "Every Magic Tool in one searchable window, grouped by what it is for, with a description of each one before you run it. Star a tool to keep it on this ribbon; unstar it to take it off. Revit keeps working while the window is open."
__author__ = 'slantis'
# Available with no document open: the ribbon of this extension is built from
# what is starred here, so the door has to work on Revit's start screen too.
__context__ = 'zero-doc'
# THE ONE LINE THAT MAKES THIS TOOL WORK, and it has to be here rather than in
# lib/toolwindow.py because pyRevit reads it off the script it runs. Without it
# the engine is torn down the moment this script returns, taking every global and
# every event handler with it: the window stays on screen and no click does
# anything at all. Same flag, same Show() as every other modeless tool.
__persistentengine__ = True

# THE DOOR of the whole extension (2026-09-04). The Favorites panel holds the
# other tools as hidden buttons; this is the one that is always shown, because
# it is where the others get starred onto the bar (see lib/favbar.py for the
# veil and lib/favorites.py for the store).
#
# This button does one thing: open the window. It does NOT build it, and it does
# not hold the tool list, the groups or the order -- the groups come from
# lib/groups.json and the order from the panel's bundle.yaml, both read through
# toolpane.AllTools, so a door cannot drift from what it opens.
import traceback

import toolpane
import toolwindow
import usage


with usage.tool_run(__file__) as run:
    try:
        toolwindow.open_window(toolpane.AllTools)
    except Exception:
        run.error()
        traceback.print_exc()
