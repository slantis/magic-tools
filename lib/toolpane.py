# -*- coding: utf-8 -*-
"""The tool galleries: one Gallery, two hosts, one configuration per tool set.

The ribbon of Magic Tools is one tab with two panels: Tools, which always
shows the All Magic Tools button (the door to the gallery) beside two fixed
stacks of tools, Navigation and Selection, and Favorites, holding every other
tool as a button that starts hidden (lib/favbar.py) and showing what the user
starred, up to favorites.MAX. So the window this file draws is how most tools
are reached at all: All Magic Tools opens over the whole Favorites panel with
a star on every tile. If a window fails its tools are unreachable, which is
why every failure in here is made visible instead of swallowed.

THREE SETS, NOT THREE FILES. The second set (2026-08-19) arrived the day after
the first and would have been a 450-line copy with a different GROUPS list.
Everything that is not the configuration is identical, so the difference is
where it belongs: a few class attributes at the bottom of this file, one class
per set (AllTools, CleanUpPane, AnalysisPane today; only AllTools has a door on
the ribbon, the other two have none: see AnalysisPane at the bottom). The XAML
is shared for the same reason -- it holds the shell and not one word about
which tools are in it.

TWO HOSTS, ONE GALLERY (2026-08-24). The tiles, the filter, the layout modes and
the launching are Gallery, which is handed four WPF elements and never asks what
holds them. This file is one host of it -- the dockable pane. The other is
lib/toolwindow.py, a modeless slantisui window, and it exists because a window
can be dragged to the second monitor while a docked strip cannot. The split was
not free-standing tidiness: the FIRST window version of this launcher, before the
pane, was a 230-line copy of the discovery code, and a second host meant either
doing that again or doing this.

Four mechanics, all of them carried over from an earlier panel's startup script
rather than rediscovered:

1. THE STARTUP SCRIPT (lib/lab_startup.py) does the boot work on the first
   Idling: arm() below, then the veil over the Favorites panel. No dockable
   pane is registered any more (2026-09-04: every set opens as a window); the
   ToolPane class stays as the holder of each set's configuration and as a
   host that still works should a pane ever be registered again.

2. A CLICK ON A MODELESS SURFACE IS NOT A VALID REVIT API CONTEXT. Launching
   a command from the click handler is exactly the bug this design exists to
   avoid, so the uid travels to an IExternalEventHandler and Revit calls us back
   when it is idle. arm() creates that event and must run in a valid context; the
   startup borrows the first Idling fire for it, and arm() is idempotent so a
   second call is free. One event serves every pane: the handler carries the uid.
   A third event (FavHandler) re-veils the ribbon after a star click, for the
   same reason: setting Visible on a RibbonItem from a WPF click is not a valid
   API context either.

3. ui.styles_xaml() IS MERGED INTO THIS PAGE'S RESOURCES. A pane is a Page, not a
   Window, so it cannot go through ui.parse(); merging the shared styles is what
   keeps this file from hand-copying the palette a fifth time.

4. NEVER call WPFPanel.hide_element / show_element / toggle_element /
   disable_element / enable_element: all five call themselves
   (forms/__init__.py:503-545) and blow the stack. Visibility here is set
   directly on the elements this module created.

And one thing NOT to do, written down because it is tempting and it deadlocks:
never call pyRevit Routes synchronously from a click handler (server.py:103 +
handler.py:91-97 spin with no sleep).
"""
import datetime
import io
import json
import os
import re
import traceback

import clr
clr.AddReference('PresentationCore')
clr.AddReference('PresentationFramework')
clr.AddReference('WindowsBase')

from System import Double, TimeSpan
from System.Windows import (Duration, FontWeights, HorizontalAlignment,
                            Thickness, TextAlignment, TextTrimming,
                            TextWrapping, UIElement, VerticalAlignment,
                            Visibility)
from System.Windows.Controls import (Border, Button, CheckBox, Dock, DockPanel,
                                     Grid, Image, Orientation, StackPanel,
                                     TextBlock, WrapPanel)
from System.Windows.Markup import XamlReader
from System.Windows.Controls import ContextMenu, MenuItem
from Microsoft.Win32 import OpenFileDialog, SaveFileDialog
from System.Windows.Media import (BitmapScalingMode, RenderOptions,
                                  SolidColorBrush, ColorConverter,
                                  TranslateTransform, VisualTreeHelper)
from System.Windows.Media.Animation import CubicEase, DoubleAnimation, EasingMode

from pyrevit import DB
from pyrevit import UI
# Only for ICON_DARK_SUFFIX: the dark icon's name is spelled in exactly one
# place, and that place is pyRevit.
from pyrevit import extensions as exts
from pyrevit import forms
from pyrevit import framework
from pyrevit.loader import sessionmgr

from slantisui import ui

# The loader lives next door because the detail panel draws the same PNGs and
# the locking bug came from having the idiom written twice.
from toolinfo import bitmap
# What each tile's number means and how it is counted. Kept out of this file
# because it is Revit knowledge (which collector, which universe, which tool it
# has to keep agreeing with) and this one is WPF.
import toolscan
# The favorites store and the veil over the ribbon: a star on a tile writes the
# first and raises an ExternalEvent that runs the second.
import favbar
import favorites


HERE = os.path.dirname(os.path.abspath(__file__))
EXT_ROOT = os.path.dirname(HERE)
CLEAR_WINDOW = 5        # seconds the "Clear N stars?" question stays open

# Measured, not guessed (16 real labels, real WPF, real BtnGhost style): 80 is
# the narrowest tile where all 16 labels still fit in TWO lines at 11.5, and 78
# is the height the worst of them then needs. Going narrower buys no extra
# column, and at 72 the longest label breaks into three lines, so the tile
# has to grow BACK to 86 tall. Six per row at the width the pane opens at,
# against four at the old 104.
TILE_W, TILE_H, ICON_PX = 80, 78, 22

# What is left for the label once the tile has spent its padding (8 top and
# bottom) and, when it has one, its icon (22 plus a 6px margin): 34 with an icon,
# 62 without. Written as arithmetic so both follow the tile if it changes, and
# taken per tile because a tool with no icon.png has the icon's room to spend on
# its name and should not be trimmed as if it did not.
LABEL_MAX = TILE_H - 16
LABEL_MAX_ICON = LABEL_MAX - (ICON_PX + 6)

# The group name sits in a gutter to the left of its row instead of eating a
# line of its own above it. Measured on Clean Up when it was six groups: the
# six headers on top cost over 200px of height without drawing a single tool,
# and every group already fits in one row, so moving them sideways took the 16
# tools from 772px tall to about 516. It costs one column, which is the trade.
# (Clean Up is five groups since 2026-09-01, which only makes the case wider:
# one row less of tiles, one header less either way.)
GUTTER = 78

# How far below the top of a row a heading prints. It lines the name up with the
# first tile's label instead of with the top edge of its border, and it is a
# constant and not a literal because TWO headings use it: the one in the gutter
# and the inline one (lib/toolpane.Head), which have to print at the same height
# or the same row shows two headings at two levels.
HEAD_DROP = 12

# ...but only while the gutter still leaves room for a real grid. Narrow
# enough and those 78px are what turns the row into a one-tile strip, and
# then the headers on top cost less height than the extra rows do.
#
# THERE IS NO THRESHOLD, and that is the fix. The first version had one
# number (283px) derived from tile arithmetic, and it was right for the six
# groups of Clean Up and wrong for a pane of four groups, where the gutter
# went on losing by 38px all the way from 290 to 370. The crossover is not a
# property of the pane machinery, it is a property of the SHAPE of a pane
# (how many groups, how big the biggest one is), so the code measures the two
# modes at the current width instead of guessing where they cross. A third
# pane gets it right without anybody re-deriving a number.
#
# Every constant below is MEASURED in real WPF, and the arithmetic reproduces
# what WPF actually does at all 70 widths swept over the two panes (560 down
# to 220 every 10px, both modes each time): zero disagreements. Two of them
# are the ones that took three tries to get right --
#   the gutter costs 90, not 78: it is 78 wide PLUS its 12 right margin;
#   a tile occupies 88 with its margin inside it, so N tiles need N*88, which
#   is floor(usable / 88) columns and NOT floor((usable + 8) / 88).
PAD = 20            # the page's own left + right padding
SCROLLBAR = 17      # shows up only once the content grows, and then takes this
HEAD_SIDE = 12      # the gutter header's right margin, on top of its width
HEAD_H = 14         # one header line, in top mode
HEAD_GAP = 9        # ...and its gap down to the tiles
ROW_GAP = 14        # between groups, top mode only
TILE_STEP = TILE_H + 8
COL_STEP = TILE_W + 8


def tile_icon(bundle):
    """The tile's icon for the theme THIS WINDOW is painted in, or None.

    Not pyrevit.revit.ui.resolve_icon_file: that one answers what the host
    RIBBON wants, and it only says dark when Revit is newer than 2024 and the
    host theme is Dark. This window follows slantisui's THEME, which
    THEME_OVERRIDE can force, so the icon has to follow the surface it is
    actually drawn on. The suffix itself still comes from pyRevit.

    It matters because the white in these icons is not decoration: it is an
    OPAQUE knockout hiding the shapes stacked behind the front one, so a light
    icon on the dark card reads as a bright blob. The dark twin paints that
    knockout in the ribbon's slate, which is also the dark card.

    Falls back to the light file when a tool has no dark twin drawn yet (a
    newly added tool, say), because a tile with no icon at all is worse than a
    tile with the light one.
    """
    names = ['icon' + exts.ICON_FILE_FORMAT]
    if getattr(ui, 'THEME', 'light') == 'dark':
        names.insert(0, 'icon' + exts.ICON_DARK_SUFFIX + exts.ICON_FILE_FORMAT)
    for name in names:
        path = os.path.join(bundle, name)
        if os.path.isfile(path):
            return path
    return None


def _cols(usable):
    return max(1, int(usable // COL_STEP))


def _mode_heights(sizes, width):
    """(height with the gutter, height with headers on top), or None for a mode
    that is not on offer at this width."""
    usable = width - PAD - SCROLLBAR
    side_usable = usable - GUTTER - HEAD_SIDE
    side_cols, top_cols = _cols(side_usable), _cols(usable)
    side = sum(-(-n // side_cols) * TILE_STEP for n in sizes)
    top = sum(HEAD_H + HEAD_GAP + -(-n // top_cols) * TILE_STEP for n in sizes)
    top += ROW_GAP * (len(sizes) - 1)
    if side_usable < TILE_W:
        # With the gutter taken out there is not room for one whole tile, so
        # that mode would clip it rather than just cost height.
        return (None, top)
    return (side, top)


# --- reading the panel ------------------------------------------------------

def command_index(pool_panel, also_from=()):
    """(pool_dir, {folder name: command}) for the tools a pane can show.

    Scoped by PATH and not by extension name, so a second extension shipping a
    panel with the same name cannot leak in: a candidate has to live under THIS
    extension and have one of the wanted panels as its parent folder. Deriving
    pool_dir from the commands instead of from __file__ also means no assumption
    about the tab's folder name, which has already changed once.

    also_from let a set show a tool that lived in another panel (an earlier
    set did, 2026-08-19). Since 2026-09-04 every tool is in the one Favorites
    panel, so no set needs it; it stays because it costs one line and a second
    panel is one bundle.yaml away. A door button in a listed panel gets indexed
    too and is simply never asked for, or hidden by the set (`hide`).
    """
    root = os.path.normcase(EXT_ROOT) + os.sep
    wanted = set(p.lower() for p in ((pool_panel,) + tuple(also_from)))
    pool, found = None, {}
    for cmd in sessionmgr.find_all_commands():
        path = cmd.script
        if not path:
            continue
        bundle = os.path.dirname(os.path.abspath(path))
        if not os.path.normcase(bundle).startswith(root):
            continue
        parent = os.path.dirname(bundle)
        panel = os.path.basename(parent)
        if panel.lower() not in wanted:
            continue
        if panel.lower() == pool_panel.lower():
            pool = parent
        folder = os.path.basename(bundle)
        if folder.lower().endswith('.pushbutton'):
            folder = folder[:-len('.pushbutton')]
        found[folder] = cmd
    return pool, found


def panel_order(pool_dir):
    """Folder names the pool panel's bundle.yaml lists as this pane's tools.

    Two shapes, one reader. A flat panel (Favorites, since 2026-09-04) lists
    every tool and nothing else, and every one of them counts: which are drawn
    is the set's business (its groups, `orphans`, `hide`). A panel with a
    slideout lists its ribbon items first, then '>>>', then the tools parked
    behind the panel's arrow, and only those count: that is the fallback shape
    if the veil's boot flash ever grates (see lib/favbar.py). '---' separators
    are skipped either way.

    The file is 'layout:' followed by '  - Name' lines and nothing else inside
    the list (comments go above it: the reader stops at the first line that is
    not an item), so a short reader beats pulling in a yaml parser.
    """
    if not pool_dir:
        return []
    path = os.path.join(pool_dir, 'bundle.yaml')
    if not os.path.isfile(path):
        return []
    names, inside = [], False
    for line in io.open(path, encoding='utf-8', errors='replace'):
        text = line.strip()
        if not inside:
            inside = text.startswith('layout:')
        elif text.startswith('- '):
            names.append(text[2:].strip().strip('"' + "'"))
        elif text:
            break
    for i, name in enumerate(names):
        if name.startswith('>>>'):
            names = names[i + 1:]
            break
    return [n for n in names if not n.startswith('---')]


_TITLE_RE = re.compile(r"^__title__\s*=\s*(['\"])(.+?)\1\s*$", re.M)


def read_title(script_path, fallback):
    """The tool's __title__, which is what the ribbon would have shown.

    Never the folder name: a tool's folder can carry one name and its title
    another. The line break exists for the width of a ribbon button, so it
    flattens to a space here.
    """
    try:
        src = io.open(script_path, encoding='utf-8', errors='replace').read()
    except IOError:
        return fallback
    hit = _TITLE_RE.search(src)
    if not hit:
        return fallback
    label = hit.group(2).replace('\\n', ' ').replace('\\t', ' ')
    return ' '.join(label.split()) or fallback


class Head(object):
    """An inline sub-heading inside a group's row, naming the tiles AFTER it.

    Why it exists: line styles and line patterns were wanted on the same line,
    each with its own title (2026-09-01), and two groups cannot do that. One
    entry of pane_groups is one DockPanel, so two entries are always two rows,
    whatever the width. This rides INSIDE the WrapPanel between tiles, so the
    group keeps its name in the gutter and the second half of the row gets its
    own without costing a row.

    A tile after a Head belongs to it: `group` on those tiles is the Head's
    name, which is what the detail panel shows, so the sub-heading is not just
    decoration on the row.

    The name may carry a line break ("Line\nPatterns"): that is where the
    heading splits when it sits beside the tiles (see head_text). Everything
    that treats the name as a NAME (the detail panel, the tile's group) reads
    it through flat_name, so the break never leaks into a label.
    """

    def __init__(self, name):
        self.name = name


def flat_name(name):
    """A group or Head name as a name: any line break folded to one space."""
    return u" ".join(name.split())


def head_text(name, side):
    """What a heading prints. Beside the tiles it keeps the break its name
    carries (design decision, 2026-09-05: line patterns and line styles on 2
    lines, right-justified); on top of the row, where there is a
    whole line to spend, it prints flat."""
    text = name if side else flat_name(name)
    return text.upper()


def _shorten(label, group):
    """The label as the TILE shows it: the group name taken off the front.

    A "Purge ..." tool under a PURGE heading is the same word twice on the one
    screen where room is tightest (design decision, 2026-09-01: take Purge off
    the purge tools and Merge off the merge ones). Only an exact leading match
    goes, so a "Merge Styles" tool under a STYLES heading keeps its Merge -- it
    is the verb there, not the heading.

    The FULL title stays on `label`: the detail panel and the pane's tooltip
    show that, and `haystack` is built from it, so typing "purge" still finds
    every purge tool even though no tile says the word any more.
    """
    if not group or not label:
        return label
    prefix = group + ' '
    if label.lower().startswith(prefix.lower()) and len(label) > len(prefix):
        rest = label[len(prefix):]
        # A door is named after its group ("Clean Up Tools" under CLEAN UP),
        # and a tile that just says "Tools" says nothing.
        if rest.lower() != 'tools':
            return rest
    return label


class Tile(object):
    """One tile: what to draw, and what to run if it gets clicked.

    `group` is the pane's own group name, carried on the tile since 2026-09-01
    because the detail panel (lib/toolinfo.py) shows it and had no other way to
    know: the gallery buckets by group but the tile it hands over did not say
    which bucket it came from. It is NOT part of `haystack` on purpose -- adding
    it would silently widen what the search box matches, for both hosts.
    """

    def __init__(self, key, uid, label, tooltip, icon, group=None,
                 foreign=False):
        self.key = key
        self.uid = uid
        self.label = label
        self.tooltip = tooltip
        self.icon = icon
        self.group = group
        # A tool of ANOTHER extension (pyRevit's own, EF Tools...), found by
        # foreign_tiles(). It runs like any other but draws no star: the bar
        # is a veil over this extension's own panel and cannot hold it.
        self.foreign = foreign
        # What the tile draws, which is not always the full title. See _shorten.
        self.short = _shorten(label, group)
        # Filled by a scan and empty until then (Gallery.take_scan). It lives on
        # the tile and not in a dict beside it because the detail panel is handed
        # the tile and nothing else -- the number has to travel with it or the
        # panel would need a second way to ask.
        self.count = None
        self.count_line = None

    @property
    def enabled(self):
        return self.uid is not None

    @property
    def haystack(self):
        """What the search box matches against."""
        return (self.key + ' ' + self.label + ' ' + (self.tooltip or '')).lower()


# THE SAME TOOL, TWO FOLDER NAMES. This module is shared by more than one
# extension, and the tools it lists were named twice: the master copies call
# them what they are, while the other extension still carries the names they
# were born with, ordering prefixes and typos included (a number in front of
# the name, a misspelt word, a small letter where a capital belongs).
#
# Renaming those when the tools were first promoted (2026-09-01) was turned
# down to keep the usage history in one piece (the folder name IS the pyRevit
# command name). On 2026-09-28 that was reversed (the drift between a tool's
# name and its folder was not wanted): the folders now carry the name the
# ribbon shows, lib/renames.json, where an edition ships one, maps every old
# folder to its new one, and favorites goes through it. What is left here is
# only where the master and the ribbon still name the same tool differently.
#
# Matching is exact and case-sensitive on purpose: an alias that differs from
# its master only by case is in here rather than folded away, so the table
# says out loud what diverged. A tool with the same name in both extensions
# is simply not in the table. The open source edition does not use it: none of
# its tools is in the table, so it has no effect there.
ALIASES = {
    "Purge Unused Filters":   ("Purge Filters",),
    "Purge Unused VT":        ("Purge Templates",),
    "Analyze and Delete Line Patterns": ("Analyze and Delete",),
    "Search and Merge Line Patterns":   ("Search and Merge",),
    "Check Phase":            ("Check phase",),
}


# The same table backwards: whichever of the names this extension uses, back to
# the master one. toolscan.COUNTERS is keyed by the master and a Tile carries the
# resolved name, so this is the single hop between them -- derived from ALIASES
# instead of written out, so a new alias is still one edit and not two.
_MASTER = {}
for _master, _aliases in ALIASES.items():
    _MASTER[_master] = _master
    for _alias in _aliases:
        _MASTER[_alias] = _master


def master_key(key):
    """The name pane_groups knows this tool by, whatever it is called here."""
    return _MASTER.get(key, key)


def _names(member):
    """Every folder name this member can go by, master name first."""
    return (member,) + ALIASES.get(member, ())


def _resolve(member, listed, index):
    """The name this member has HERE: the first one the extension has, or None."""
    for name in _names(member):
        if name in index or name in listed:
            return name
    return None


def _drop_lone_heads(bucket):
    """Take out every Head that has no tile between it and the next one."""
    kept = []
    for item in bucket:
        if isinstance(item, Head) and kept and isinstance(kept[-1], Head):
            kept[-1] = item                 # two in a row: the first labels nothing
        else:
            kept.append(item)
    if kept and isinstance(kept[-1], Head):
        kept.pop()                          # a Head at the end labels nothing
    return kept


def collect(pool_panel, groups_config, also_from=(), orphans=False, hide=()):
    """(groups, tiles): every tool this set shows, bucketed and ordered.

    `orphans` adds a trailing "Other" group with every tool the pool panel lists
    that no group named. On for All Tools only: since every tool of the gallery
    sits in the one Favorites panel (2026-09-04), a door over one group (Clean
    Up, Analysis) would otherwise draw every other tool under "Other".
    `hide` are folder names never drawn, named or orphan; unused today, it was
    the All Tools door while that button lived in the pool panel.
    """
    pool, index = command_index(pool_panel, also_from)
    listed = panel_order(pool)
    known, groups = set(), []

    for name, members in groups_config:
        bucket = []
        for member in members:
            # A Head is not a tool, so it cannot be looked up -- it passes
            # through and then any Head left with no tile under it is dropped,
            # which is what keeps a sub-heading from labelling nothing when its
            # tools are not installed in this session.
            if isinstance(member, Head):
                bucket.append(member)
                continue
            if member in hide:
                continue
            # Every alias goes into `known`, or the name this extension uses
            # would come back as an orphan and the tool would draw twice.
            known.update(_names(member))
            hit = _resolve(member, listed, index)
            if hit is not None:
                bucket.append(hit)
        bucket = _drop_lone_heads(bucket)
        if any(not isinstance(m, Head) for m in bucket):
            groups.append((name, bucket))

    if orphans:
        rest = [n for n in listed if n not in known and n not in hide]
        if rest:
            groups.append(("Other", rest))

    tiles = []
    for group_name, members in groups:
        # The group's name until a Head says otherwise, and from there the
        # Head's: that is what puts LINE PATTERNS on the detail panel of a tool
        # sitting in the row headed LINE STYLES.
        current = flat_name(group_name)
        for key in members:
            if isinstance(key, Head):
                current = flat_name(key.name)
                continue
            cmd = index.get(key)
            if cmd is None:
                # Visible at first glance beats a mystery: a tool listed in the
                # panel but not loaded means the hiding went wrong, not that the
                # tool disappeared.
                tiles.append(Tile(key, None, key,
                                  'Not loaded in this session', None,
                                  current))
                continue
            icon = tile_icon(os.path.dirname(cmd.script))
            tiles.append(Tile(key, cmd.unique_id,
                              read_title(cmd.script, key),
                              cmd.tooltip or '',
                              icon,
                              current))
    return groups, tiles


# THE OTHER RIBBONS (2026-09-05). Design decision: the search in All Tools
# should find every pyRevit tool too, so that typing 'who did that' (a heavily
# used pyRevit tool) brings it up. Every command pyRevit has loaded from an
# extension that is not this one, grouped by the TAB it sits in ("pyRevit
# Tools", "EF Tools"...), so a search in All Tools or in the palette reaches
# the whole ribbon and not just ours. No star on them (rule: they cannot go
# to favorites), and the gallery draws them last, after every group of this
# extension, so the window at rest is still Magic Tools.
#
# NARROWED (2026-09-07). Design decision: from pyRevit only a short list goes
# into All Magic Tools, and the tools of the other ribbons come out entirely;
# start concise. So: nothing from EF Tools, DiRoots or any third-party tab,
# and from pyRevit's own tab only the tools in the tuple below. That list is
# CURATED, not measured: a hand-kept list of the folder names of the pyRevit
# tools the extension recommends (7 on 2026-09-07, 9 on 2026-09-13).
#
# The open source edition leaves all of this off (lib/edition.json sets
# "foreign": false), so its search covers Magic Tools only.
_SKIP_BUNDLES = ('.urlbutton', '.linkbutton', '.invokebutton', '.content')
SALEM_PYREVIT = (
    'Keynotes',
    'Make Pattern',
    'Match',
    'Orient Section Box To Face',
    'ReNumber',
    'Set Views Crop Box Line Weight',
    'Who Did That',
    # 2026-09-13: both tools were added to All Magic Tools. The
    # factory Excel round-trip (schedule -> .xlsx with ElementId -> model)
    # answers the bulk-edit request.
    'XLS Export',
    'XLS Import',
)


def _tab_group(script_path):
    """'pyRevit Tools' for .../pyRevit.tab/.../script.py, or None."""
    folder = os.path.dirname(os.path.abspath(script_path))
    while folder and folder != os.path.dirname(folder):
        name = os.path.basename(folder)
        if name.lower().endswith('.tab'):
            tab = name[:-4].strip()
            if not tab.lower().endswith('tools'):
                tab += ' Tools'
            return tab
        folder = os.path.dirname(folder)
    return None


# Why the last foreign_tiles() came back empty, for the footer and the bar to
# show. None while it has something to show. A search that silently covers
# less than it says it does is the failure this guards against (2026-09-05:
# the pyRevit tools were missing from the search).
FOREIGN_NOTE = None


def foreign_tiles():
    """(groups, tiles) for every loaded tool that is not this extension's.

    Same shape as collect() so build() can append them to its own. Since
    2026-09-07 that is one group, pyRevit's own tab, holding only the tools of
    the curated tuple above, in title order. Never raises: a ribbon that
    cannot be read is a ribbon that is not searched -- but FOREIGN_NOTE says
    so.
    """
    global FOREIGN_NOTE
    FOREIGN_NOTE = None
    root = os.path.normcase(EXT_ROOT) + os.sep
    buckets = {}
    try:
        commands = list(sessionmgr.find_all_commands())
    except Exception as ex:
        traceback.print_exc()
        FOREIGN_NOTE = "other ribbons: could not list them ({0})".format(ex)
        return [], []
    seen = failed = 0
    for cmd in commands:
        try:
            path = cmd.script
            uid = cmd.unique_id
            if not path or not uid:
                continue
            bundle = os.path.dirname(os.path.abspath(path))
            if os.path.normcase(bundle).startswith(root):
                continue
            if os.path.basename(bundle).lower().endswith(_SKIP_BUNDLES):
                continue
            group = _tab_group(path)
            if not group or not group.lower().startswith('pyrevit'):
                continue
            folder = os.path.basename(bundle)
            if '.' in folder:
                folder = folder[:folder.rfind('.')]
            if folder not in SALEM_PYREVIT:
                continue
            seen += 1
            icon = tile_icon(bundle)
            tile = Tile(uid, uid, read_title(path, cmd.name or folder),
                        cmd.tooltip or '',
                        icon,
                        group, foreign=True)
            buckets.setdefault(group, []).append(tile)
        except Exception:
            failed += 1
            if failed == 1:
                traceback.print_exc()
            continue
    if not buckets:
        FOREIGN_NOTE = ("pyRevit tools: {0} commands loaded, {1} of Salem's {2} "
                        "found, {3} failed to read".format(
                            len(commands), seen, len(SALEM_PYREVIT), failed))
    names = sorted(buckets, key=lambda g: (not g.lower().startswith('pyrevit'),
                                           g.lower()))
    groups, tiles = [], []
    for name in names:
        members = sorted(buckets[name], key=lambda t: t.label.lower())
        groups.append((name, [t.key for t in members]))
        tiles.extend(members)
    return groups, tiles


# --- launching (the only valid route out of a modeless pane) -----------------

# Moved to lib/launch.py on 2026-09-15 (Inspect Model Overrides launches
# Inspect View Overrides through it too); the docstring with the 2026-09-02 bug
# went with it.
from launch import run_command  # noqa: E402,F401


# NOT PostCommand (tried 2026-09-08, 01:40-01:55, as a guard for a launch bug):
# RevitCommandId.LookupCommandId resolves the control id pyRevit bakes into
# ScriptData.CommandControlId and CanPostCommand answers True, but a posted
# command whose ribbon button is HIDDEN never runs -- and the veil hides every
# tool that is not starred, so a tool run from its window did nothing
# (reported: Run on an unstarred tool did nothing). Measured live through
# Routes: a hidden button had IsVisible False, CanPostCommand True, no run.
# Posting only works for the doors and the starred buttons, which is not where
# the launch happens.


class LaunchHandler(UI.IExternalEventHandler):
    """Runs one command per Raise(), inside a context Revit says is valid."""

    def __init__(self):
        self.uid = None
        self.report = None      # set by the pane, so a failure is visible

    def Execute(self, uiapp):
        uid, self.uid = self.uid, None
        if not uid:
            return
        try:
            run_command(uid, uiapp)
        except Exception as ex:
            if self.report:
                self.report("could not run that tool: {0}".format(ex))
            else:
                print("tool pane: {0}".format(ex))

    def GetName(self):
        return "slantis tool pane launcher"


class ScanHandler(UI.IExternalEventHandler):
    """Counts what the model holds, for one gallery, per Raise().

    A SECOND event rather than a second kind of job on the launcher's, and the
    reason is that they are not the same act: run_command carries a bug that
    cost an afternoon to find and its handler is left alone. Reading the model
    only needs the document, which Revit hands over here.

    WHY IT GOES THROUGH AN EXTERNAL EVENT AT ALL, given that these are reads
    with no transaction: because a click on a modeless surface is not a valid
    Revit API context (see the header of this file), and whether a bare
    collector survives that context is NOT something this project has measured
    -- what is measured, since 2026-08-24, is that an ExternalEvent gives a
    valid one. Using the route that is proven costs one Raise. If the direct
    read turns out to be fine, this becomes a simplification and never a bug.
    """

    def __init__(self):
        self.gallery = None

    def Execute(self, uiapp):
        gallery, self.gallery = self.gallery, None
        if gallery is None:
            return
        try:
            doc = uiapp.ActiveUIDocument.Document
        except Exception:
            doc = None
        if doc is None:
            gallery.scan_failed("no open model to scan")
            return
        try:
            gallery.take_scan(doc)
        except Exception as ex:
            gallery.scan_failed("could not scan: {0}".format(ex))

    def GetName(self):
        return "slantis tool pane scanner"


class FavHandler(UI.IExternalEventHandler):
    """Re-veils the Favorites panel after a star click, per Raise().

    Its own event, like the scanner's, and for the same reason the adversarial
    pass gave on 2026-09-04: Raise() coalesces, so a handler carrying two kinds
    of job on one field would let "star" overwrite "run" when they land close.
    It carries nothing at all -- the store is the payload, favbar.sync reads it.
    """

    def Execute(self, uiapp):
        try:
            favbar.sync(uiapp)
        except Exception as ex:
            print("favorites: could not refresh the ribbon -> {0}".format(ex))

    def GetName(self):
        return "slantis favorites bar"


_EVENT = None
_HANDLER = None
_SCAN_EVENT = None
_SCAN_HANDLER = None
_FAV_EVENT = None
_FAV_HANDLER = None


def arm():
    """Create the ExternalEvents. Must be called from a valid API context.

    One pair for every pane, because a Raise() carries its payload on the
    handler and Revit serialises them anyway. Idempotent, because the startup's
    one-shot Idling subscription is not guaranteed to be the only caller -- and
    idempotent PER EVENT, so the scan event added on 2026-09-04 is created for
    the callers that armed the launcher before it existed.

    Returns the LAUNCH event, which is what every caller so far asked for; the
    scanner reaches its own through _SCAN_EVENT.
    """
    global _EVENT, _HANDLER, _SCAN_EVENT, _SCAN_HANDLER
    global _FAV_EVENT, _FAV_HANDLER
    if _EVENT is None:
        _HANDLER = LaunchHandler()
        _EVENT = UI.ExternalEvent.Create(_HANDLER)
    if _SCAN_EVENT is None:
        _SCAN_HANDLER = ScanHandler()
        _SCAN_EVENT = UI.ExternalEvent.Create(_SCAN_HANDLER)
    if _FAV_EVENT is None:
        _FAV_HANDLER = FavHandler()
        _FAV_EVENT = UI.ExternalEvent.Create(_FAV_HANDLER)
    return _EVENT


# NOT the ribbon button's CommandHandler either (tried 2026-09-08, 11:00-11:45,
# as the route that would keep the tool's engine alive for that bug). pyRevit
# bakes the control id into every command (ScriptData.CommandControlId) and
# Autodesk.Windows.ComponentManager.Ribbon.FindItem finds the button, hidden
# or not, with a CustomRibbonItemHandler as CommandHandler. Executing that
# handler from the window's WPF click does NOTHING, whatever the button's
# state: hidden (11:15, seven fires, no "Execute external command" in the
# journal), unveiled-fired-reveiled in one go (11:17), and VISIBLE too (11:40,
# five fires on the starred Inspect Model Overrides, journal.0674 empty of
# it; reported: "the second Run did not work"). CanExecute answers False for
# every button, so it cannot gate anything. Revit runs its ribbon commands
# from its own click pipeline, not from the ICommand a third party invokes.
# The one route that runs a tool from a modeless surface is the ExternalEvent
# below.


def _trace(msg):
    """The forensic trace of lib/modeless.py, shared so one file tells the story."""
    try:
        import modeless
        modeless._trace("toolpane " + msg)
    except Exception:
        pass


def run_tile(tile, report):
    """Queue one tile's tool on Revit's thread. Returns an error line or None.

    The one route out of a modeless surface, shared by the gallery (its tiles,
    the detail panel's Run, Enter) and the palette of a build that has one
    (lib/palette.py): raise the ExternalEvent, never call. `report` is where
    the handler puts what the run had to say; the returned line is what the
    caller shows when the raise itself failed.
    """
    uid = getattr(tile, 'uid', None)
    if not uid:
        return "that tool is not loaded in this session"
    try:
        # Normally already armed by the door script (a valid API context).
        # This is the fallback, and arm() is idempotent so it costs nothing.
        event = arm()
    except Exception as ex:
        return "cannot run: {0}".format(ex)
    if event is None:
        return "cannot run: no external event"
    _HANDLER.report = report
    _HANDLER.uid = uid
    # Raise() only queues it. Revit runs the handler when it is idle, which is
    # the whole point: a WPF click is not a valid API context. And it RETURNS
    # A STATE: a raise Revit refused must not look like a click that did
    # nothing.
    state = event.Raise()
    if state != UI.ExternalEventRequest.Accepted:
        return "Revit did not take that ({0}), try again".format(state)
    return None


def rank(tiles, query, limit=8):
    """The tiles `query` finds, best first, at most `limit`.

    Three ranks and then the given order: a title that STARTS with the words,
    a title that contains them, a description that does. The wrap gallery
    only asks "does it match"; a list that runs its first row on Enter (the
    palette) has to put the likeliest one there. Disabled tiles are left out:
    the list runs, and they cannot. This extension's tiles come before the
    other ribbons' because that is the order they are handed in.
    """
    query = (query or '').strip().lower()
    if not query:
        return []
    ranked = []
    for order, tile in enumerate(tiles):
        if not tile.enabled:
            continue
        label = tile.label.lower()
        if label.startswith(query):
            score = 0
        elif query in label:
            score = 1
        elif query in tile.haystack:
            score = 2
        else:
            continue
        ranked.append((score, order, tile))
    ranked.sort(key=lambda r: (r[0], r[1]))
    return [r[2] for r in ranked[:limit]]


# --- the pane ---------------------------------------------------------------

def _brush(hex_colour):
    return SolidColorBrush(ColorConverter.ConvertFromString(hex_colour))


# THE STAR on every tile (2026-09-04): a CheckBox whose whole template is one
# glyph, empty star off and filled star on, the lib's checkbox border colour
# when off and the accent when on or hovered. XAML rather than code because a
# ControlTemplate with triggers is three lines here and thirty in C#-style
# calls; the colours are formatted in from the tokens, never written. Same
# pattern as in an earlier panel, where it has run since 2026-08-17.
_STAR_XAML = (
    u'<CheckBox xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" '
    u'xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml" '
    u'Focusable="False" Cursor="Hand" Width="20" Height="20" '
    u'HorizontalAlignment="Left" VerticalAlignment="Top">'
    u'<CheckBox.Template><ControlTemplate TargetType="CheckBox">'
    u'<Grid Background="Transparent">'
    u'<TextBlock x:Name="glyph" Text="&#x2606;" FontSize="15" Foreground="{off}" '
    u'HorizontalAlignment="Center" VerticalAlignment="Center"/>'
    u'</Grid>'
    u'<ControlTemplate.Triggers>'
    u'<Trigger Property="IsChecked" Value="True">'
    u'<Setter TargetName="glyph" Property="Text" Value="&#x2605;"/>'
    u'<Setter TargetName="glyph" Property="Foreground" Value="{on}"/>'
    u'</Trigger>'
    u'<Trigger Property="IsMouseOver" Value="True">'
    u'<Setter TargetName="glyph" Property="Foreground" Value="{on}"/>'
    u'</Trigger>'
    u'</ControlTemplate.Triggers>'
    u'</ControlTemplate></CheckBox.Template></CheckBox>')


def star_xaml():
    return _STAR_XAML.replace('{off}', ui.CHECK_BD).replace('{on}', ui.ACCENT)


def _inside_star(element, button):
    """Is `element` (a routed event's OriginalSource) inside the tile's star?"""
    while element is not None and element is not button:
        if isinstance(element, CheckBox):
            return True
        try:
            element = VisualTreeHelper.GetParent(element)
        except Exception:
            return False
    return False


class Gallery(object):
    """The tile gallery: discovery, drawing, the live filter, and launching.

    HOST-AGNOSTIC ON PURPOSE, and that is the only reason it is a class of its
    own instead of the body of ToolPane. It is handed the four elements it draws
    into and knows nothing about what contains them, so these ~200 lines serve
    the dockable pane (a Page) and the modeless window (lib/toolwindow.py,
    2026-08-24) without either one copying the other.

    The debt this avoids is not hypothetical: the FIRST window version of this
    launcher was a 230-line copy of panel_order / read_title / command_index /
    collect. One host away from a second copy was the moment to split.

    `spec` is a TOOL-SET class -- pool_panel, pane_groups, and optionally
    also_from. Those attributes live on the pane classes at the bottom of this
    file, which is what lets a window open over CleanUpPane with no pane
    involved: the class is the configuration, the pane is one way to show it.
    """

    def __init__(self, spec, host, search, hint, count, fade=False,
                 on_pick=None, scan_button=None, presets=None):
        self.spec = spec
        self.host = host        # the StackPanel the group rows go into
        self.search = search    # the filter TextBox
        self.hint = hint        # the placeholder TextBlock laid over it
        self.count = count      # the footer status line
        self.fade = fade        # entrance animation, for a host that OPENS
        self._entered = False   # ...and it plays once, never twice
        # ONE CLICK, TWO MEANINGS, and which one is the HOST's call (2026-09-01).
        # With no on_pick a click RUNS the tool, which is what a bare gallery is
        # and what the dockable pane stays: one click, one launch, nothing new to
        # learn. A host that has somewhere to put a description (the window, with
        # its detail column) passes one, and then a click SELECTS -- running moves
        # to that panel's Run button, to a double click, and to Enter.
        #
        # A flag on the host and not a mode on the gallery: the pane cannot show
        # a detail panel at all (it opens ~300px wide, which is where its own
        # header-mode arithmetic already runs out of room), so "does a click run"
        # is a property of the surface, not a preference.
        # on_pick is called with the picked Tile, or with None when the pick
        # goes away (the filter hid it): a host that shows a detail has to be
        # told to empty it, not only to fill it.
        self.on_pick = on_pick
        # Tile geometry is the PANE's, with the measured grid as default: the
        # 80 x 78 tile is what sixteen labels fit in, and a pane of three big
        # tools (an early three-tile set, 2026-09-03) has no sixteen to fit.
        self.tile_w, self.tile_h, self.icon_px = (getattr(spec, 'tile', None)
                                                  or (TILE_W, TILE_H, ICON_PX))
        self.stacked = bool(getattr(spec, 'stacked', False))
        self.selected = None    # (button, tile) of the picked tile, or None
        self.groups = []        # [(row, header, wrap, [(button, haystack)], i)]
        # {group index: [(the sub-heading element, [the buttons it names])]}.
        # Kept beside self.groups and not inside its tuples so the five places
        # that unpack a group keep unpacking five things.
        self.subheads = {}
        # The wide default; build() sets it from the real width before it draws,
        # and relayout() keeps it right from there on.
        self.side = True
        self.total = 0
        self._empty = None
        self.search.TextChanged += self.on_search
        # THE SCAN (2026-09-04). A button and not something that happens on
        # open, by design decision: the user should have a button that says
        # scan, so they choose whether to see the numbers or not. Until it is
        # pressed this is the window it has always been, which is also what
        # keeps it opening fast -- fifteen sweeps of a real model is not
        # something to spend before anybody asked.
        #
        # The BUTTON belongs to the host (each one declares its own search row),
        # the counting belongs here, so the two surfaces cannot end up with two
        # different ideas of what a number means. A host that passes none simply
        # never shows numbers.
        self.scan_button = scan_button
        self.badges = {}        # tile key -> the TextBlock in its corner
        self.stars = {}         # tile key -> the star CheckBox in the other
        self.tiles = []
        # THE PRESETS (2026-09-04, evening). A row of chips above the tiles:
        # Standard, Mine (the stars) and one per group of groups.json. Clicking one puts
        # that whole group on the ribbon in place of the stars, through the
        # same FavHandler a star uses, so switching from drafting to view
        # templates is one click and no Reload (the requirement: ready-made
        # presets that can be switched quickly). The stars are kept underneath.
        # The ROW belongs to the host (a WrapPanel it declares) and only the
        # window that says `presets = True` on its spec gets chips: a door over
        # one group has nothing to switch between.
        self.presets_host = presets
        self.chips = {}         # preset name -> its Button
        self.clear_chip = None  # the "Clear stars" chip, armed by a first click
        self._clear_armed = None
        self.foreign_rows = set()   # indexes of the other ribbons' rows
        # A host with a detail panel sets this to be told after any change of
        # the stars, so its own star button (toolinfo) can follow.
        self.on_fav_change = None
        self._stamp = u""       # " . scanned 15:42", once there has been one
        self._scanning = False
        if self.scan_button is not None:
            self.scan_button.Click += self.on_scan_click

    # -- setup ------------------------------------------------------------
    def _status(self, text):
        """The footer line, with the scan's timestamp kept on the end of it.

        Every place that writes the count goes through here, which is what stops
        a keystroke in the search box from wiping the one thing on screen that
        says WHEN the numbers were read. A number with no hour is a number that
        will still be there after the model changes underneath it.

        The error messages do not come through here on purpose: an error is not
        a state the stamp belongs to.
        """
        self.count.Text = text + self._fav_mark() + self._stamp

    def _fav_mark(self):
        """' . * 3 of 15', plus ' . bar: Annotate' while a preset is up."""
        mark = u"  \u00b7  \u2605 %d of %d" % (favorites.count(), favorites.MAX)
        active = favorites.bar()
        if active != favorites.MINE:
            mark += u"  \u00b7  bar: %s" % favorites.label(active)
        return mark

    def _count_text(self):
        tiles = [t for t in self.tiles if not t.foreign]
        ready = [t for t in tiles if t.enabled]
        more = len(self.tiles) - len(tiles)
        if not tiles:
            return "no tools found in " + (self.spec.pool_panel or '?')
        if len(ready) == len(tiles):
            text = "%d tools" % len(tiles)
        else:
            text = "%d of %d available" % (len(ready), len(tiles))
        if more:
            text += ", %d more from other ribbons" % more
        elif getattr(self.spec, 'foreign', False) and FOREIGN_NOTE:
            text += ", " + FOREIGN_NOTE
        return text

    def paint(self):
        """The two colours the gallery owns. The host paints its own ground."""
        self.hint.Foreground = _brush(ui.TEXT_MUTED)
        self.count.Foreground = _brush(ui.TEXT_DIM)

    def _style(self, key):
        # FindResource, not Resources[key]: it walks UP the tree, so the same
        # call finds the styles whether they were merged into a Page (the pane
        # does that, being unable to go through ui.parse) or written into a
        # Window by slantisui itself (the window).
        return self.host.FindResource(key)

    # -- building ---------------------------------------------------------
    def build(self):
        groups, tiles = collect(self.spec.pool_panel, self.spec.pane_groups,
                                getattr(self.spec, 'also_from', ()),
                                orphans=getattr(self.spec, 'orphans', False),
                                hide=getattr(self.spec, 'hide', ()))
        own = len(tiles)
        self.foreign_rows = set()
        if getattr(self.spec, 'foreign', False):
            more_groups, more_tiles = foreign_tiles()
            self.foreign_rows = set(range(len(groups),
                                          len(groups) + len(more_groups)))
            groups = groups + more_groups
            tiles = tiles + more_tiles
        by_key = dict((t.key, t) for t in tiles)
        self.host.Children.Clear()
        self.groups = []
        # Rebuilt with the rows, or a second build would leave the badges of the
        # first one pointing at buttons that are no longer on screen.
        self.badges = {}
        self.stars = {}
        self.tiles = tiles

        for index, (name, members) in enumerate(groups):
            # One DockPanel per group, so switching the header between the left
            # gutter and the top is a single SetDock and never a rebuild.
            row = DockPanel()
            row.LastChildFill = True

            header = TextBlock()
            header.Tag = name       # _apply_head_mode prints it per mode
            header.Text = head_text(name, self.side)
            header.Style = self._style('SectionHead')
            # Orange, not the style's muted grey (design decision, 2026-09-07:
            # the titles of the tool groups should be orange).
            # Set on the element, so SectionHead itself stays muted where the
            # detail panel and the long forms use it.
            header.Foreground = _brush(ui.ACCENT)
            row.Children.Add(header)

            wrap = WrapPanel()
            if self.stacked:
                # One under the other. A vertical WrapPanel with no height
                # limit (the host scrolls) never wraps into a second column.
                wrap.Orientation = Orientation.Vertical
            entries = []
            heads = []
            for key in members:
                if isinstance(key, Head):
                    element = self._subhead(key.name)
                    wrap.Children.Add(element)
                    heads.append((element, []))
                    continue
                tile = by_key.get(key)
                if tile is None:
                    continue
                button = self._tile(tile)
                wrap.Children.Add(button)
                entries.append((button, tile.haystack))
                if heads:
                    heads[-1][1].append(button)
            row.Children.Add(wrap)
            # The other ribbons' rows come after every group of this
            # extension (foreign_tiles is appended to the groups above), so
            # they sit at the bottom of the gallery: visible when you scroll
            # down, first to go when you type (decided 2026-09-05: they go at
            # the very bottom). Before that they were
            # collapsed at rest and only appeared while searching.
            self.host.Children.Add(row)
            self.groups.append((row, header, wrap, entries, index))
            if heads:
                self.subheads[index] = heads

        self.side = self._wants_side(self._page_width())
        self._apply_head_mode()

        self._empty = TextBlock()
        self._empty.Text = "No tool matches that."
        self._empty.Foreground = _brush(ui.TEXT_MUTED)
        self._empty.Margin = Thickness(0, 6, 0, 0)
        self._empty.Visibility = Visibility.Collapsed
        self.host.Children.Add(self._empty)

        self.total = own
        self._build_chips()
        self._status(self._count_text())

        if self.fade:
            _arm_entrance([g[0] for g in self.groups])

    def entrance(self):
        """Play the entrance, once, when the HOST says it has appeared.

        The host owns this call and build() does not, because only the host
        knows when its surface is actually on screen: a window has
        ContentRendered, and a dockable pane has no such moment (it passes
        fade=False, so this is a no-op for it). build() only ARMS the rows.

        Idempotent: ContentRendered can fire again and must not restart six
        animations mid-flight.
        """
        if not self.fade or self._entered:
            return
        self._entered = True
        _play_entrance([g[0] for g in self.groups])

    def _subhead(self, name):
        """A group heading that rides in the row instead of in the gutter.

        Same style, same width, same alignment and **the same height off the top
        of the row** as the gutter one, so the two read as one kind of thing at
        one glance: what changes is that this one sits between tiles. Centred in
        the tile's height it sat about 30px lower than the group name beside it,
        which is what QA caught (2026-09-01) -- two headings of the same
        row printing at two heights.

        The Border is what makes that placement possible: a TextBlock has no
        vertical alignment for its own content, so the box takes the tile's
        height and the text is pinned to its top with the gutter header's own
        12px, which is measured from the same line (a tile's top margin is 0).
        """
        box = Border()
        box.Height = self.tile_h
        box.Margin = Thickness(4, 0, 8, 8)
        text = TextBlock()
        text.Tag = name
        text.Text = head_text(name, self.side)
        text.Style = self._style('SectionHead')
        text.Foreground = _brush(ui.ACCENT)   # same orange as the group header
        text.Width = GUTTER
        text.TextWrapping = TextWrapping.Wrap
        text.TextAlignment = TextAlignment.Right
        text.VerticalAlignment = VerticalAlignment.Top
        # The style carries a margin meant for the gutter; replaced, not kept,
        # by the one number that matters here: HEAD_DROP, shared with the
        # gutter header so the two cannot drift apart.
        text.Margin = Thickness(0, HEAD_DROP, 0, 0)
        box.Child = text
        return box

    def _tile(self, tile):
        button = Button()
        button.Style = self._style('BtnGhost')
        button.Width, button.Height = self.tile_w, self.tile_h
        button.Padding = Thickness(6, 8, 6, 8)
        button.Margin = Thickness(0, 0, 8, 8)
        # THE WHOLE TILE, not just its uid (it was the uid until 2026-09-01):
        # the click handler now has to hand the label, the description, the icon
        # and the group to a detail panel, and reading them back off the button's
        # visual tree would be a second source of truth for the same strings.
        button.Tag = tile
        # The tooltip is the description, and with a detail panel on screen it is
        # the same text twice -- so the host that shows one does not get tooltips.
        # A tile that could not be loaded keeps its own, because it is never
        # clickable and the panel is the only other place that would say so.
        if tile.tooltip and (self.on_pick is None or not tile.enabled):
            button.ToolTip = tile.tooltip

        box = StackPanel()
        if tile.icon:
            image = Image()
            image.Source = bitmap(tile.icon)
            image.Width = image.Height = self.icon_px
            # The source PNG is 96x96, so this is a 4x downscale and WPF's
            # default linear mode renders it soft.
            RenderOptions.SetBitmapScalingMode(image, BitmapScalingMode.HighQuality)
            image.Margin = Thickness(0, 0, 0, 6)
            box.Children.Add(image)
        label = TextBlock()
        label.Text = tile.short
        label.TextWrapping = TextWrapping.Wrap
        label.TextAlignment = TextAlignment.Center
        # 11.5 is measured for the 80px tile; a taller tile has room for a
        # size that reads as a title rather than a caption.
        label.FontSize = 12.5 if self.tile_h > TILE_H else 11.5
        # TWO LINES AND AN ELLIPSIS, and the MaxHeight is what makes the
        # ellipsis possible: inside a StackPanel the TextBlock is measured with
        # infinite height, so it never knows it is being cut and trims nothing --
        # the Button clips it, mid-word, with no mark. Capped, WPF does the
        # trimming itself. LABEL_MAX is that cap, and the one label that reaches
        # it is the longest one (found 2026-09-01, when a regroup moved its
        # group to the top row and put it on screen; it had been reading
        # cut off mid-word in both surfaces all along). The full name is in
        # the detail panel and in the pane's tooltip, so nothing is lost.
        label.TextTrimming = TextTrimming.CharacterEllipsis
        # Same arithmetic as LABEL_MAX / LABEL_MAX_ICON, on this pane's tile.
        label.MaxHeight = (self.tile_h - 16
                           - ((self.icon_px + 6) if tile.icon else 0))
        box.Children.Add(label)

        # THE NUMBER, top right, over the tile's own content. A Grid and not a
        # third row in the StackPanel because it must not push the label down:
        # the badge has to cost zero height or the gallery grows by a row and
        # the window that fits today (430px of content in 436.6) stops fitting.
        # Overlaid, a scan changes nothing about the layout.
        #
        # NO PILL BEHIND IT, and that is not the mockup being ignored: the only
        # light orange in the palette is ACCENT_SEL, which is exactly what
        # select() paints the picked tile with, so a pill would vanish on the
        # one tile the user is looking at. Bold accent text on the tile's own
        # ground reads at 10px and cannot collide.
        cell = Grid()
        cell.Children.Add(box)
        badge = TextBlock()
        badge.FontSize = 10
        badge.FontWeight = FontWeights.Bold
        badge.HorizontalAlignment = HorizontalAlignment.Right
        badge.VerticalAlignment = VerticalAlignment.Top
        # Out into the padding the button keeps for itself (6 right, 8 top), so
        # the number sits in the corner instead of floating over the icon.
        badge.Margin = Thickness(0, -5, -3, 0)
        badge.Visibility = Visibility.Collapsed
        cell.Children.Add(badge)
        self.badges[tile.key] = badge
        # THE STAR, top left, the badge's twin: same zero-height overlay, out
        # into the padding, so a tile with a star is exactly as tall as one
        # without. Only on tiles that loaded: a tool that is not in this
        # session cannot be put on the bar.
        if tile.enabled and not tile.foreign:
            star = XamlReader.Parse(star_xaml())
            star.Margin = Thickness(-6, -8, 0, 0)
            star.IsChecked = favorites.is_fav(tile.key)
            star.Tag = tile
            star.Click += self.on_star
            cell.Children.Add(star)
            self.stars[tile.key] = star
        button.Content = cell

        if tile.enabled:
            button.Click += self.on_tile
            if self.on_pick is not None:
                # The click that was lost to selection, given back: a double
                # click runs. WPF raises Click on each press underneath, so the
                # tile is simply selected twice first, which is a no-op.
                button.MouseDoubleClick += self.on_tile_double
        else:
            button.IsEnabled = False
        return button

    # -- the presets ------------------------------------------------------
    def _build_chips(self):
        """One chip per preset in the host's row, or no row at all."""
        host = self.presets_host
        if host is None:
            return
        host.Children.Clear()
        self.chips = {}
        if not getattr(self.spec, 'presets', False):
            host.Visibility = Visibility.Collapsed
            return
        # Standard first, Mine second, then the groups that carry a chip
        # (design decision, 2026-09-08: Standard first and Mine second; and,
        # the same day, fewer chips: a group that has a door of its own opts
        # out with "chip": false in groups.json, see favorites.presets()).
        names = list(favorites.BUILTIN) + [n for n, _ in favorites.presets()]
        for name in names:
            chip = self._chip(u"\u2605 Mine" if name == favorites.MINE
                              else favorites.label(name))
            chip.Tag = name
            if name == favorites.STANDARD:
                tip = (u"The %d everyday tools we recommend to start with"
                       % len(favorites.STANDARD_TOOLS))
            elif name == favorites.MINE:
                tip = u"Show the tools you starred on the ribbon"
            else:
                tip = u"Show the whole %s group on the ribbon" % name
            chip.ToolTip = tip
            chip.Click += self.on_chip
            host.Children.Add(chip)
            self.chips[name] = chip
        # THE USER'S OWN SETS (2026-09-05, afternoon), after the built-in
        # groups and before the actions. Same chip, same click; the right
        # click is where a set is deleted, because a cross on every chip is
        # more furniture than a row of presets should carry.
        for name, tools in favorites.user_sets():
            chip = self._chip(name)
            chip.Tag = name
            chip.ToolTip = (u"Your set (%d tools). Click to put it on the "
                            u"ribbon, right click to delete it." % len(tools))
            chip.Click += self.on_chip
            menu = ContextMenu()
            item = MenuItem()
            item.Header = u"Delete set \u201c%s\u201d" % name
            item.Tag = name
            item.Click += self.on_delete_set
            menu.Items.Add(item)
            chip.ContextMenu = menu
            host.Children.Add(chip)
            self.chips[name] = chip
        # THE ACTIONS, dimmer, at the end of the row. Save set keeps whatever
        # is on the ribbon right now under a name, so that people can really
        # customize their ribbon; Export and Import move the sets as one JSON
        # file so they can be shared. Export only shows with something to
        # export.
        self.save_chip = self._action_chip(u"+ Save set",
                                           u"Save the tools on the ribbon "
                                           u"right now as a set with a name",
                                           self.on_save_set)
        host.Children.Add(self.save_chip)
        self.export_chip = self._action_chip(u"Export",
                                             u"Write your sets to a JSON "
                                             u"file to share them",
                                             self.on_export_sets)
        if not favorites.user_sets():
            self.export_chip.Visibility = Visibility.Collapsed
        host.Children.Add(self.export_chip)
        host.Children.Add(self._action_chip(u"Import",
                                            u"Add the sets of a JSON file "
                                            u"someone exported",
                                            self.on_import_sets))
        # CLEAR STARS, at the end of the row (asked for 2026-09-05: a button to
        # clear favorites). Two clicks, not one: the first
        # turns it into the question "Clear 7 stars?", the second within a few
        # seconds answers it. Anything else in between (a star, a chip) puts
        # it back. Hidden with nothing to clear.
        chip = self._action_chip(None, u"Unstar every tool (asks first)",
                                 self.on_clear)
        host.Children.Add(chip)
        self.clear_chip = chip
        host.Visibility = Visibility.Visible
        self._paint_chips()

    def _chip(self, label):
        chip = Button()
        chip.Style = self._style('BtnGhost')
        chip.Content = label
        chip.FontSize = 11
        chip.Height = 24
        chip.Padding = Thickness(10, 0, 10, 0)
        chip.Margin = Thickness(0, 0, 6, 6)
        return chip

    def _action_chip(self, label, tooltip, handler):
        chip = self._chip(label)
        chip.Margin = Thickness(6, 0, 0, 6)
        chip.Foreground = _brush(ui.TEXT_DIM)
        chip.ToolTip = tooltip
        chip.Click += handler
        return chip

    def _changed(self):
        """Tell the host the stars moved (its detail panel has a star too)."""
        if self.on_fav_change is None:
            return
        try:
            self.on_fav_change()
        except Exception:
            traceback.print_exc()

    def _reset_chips(self, message=None):
        """Rebuild the row after a set came or went, and say what happened."""
        self._build_chips()
        self._status(self._count_text())
        if message:
            self.count.Text = message + self._fav_mark()
        self._sync_ribbon()

    def on_save_set(self, sender, args):
        try:
            tools = favorites.bar_keys()
            if not tools:
                self.count.Text = u"Nothing on the ribbon to save: star a tool first."
                return
            active = favorites.bar()
            default = active if active in dict(favorites.user_sets()) else u""
            name = ui.ask_for_string(
                u"Name for the %d tools on the ribbon right now:" % len(tools),
                title=u"Save set", default=default)
            if name is None:
                return
            clean, why = favorites.save_set(name, tools)
            if clean is None:
                self.count.Text = why
                return
            favorites.set_bar(clean)
            self._reset_chips(u"set \u201c%s\u201d saved" % clean)
        except Exception:
            traceback.print_exc()

    def on_delete_set(self, sender, args):
        try:
            name = getattr(sender, 'Tag', None)
            if name and favorites.delete_set(name):
                self._reset_chips(u"set \u201c%s\u201d deleted" % name)
        except Exception:
            traceback.print_exc()

    def on_export_sets(self, sender, args):
        try:
            dlg = SaveFileDialog()
            dlg.Title = u"Export Magic Tools sets"
            dlg.Filter = u"Magic Tools sets (*.json)|*.json"
            dlg.FileName = u"magic-tools-sets.json"
            dlg.DefaultExt = u".json"
            if dlg.ShowDialog() != True:
                return
            n = favorites.export_sets(dlg.FileName)
            self.count.Text = (u"%d set%s exported to %s"
                               % (n, u"" if n == 1 else u"s",
                                  os.path.basename(dlg.FileName)))
        except Exception as ex:
            traceback.print_exc()
            self.count.Text = u"export failed: %s" % ex

    def on_import_sets(self, sender, args):
        try:
            dlg = OpenFileDialog()
            dlg.Title = u"Import Magic Tools sets"
            dlg.Filter = u"Magic Tools sets (*.json)|*.json|All files|*.*"
            if dlg.ShowDialog() != True:
                return
            added, updated, skipped = favorites.import_sets(dlg.FileName)
            note = u"%d set%s imported" % (added, u"" if added == 1 else u"s")
            if updated:
                note += u", %d updated" % updated
            if skipped:
                note += u", %d skipped (built-in names)" % skipped
            self._reset_chips(note)
        except ValueError as ex:
            self.count.Text = u"import: %s" % ex
        except Exception as ex:
            traceback.print_exc()
            self.count.Text = u"import failed: %s" % ex

    def _paint_chips(self):
        """Tint the active preset, the way select() tints the picked tile."""
        active = favorites.bar()
        for name, chip in self.chips.items():
            if name == active:
                chip.Background = _brush(ui.ACCENT_SEL)
            else:
                try:
                    chip.ClearValue(Button.BackgroundProperty)
                except Exception:
                    chip.Background = _brush(ui.WIN_BG)
        self._disarm_clear()

    def _disarm_clear(self):
        """The clear chip back to its resting label, or hidden."""
        chip = self.clear_chip
        if chip is None:
            return
        self._clear_armed = None
        count = favorites.count()
        chip.Content = u"\u2606 Clear stars"
        chip.Visibility = Visibility.Visible if count else Visibility.Collapsed

    def on_clear(self, sender, args):
        """First click asks, second click within CLEAR_WINDOW seconds clears."""
        try:
            now = datetime.datetime.now()
            armed = self._clear_armed
            if armed is None or (now - armed).total_seconds() > CLEAR_WINDOW:
                self._clear_armed = now
                sender.Content = u"Clear %d stars? Click again" % favorites.count()
                return
            gone = favorites.clear()
            for star in self.stars.values():
                star.IsChecked = False
            self._disarm_clear()
            self._status(self._count_text())
            self.count.Text = (u"%d stars cleared" % gone) + self._fav_mark()
            self._sync_ribbon()
            self._changed()
        except Exception:
            traceback.print_exc()

    def on_chip(self, sender, args):
        """Put this preset on the bar, now."""
        name = getattr(sender, 'Tag', None)
        if name is None:
            return
        favorites.set_bar(name)
        self._paint_chips()
        self._status(self._count_text())
        self._sync_ribbon()

    # -- the star ---------------------------------------------------------
    def on_star(self, sender, args):
        """A tile's own star: star or unstar it, and refresh the ribbon."""
        args.Handled = True
        tile = getattr(sender, 'Tag', None)
        if tile is not None:
            self.star(tile)

    def star(self, tile):
        """Star or unstar one tile, from its star or from the detail panel.

        The STORE decides, not the click: an eleventh star is refused by
        favorites.toggle, and the tile's box is set from what it returns, so
        the glyph never shows a state the file does not hold. Another open
        window over the same tools keeps its stars until it is next built;
        its next click still reads the store, so it corrects itself.
        Returns whether the tile is starred now.
        """
        now, reason = favorites.toggle(tile.key, tile.label)
        box = self.stars.get(tile.key)
        if box is not None:
            box.IsChecked = now
        self._disarm_clear()
        if reason:
            self.count.Text = reason
        else:
            self._status(self._count_text())
            self._sync_ribbon()
        self._changed()
        return now

    def _sync_ribbon(self):
        """Put the change on the bar now, not on the next Reload.

        Same contract as launch(): a WPF click is not a valid API context, so
        the veil is redone by FavHandler when Revit is idle. Every failure
        here is a footer line and never an exception: the star is already
        saved, and the worst case is a bar that catches up on Reload.
        """
        try:
            arm()
        except Exception as ex:
            self.count.Text = ("saved; the ribbon will follow on Reload "
                               "({0})".format(ex))
            return
        if _FAV_EVENT is None:
            self.count.Text = "saved; the ribbon will follow on Reload"
            return
        state = _FAV_EVENT.Raise()
        if state != UI.ExternalEventRequest.Accepted:
            self.count.Text = ("saved; Revit did not take the refresh ({0}), "
                               "the ribbon will follow on Reload".format(state))

    # -- behaviour --------------------------------------------------------
    def on_search(self, sender, args):
        query = (self.search.Text or '').strip().lower()
        self.hint.Visibility = (Visibility.Collapsed if query
                                else Visibility.Visible)
        shown = 0
        for row, header, wrap, entries, index in self.groups:
            visible = 0
            for button, haystack in entries:
                hit = (not query) or (query in haystack)
                button.Visibility = Visibility.Visible if hit else Visibility.Collapsed
                visible += 1 if hit else 0
            # Same rule one level down: a sub-heading whose tiles the filter
            # took away would sit in the row naming nothing.
            for element, buttons in self.subheads.get(index, ()):
                lit = any(b.Visibility == Visibility.Visible for b in buttons)
                element.Visibility = (Visibility.Visible if lit
                                      else Visibility.Collapsed)
            # A header with nothing under it reads as an empty category, so the
            # group goes away whole -- the row carries both now.
            row.Visibility = Visibility.Visible if visible else Visibility.Collapsed
            shown += visible
        if self._empty is not None:
            self._empty.Visibility = (Visibility.Visible if not shown
                                      else Visibility.Collapsed)
        # A pick the filter just hid stops being a pick. Without this the detail
        # panel keeps describing a tool that is no longer in the list and Enter
        # RUNS it, with nothing on screen saying which one -- caught by the
        # adversarial pass on 2026-09-01, when clear_selection() had no callers
        # at all. on_pick(None) is the host's cue to empty its panel.
        if (self.selected is not None
                and self.selected[0].Visibility != Visibility.Visible):
            self.clear_selection()
            if self.on_pick is not None:
                try:
                    self.on_pick(None)
                except Exception:
                    traceback.print_exc()
        if not query:
            self._status(self._count_text())
        else:
            self._status("%d of %d" % (shown, len(self.tiles)))

    # -- layout mode ------------------------------------------------------
    def _page_width(self, width=None):
        """The width _mode_heights expects, which is the PANE's outer width.

        Every constant in _mode_heights was measured against that number, so a
        host holding a different one (the window knows its card's inner width,
        never a page's) passes None and we rebuild it from the StackPanel: PAD
        and SCROLLBAR are exactly what the page took off before the tiles got
        their room, so adding them back lands on the number the 70-width sweep
        was calibrated on. With no scrollbar showing this over-adds 17px, which
        biases towards the gutter -- the same way the tie already does.
        """
        if width is not None:
            return width
        own = self.host.ActualWidth
        if own <= 0:
            return 0       # too early to know; _wants_side keeps the default
        return own + PAD + SCROLLBAR

    def _apply_head_mode(self):
        """Point every group header at the gutter, or back on top of its row."""
        self._align_subheads()
        for row, header, wrap, entries, index in self.groups:
            header.Text = head_text(header.Tag, self.side)
            if self.side:
                DockPanel.SetDock(header, Dock.Left)
                header.Width = GUTTER
                header.TextAlignment = TextAlignment.Right
                header.Margin = Thickness(0, HEAD_DROP, 12, 0)
                row.Margin = Thickness(0, 0, 0, 0)
            else:
                DockPanel.SetDock(header, Dock.Top)
                header.Width = Double.NaN
                header.TextAlignment = TextAlignment.Left
                header.Margin = Thickness(0, 0, 0, 9)
                row.Margin = Thickness(0, 0 if index == 0 else 14, 0, 0)

    def _align_subheads(self):
        """An inline sub-heading follows the mode of the real headers.

        With the gutter it is right aligned, pointing at the tiles beside it,
        the way the group name does from the left column. With the headers on
        top the row is narrow enough that the tiles under a sub-heading have
        wrapped BELOW it, so pointing right points at nothing: left, like every
        other header in that mode.
        """
        for heads in self.subheads.values():
            for element, buttons in heads:
                element.Child.Text = head_text(element.Child.Tag, self.side)
                element.Child.TextAlignment = (TextAlignment.Right if self.side
                                               else TextAlignment.Left)

    def _wants_side(self, width):
        """Which mode is shorter at this width, for THIS gallery's groups."""
        if self.stacked:
            # Headers on top, always: a gutter beside a single column of big
            # tiles would spend 90px to label three things once each.
            return False
        if width <= 0 or not self.groups:
            return self.side
        # A sub-heading is GUTTER wide plus margins, near enough to one tile
        # step, and it takes a slot on the line like a tile does: counted, or
        # the row it lives in is measured a tile short of what it draws.
        sizes = [len(entries) + len(self.subheads.get(i, ()))
                 for row, head, wrap, entries, i in self.groups]
        side, top = _mode_heights(sizes, width)
        if side is None:
            return False
        # A tie keeps the gutter: it is what the pane opens in, and at the same
        # height it reads better than a stack of headers.
        return side <= top

    def relayout(self, width=None):
        # No dead band, and none needed: the mode is a pure function of the
        # width, so it flips exactly where the two heights cross and cannot
        # oscillate the way a fixed threshold plus hysteresis could.
        side = self._wants_side(self._page_width(width))
        if side == self.side:
            return
        self.side = side
        self._apply_head_mode()

    # -- picking ----------------------------------------------------------
    def on_tile(self, sender, args):
        """A click: run it, or select it and let the host say what that means."""
        tile = sender.Tag
        if tile is None or not tile.enabled:
            return
        if self.on_pick is None:
            self.launch(tile)
            return
        self.select(sender, tile)
        try:
            self.on_pick(tile)
        except Exception:
            traceback.print_exc()

    def on_tile_double(self, sender, args):
        # A double click ON THE STAR is two toggles, not a launch: WPF raises
        # the button's MouseDoubleClick even for presses the star handled.
        if _inside_star(args.OriginalSource, sender):
            return
        tile = sender.Tag
        if tile is not None and tile.enabled:
            self.launch(tile)

    def select(self, button, tile):
        """Mark one tile as the picked one, and un-mark whoever held it.

        The mark is the BACKGROUND and not the border, and that is forced by the
        lib rather than chosen: BtnGhost writes its border INSIDE the control
        template (BorderBrush="#D8D4CD" on the template's Border, not a
        TemplateBinding), so a BorderBrush set from here paints nothing at all.
        Background IS template-bound, so it takes.

        Known and accepted: the style's IsMouseOver trigger sets the template
        Border's own background, so hovering the selected tile hides its tint
        for as long as the cursor is on it. That is the one moment the user
        already knows which tile they are pointing at, and the alternative --
        a second Style with a copied ControlTemplate -- is a copy of the lib's
        template living here, which is exactly the drift this file avoids.
        """
        if self.selected is not None:
            old, _ = self.selected
            try:
                old.ClearValue(Button.BackgroundProperty)
            except Exception:
                old.Background = _brush(ui.WIN_BG)
        button.Background = _brush(ui.ACCENT_SEL)
        self.selected = (button, tile)

    def clear_selection(self):
        if self.selected is None:
            return
        old, _ = self.selected
        try:
            old.ClearValue(Button.BackgroundProperty)
        except Exception:
            old.Background = _brush(ui.WIN_BG)
        self.selected = None

    # -- launching --------------------------------------------------------
    def launch(self, tile):
        """The only valid route out of a modeless surface. Both hosts, one path.

        Everything that runs a tool from a gallery goes through here -- the
        tile's click in the pane, its double click in the window, the detail
        panel's Run button and Enter -- so there is one place where the
        ExternalEvent contract is honoured and one place a failure is reported.
        """
        if not getattr(tile, 'uid', None):
            return
        # Clear whatever a previous failure pinned here before running the
        # next tool. Without this the counter keeps showing an old error over a
        # launch that is working, which is a surface lying about its state.
        try:
            self._status(self._count_text())
        except Exception:
            pass
        error = run_tile(tile, self.report)
        if error:
            self.count.Text = error

    def report(self, message):
        self.count.Text = message

    # -- the scan ---------------------------------------------------------
    def on_scan_click(self, sender, args):
        self.scan()

    def scan(self):
        """Ask Revit for the numbers. Same contract as launch(): raise, wait.

        Nothing is read here. The click is not a valid API context, so all this
        does is queue the request and put the surface in its waiting state; the
        numbers arrive later in take_scan(), on Revit's thread.
        """
        if self._scanning or not self.tiles:
            return
        try:
            arm()
        except Exception as ex:
            self.count.Text = "cannot scan: {0}".format(ex)
            return
        if _SCAN_EVENT is None:
            self.count.Text = "cannot scan: no external event"
            return
        _SCAN_HANDLER.gallery = self
        state = _SCAN_EVENT.Raise()
        if state != UI.ExternalEventRequest.Accepted:
            _SCAN_HANDLER.gallery = None
            self.count.Text = "Revit did not take that ({0}), try again".format(
                state)
            return
        self._scanning = True
        self._busy(True)

    def _busy(self, on):
        """The waiting state. Raise() only queues, so this paints before it runs.

        The button says what is happening and stops taking clicks, because a
        second raise while the first is pending would read the model twice for
        one answer.
        """
        if self.scan_button is not None:
            self.scan_button.IsEnabled = not on
            self.scan_button.Content = "Scanning" if on else "Scan"
        if on:
            self.count.Text = "reading the model..."

    def take_scan(self, doc):
        """Revit's thread, with a document: the only place the badges are set."""
        pairs = [(t, master_key(t.key)) for t in self.tiles]
        counts, failed = toolscan.scan(doc, [m for _, m in pairs])
        for tile, master in pairs:
            value = counts.get(master)
            tile.count = value
            tile.count_line = toolscan.sentence(master, value)
            badge = self.badges.get(tile.key)
            if badge is None:
                continue
            if value is None:
                # No counter (some tools have none) or the counter threw.
                # Either way the honest badge is no badge: a 0 here would be a
                # lie about the model rather than a fact about this tool.
                badge.Visibility = Visibility.Collapsed
                continue
            badge.Text = "{0}".format(value)
            badge.Foreground = _brush(ui.TEXT_MUTED if value == 0
                                      else ui.ACCENT)
            badge.Visibility = Visibility.Visible

        self._scanning = False
        self._busy(False)
        # The hour, always. These numbers are a photograph of a model that keeps
        # changing under them, and a number with no timestamp is the kind of
        # thing someone trusts an hour after it stopped being true.
        self._stamp = u"  \u00b7  scanned " + datetime.datetime.now().strftime(
            "%H:%M")
        if failed:
            self._stamp += u" ({0} not read)".format(len(failed))
        # Repaint the footer through the same path a keystroke uses, so the
        # stamp lands on whatever the filter is showing right now.
        self.on_search(None, None)
        # And refresh the detail panel, which is where the number becomes a
        # sentence -- without this the selected tool keeps its pre-scan text.
        if self.on_pick is not None and self.selected is not None:
            try:
                self.on_pick(self.selected[1])
            except Exception:
                pass

    def scan_failed(self, message):
        self._scanning = False
        self._busy(False)
        self.count.Text = message


# --- the entrance ------------------------------------------------------------

FADE_SLIDE = 14         # px the row travels up
FADE_STEP_MS = 55       # ...and how far behind the previous group it starts


def _arm_entrance(rows):
    """Put the rows where the entrance starts: invisible and 14px low.

    Split from _play_entrance because of what the first version got wrong. It
    did both at once, at the end of build(), which runs on Loaded -- and Loaded
    is BEFORE the first frame is painted. Measured inside Revit on 2026-08-24:
    the window's first paint (ContentRendered) landed with the opacities already
    at 0.945-1.0, so the whole animation had run while there was nothing on
    screen and the tools simply appeared. Arming here (during build) is what
    keeps the first painted frame from flashing the rows at full opacity before
    they fade in.
    """
    for row in rows:
        try:
            row.RenderTransform = TranslateTransform(0, FADE_SLIDE)
            row.Opacity = 0
        except Exception:
            row.Opacity = 1
            row.RenderTransform = None
            traceback.print_exc()


def _play_entrance(rows):
    """The entrance, staggered down the groups.

    The usual two animations (opacity 0 -> 1 over 0.45s, a 14px lift over
    0.5s), which a XAML page would declare itself, built in code here because
    these rows are built in code and there is no XAML of theirs to hang an
    EventTrigger on. Staggered because there are six of them: arriving
    one after another is the whole effect, and all six at once is just a fade.

    BeginAnimation on each object, NOT a Storyboard. The first version used a
    Storyboard with SetTarget(lift, the TranslateTransform), which is the
    documented way to animate a Freezable by object reference -- and inside Revit
    it silently did nothing: the same measurement above found Y pinned at 14.0
    for 2.3 seconds, so every row sat 14px low for good while only the opacity
    moved. BeginAnimation asks the object itself and has no target to resolve,
    which is why it cannot fail that way.

    HoldEnd (the default) is what holds the end state, and it has to: the base
    Opacity is 0 and the base Y is 14, so an animation that RELEASED its property
    would drop the row back to invisible and low.

    Per row inside the try, and the row is restored if it throws: an entrance
    that fails must leave the tools visible, not invisible.
    """
    ease = CubicEase()
    ease.EasingMode = EasingMode.EaseOut
    for index, row in enumerate(rows):
        try:
            slide = row.RenderTransform
            begin = TimeSpan.FromMilliseconds(index * FADE_STEP_MS)

            appear = DoubleAnimation(0, 1, Duration(TimeSpan.FromSeconds(0.45)))
            appear.BeginTime = begin
            appear.EasingFunction = ease
            row.BeginAnimation(UIElement.OpacityProperty, appear)

            if slide is not None:
                lift = DoubleAnimation(FADE_SLIDE, 0,
                                       Duration(TimeSpan.FromSeconds(0.5)))
                lift.BeginTime = begin
                lift.EasingFunction = ease
                slide.BeginAnimation(TranslateTransform.YProperty, lift)
        except Exception:
            row.BeginAnimation(UIElement.OpacityProperty, None)
            row.Opacity = 1
            row.RenderTransform = None
            traceback.print_exc()

# --- the dockable host -------------------------------------------------------

class ToolPane(forms.WPFPanel):
    """A Gallery docked to the edge of Revit. The shell, and nothing else.

    Subclasses set three things and nothing else:
        panel_title  the window's title (and a pane's tab, if one is ever
                     registered again)
        pool_panel   the panel it collects, '<name>.panel' -- since 2026-09-04
                     always "Favorites.panel", the panel of the tools; behind
                     a '>>>' in its layout, only the tools past the slideout
                     count
        pane_groups  [(group name, [folder names])], the set's own order
    and optionally:
        panel_id     a uuid, only needed to register the set as a dockable
                     pane; no set carries one since 2026-09-04
        also_from    other panels whose tools also belong in this set
        orphans      True draws every tool of the pool panel that no group
                     named, under "Other" -- the All Tools window; a door
                     over one group leaves it False
        hide         folder names never drawn, named or not
        card_pose    a PNG from the mascot's pose folder (see toolinfo),
                     shown on the window's title card
        card_line    the line under the set's name on that card
        empty_pose   a PNG from the same folder, the mascot in the empty
                     detail panel
        tile         (width, height, icon px) for this pane's tiles; None
                     takes the module's measured 80 x 78 x 22
        stacked      True draws the tiles one under the other, headers on
                     top, whatever the width -- for a set of two or three
                     big tiles, where a grid has nothing to arrange
        gallery_w    the gallery column the WINDOW opens with; None takes
                     toolwindow's measured 660 (the dockable pane ignores it)
    Those are what lib/toolwindow.py reads to open one in a window; the card,
    the detail poses and the geometry are read there and ONLY there -- a pane
    opens at about 300px and has room for neither the card nor the detail panel.
    """
    panel_id = None
    orphans = False
    hide = ()
    # The Scan button (the model's counts on the tiles) is the Clean Up set's:
    # toolscan knows those sixteen tools and no other. Off unless a spec says.
    scan = False
    # The other ribbons in the search (foreign_tiles) and the chips row: the
    # All Magic Tools set only, see there.
    foreign = False
    presets = False
    card_pose = None
    card_line = None
    empty_pose = None
    tile = None
    stacked = False
    gallery_w = None

    panel_source = os.path.join(HERE, 'ToolPane.xaml')
    pool_panel = None
    also_from = ()
    # Group names are ours; members are FOLDER names, exactly as they appear in
    # the hidden panel's bundle.yaml. A tool in the panel that is missing from
    # this list is not lost: it lands in a trailing "Other" group, visible enough
    # to notice. This list plus that bundle.yaml are what the panel steward edits.
    pane_groups = ()

    def __init__(self):
        forms.WPFPanel.__init__(self)
        self.gallery = None
        self._built = False
        try:
            self._merge_styles()
            self.Background = _brush(ui.WIN_BG)
            # No fade: a pane is toggled, not opened, and it draws once for the
            # whole session -- an entrance would play for a pane already on
            # screen and never again for the times it actually appears.
            self.gallery = Gallery(type(self), self.host, self.txtSearch,
                                   self.lblHint, self.lblCount)
            self.gallery.paint()
            # Built on Loaded, not here: the provider instantiates this type
            # while Revit is still starting, and the tool list is worth reading
            # once the session is actually up. It also keeps a hidden pane from
            # doing any work at all.
            self.Loaded += self.on_loaded
            self.SizeChanged += self.on_resize
        except Exception:
            traceback.print_exc()

    def _merge_styles(self):
        """Inherit the /slantis control styles without copying the palette."""
        xaml = ('<ResourceDictionary '
                'xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" '
                'xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">'
                + ui.styles_xaml() + '</ResourceDictionary>')
        self.Resources.MergedDictionaries.Add(XamlReader.Parse(xaml))

    def on_loaded(self, sender, args):
        if self._built or self.gallery is None:
            return
        self._built = True
        try:
            self.gallery.build()
        except Exception:
            traceback.print_exc()
            self.lblCount.Text = "failed to load the tool list"

    def on_resize(self, sender, args):
        if self.gallery is not None:
            # The Page's own width, which is the number _mode_heights measures
            # against, so it goes in explicitly rather than reconstructed.
            self.gallery.relayout(args.NewSize.Width)


# --- the tool sets themselves ------------------------------------------------
# Everything specific to a set lives here and nowhere else. Adding a fourth one
# is this block plus a door pushbutton that calls toolwindow.open_window on it.

GROUPS_JSON = os.path.join(HERE, 'groups.json')


def groups_from_json(path=GROUPS_JSON):
    """[(group, [folder names])] for All Tools, from lib/groups.json.

    Edited by hand: it is the one place that says which tool belongs to which
    group, now that the folders do not say it (the Favorites panel is flat).
    An unreadable file means no groups, and then every tool of the panel draws
    under "Other" -- visible, not silent.
    """
    try:
        handle = io.open(path, encoding='utf-8')
        try:
            data = json.load(handle)
        finally:
            handle.close()
        return [(entry['group'], list(entry['tools'])) for entry in data]
    except Exception as ex:
        print("toolpane: could not read {0} -> {1}".format(path, ex))
        return []


class AllTools(ToolPane):
    """Every tool of the Favorites panel, grouped by lib/groups.json. The door.

    The one window with orphans on: whatever the Favorites panel lists and
    groups.json does not name still draws, under "Other", so a tool added to
    the panel without a group is found and not lost. The buttons of Tools.panel
    are not in this window at all: they are always on the ribbon, so there is
    nothing to find or star (`hide` stays free for a future case).
    """

    panel_title = "All Magic Tools"
    pool_panel = "Favorites.panel"
    orphans = True
    # This set's only: the chips row (Gallery._build_chips) and the other
    # ribbons in its search (foreign_tiles). The same spec feeds the palette of
    # a build that has one (lib/palette.py), so it finds exactly what the
    # window finds. A build with lib/edition.json can turn either off
    # (favorites.edition), and the open source edition turns both off: with
    # presets off the chips row collapses in _build_chips and the
    # "Customize your ribbon" label stays hidden in toolwindow.
    presets = favorites.PRESETS
    foreign = favorites.edition("foreign", True) is not False
    card_pose = "salem-13-apoyado.png"
    card_line = "Every tool, one search"
    empty_pose = "salem-18-perfil-cabeza-baja.png"
    pane_groups = groups_from_json()


class CleanUpPane(ToolPane):
    """The 15 audit / merge / purge / line tools (not part of this edition).

    A door over one group of the Favorites panel: it names its tools and
    draws nothing else (orphans off), so the other tools that share the
    panel with these stay in All Tools. No button opens it here; it stays as
    the example of a set that is a door over one group.
    """

    panel_title = "Clean Up Tools"
    pool_panel = "Favorites.panel"
    scan = True
    # The mascot, presenting the set. Two appearances, and they can never be
    # on screen together (it cannot be in two places at once): the title
    # card holds the first painted frame while the window is still building and
    # fades as the tools play in, and the second one waits in the detail panel
    # until a tool is picked. Different poses because they do different jobs --
    # one presents, one waits.
    card_pose = "salem-12-se-lame.png"
    card_line = "Clean model, happy cat"
    empty_pose = "salem-13-apoyado.png"
    # ORDER IS WHAT THIS LIST IS FOR: collect() walks it as written and build()
    # draws one row per entry, so the top of the list is the top of the gallery.
    # Reordered 2026-09-01 to what gets reached for most: purge and merge first,
    # audit and links last (they are read before a delivery, not all day).
    #
    # The four line tools are ONE group and not two, with a Head splitting it:
    # each entry here is a DockPanel of its own -- a header plus a WrapPanel --
    # so two groups can never share a row whatever the width is, and 2 + 2 tiles
    # would cost two rows to say what four tiles say in one. They had to sit on
    # one line AND be told apart, which is exactly what the inline sub-heading
    # is for: LINE STYLES in the gutter, its two tools, LINE PATTERNS between
    # the tiles, its two. Both names carry their line break: two words, two
    # lines, right aligned against the tiles (2026-09-05). Links rides
    # the Audit row the same way since the same day.
    #
    # 2026-09-05: one audit leaves this window (found hard to understand; it
    # stays in All Tools only), and another audit gives its slot to a tool
    # brought over from another extension: the Scan already says how many
    # in-place families the model has, so the slot is better spent on the tool
    # that ACTS on them. Both audits stay in All Magic Tools.
    pane_groups = [
        ("Purge", ["Scope Boxes",
                   "Purge Unused Filters",
                   "Purge Unused VT",
                   "Purge Lonely Groups"]),
        ("Merge", ["Merge Filled Regions",
                   "Merge Dim Styles",
                   "Merge Text Types"]),
        ("Line\nStyles", ["Line Style Cleaner",
                           "Merge Line Styles",
                           Head("Line\nPatterns"),
                           "Analyze and Delete Line Patterns",
                           "Search and Merge Line Patterns"]),
        ("Audit", ["Replace In-Place",
                   "In-Place to Loadable",
                   "Audit Ref Planes",
                   "Unplaced Rooms",
                   Head("Links"),
                   "Pin All Links"]),
    ]


class AnalysisPane(ToolPane):
    """The analysis tools, as a window (not part of this edition).

    The Analysis group of groups.json, as a window. Born 2026-09-03 as three
    big stacked tiles over the Inspect tools that came from another extension
    (folder names kept for the command names, tiles read the new __title__
    off each script). Widened to the whole group on 2026-09-04 when the ribbon
    became one panel and the group needed one door; in the full Magic Tools,
    with eight tools, it is a grid again, on the measured defaults, and the
    stacked square tiles went with the three-tool set.

    2026-09-07: the groups were redrawn for the favorites bar and this one
    came out with eight tools again in the full Magic Tools, but not the same
    eight. Out went the three that WRITE to the model, and in came the other
    tools that only ever answer a question. What is left is one clean rule
    for the door: everything here reads the model and changes nothing in it.

    The mascot holds a magnifying glass on the title card (pose 19, drawn for
    this window) and looks down at the detail panel from pose 18.

    2026-09-15: NO DOOR ON THE RIBBON. The slot went to other buttons, so
    nothing opens this window today: the analysis tools are reached through
    All Magic Tools, where they are still their own section, and the group
    went back to being a chip (where presets are on) so it can be put on the
    bar in one click. The spec stays because it IS the window -- putting the
    door back is one pushbutton folder, not a rewrite. Of the tools it names,
    this edition has only the three Inspect ones.
    """

    panel_title = "Analysis Tools"
    pool_panel = "Favorites.panel"
    card_pose = "salem-19-lupa.png"
    card_line = "Nothing stays hidden"
    empty_pose = "salem-18-perfil-cabeza-baja.png"
    pane_groups = [
        ("Inspect", ["Inspect Model Overrides",
                     "Inspect View Overrides",
                     "Inspect Element Graphics"]),
        ("Track", ["Workset tracker",
                   "Filter Tracker"]),
        ("Scan", ["Scan Model",
                  "Scan Current View",
                  "Find Detail Callouts"]),
    ]
