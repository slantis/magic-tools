# -*- coding: utf-8 -*-
"""View Template Manager -- window, row models and every handler live HERE,
not in the pushbutton's script.py.

Why: under rocket mode Magic Tools shares ONE engine across clicks and tears
it down the moment a script.py returns, which kills every handler that
script hung off a modeless window -- the window stays painted, no button
responds (diagnosed 2026-09-17). A module in lib/ survives in sys.modules for
as long as Revit runs, so hosting the window and its handlers here keeps them
alive.
The door (the .pushbutton's script.py) is a three-line stub: `import vtm`
+ `vtm.open_window()`, the same shape lib/toolwindow.py uses for All Magic
Tools.

Step 3 built a READ-ONLY preview: category
tree left, ViewType > template tree right, per-cell state read lazily and
cached, never all at once. Step 4 added the write path: a module-level
`_PENDING` dict staged every change (badge click, footer bulk action) and
`on_apply` was the only place that opened a `DB.Transaction`, writing every
staged pair in one commit and resyncing `_CACHE` from the model.

Step 4b (2026-09-18) transposes the grid into a matrix. Request: "tick 5 view
templates and be able to have them side by side with all their categories"
-- V/G's own Visibility/Graphics runs out of screen once a few templates
are compared side by side, so ticked view TEMPLATES became the matrix's
COLUMNS and CATEGORIES its ROWS, instead of nesting one under the other.
What moved: the old right tree (ViewType > TplRow) is now the LEFT picker,
minus its per-category detail level -- ticking a template no longer expands
anything inside itself, it adds a column to the matrix. What stayed: the
category tree (independent ticks, no cascade -- tri-state now lives only on
VTypeNode, which groups rather than owns its own override), the discipline
filter, the On-sheets/print-set filter, and the whole `_CACHE`/`_PENDING`
staging engine -- a cell's state is still read once and cached, still never
written outside `on_apply`'s transaction. Closing with pending changes just
closes -- nothing was ever written.

The matrix is a `DataGrid` with two frozen columns (chevron+tick, category
name) plus one `DataGridTemplateColumn` appended per ticked template, built
in code by `_rebuild_columns()`. Each cell binds `Cells[t123].Badge` --
an indexer binding WPF resolves against a plain
`System.Collections.Generic.Dictionary[String, Object]` stored on the row's
`CatNode` (`CatNode.Cells`), one cell VM (`CatCellVM`) per (row, ticked template). The
per-column `DataTemplate` is parsed once per column, at column-creation
time, from a XAML string with the key ("t123") substituted in via
`System.Windows.Markup.XamlReader.Parse` -- a `DataGridTemplateColumn` needs
one concrete `CellTemplate` object, and the key has to be baked into the
`{Binding}` path text itself before parsing, there is no way to parameterise
a WPF binding path at runtime otherwise. `_ensure_cells` always creates the
cell VMs for a (node, template) pair BEFORE either the row or the column
reaches the grid, so the very first bind finds a live object at that key.

Step 5 (2026-09-18) turns on the third tab, Filters, INSIDE the same
matrix: same shell, same template picker, same footer, same overrides
editor -- only the ROWS and the CELL MEANING change. A row is a
`DB.ParameterFilterElement` or `DB.SelectionFilterElement` (`FilterRow`; the
selection ones carry a "selection" tag, QA 2026-10-02), flat (no children, no fold, no
discipline -- built to look like a childless `CatNode` so the tree helpers
`_cat_matching`/`_cat_tickable`/`_cat_visible`/`_scope_nodes` need no
`if tab == Filters` branch of their own). A cell (`FilterCellVM`; since v5 a mini table, see below) is
three chips instead of two: STATE (not applied / Visible / Hidden -- a
filter can be entirely absent from a template, categories cannot), OVERRIDES
(identical semantics to categories), and ENABLED (`View.GetIsFilterEnabled`,
hidden when the API lacks it or the cell is not applied -- no category
equivalent). Staging an override or a visibility/enabled change on a filter
NOT yet applied to a template also stages "add it there" -- editing implies
attaching. Read stays per TEMPLATE, not per cell: one `GetFilters()` call
fills every filter row's cache entry for that column in one shot, same
"lazy, only when a template becomes a column" rule steps 3/4 set for
categories. "N/A" on a filter cell means "not applied here", not "cannot be
controlled" -- unlike a category's true N/A, adding the filter at Apply
turns it controllable.

One assumption, flagged and not verified against a live document (no Revit
access in this step): filter PRIORITY ORDER is read via
`View.GetOrderedFilters()`, wrapped in try/except because it was not possible
to confirm which installed Revit version carries it. Order is read-only in
this tool either way -- the API exposes no setter for filter priority, so
the state chip's tooltip can only report "#3 of 7 filters in this template",
never let the user change it.

Filters tab, v5 (2026-10-01, from the design mockup of 2026-09-26): the
Filters cell stopped being three chips and became a mini table of 7 fixed rows
(Projection lines, Surface pattern, Transparency, Cut lines, Cut pattern,
Halftone, Detail level), each inactive / active / pending, under a strip that
keeps what a row cannot express (Visible/Hidden and Enable). It has its own
template (`_FILTER_CELL_TEMPLATE_XAML`, parsed per column by
`_filter_cell_template_for`) and its own VM surface (`FilterCellVM` + `FRowVM`);
Filter rows
fold (collapsed by default, a row with something staged opens itself), the Filters column is 240 px wide, the three tabs
are folio tabs on their own row, and Apply carries the pending count. Read/
write to the model and `on_apply`'s transactions did not change.

Model and Annotation (2026-10-01): the same mini table came to the two
category tabs (`CatCellVM` + `_MODEL_CELL_TEMPLATE_XAML` / `_ANNO_CELL_TEMPLATE_XAML`,
which replace the old two-chip cell). The strip keeps only the Visible/Hidden
state (a category has no Enable and nothing to remove). Model shows the 7 rows
under "Show 7 overrides"; Annotation always shows its 2 (Projection lines,
Halftone). Cut lines / Cut pattern of a category that cannot be cut
(`Category.IsCuttable`) are a fourth row state, "na". Cost, reads: a collapsed
Model cell costs what the old chip cell cost (the `_read_state` it already did,
summary from the tags in `_CACHE`); the 7 rows (`_ogs_rows`, `IsCuttable`) are
read per cell only when its row opens for the first time (`_make_rows_queue`, in
API context) and cached in `_CROWS`. Cost, visuals: the rows' visual tree (~100
bindings) is built only while a cell is open (`MiniHost`, a `ContentControl`
that a trigger gives the rows), so a collapsed cell binds about 15 properties
against the ~6 of the old chip cell. Overrides open like an accordion on Model
and Filters (`_Accordion`, one open row per tab; the Filters "Expand all" is the
one exception), and the gridCat columns are virtualised.
"""

import time

from pyrevit import revit, DB

from slantisui import ui
import modeless
import vgrow
# This module is itself cached for the whole Revit session (that is the point
# of hosting the window here), so a hot edit to lib/vgrow.py would otherwise
# stay invisible until Revit restarts: every caller reloads it, per its own
# docstring. Same guard Inspect View Overrides carries.
try:
    reload(vgrow)
except Exception:
    pass

from System.Collections.Generic import Dictionary
from System.Collections.ObjectModel import ObservableCollection
from System.ComponentModel import INotifyPropertyChanged, PropertyChangedEventArgs
from System.Windows import (Thickness, Visibility, UIElement, RoutedEventHandler, FontWeights,
                            GridLength, WindowState, TextWrapping)
from System.Windows.Controls import (CheckBox as _CheckBox, DataGridRow, DataGridCell,
                                      DataGridTemplateColumn, DataGridLength, TextBlock,
                                      StackPanel)
from System.Windows.Controls.Primitives import PlacementMode
from System.Windows.Documents import Run, LineBreak
from System.Windows.Input import MouseButtonEventHandler
from System.Windows.Markup import XamlReader
from System.Windows.Media import SolidColorBrush, ColorConverter, VisualTreeHelper
from System.Windows.Threading import DispatcherTimer
from System import TimeSpan, Enum as _Enum, String, Object
from System.Diagnostics import Debug

TITLE = u"View Template Manager"


def _id_val(eid):
    # ElementId value compat Revit 2022-2026 (IntegerValue removed in 2026)
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def _brush(hexstr):
    return SolidColorBrush(ColorConverter.ConvertFromString(hexstr))


# Read from ui, never hardcoded -- ui._theme() only rewrites hexes INSIDE the
# window XAML string; a brush built here in Python never passes through it,
# so these are resolved once against whichever theme is active (light/dark).
GREEN = _brush(ui.STATUS_OK)     # Visible
RED = _brush(ui.STATUS_BAD)      # Hidden
GREY = _brush(ui.TEXT_MUTED)     # N/A / placeholder
GOLD = _brush(ui.STATUS_WARN)    # has overrides
DIM = _brush(ui.TEXT_DIM)        # rollup text
INK = _brush(ui.TEXT)            # the fold chevron of a filter row (needs contrast)

VIS = Visibility.Visible
GONE = Visibility.Collapsed

CHEV_OPEN = u"▾"
CHEV_SHUT = u"▸"

INDENT_CAT = Thickness(0)
INDENT_SUB = Thickness(22, 0, 0, 0)
# Left picker is 2 levels (ViewType > TplRow), one indent step per level --
# same idiom as the category tree's Category/Sub.
INDENT_VTYPE = Thickness(0)
INDENT_TPL = Thickness(22, 0, 0, 0)

# Set while a bulk tick (All / None) is running, so one Select-all-style click
# does not fan out into one PropertyChanged raise per node -- lifted from
# Rename Families' `_QUIET`, same reason: quadratic UI churn on a document
# with a few hundred categories/subcategories.
_QUIET = {"on": False}

# (view_template_id, category_id) -> (controllable, hidden_or_None, tags).
# Populated lazily: only when a template becomes a matrix COLUMN (ticked,
# step 4b) does this fill in for every row of the active tab at once, via
# `_schedule_reads()`; switching tabs reads whatever is still missing for
# the templates already ticked. Unticking a template never removes an
# entry. Only one window can be open at a time (modeless keys by TITLE), so
# a plain module-level cache is safe -- cleared at the top of every
# open_window().
_CACHE = {}

# (view_template_id, category_id) -> Pending. Step 4's staging area: a bulk
# footer action or a badge click writes here, `on_apply` is the only reader
# that ever calls the API. Cleared at the top of every open_window(), same as
# `_CACHE`. Only CONTROLLABLE cells (per `_CACHE`) ever get an entry -- a
# cell with no cached entry, or `controllable == False`, is skipped silently
# by every mutator below.
_PENDING = {}

# (view_template_id, filter_id) -> (applied, visible_or_None, enabled_or_None,
# tags, order_or_None). The Filters tab's own cache (step 5, 2026-09-18) --
# same lazy/lifecycle rules as `_CACHE`, but filled per TEMPLATE (one
# `GetFilters()` read covers every filter row of that column at once, see
# `_read_filters_for_template`), not per cell. A filter absent from
# `vt.GetFilters()` still gets an entry here (`applied=False`), unlike a
# category, which is either controllable or not -- a filter's "not applied"
# is itself a real, writable state (Apply can attach it).
_FCACHE = {}

# (view_template_id, filter_id) -> FilterPending. Step 5's staging area,
# same role `_PENDING` plays for categories -- cleared at the top of every
# open_window(), only `on_apply` ever writes to the model from it.
_FPENDING = {}

# view_template_id -> total filter count from `vt.GetOrderedFilters()`, so a
# cell's tooltip can say "#3 of 7 filters in this template" without every
# `FilterCellVM` re-deriving the total from `_FCACHE` on every refresh.
# Filled by `_read_filters_for_template`, cleared alongside `_FCACHE`.
_FORDER_TOTAL = {}

# (view_template_id, filter_id) -> {row key: (active, text, swatch_hex_or_None,
# sig)}: the Filters cell's 7-row mini table as READ from the model (see
# `_ogs_rows`), filled by `_read_filters_for_template` right next to
# `_FCACHE` and cleared with it. `sig` is the raw field values of that row's
# OGS group, so "this row is pending" is a field-by-field comparison of the
# staged OGS against this, never a guess (the staged OGS is absolute since
# step 8b and drags the fields that did not change).
_FROWS = {}

# ElementId value -> name of every line/fill pattern of the document, filled
# ONCE per window by `_prime_pattern_names` (inside an API context). A cell
# refresh runs from plain UI handlers too, where `doc.GetElement` is not
# allowed, so the mini table only ever looks names up here, never in the model.
_PATNAMES = {}
_PRIMED = {"on": False}

# (view_template_id, category_id) -> {row key: (active, text, swatch_hex_or_None,
# sig)} -- the CATEGORY twin of `_FROWS`. Filled LAZILY: Model reads a
# cell's rows only when its row opens for the first time (`_queue_rows`),
# Annotation (always open, 2 rows) reads them in the same pass as the state.
# `{}` = the model side could not be read: the table then shows blank rows and
# computes no pending against a made-up baseline. Cleared with `_CACHE`, and
# re-read after Apply for the pairs that had rows.
_CROWS = {}

# category_id -> `Category.IsCuttable`, read in API context together with the
# rows (like `_PATNAMES`: a UI handler may not touch the model). Absent = not
# read yet, which the cells treat as cuttable (their rows do not exist yet).
_CUTTABLE = {}

# view_template_id -> frozenset of the tabs ("Model", "Annotation", "Filters")
# whose V/G group the template does NOT control (it is not ticked in the
# template's Include list), from `GetNonControlledTemplateParameterIds`. Writing
# an override to such a template succeeds and changes nothing in its views, which
# is what QA 2026-10-02 found. Filled by `_read_noncontrolled` inside an API
# context; absent = not read yet, empty = controls everything (or unknown).
_NONCTRL = {}

# The cell asks for its rows through this hook (set by `open_window`, which owns
# `modeless.run`): a refresh that finds a row open but not read yet calls it.
_ROWS_REQ = {"fn": None}

# Auto-expand on a new pending is off while a BULK action refreshes the grid
# (`_after_bulk`): 200 rows staged at once must not open and close 200 rows in
# an accordion (nor queue 200 reads). They keep the PENDING chip on the summary.
_AUTO = {"on": True}


class Pending(object):
    """Staged-but-not-written changes for one (template, category) cell.

    `vis`: None (no visibility change staged), True (stage "make visible"),
    False (stage "make hidden"). `reset`: True stages "clear overrides at
    Apply" and clears `edit`. `edit`: an `OverrideGraphicSettings` to merge
    in at Apply and clears `reset` (setting one always clears the other --
    a cell cannot stage both a reset and a fresh override). A Pending left
    with `vis is None and not reset and edit is None` is a no-op and
    `_prune_pending` drops it from `_PENDING`.
    """

    def __init__(self, tpl, cat_eid, cat_name, annot=False):
        self.tpl = tpl
        self.cat_eid = cat_eid
        self.cat_name = cat_name
        # Annotation or Model category: which V/G group of the template governs it.
        self.annot = bool(annot)
        self.vis = None
        self._reset = False
        self._edit = None

    # reset and edit exclude each other by construction, not by every
    # mutator remembering to clear the sibling (reviewer, 2026-09-18).
    @property
    def reset(self):
        return self._reset

    @reset.setter
    def reset(self, value):
        self._reset = bool(value)
        if self._reset:
            self._edit = None

    @property
    def edit(self):
        return self._edit

    @edit.setter
    def edit(self, value):
        self._edit = value
        if value is not None:
            self._reset = False


class FilterPending(object):
    """Staged-but-not-written change for one (template, filter) cell in the
    Filters tab (step 5). Same `reset`/`edit` exclusion trick `Pending`
    uses, plus two properties a category never needed: `add` (attach the
    filter to the template at Apply) and `remove` (detach it), which also
    exclude each other by construction. `vis`/`enabled` are plain
    None-means-untouched flags, same shape as `Pending.vis`.

    Staging `remove` clears everything else on this Pending -- once the
    filter is coming off the template at Apply, a staged visibility/enabled/
    override/reset on it is meaningless -- and, the other way round, staging
    any of those cancels `remove`: the cell shows one intent at a time. The opposite rule (staging vis/
    enabled/edit/reset on a filter NOT currently applied also implies
    `add`) is NOT enforced here, because this object alone cannot see
    whether the filter is applied (that lives in `_FCACHE`) -- callers stage
    `add` themselves alongside the other field; see `_toggle_filter_cell_
    pending`, `_edit_filter_cell_override`, `_stage_show`/`_stage_hide`/
    `_stage_filter_edit` (module level).
    """

    def __init__(self, tpl, fid, name):
        self.tpl = tpl
        self.fid = fid
        self.name = name
        self._vis = None
        self._enabled = None
        self._reset = False
        self._edit = None
        self._add = False
        self._remove = False

    # Every "the filter stays and changes" field cancels a staged remove by
    # construction (reviewer, 09-18): otherwise "Remove selected" followed
    # by an override on the same cell showed both as pending while Apply
    # silently dropped the override behind the `remove` short-circuit.
    @property
    def vis(self):
        return self._vis

    @vis.setter
    def vis(self, value):
        self._vis = value
        if value is not None:
            self._remove = False

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = value
        if value is not None:
            self._remove = False

    @property
    def reset(self):
        return self._reset

    @reset.setter
    def reset(self, value):
        self._reset = bool(value)
        if self._reset:
            self._edit = None
            self._remove = False

    @property
    def edit(self):
        return self._edit

    @edit.setter
    def edit(self, value):
        self._edit = value
        if value is not None:
            self._reset = False
            self._remove = False

    @property
    def add(self):
        return self._add

    @add.setter
    def add(self, value):
        self._add = bool(value)
        if self._add:
            self._remove = False

    @property
    def remove(self):
        return self._remove

    @remove.setter
    def remove(self, value):
        self._remove = bool(value)
        if self._remove:
            self._add = False
            self._vis = None
            self._enabled = None
            self._reset = False
            self._edit = None


def _pending_for(tpl, node, create=True):
    """Look up (or, by default, create) the Pending for (tpl, node)."""
    key = (tpl.id_val, node.id_val)
    p = _PENDING.get(key)
    if p is None and create:
        p = Pending(tpl, node.eid, node.Name, getattr(node, "annot", False))
        _PENDING[key] = p
    return p


def _prune_pending(tpl, node):
    """Drop this cell's Pending once it went back to a no-op -- toggling a
    badge back to the current state (or clearing reset/edit) should not
    linger as staged work."""
    key = (tpl.id_val, node.id_val)
    p = _PENDING.get(key)
    if p is not None and p.vis is None and not p.reset and p.edit is None:
        del _PENDING[key]


def _fpending_for(tpl, row, create=True):
    """Look up (or, by default, create) the FilterPending for (tpl, row)."""
    key = (tpl.id_val, row.id_val)
    p = _FPENDING.get(key)
    if p is None and create:
        p = FilterPending(tpl, row.eid, row.Name)
        _FPENDING[key] = p
    return p


def _fprune(tpl, row):
    """Drop this filter cell's FilterPending once every field went back to
    a no-op -- same housekeeping `_prune_pending` does for categories."""
    key = (tpl.id_val, row.id_val)
    p = _FPENDING.get(key)
    if (p is not None and p.vis is None and p.enabled is None and not p.reset
            and p.edit is None and not p.add and not p.remove):
        del _FPENDING[key]


# --------------------------------------------------------------------------
# The window follows the model (QA 2026-10-02). The caches above are
# read once, so a Ctrl+Z in Revit left the cell on "Hidden" while the walls were
# back. `Application.DocumentChanged` now tells the window which of ITS templates
# / filters changed; only their cache entries are dropped and re-read, in one job
# per burst of events. Staged changes (`_PENDING`, `_FPENDING`) are never touched.
# --------------------------------------------------------------------------
DOC_DEBOUNCE_MS = 400

# > 0 while this window is inside its own Apply transaction: the DocumentChanged
# that commit raises must not trigger a second read of what Apply already re-reads.
_SELF = {"n": 0}


class _Burst(object):
    """The template / filter ids (values) reported changed since the last
    re-read. The event can fire many times for one user action (an Undo of a
    big edit, a sync); they pile up here and one timer tick turns them into one
    re-read."""

    def __init__(self):
        self.tpls = set()
        self.flts = set()

    def add(self, tpl_ids, flt_ids):
        self.tpls |= set(tpl_ids)
        self.flts |= set(flt_ids)

    def take(self):
        out = (self.tpls, self.flts)
        self.tpls, self.flts = set(), set()
        return out


def _hit_ids(args, watched_tpl, watched_flt):
    """(template ids, filter ids) of the window that a DocumentChangedEventArgs
    reports as modified, deleted or added. Anything else the transaction touched
    (walls, sheets, other views) is none of this window's business."""
    tpl_hit, flt_hit = set(), set()
    for getter in ("GetModifiedElementIds", "GetDeletedElementIds", "GetAddedElementIds"):
        try:
            ids = getattr(args, getter)()
        except Exception:
            continue
        if not ids:
            continue
        for eid in ids:
            try:
                v = _id_val(eid)
            except Exception:
                continue
            if v in watched_tpl:
                tpl_hit.add(v)
            elif v in watched_flt:
                flt_hit.add(v)
    return tpl_hit, flt_hit


def _invalidate(tpl_ids=(), flt_ids=(), everything=False):
    """Forget what was read from the model so the next `_schedule_reads` reads
    it again: every entry of the given templates (`tpl_ids`), and every entry of
    the given filters across templates (`flt_ids`); `everything` is the Refresh
    button. Pure dict housekeeping, no API call. The cells keep showing what
    they showed until the new read lands (no flicker to "..."); nothing staged
    is touched."""
    if everything:
        for d in (_CACHE, _CROWS, _FCACHE, _FROWS, _FORDER_TOTAL, _NONCTRL, _PATNAMES):
            d.clear()
        _PRIMED["on"] = False
        return
    tpl_ids = set(tpl_ids)
    flt_ids = set(flt_ids)
    if tpl_ids:
        for d in (_CACHE, _CROWS, _FCACHE, _FROWS):
            for k in [k for k in d if k[0] in tpl_ids]:
                del d[k]
        for t in tpl_ids:
            _FORDER_TOTAL.pop(t, None)
            _NONCTRL.pop(t, None)
    if flt_ids:
        for d in (_FCACHE, _FROWS):
            for k in [k for k in d if k[1] in flt_ids]:
                del d[k]


# Column widths (QA 2026-10-02: "Category is too wide for its content and
# the second template column is cut, so you have to scroll"). The name column is
# 160 px on the category tabs (the full name is in its tooltip) and 190 on
# Filters, whose names are longer; the template columns are 240 px, but share
# the room that is left when a couple of them would not fit whole (never below
# 180: the mini table still reads at that width).
NAME_COL_W = {u"Model": 160, u"Annotation": 160, u"Filters": 190}
FIXED_COL_W = 72          # chevron + tick
TPL_COL_MAX = 240
TPL_COL_MIN = 180
GRID_SLACK = 24           # borders + the vertical scrollbar of the grid


def _tpl_col_width(avail, n):
    """Width of each of `n` template columns given `avail` px for all of them:
    240 when they fit, shrinking to share the space down to 180."""
    if n <= 0 or avail <= 0:
        return TPL_COL_MAX
    return max(TPL_COL_MIN, min(TPL_COL_MAX, int(avail // n)))


def _fresh_vt(tpl, doc):
    """After an Undo / Redo the View a TplRow holds can be a stale wrapper:
    fetch it again when Revit says it is no longer valid. API context."""
    try:
        if tpl.vt.IsValidObject:
            return
    except Exception:
        pass
    try:
        vt = doc.GetElement(tpl.eid)
        if vt is not None:
            tpl.vt = vt
    except Exception:
        pass


# --------------------------------------------------------------------------
# Templates that do not control what is being edited (QA 2026-10-02):
# a view template only drives the V/G group ticked in its Include list. Editing
# the Filters of one that does not include "V/G Overrides Filters" writes into the
# template and changes nothing in its views, silently. The header of that column
# now says so, and so does the Apply confirm.
# --------------------------------------------------------------------------
# (tab, BuiltInParameter of the V/G group, what Revit's Include list calls it)
_VG_GROUPS = (
    (u"Model", "VIS_GRAPHICS_MODEL", u"V/G Overrides Model"),
    (u"Annotation", "VIS_GRAPHICS_ANNOTATION", u"V/G Overrides Annotation"),
    (u"Filters", "VIS_GRAPHICS_FILTERS", u"V/G Overrides Filters"),
)
_GROUP_NAME = {u"Model": u"Model categories", u"Annotation": u"Annotation categories",
               u"Filters": u"Filters"}
_GROUP_INCLUDE = dict((g[0], g[2]) for g in _VG_GROUPS)


def _read_noncontrolled(tpl):
    """Fill `_NONCTRL` for one template. API context. A read that fails stores
    "controls everything": no warning beats a wrong one."""
    try:
        ids = set(_id_val(i) for i in tpl.vt.GetNonControlledTemplateParameterIds())
    except Exception:
        _NONCTRL[tpl.id_val] = frozenset()
        return
    out = set()
    for tab, bip_name, _label in _VG_GROUPS:
        try:
            if _id_val(DB.ElementId(getattr(DB.BuiltInParameter, bip_name))) in ids:
                out.add(tab)
        except Exception:
            continue
    _NONCTRL[tpl.id_val] = frozenset(out)


def _not_controlled(tpl, tab):
    return tab in _NONCTRL.get(tpl.id_val, ())


def _warn_short(tab):
    return (u"\u26a0 Doesn't control {}: changes made here won't show in its views."
            .format(_GROUP_NAME[tab]))


def _warn_full(tab):
    return (u"This view template doesn't control {}, so what you change here won't show "
            u"in its views. Tick \"{}\" in the template's Include list to change that."
            .format(_GROUP_NAME[tab], _GROUP_INCLUDE[tab]))


def _staged_warnings(items, fitems):
    """One line per (template, V/G group) with staged changes that its views
    will not show, for the Apply confirm."""
    hit = set()
    for p in items:
        tab = u"Annotation" if getattr(p, "annot", False) else u"Model"
        if _not_controlled(p.tpl, tab):
            hit.add((p.tpl.Template, tab))
    for fp in fitems:
        if _not_controlled(fp.tpl, u"Filters"):
            hit.add((fp.tpl.Template, u"Filters"))
    return [u"\u26a0 {}: doesn't control {}, so these changes won't show in its views.".format(
        name, _GROUP_NAME[tab]) for name, tab in sorted(hit)]


# --------------------------------------------------------------------------
# The staged-changes counter (QA 2026-10-02): the footer used to count
# CELLS, so Walls / Architecture set to Hidden plus a new colour read "1 change
# pending". It now counts what the user did: how many categories / filters are
# touched, and how many separate changes that adds up to.
# --------------------------------------------------------------------------
def _cat_changes(p):
    """Separate changes staged on one category cell: its visibility, and its
    overrides (an edit or a reset counts as ONE change however many rows of the
    mini table it touches)."""
    return (1 if p.vis is not None else 0) + (1 if (p.reset or p.edit is not None) else 0)


def _filter_changes(fp):
    """Same for a filter cell: remove, visibility, enabled, overrides. Attaching
    the filter (`add`) is implied by any of those on a filter that was not
    applied, so it only counts when it is the whole change ("added")."""
    if fp.remove:
        return 1
    n = ((1 if fp.vis is not None else 0) + (1 if fp.enabled is not None else 0)
         + (1 if (fp.reset or fp.edit is not None) else 0))
    if not n and fp.add:
        n = 1
    return n


def _summarize(items, fitems):
    """(categories, filters, changes, templates) of a list of staged `Pending`
    and `FilterPending`: the distinct rows touched, the total of separate
    changes, and the distinct templates they are in."""
    cats = set(_id_val(p.cat_eid) for p in items)
    flts = set(_id_val(fp.fid) for fp in fitems)
    tpls = set(p.tpl.id_val for p in items) | set(fp.tpl.id_val for fp in fitems)
    n = sum(_cat_changes(p) for p in items) + sum(_filter_changes(fp) for fp in fitems)
    return len(cats), len(flts), n, len(tpls)


def _plural(n, one, many):
    return u"{} {}".format(n, one if n == 1 else many)


def _summary_text(n_cat, n_flt, n_changes):
    """"1 category · 2 changes", "2 categories · 1 filter · 5 changes"."""
    parts = []
    if n_cat:
        parts.append(_plural(n_cat, u"category", u"categories"))
    if n_flt:
        parts.append(_plural(n_flt, u"filter", u"filters"))
    parts.append(_plural(n_changes, u"change", u"changes"))
    return u" \u00b7 ".join(parts)


# --------------------------------------------------------------------------
# Apply / Discard / Cancel (QA 2026-10-02): Close, the window's X, Clear
# selection and unticking a template used to throw away what was staged without
# a word. slantisui has no 3-button confirm (Print Set Manager builds its own
# Save / Discard / Cancel with ui.parse, `_ask_save_discard_cancel`), so this is
# the same idea, made generic: `buttons` is [(label, value, primary)] and the
# LAST value is what Esc / the X return.
# --------------------------------------------------------------------------
_ASK_BODY = u"""
  <StackPanel>
    <TextBlock x:Name="lblMsg" Foreground="#202022" FontSize="13" TextWrapping="Wrap"/>
    <Border x:Name="boxWarn" Visibility="Collapsed" Margin="0,14,0,0" Padding="10,8"
            CornerRadius="6" Background="#1AC46A00" BorderBrush="#C46A00" BorderThickness="1">
      <TextBlock x:Name="lblWarn" Foreground="#C46A00" FontSize="12" TextWrapping="Wrap"/>
    </Border>
  </StackPanel>
"""


def _ask(title, message, buttons, warnings=None, owner=None, width=500):
    foot = [u'<Grid><StackPanel HorizontalAlignment="Right" Orientation="Horizontal">']
    for i, (label, _value, primary) in enumerate(buttons):
        foot.append(u'<Button x:Name="btnAsk{}" Content="{}" Style="{{StaticResource {}}}"{}/>'.format(
            i, label, u"BtnPrimary" if primary else u"BtnGhost",
            u' Margin="0,0,8,0"' if i < len(buttons) - 1 else u""))
    foot.append(u"</StackPanel></Grid>")
    win = ui.parse(title, u"", _ASK_BODY, u"".join(foot), width=width)
    win.FindName("lblMsg").Text = message
    if warnings:
        win.FindName("lblWarn").Text = u"\n".join(warnings)
        win.FindName("boxWarn").Visibility = VIS
    result = [buttons[-1][1]]

    def pick(value):
        def handler(s, e):
            result[0] = value
            win.Close()
        return handler

    for i, (_label, value, _primary) in enumerate(buttons):
        win.FindName("btnAsk{}".format(i)).Click += pick(value)
    if owner is not None:
        try:
            win.Owner = owner
        except Exception:
            pass
    win.ShowDialog()
    return result[0]


def _staged_for(tpl_ids):
    """(items, fitems): what is staged for these templates (id values)."""
    items = [p for k, p in _PENDING.items() if k[0] in tpl_ids]
    fitems = [fp for k, fp in _FPENDING.items() if k[0] in tpl_ids]
    return items, fitems


def _has_staged(tpl_id):
    for k in _PENDING:
        if k[0] == tpl_id:
            return True
    for k in _FPENDING:
        if k[0] == tpl_id:
            return True
    return False


class _Accordion(object):
    """Which row of a tab has its overrides open (rule, 2026-10-01: never two
    open at once). One instance per tab (`_ACC`), so opening a row asks ONE
    object whom to close instead of sweeping every row of a grid with hundreds.
    `many` is the exception: the Filters "Expand all" opens every row together
    (to compare); the next single expand closes all of those but the new one."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.open = None
        self.many = []
        self.bulk = False

    def opened(self, row):
        """`row` is about to open: the rows that must close first."""
        if self.bulk:
            return []
        if self.many:
            victims = [r for r in self.many if r is not row]
        elif self.open is not None and self.open is not row:
            victims = [self.open]
        else:
            victims = []
        self.many = []
        self.open = row
        return victims

    def closed(self, row):
        if self.open is row:
            self.open = None
        if row in self.many:
            self.many.remove(row)

    def bulk_expand(self, rows):
        self.bulk = True
        try:
            for r in rows:
                r._expanded = True
        finally:
            self.bulk = False
        self.open = None
        self.many = list(rows)


_ACC = {"Model": _Accordion(), "Filters": _Accordion()}


_UNSUPPORTED_VIEW_TYPES = None  # filled lazily, needs DB loaded


def _unsupported_view_types():
    global _UNSUPPORTED_VIEW_TYPES
    if _UNSUPPORTED_VIEW_TYPES is None:
        # Same exclusion list a retired tool used (2026-09-19): view
        # types a View Template does not meaningfully apply to.
        _UNSUPPORTED_VIEW_TYPES = set([
            DB.ViewType.Schedule, DB.ViewType.DraftingView, DB.ViewType.Legend,
            DB.ViewType.ProjectBrowser, DB.ViewType.SystemBrowser,
            DB.ViewType.Undefined,
        ])
    return _UNSUPPORTED_VIEW_TYPES


# --------------------------------------------------------------------------
# Category tree: one CatNode per top-level Category (kind="Category") or one
# of its SubCategories (kind="Sub") -- the matrix's ROWS since step 4b (it
# used to sit in the left picker; the tree and its tick mechanic did not
# change, only where it is displayed). `picked` is this node's own tick (the
# only thing scope reads) and `Checked` is a plain bool the box paints -- NO
# cascade, NO tri-state (decision of 2026-09-18: overriding a
# category does not mean overriding each subcategory individually). Each
# node is its own independent override target: ticking a Category never
# ticks its SubCategories, and a parent never shows a dash from its
# children's state. `_parent` is kept on the node (harmless) but nothing
# walks it any more. `on_cat_all` (All/None) still reaches every node
# because it iterates `_cat_tickable` directly, one tick per node.
# --------------------------------------------------------------------------
class CatNode(INotifyPropertyChanged):

    def __init__(self, cat, name, kind, parent=None):
        self.cat = cat
        self.eid = cat.Id
        self.id_val = _id_val(cat.Id)
        self.Name = name
        self.kind = kind
        self._parent = parent
        self.children = []
        self._checked = False
        self._expanded = False        # the SUBCATEGORY tree chevron, nothing else
        # The override table of this row's cells. Model rows open and
        # close it ("Show 7 overrides" / "Hide", accordion, auto-expand on a
        # new pending) through `ovr_open`; it is NOT `_expanded`, which keeps
        # folding only the subcategories. Annotation rows (`annot`) have a
        # 2-row table that is always open and never folds.
        self._ovr = False
        self.annot = False
        self._pc_handlers = []
        # Set by _build_cat_tree on roots; a subcategory answers with its
        # parent's set (see `in_discipline`).
        self.disciplines = None
        # Matrix cells (step 4b): one CatCellVM per ticked template, keyed
        # "t{id_val}". A .NET Dictionary, not a plain Python dict, because
        # the grid binds `Cells[t123].Badge` and WPF's indexer-binding only
        # resolves against a real .NET indexer. Populated lazily by
        # `_ensure_cells`, never removed once created (unticking a template
        # only drops its COLUMN, the cache and the cell survive).
        self.Cells = Dictionary[String, Object]()
        # Column-0 checkbox visibility, shared shape with VTypeNode/TplRow's
        # own ScopeVis so the matrix's first column can reuse their template.
        self.ScopeVis = VIS

    def in_discipline(self, disc):
        if disc is None:
            return True
        node = self
        while node.disciplines is None and node._parent is not None:
            node = node._parent
        return bool(node.disciplines) and disc in node.disciplines

    def add_PropertyChanged(self, value):
        self._pc_handlers.append(value)

    def remove_PropertyChanged(self, value):
        if value in self._pc_handlers:
            self._pc_handlers.remove(value)

    def _raise(self, *props):
        if _QUIET["on"]:
            return
        for p in props:
            args = PropertyChangedEventArgs(p)
            for h in list(self._pc_handlers):
                h(self, args)

    @property
    def picked(self):
        """This node's OWN tick -- the only thing scope computation reads."""
        return self._checked

    @property
    def Checked(self):
        return self._checked

    @Checked.setter
    def Checked(self, value):
        self._checked = bool(value)
        self._raise("Checked")

    def _set_checked(self, value):
        # Only this node -- no cascade. `on_cat_all` reaches every node by
        # calling this on each one from `_cat_tickable`, so All/None still
        # covers the whole tree without a parent->children fan-out.
        self._checked = value
        self._raise("Checked")

    @property
    def Indent(self):
        return INDENT_CAT if self.kind == u"Category" else INDENT_SUB

    @property
    def RowAlign(self):
        # The grid's cell style reads this to centre or top-align the fixed
        # columns (see gridCat.Resources). With the mini table on the category
        # tabs a category row is tall too (strip + summary, or the mini
        # table), so the name hangs from the top like a filter's.
        return u"Top"

    # The shared chevron cell binds these; a category keeps the quiet 11 px dim.
    @property
    def ChevSize(self):
        return 11

    @property
    def ChevBrush(self):
        return DIM

    @property
    def Count(self):
        return u"{} sub".format(len(self.children)) if self.children else u""

    @property
    def Chevron(self):
        return (CHEV_OPEN if self._expanded else CHEV_SHUT) if self.children else u""

    def toggle(self):
        if not self.children:
            return
        self._expanded = not self._expanded
        self._raise("Chevron")

    @property
    def ovr_open(self):
        return self._ovr

    @ovr_open.setter
    def ovr_open(self, value):
        value = bool(value)
        if value == self._ovr or self.annot:
            return
        if value:
            for victim in _ACC["Model"].opened(self):
                victim._set_ovr(False)
        else:
            _ACC["Model"].closed(self)
        self._set_ovr(value)

    def toggle_ovr(self):
        self.ovr_open = not self._ovr

    def _set_ovr(self, value):
        # Opens/closes the table in THIS row's cells, one per template column.
        self._ovr = bool(value)
        for cell in list(self.Cells.Values):
            try:
                cell.refresh()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Filters tab rows (step 5, 2026-09-18): one FilterRow per
# `DB.ParameterFilterElement` in the document, flat -- deliberately shaped
# like a childless CatNode (`.Name`, `.children` (always empty), `._expanded`,
# `.in_discipline()`, `.picked`/`Checked`/`_set_checked`, `.Cells`) so the
# category tree's generic helpers (`_cat_matching`, `_cat_tickable`,
# `_cat_visible`, `_scope_nodes`, `_iter_nodes`, `on_cat_all`) drive the
# Filters tab's search box / All-None / row-scope without a single
# `if tab == Filters` inside any of them. What is genuinely different about
# this tab (no discipline grouping) falls out of that shape for free:
# `_active_disc()` always hands this tab `None`. Folding is the one thing it
# does have since v5: `_expanded` (default False) switches each filter row's
# cells between the 7-row mini table and a one-line summary, and
# `_cat_foldable()` hands btnCatFold the filter rows on this tab.
# --------------------------------------------------------------------------
FILTER_KIND_SELECTION = u"selection"


class FilterRow(INotifyPropertyChanged):

    def __init__(self, pf, name, kind=u""):
        self.pf = pf
        self.eid = pf.Id
        self.id_val = _id_val(pf.Id)
        self.Name = name
        # u"" for a rule-based `ParameterFilterElement`, `FILTER_KIND_SELECTION`
        # for a `SelectionFilterElement` (a fixed set of elements picked by hand).
        # Both take AddFilter / SetFilterOverrides / SetFilterVisibility alike.
        self.kind = kind
        self.children = []
        self._checked = False
        # Collapsed by default (2026-10-01, after seeing v5 in Revit:
        # 7 rows per filter is too tall to open on). A row that gets something
        # staged opens by itself (`FilterCellVM.refresh`); the user can fold it
        # back by hand and it stays folded until the next staging on that row.
        self._exp = False
        self._pc_handlers = []
        # Matrix cells, same shape as CatNode.Cells -- populated lazily by
        # `_ensure_cells` with `FilterCellVM` instead of `CellVM`.
        self.Cells = Dictionary[String, Object]()
        self.ScopeVis = VIS

    @property
    def _expanded(self):
        return self._exp

    @_expanded.setter
    def _expanded(self, value):
        # A property, not a bare attribute, so the shared fold helpers
        # (`_fold_all`, `toggle`) that just assign `_expanded` also repaint
        # this row's cells: expanded = the 7-row table, collapsed = a one-line
        # summary, and both live inside each cell, one per template column.
        value = bool(value)
        if value == self._exp:
            return
        # Accordion (2026-10-01): opening a row closes the one that
        # was open. `_ACC["Filters"]` names it; no sweep of the other rows.
        if value:
            for victim in _ACC["Filters"].opened(self):
                victim._set_exp(False)
        else:
            _ACC["Filters"].closed(self)
        self._set_exp(value)

    def _set_exp(self, value):
        self._exp = value
        self._raise("Chevron")
        for cell in list(self.Cells.Values):
            try:
                cell.refresh()
            except Exception:
                pass

    def in_discipline(self, disc):
        # No discipline grouping on this tab; `_active_disc()` always hands
        # this `None` while Filters is active, which every caller already
        # treats as "no filter" -- this exists so a stray call is still safe.
        return True

    def add_PropertyChanged(self, value):
        self._pc_handlers.append(value)

    def remove_PropertyChanged(self, value):
        if value in self._pc_handlers:
            self._pc_handlers.remove(value)

    def _raise(self, *props):
        if _QUIET["on"]:
            return
        for p in props:
            args = PropertyChangedEventArgs(p)
            for h in list(self._pc_handlers):
                h(self, args)

    @property
    def picked(self):
        return self._checked

    @property
    def Checked(self):
        return self._checked

    @Checked.setter
    def Checked(self, value):
        self._checked = bool(value)
        self._raise("Checked")

    def _set_checked(self, value):
        self._checked = value
        self._raise("Checked")

    @property
    def Indent(self):
        return INDENT_CAT

    @property
    def RowAlign(self):
        # Rows are tall here (the mini table), so the name, tick and chevron
        # hang from the top like the mockup instead of floating mid-row.
        return u"Top"

    # "The little arrow gets lost" (feedback): ink colour and 20 px, vs the
    # 11 px dim one of a category. The whole name cell is clickable too (see
    # FilterNameCell in the XAML and `on_cat_grid_click`).
    @property
    def ChevSize(self):
        return 20

    @property
    def ChevBrush(self):
        return INK

    @property
    def Count(self):
        # The dim tag at the right of the name: the kind of filter, when it is
        # not the usual rule-based one.
        return self.kind

    @property
    def Chevron(self):
        return CHEV_OPEN if self._exp else CHEV_SHUT

    def toggle(self):
        # Fold THIS filter's cells in every template column at once: mini
        # table <-> one-line summary. Touches no cache and no staged change.
        self._expanded = not self._exp


# --------------------------------------------------------------------------
# Discipline filter (2026-09-18: "be able to filter by the big groups of
# categories, Revit itself offers those filters"). V/G's "Filter list" is UI
# only: probed live on Revit 2024 through Routes, `Category` exposes
# CategoryType / BuiltInCategory / IsTagCategory and nothing about
# discipline, so the table lives HERE. Model categories are transcribed from
# Revit's own Filter list (captures per discipline); annotation categories
# follow an OST_ name rule until they are captured too. A category can belong
# to several disciplines (Walls: Architecture and Structure; Mechanical
# Equipment: Mechanical, Piping and Architecture), exactly like Revit's list.
# --------------------------------------------------------------------------
DISC_ARCH = u"Architecture"
DISC_STRUCT = u"Structure"
DISC_MECH = u"Mechanical"
DISC_ELEC = u"Electrical"
DISC_PIPE = u"Piping"
DISC_INFRA = u"Infrastructure"
DISCIPLINES = [DISC_ARCH, DISC_STRUCT, DISC_MECH, DISC_ELEC, DISC_PIPE, DISC_INFRA]

# MODEL categories per discipline: transcribed from V/G > Filter list with a
# single discipline ticked, Revit 2024, captures of 2026-09-18 (one
# per discipline, two for Architecture). Keys are lower-cased
# BuiltInCategory names without the "ost_" prefix.
_MODEL_COMMON = (
    # Shown under every one of the six disciplines.
    u"audiovisualdevices", u"detailcomponents", u"fireprotection",
    u"foodserviceequipment", u"genericmodel", u"lines", u"mass",
    u"medicalequipment", u"parts", u"rasterimages", u"signage",
    u"temporarystructure", u"verticalcirculation",
)
_MODEL_STRUCT = _MODEL_COMMON + (
    u"columns", u"floors", u"ramps", u"roofs", u"shaftopening", u"stairs", u"walls",
    u"arearein", u"structuralframingsystem", u"structuralcolumns",
    u"structconnections", u"fabricareas", u"fabricreinforcement",
    u"structuralfoundation", u"structuralframing", u"pathrein", u"rebar",
    u"coupler", u"structuralstiffener", u"structuraltruss",
)
_MODEL_MECH = _MODEL_COMMON + (
    u"ductterminal", u"areas", u"ductaccessory", u"ductfitting", u"ductinsulations",
    u"ductlinings", u"placeholderducts", u"ductcurves", u"flexductcurves",
    u"hvac_zones", u"mechanicalcontroldevices", u"mechanicalequipment",
    u"mepancillaryframing", u"fabricationductwork",
    u"fabricationductworkstiffeners", u"fabricationhangers", u"plumbingequipment",
    u"rooms", u"mepspaces",
)
_MODEL_ELEC = _MODEL_COMMON + (
    u"areas", u"cabletrayfitting", u"cabletray", u"communicationdevices",
    u"conduitfitting", u"conduit", u"datadevices", u"electricalequipment",
    u"electricalfixtures", u"firealarmdevices", u"lightingdevices",
    u"lightingfixtures", u"mechanicalcontroldevices", u"fabricationcontainment",
    u"fabricationhangers", u"nursecalldevices", u"rooms", u"securitydevices",
    u"mepspaces", u"telephonedevices", u"wire",
)
_MODEL_PIPE = _MODEL_COMMON + (
    u"areas", u"flexpipecurves", u"mechanicalequipment", u"fabricationhangers",
    u"fabricationpipework", u"pipeaccessory", u"pipefitting", u"pipeinsulations",
    u"placeholderpipes", u"pipecurves", u"plumbingequipment", u"plumbingfixtures",
    u"rooms", u"mepspaces", u"sprinklers",
)
_MODEL_INFRA = _MODEL_COMMON + (
    u"bridgeabutments", u"bridgebearings", u"bridgecables", u"bridgedecks",
    u"bridgeframing", u"expansionjoints", u"lightingfixtures", u"bridgepiers",
    u"stairsrailing", u"site", u"arearein", u"structuralcolumns",
    u"structconnections", u"fabricareas", u"fabricreinforcement",
    u"structuralfoundation", u"structuralframing", u"pathrein", u"rebar",
    u"coupler", u"structuralstiffener", u"structuraltendons", u"toposolid",
    u"vibrationmanagement",
)
_MODEL_ARCH = _MODEL_COMMON + (
    # Two captures (the list did not fit in one), 2026-09-18.
    u"areas", u"casework", u"ceilings", u"columns", u"curtainwallpanels",
    u"curtasystem", u"curtainwallmullions", u"doors", u"electricalequipment",
    u"electricalfixtures", u"entourage", u"floors", u"furniture", u"furnituresystems",
    u"hardscape", u"lightingfixtures", u"mechanicalcontroldevices",
    u"mechanicalequipment", u"parking", u"planting", u"plumbingequipment",
    u"plumbingfixtures", u"stairsrailing", u"ramps", u"roads", u"roofs", u"rooms",
    u"shaftopening", u"site", u"specialityequipment", u"stairs",
    u"structuralframingsystem", u"structuralcolumns", u"structconnections",
    u"structuralfoundation", u"structuralframing", u"rebar", u"coupler",
    u"structuralstiffener", u"topography", u"toposolid", u"walls", u"windows",
)
_MODEL_SETS = (
    (DISC_ARCH, frozenset(_MODEL_ARCH)),
    (DISC_STRUCT, frozenset(_MODEL_STRUCT)),
    (DISC_MECH, frozenset(_MODEL_MECH)),
    (DISC_ELEC, frozenset(_MODEL_ELEC)),
    (DISC_PIPE, frozenset(_MODEL_PIPE)),
    (DISC_INFRA, frozenset(_MODEL_INFRA)),
)

# ANNOTATION categories were not captured: tags and MEP annotation follow
# their host by OST_ name (OST_DuctTags contains "duct"), and whatever
# matches no rule (Grids, Levels, Text, Dimensions...) is in every
# discipline, which is how V/G lists them. Approximation, flagged as such.
_ANNO_MECH_KEYS = (u"duct", u"mechanical", u"hvac", u"space")
_ANNO_ELEC_KEYS = (u"electrical", u"lighting", u"datadevice", u"communicationdevice",
                   u"firealarm", u"nursecall", u"securitydevice", u"telephonedevice",
                   u"conduit", u"cabletray", u"wire", u"panelschedule", u"audiovisual")
_ANNO_PIPE_KEYS = (u"pipe", u"plumbing", u"sprinkler", u"space")
_ANNO_STRUCT_KEYS = (u"struct", u"rebar", u"fabric", u"truss", u"brace", u"foundation",
                     u"stiffener", u"coupler", u"load", u"analytical",
                     u"boundarycondition", u"span")
_ANNO_INFRA_KEYS = (u"bridge", u"alignment", u"road", u"abutment", u"pier", u"bearing",
                    u"expansionjoint", u"vibration")


def _ost_key(cat):
    """Lower-cased BuiltInCategory name without "ost_" ("walls"), or "" for
    a category that has none (custom subcategories; only roots are asked)."""
    try:
        bic = cat.BuiltInCategory                      # Revit 2023+
    except AttributeError:
        try:
            bic = _Enum.ToObject(DB.BuiltInCategory, _id_val(cat.Id))
        except Exception:
            return u""
    except Exception:
        return u""
    try:
        name = unicode(bic).lower()
    except Exception:
        return u""
    return name[4:] if name.startswith(u"ost_") else name


def _any(name, keys):
    for k in keys:
        if k in name:
            return True
    return False


def _disciplines_of(cat, cat_type):
    """The set of DISCIPLINES this top-level category belongs to. Empty for
    a model category outside every captured list (V/G does not show it)."""
    key = _ost_key(cat)
    out = set()
    if cat_type == DB.CategoryType.Model:
        for disc, members in _MODEL_SETS:
            if key in members:
                out.add(disc)
        # Empty set = V/G does not list this category under any discipline
        # (Sheets, Project Information, Duct Systems, Filled region... 20 in
        # the 2024 model checked on 09-18): _build_cat_tree drops it. That is
        # table-driven on purpose: the API call that would tell us the same
        # (Category.get_AllowsVisibilityControl(doc), over every category)
        # is the prime suspect for a Revit crash during a probe that day.
        return out
    if _any(key, _ANNO_MECH_KEYS):
        out.add(DISC_MECH)
    if _any(key, _ANNO_ELEC_KEYS):
        out.add(DISC_ELEC)
    if _any(key, _ANNO_PIPE_KEYS):
        out.add(DISC_PIPE)
    if _any(key, _ANNO_STRUCT_KEYS) and not out:
        out.add(DISC_STRUCT)
    if _any(key, _ANNO_INFRA_KEYS):
        out.add(DISC_INFRA)
    if not out:
        out.update(DISCIPLINES)      # Grids, Levels, Text, Dimensions...
    return out


def _build_cat_tree(doc, cat_type):
    """One root CatNode per top-level Category of `cat_type`, each with its
    SubCategories as children -- same source as an older category tool's
    `_category_entries` (doc.Settings.Categories, IsVisibleInUI,
    SubCategories), reshaped into a tree instead of a flat picker list."""
    roots = []
    annot = cat_type == DB.CategoryType.Annotation
    for cat in doc.Settings.Categories:
        if cat is None:
            continue
        try:
            if cat.CategoryType != cat_type:
                continue
        except Exception:
            continue
        try:
            if not cat.IsVisibleInUI:
                continue
        except Exception:
            pass
        try:
            name = cat.Name
        except Exception:
            continue
        disciplines = _disciplines_of(cat, cat_type)
        if not disciplines:
            continue        # not in V/G's list for any discipline, see above
        node = CatNode(cat, name, u"Category")
        node.disciplines = disciplines
        node.annot = annot
        try:
            subs = cat.SubCategories
        except Exception:
            subs = None
        if subs:
            for sub in subs:
                try:
                    sub_name = sub.Name
                except Exception:
                    continue
                kid = CatNode(sub, sub_name, u"Sub", parent=node)
                kid.annot = annot
                node.children.append(kid)
        node.children.sort(key=lambda n: n.Name.lower())
        roots.append(node)
    roots.sort(key=lambda n: n.Name.lower())
    return roots


def _build_filter_rows(doc):
    """One `FilterRow` per filter of the document, sorted by name -- the Filters
    tab's rows (step 5). Rule-based filters (`ParameterFilterElement`) AND
    selection filters (`SelectionFilterElement`, QA 2026-10-02: they were left
    out, so a template that used one showed "#k of n filters" numbers that did
    not add up). Built once in `open_window()`, same place `_build_cat_tree`
    runs."""
    rows = []
    for cls_name, kind in (("ParameterFilterElement", u""),
                           ("SelectionFilterElement", FILTER_KIND_SELECTION)):
        cls = getattr(DB, cls_name, None)
        if cls is None:
            continue
        for pf in DB.FilteredElementCollector(doc).OfClass(cls):
            try:
                name = pf.Name
            except Exception:
                continue
            rows.append(FilterRow(pf, name, kind))
    rows.sort(key=lambda r: r.Name.lower())
    return rows


def _iter_nodes(roots):
    for root in roots:
        yield root
        for kid in root.children:
            yield kid


def _scope_nodes(roots):
    """Every CatNode (root or sub) currently ticked, in tree order."""
    return [n for n in _iter_nodes(roots) if n.picked]


def _node_matches(node, q):
    return (not q) or (q in node.Name.lower())


def _cat_matching(roots, q, disc=None):
    """[(root, kids, hit)] honouring the search box and the discipline combo
    -- Rename Families' filtered_groups(), minus the Model/Annotation split
    (the tab is that). `disc` is one of DISCIPLINES or None for all; a root
    outside it drops with its whole subtree. Feeds the matrix's rows
    (step 4b moved the category tree from the left picker to the right
    grid; the filtering logic itself did not change)."""
    out = []
    for root in roots:
        if not root.in_discipline(disc):
            continue
        hit = _node_matches(root, q)
        if hit:
            kids = root.children
        else:
            kids = [k for k in root.children if _node_matches(k, q)]
            if not kids:
                continue
        out.append((root, kids, hit))
    return out


def _cat_tickable(roots, q, disc=None):
    """What All/None acts on: every matching node, fold state ignored -- a
    folded subcategory is one chevron away, not hidden."""
    out = []
    for root, kids, hit in _cat_matching(roots, q, disc):
        if hit:
            out.append(root)
        out.extend(kids)
    return out


def _cat_visible(roots, q, disc=None):
    """What the matrix renders: respects fold, unless a search is narrowing
    the tree (then a match has to be visible to mean anything)."""
    out = []
    searching = bool(q)
    for root, kids, hit in _cat_matching(roots, q, disc):
        out.append(root)
        if kids and (root._expanded or searching):
            out.extend(kids)
    return out


# --------------------------------------------------------------------------
# Filters cell, v5 (2026-10-01, from the v5 design mockup): every
# (filter, template) cell is a MINI TABLE of 7 fixed rows, always visible, so
# there is always somewhere to click -- including the property a filter does
# not override yet. Each row has one of three states:
#   inactive  the filter does not override that property in that template
#   active    the override is already written in the model
#   pending   a staged change touches that row (shown until Apply)
# The row values come from the cell's OverrideGraphicSettings, read once per
# template into `_FROWS` and compared FIELD BY FIELD against the staged OGS.
# --------------------------------------------------------------------------

# (key, title, short title for the collapsed summary, inactive text, swatch).
# The 7 rows and the OGS fields behind each one (vgrow.read_tags is the
# source of truth for "has an override"; the conditions below mirror it):
#   Proj   Projection lines   ProjectionLineColor / Weight / PatternId
#   Surf   Surface pattern    SurfaceForeground + SurfaceBackground (pattern, color, visible)
#   Trans  Transparency       Transparency
#   CutL   Cut lines          CutLineColor / Weight / PatternId
#   CutP   Cut pattern        CutForeground + CutBackground (pattern, color, visible)
#   Half   Halftone           Halftone
#   Det    Detail level       DetailLevel
FILTER_ROWS = (
    ("Proj",  u"Projection lines", u"Proj lines",   u"None", "line"),
    ("Surf",  u"Surface pattern",  u"Surface pat.", u"None", "box"),
    ("Trans", u"Transparency",     u"Transp.",      u"0%",   None),
    ("CutL",  u"Cut lines",        u"Cut lines",    u"None", "line"),
    ("CutP",  u"Cut pattern",      u"Cut pattern",  u"None", "box"),
    ("Half",  u"Halftone",         u"Halftone",     u"Off",  None),
    ("Det",   u"Detail level",     u"Detail",       u"None", None),
)
_ROW_DEFAULT = dict((r[0], r[3]) for r in FILTER_ROWS)

_WARNED = set()    # row keys whose getter already failed once (see `_ogs_rows`)
_NO_ID = -1        # ElementId.InvalidElementId, by value (works on 2022-2026)
_NO_WEIGHT = -1    # OverrideGraphicSettings "<By Category>" line weight


def _is_set_id(eid):
    try:
        return _id_val(eid) != _NO_ID
    except Exception:
        return False


def _rgb_of(color):
    try:
        if color is not None and color.IsValid:
            return (int(color.Red), int(color.Green), int(color.Blue))
    except Exception:
        pass
    return None


def _hex_of(rgb):
    return u"#{:02X}{:02X}{:02X}".format(rgb[0], rgb[1], rgb[2])


def _prime_pattern_names(doc):
    """Fill `_PATNAMES` with every line and fill pattern of `doc`, once per
    window. MUST run inside an API context (it uses collectors); the rows are
    then named from the cache alone, wherever they are refreshed from."""
    if _PRIMED["on"]:
        return
    try:
        for cls in (DB.FillPatternElement, DB.LinePatternElement):
            for el in DB.FilteredElementCollector(doc).OfClass(cls):
                try:
                    _PATNAMES[_id_val(el.Id)] = el.Name
                except Exception:
                    continue
        try:
            # The built-in "Solid" line pattern is an id, not always an element.
            _PATNAMES.setdefault(_id_val(DB.LinePatternElement.GetSolidPatternId()), u"Solid")
        except Exception:
            pass
        _PRIMED["on"] = True
    except Exception:
        pass


def _pat_name(pid):
    name = _PATNAMES.get(_id_val(pid))
    return name if name else u"Pattern"


def _line_row(color, weight, pid):
    rgb = _rgb_of(color)
    w = int(weight)
    has_pat = _is_set_id(pid)
    active = rgb is not None or w != _NO_WEIGHT or has_pat
    parts = []
    if has_pat:
        parts.append(_pat_name(pid))
    if w != _NO_WEIGHT:
        parts.append(u"Wt {}".format(w))
    if not parts and rgb is not None:
        parts.append(u"Color")
    text = u", ".join(parts) if active else u"None"
    return (active, text, _hex_of(rgb) if rgb is not None else None,
            (rgb, w, _id_val(pid)))


def _pattern_side(color, pid, visible, prefix):
    rgb = _rgb_of(color)
    is_set = _is_set_id(pid)
    txt = u""
    if is_set:
        txt = _pat_name(pid)
    elif rgb is not None:
        txt = u"Color"
    if not visible:
        txt = (txt + u" (off)") if txt else u"Hidden"
    if txt and prefix:
        txt = prefix + txt
    active = rgb is not None or is_set or not visible
    return active, txt, rgb, (rgb, _id_val(pid), bool(visible))


def _pattern_row(fg, bg):
    """`fg`/`bg` are (color, pattern id, visible). Foreground and background
    collapse into one row, as the mockup asked."""
    fa, ft, frgb, fsig = _pattern_side(fg[0], fg[1], fg[2], u"")
    ba, bt, brgb, bsig = _pattern_side(bg[0], bg[1], bg[2], u"bg ")
    active = fa or ba
    text = u", ".join([t for t in (ft, bt) if t]) if active else u"None"
    rgb = frgb if frgb is not None else brgb
    return (active, text, _hex_of(rgb) if rgb is not None else None, (fsig, bsig))


def _trans_row(value):
    t = int(value)
    return (t > 0, u"{}%".format(t), None, t)


def _half_row(value):
    h = bool(value)
    return (h, u"On" if h else u"Off", None, h)


def _detail_row(value):
    s = u"{}".format(value)
    active = s != u"Undefined"
    return (active, s if active else u"None", None, s)


def _ogs_rows(ogs, keys=None):
    """{row key: (active, text, swatch_hex_or_None, sig)} for one
    OverrideGraphicSettings. Pure reads of the OGS (no Revit context needed);
    a group that refuses to read degrades to an inactive row with no sig, so a
    broken getter can never fake a "pending". `keys` (a set of row keys) limits
    the read to those rows: Annotation shows 2 of the 7 and has no reason to
    pay for the other 5 (3 getters each)."""
    rows = {}

    def put(key, make):
        if keys is not None and key not in keys:
            return
        try:
            rows[key] = make()
        except Exception as ex:
            rows[key] = (False, _ROW_DEFAULT[key], None, None)
            if key not in _WARNED:
                # Once per row, to the debugger stream (never print: it can
                # kill Revit from the wrong thread). Degradation unchanged.
                _WARNED.add(key)
                try:
                    Debug.WriteLine(u"vtm: could not read OGS row {}: {}".format(key, ex))
                except Exception:
                    pass

    put("Proj", lambda: _line_row(ogs.ProjectionLineColor, ogs.ProjectionLineWeight,
                                    ogs.ProjectionLinePatternId))
    put("Surf", lambda: _pattern_row(
        (ogs.SurfaceForegroundPatternColor, ogs.SurfaceForegroundPatternId,
         ogs.IsSurfaceForegroundPatternVisible),
        (ogs.SurfaceBackgroundPatternColor, ogs.SurfaceBackgroundPatternId,
         ogs.IsSurfaceBackgroundPatternVisible)))
    put("Trans", lambda: _trans_row(ogs.Transparency))
    put("CutL", lambda: _line_row(ogs.CutLineColor, ogs.CutLineWeight,
                                    ogs.CutLinePatternId))
    put("CutP", lambda: _pattern_row(
        (ogs.CutForegroundPatternColor, ogs.CutForegroundPatternId,
         ogs.IsCutForegroundPatternVisible),
        (ogs.CutBackgroundPatternColor, ogs.CutBackgroundPatternId,
         ogs.IsCutBackgroundPatternVisible)))
    put("Half", lambda: _half_row(ogs.Halftone))
    put("Det", lambda: _detail_row(ogs.DetailLevel))
    return rows


# What a blank OverrideGraphicSettings reads as, row by row: the "staged" side
# of a pending Reset / Remove, and the "current" side of a filter that is not
# applied yet. Built from a real blank OGS the first time it is needed; the
# static table is only the net for a Revit that refuses to construct one.
_STATIC_BLANK = {
    "Proj":  (False, u"None", None, (None, -1, -1)),
    "Surf":  (False, u"None", None, ((None, -1, True), (None, -1, True))),
    "Trans": (False, u"0%", None, 0),
    "CutL":  (False, u"None", None, (None, -1, -1)),
    "CutP":  (False, u"None", None, ((None, -1, True), (None, -1, True))),
    "Half":  (False, u"Off", None, False),
    "Det":   (False, u"None", None, u"Undefined"),
}
_BLANK = {"rows": None}


def _blank_rows():
    rows = _BLANK["rows"]
    if rows is None:
        try:
            rows = _ogs_rows(DB.OverrideGraphicSettings())
        except Exception:
            rows = None
        if not rows:
            rows = _STATIC_BLANK
        _BLANK["rows"] = rows
    return rows


def _same_rows(ogs_a, ogs_b):
    """True when two OverrideGraphicSettings agree on all 7 rows (every one of
    the 21 fields belongs to exactly one row). A row that could not be read
    (sig None) counts as different: when in doubt, keep the edit."""
    a = _ogs_rows(ogs_a)
    b = _ogs_rows(ogs_b)
    for rkey in _ROW_DEFAULT:
        sa, sb = a[rkey][3], b[rkey][3]
        if sa is None or sb is None or sa != sb:
            return False
    return True


def _prefill_ogs(pend, read_model):
    """What the editor should open on for a cell: what is STAGED if anything
    is (a staged edit, or a staged reset = blank), otherwise the model's own
    override via `read_model()`. Without this, re-opening a pending cell showed
    the model and the second Apply in the dialog silently dropped the first.
    `pend` is a Pending / FilterPending or None."""
    if pend is not None:
        if pend.edit is not None:
            return pend.edit
        if pend.reset:
            return DB.OverrideGraphicSettings()
    return read_model()


def _stage_filter_edit(tpl, row, new_ogs, model_ogs, applied, same=None):
    """Stage an edited OGS on a (template, filter) cell -- unless it equals
    what the model already has, in which case the cell goes back to "nothing
    staged for the overrides" (a no-change edit must not stay counted in the
    footer or show PENDING)."""
    p = _fpending_for(tpl, row, create=True)
    if not applied:
        p.add = True
    if same is None:
        same = _same_rows(new_ogs, model_ogs)
    if same:
        p.reset = False
        p.edit = None
    else:
        p.edit = new_ogs
    _fprune(tpl, row)


def _stage_cat_edit(tpl, node, new_ogs, model_ogs, same=None):
    """Category twin of `_stage_filter_edit`."""
    p = _pending_for(tpl, node, create=True)
    if same is None:
        same = _same_rows(new_ogs, model_ogs)
    if same:
        p.reset = False
        p.edit = None
    else:
        p.edit = new_ogs
    _prune_pending(tpl, node)


# The editor's intent keys that write a Cut* field (see `vgrow._INTENT_FIELDS`).
_CUT_INTENT_KEYS = frozenset((u"cut_color", u"cut_weight", u"cut_pattern",
                              u"cfg_vis", u"cfg_color", u"cfg_pattern",
                              u"cbg_vis", u"cbg_color", u"cbg_pattern"))


def _cat_intents(node, intents):
    """The editor's intents for one category: a category already known not to
    be cuttable (`_CUTTABLE`, filled when its rows were read) never gets Cut
    fields staged. Unknown (rows never read) is left alone: if Revit refuses it
    at Apply the pair lands in the failed list, as before."""
    if _CUTTABLE.get(node.id_val) is False and intents:
        return dict((k, v) for k, v in intents.items() if k not in _CUT_INTENT_KEYS)
    return intents


def _touched_same(new_ogs, model_ogs, intents):
    """Cheap no-op test: do the fields the editor touched equal the model's?
    Only valid when the edit started FROM the model (see `_bulk_same`). Any
    doubt answers False: keep the edit."""
    try:
        for key, attr, _setter in vgrow._INTENT_FIELDS:
            if key not in intents:
                continue
            a, b = getattr(new_ogs, attr), getattr(model_ogs, attr)
            if hasattr(a, "IsValid"):
                if not vgrow._color_eq(a, b):
                    return False
            elif a != b:
                return False
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# The Cut lines hint (QA 2026-10-02): a colour set on the Projection lines
# of Walls does not show in a plan, because walls are CUT there and drawn with
# their Cut lines. The editor (vgrow.show_edit_dialog) has no slot for a note and
# lib/vgrow.py is not this tool's to change, so the hint rides on the one thing
# the editor already supports: `ui.parse(..., context=)`. For the single call that
# opens the editor, vgrow's reference to `ui` is swapped for a shim whose `parse`
# adds the `context` and puts the real module back at once (see `_edit_dialog`).
# --------------------------------------------------------------------------
_HINT_ONE = (u"In plans and sections this category is cut, so it is drawn with its Cut lines. "
             u"Projection lines apply to elevations, 3D views and what is seen beyond the cut plane.")
_HINT_MANY = (u"Some of the selected categories are cut in plans and sections, where they are "
              u"drawn with their Cut lines. Projection lines apply to elevations, 3D views and "
              u"what is seen beyond the cut plane.")


def _is_cuttable(node):
    """`Category.IsCuttable`, cached in `_CUTTABLE` (also filled when a cell's
    rows are read). Annotation categories are never cut. API context."""
    if getattr(node, "annot", False):
        return False
    v = _CUTTABLE.get(node.id_val)
    if v is None:
        try:
            v = bool(node.cat.IsCuttable)
            _CUTTABLE[node.id_val] = v
        except Exception:
            return False
    return bool(v)


def _cut_hint(nodes):
    """The note for the editor, or u"" when none of `nodes` is cut."""
    cut = [n for n in nodes if _is_cuttable(n)]
    if not cut:
        return u""
    return _HINT_ONE if len(nodes) == 1 else _HINT_MANY


def _edit_dialog(prefill, owner_win, doc, focus=None, prefill_many=None, hint=u""):
    """`vgrow.show_edit_dialog`, with `hint` shown under the title when given."""
    if not hint:
        return vgrow.show_edit_dialog(prefill, owner_win=owner_win, doc=doc, focus=focus,
                                      prefill_many=prefill_many)
    real = getattr(vgrow, "ui", None)
    if real is None:       # vgrow no longer exposes it: no hint beats a crash
        return vgrow.show_edit_dialog(prefill, owner_win=owner_win, doc=doc, focus=focus,
                                      prefill_many=prefill_many)

    class _OneShot(object):
        def __getattr__(self, name):
            return getattr(real, name)

        def parse(self, *a, **k):
            vgrow.ui = real          # one shot: nothing else sees the shim
            if not k.get("context"):
                k["context"] = hint
            return real.parse(*a, **k)

    vgrow.ui = _OneShot()
    try:
        return vgrow.show_edit_dialog(prefill, owner_win=owner_win, doc=doc, focus=focus,
                                      prefill_many=prefill_many)
    finally:
        vgrow.ui = real


def _bulk_same(new_ogs, model_ogs, intents, pend):
    """`same` for `_stage_*_edit` on the bulk path, where `_same_rows` (two
    `_ogs_rows`, 42 getters per pair) over hundreds of pairs inside one Idling
    can freeze Revit. If nothing is staged on the pair the edit started from the
    model, so comparing the touched fields is exact; otherwise None: the full
    row comparison decides (the staged OGS may differ from the model on fields
    this edit did not touch)."""
    if pend is not None and (pend.edit is not None or pend.reset):
        return None
    return _touched_same(new_ogs, model_ogs, intents or {})


_SW_BRUSHES = {}


def _sw_brush(hexstr):
    if not hexstr:
        return None
    b = _SW_BRUSHES.get(hexstr)
    if b is None:
        b = _brush(hexstr)
        _SW_BRUSHES[hexstr] = b
    return b


class FRowVM(INotifyPropertyChanged):
    """One of the 7 mini-table rows of one Filters cell: what the row's
    `DataContext` binds to (`State` drives the three looks through
    DataTriggers in `_FILTER_CELL_TEMPLATE_XAML`). `set` raises only the
    properties that actually changed -- a window with a few hundred cells
    refreshes thousands of rows on every read."""

    _FIELDS = ("State", "Val", "Before", "BeforeVis", "Sw", "SwVis", "Tip")

    def __init__(self, default_text):
        self.State = u"inactive"
        self.Val = default_text
        self.Before = u""
        self.BeforeVis = GONE
        self.Sw = None
        self.SwVis = GONE
        self.Tip = u""
        self._pc_handlers = []

    def add_PropertyChanged(self, value):
        self._pc_handlers.append(value)

    def remove_PropertyChanged(self, value):
        if value in self._pc_handlers:
            self._pc_handlers.remove(value)

    def _raise(self, *props):
        for p in props:
            args = PropertyChangedEventArgs(p)
            for h in list(self._pc_handlers):
                h(self, args)

    def set(self, state, val, before, sw_hex, tip=u""):
        sw = _sw_brush(sw_hex)
        new = (state, val, before, VIS if before else GONE, sw,
               VIS if sw is not None else GONE, tip)
        changed = []
        for name, value in zip(self._FIELDS, new):
            if getattr(self, name) != value:
                setattr(self, name, value)
                changed.append(name)
        if changed:
            self._raise(*changed)


class FilterCellVM(INotifyPropertyChanged):
    """One matrix cell for the Filters tab (step 5, redesigned in v5): this
    template's state for one filter (rule-based or selection), read from `_FCACHE`/
    `_FROWS` and overlaid with any staged `_FPENDING`.

    Since v5 the cell is a mini table with its own template (Model/Annotation
    got the same idea, see `CatCellVM`):
    `_FILTER_CELL_TEMPLATE_XAML`, made of
      - a STRIP: the Visible/Hidden toggle (`Badge`, `EyeOpenVis`/`EyeShutVis`)
        and Enable (`On*`), the two binary things a filter has that no row of
        the table can express;
      - the MINI TABLE (`ProjRow` ... `DetRow`, one `FRowVM` each), shown
        while the filter row is expanded;
      - a one-line SUMMARY (`SummaryText`), shown while it is collapsed;
      - or just the "+ Add to template" cell (`AddVis`) when the filter is not
        applied to this template and nothing is staged to attach it.

    Note on `Tip`: it carries the order/pending summary on the strip's state
    toggle; the add cell reuses it with the "click to add" sentence.
    """

    def __init__(self, tpl, node):
        self.tpl = tpl
        self.node = node
        self.Badge = u"…"
        self.BadgeVis = VIS
        self.BadgeBrush = GREY
        self.OnText = u"Enabled"
        self.OnBrush = GREY
        self.OnTextBrush = DIM
        self.OnVis = GONE
        self.Tip = u""
        self.AddVis = VIS
        self.AddText = u"…"
        self.StripVis = GONE
        self.MiniVis = GONE
        self.SummaryVis = GONE
        self.EyeOpenVis = VIS
        self.EyeShutVis = GONE
        self.SummaryText = u""
        self.SummaryPend = GONE
        self._pend_sig = None    # what was staged at the last refresh (auto-expand)
        self.RmVis = GONE        # the little x: remove this filter from THIS template
        self.RmOn = False        # True while a remove is staged (the x then undoes it)
        self.RmTip = u"Remove from this template"
        for rkey, _title, _short, default, _kind in FILTER_ROWS:
            setattr(self, rkey + "Row", FRowVM(default))
        self._pc_handlers = []

    def add_PropertyChanged(self, value):
        self._pc_handlers.append(value)

    def remove_PropertyChanged(self, value):
        if value in self._pc_handlers:
            self._pc_handlers.remove(value)

    def _raise(self, *props):
        for p in props:
            args = PropertyChangedEventArgs(p)
            for h in list(self._pc_handlers):
                h(self, args)

    def _put(self, **vals):
        changed = []
        for name, value in vals.items():
            if getattr(self, name) != value:
                setattr(self, name, value)
                changed.append(name)
        if changed:
            self._raise(*changed)

    def refresh(self):
        key = (self.tpl.id_val, self.node.id_val)
        entry = _FCACHE.get(key)
        if entry is None:
            # Not read yet: show the add cell as a "…" placeholder.
            self._put(Badge=u"…", BadgeBrush=GREY, BadgeVis=VIS, OnVis=GONE,
                      Tip=u"", AddVis=VIS, AddText=u"…", StripVis=GONE,
                      MiniVis=GONE, SummaryVis=GONE, RmVis=GONE)
            return

        applied, visible, enabled, tags, order = entry
        pend = _FPENDING.get(key)
        removing = pend is not None and pend.remove
        adding = pend is not None and pend.add and not applied
        shows = bool(applied or adding)    # strip + table, vs the add cell

        # -- strip, left: STATE toggle --
        if not applied:
            badge, brush = u"not applied", GREY
        elif visible:
            badge, brush = u"Visible", GREEN
        else:
            badge, brush = u"Hidden", RED

        if pend is not None:
            if pend.remove:
                badge, brush = u"→ removed", RED
            elif pend.vis is not None:
                badge = u"→ Visible" if pend.vis else u"→ Hidden"
                brush = GREEN if pend.vis else RED
            elif pend.add:
                badge, brush = u"→ applied", GREEN

        eye_open = bool(visible) if applied else True
        if pend is not None and pend.vis is not None:
            eye_open = bool(pend.vis)

        # -- strip, right: ENABLED. Colour is INVERTED on purpose (v5): a
        # filter that is on is the normal case and stays quiet grey; a
        # disabled one is the surprise and takes the warn colour. Hidden on
        # N/A or when the API lacks the flag. --
        if applied and enabled is not None and not removing:
            eff = bool(enabled)
            staged = pend is not None and pend.enabled is not None
            if staged:
                eff = bool(pend.enabled)
            text = u"Enabled" if eff else u"Disabled"
            if staged:
                text = u"→ " + text
            on = dict(OnVis=VIS, OnText=text, OnBrush=GREY if eff else GOLD,
                      OnTextBrush=DIM if eff else GOLD)
        else:
            on = dict(OnVis=GONE)

        # -- tooltip on the state toggle: order (if known) + pending summary --
        order_tip = u""
        if order is not None:
            total = _FORDER_TOTAL.get(self.tpl.id_val)
            if total:
                order_tip = u"#{} of {} filters in this template".format(order + 1, total)
        pend_tip = _filter_pending_desc(pend) if pend is not None else u""
        if order_tip and pend_tip:
            tip = order_tip + u"\n" + pend_tip
        elif order_tip:
            tip = order_tip
        elif pend_tip:
            tip = pend_tip
        elif applied:
            tip = u", ".join(tags) if tags else u"No overrides. Click a row to set one."
        else:
            tip = u"Not applied to this template. Click to add it."

        # -- the 7 rows: current (model) vs staged, field by field --
        names = []
        any_pend = False
        if shows:
            # Applied but never read into `_FROWS` (GetFilterOverrides threw):
            # the model side is unknown, so show rows from blank and do NOT
            # compute pending against a made-up baseline.
            cur_known = (not applied) or (key in _FROWS)
            cur = (_FROWS.get(key) if applied else None) or _blank_rows()
            staged_rows = None
            if pend is not None and cur_known:
                if pend.remove or pend.reset:
                    staged_rows = _blank_rows()
                elif pend.edit is not None:
                    staged_rows = _ogs_rows(pend.edit)
            for rkey, _title, short, default, _kind in FILTER_ROWS:
                c = cur.get(rkey) or _STATIC_BLANK[rkey]
                s = staged_rows.get(rkey) if staged_rows is not None else None
                rowvm = getattr(self, rkey + "Row")
                if (s is not None and s[3] is not None and c[3] is not None
                        and s[3] != c[3]):
                    # Only a row whose fields really differ is pending: the
                    # staged OGS is absolute and drags the untouched rows too.
                    any_pend = True
                    before = (c[1] if c[0] else default) + u" →"
                    after = s[1] if s[0] else default
                    rowvm.set(u"pending", after, before, s[2] if s[0] else None,
                              before + u" " + after)
                    if s[0]:
                        names.append(short)
                else:
                    # An inactive row's tooltip must not say "None": it is
                    # an invitation, not a value.
                    rowvm.set(u"active" if c[0] else u"inactive", c[1], u"",
                              c[2] if c[0] else None,
                              c[1] if c[0] else u"Click to add an override")
                    if c[0]:
                        names.append(short)

        expanded = bool(getattr(self.node, "_expanded", True))
        self._put(Badge=badge, BadgeBrush=brush, BadgeVis=VIS, Tip=tip,
                  EyeOpenVis=VIS if eye_open else GONE,
                  EyeShutVis=GONE if eye_open else VIS,
                  AddVis=GONE if shows else VIS, AddText=u"+  Add to template",
                  StripVis=VIS if shows else GONE,
                  MiniVis=VIS if (shows and expanded) else GONE,
                  SummaryVis=VIS if (shows and not expanded) else GONE,
                  SummaryText=u", ".join(names) if names else u"No overrides",
                  SummaryPend=VIS if any_pend else GONE,
                  RmVis=VIS if applied else GONE, RmOn=bool(removing),
                  RmTip=(u"Undo: keep this filter in this template" if removing
                         else u"Remove from this template"), **on)

        # A filter row opens ITSELF when something new is staged on it, so a
        # pending change is never hidden behind a collapsed row. "New" = the
        # staged state differs from the previous refresh (a second edit
        # counts, via the edit object's identity); a refresh that finds the
        # same staging leaves the row as the user left it, and a pending that
        # disappears (Apply, undo) never folds anything.
        sig = None
        if pend is not None:
            sig = (pend.vis, pend.enabled, pend.reset, pend.add, pend.remove,
                   id(pend.edit) if pend.edit is not None else None)
        prev = self._pend_sig
        self._pend_sig = sig
        if (sig is not None and sig != prev and _AUTO["on"]
                and not getattr(self.node, "_exp", True)):
            self.node._expanded = True      # refreshes every cell of the row


def _join_and(bits):
    """["a", "b", "c"] -> "a, b and c"."""
    if len(bits) <= 1:
        return u"".join(bits)
    return u", ".join(bits[:-1]) + u" and " + bits[-1]


def _pending_sentence(states, overrides):
    """The tooltip of a staged cell, as a sentence. `states` are past
    participles ("set to Hidden", "added to this template"), `overrides` is
    None, "reset" or "changed". QA 2026-10-02 (N9): the old text glued both
    kinds into one list and read "Will be set to Hidden, overrides changed on
    Apply."; the two now get their own verb: "Will be set to Hidden and have
    its overrides changed on Apply." """
    have = u"have its overrides " + overrides if overrides else u""
    if states and have:
        # With two or more states a bare "and" would read as one more state
        # ("... Visible and disabled and have its overrides changed").
        glue = u", and " if len(states) > 1 else u" and "
        return u"Will be " + _join_and(states) + glue + have + u" on Apply."
    if states:
        return u"Will be " + _join_and(states) + u" on Apply."
    if have:
        return u"Will " + have + u" on Apply."
    return u""


def _filter_pending_desc(pend):
    """One-line, human summary of a FilterPending for the state chip's
    tooltip -- e.g. "Will be added to this template and set to Visible on
    Apply.\""""
    if pend.remove:
        return u"Will be removed from this template on Apply."
    states = []
    if pend.add:
        states.append(u"added to this template")
    if pend.vis is not None:
        states.append(u"set to " + (u"Visible" if pend.vis else u"Hidden"))
    if pend.enabled is not None:
        states.append(u"enabled" if pend.enabled else u"disabled")
    ovr = u"reset" if pend.reset else (u"changed" if pend.edit is not None else None)
    return _pending_sentence(states, ovr)


# --------------------------------------------------------------------------
# Category cell (2026-10-01): the Filters mini table on the Model and
# Annotation tabs. Same pieces (`_ogs_rows`, `FRowVM`, the row template, the
# pending-by-signature rule), three differences:
#   - the strip is ONLY the Visible/Hidden state (no Enable, no x);
#   - rows are read lazily (`_CROWS`, see `_queue_rows`); until they arrive an
#     open cell shows a one-line "Loading...";
#   - a 4th row state, "na": Cut lines / Cut pattern of a category that cannot
#     be cut (`Category.IsCuttable`, read with the rows). Greyed, no hover, and
#     the click handler ignores it.
# Model cells fold per ROW (`CatNode.ovr_open`, accordion) between a summary
# (the tags of `_CACHE`, no row read) and the 7 rows; Annotation cells are
# always open with 2 rows.
# --------------------------------------------------------------------------
_ANNO_ROWS = tuple(r for r in FILTER_ROWS if r[0] in ("Proj", "Half"))
_ANNO_KEYS = frozenset(r[0] for r in _ANNO_ROWS)
_NA_ROWS = frozenset(("CutL", "CutP"))
_NA_TEXT = u"n/a"
_NA_TIP = u"This category can't be cut"
_TITLE_SHORT = tuple((r[1], r[2]) for r in FILTER_ROWS)


def _short_tag(tag):
    """`vgrow.read_tags` names ("Transparency 40%", "Detail level: Fine") in the
    short form the Filters summary uses."""
    for title, short in _TITLE_SHORT:
        if tag.startswith(title):
            return short
    return tag


def _store_cat_rows(tpl, node, ogs):
    """Fill `_CROWS` for one pair from an OGS already read (Annotation, which
    gets its rows in the same pass as the state, with no second getter call)."""
    try:
        _prime_pattern_names(tpl.vt.Document)
    except Exception:
        pass
    _CROWS[(tpl.id_val, node.id_val)] = _ogs_rows(ogs, _ANNO_KEYS if node.annot else None)


def _read_cat_rows(tpl, node):
    """Read one pair's mini-table rows and its category's `IsCuttable`. MUST run
    in an API context (patterns, `GetCategoryOverrides`); always leaves an entry
    in `_CROWS` (`{}` on failure), so a cell can never wait for it forever."""
    key = (tpl.id_val, node.id_val)
    try:
        _prime_pattern_names(tpl.vt.Document)
    except Exception:
        pass
    if not node.annot and node.id_val not in _CUTTABLE:
        try:
            _CUTTABLE[node.id_val] = bool(node.cat.IsCuttable)
        except Exception:
            _CUTTABLE[node.id_val] = True
    try:
        _CROWS[key] = _ogs_rows(tpl.vt.GetCategoryOverrides(node.eid),
                                _ANNO_KEYS if node.annot else None)
    except Exception:
        _CROWS[key] = {}


def _read_cell(tpl, node):
    """`_read_state` for one pair. An Annotation node also fills `_CROWS` from
    the same OGS read (its table is always open)."""
    sink = None
    if node.annot:
        sink = lambda ogs: _store_cat_rows(tpl, node, ogs)
    return _read_state(tpl.vt, node.eid, sink)


def _make_rows_queue(run):
    """The cell's "I need my rows" hook. `run(work)` queues `work` for an API
    context (modeless.run). The only state is `todo`, the pairs still waiting:
    every ask queues a job, and a job that never executes (another document is
    active, a failed Raise: modeless.py skips it) leaves its pairs in `todo`,
    so the next refresh simply asks again; a job with nothing left is a no-op.
    A persistent "already asked" flag would strand the cell on "Loading..."
    forever after one lost job. Returns (ask, todo)."""
    todo = []

    def work(uiapp):
        pending = list(todo)
        if not pending:
            return
        try:
            for t, n in pending:
                _read_cat_rows(t, n)      # always leaves an entry in _CROWS
        finally:
            # Whatever was not reached stays queued; whatever was read leaves
            # and its cells repaint (one failing cell never blocks the rest).
            todo[:] = [p for p in todo if (p[0].id_val, p[1].id_val) not in _CROWS]
            for t, n in pending:
                if (t.id_val, n.id_val) not in _CROWS:
                    continue
                k = u"t{}".format(t.id_val)
                try:
                    if n.Cells.ContainsKey(k):
                        n.Cells[k].refresh()
                except Exception:
                    pass

    def ask(tpl, node):
        if (tpl.id_val, node.id_val) in _CROWS:
            return
        for t, n in todo:
            if t is tpl and n is node:
                break
        else:
            todo.append((tpl, node))
        run(work)

    return ask, todo


def _fullscreen_vis(window_state):
    """The Full screen invitation is for a window that is not maximised."""
    return GONE if window_state == WindowState.Maximized else VIS


def _wire_fullscreen(win, btn):
    """Wire the "Full screen" button: a click maximises the window (slantisui's
    StateChanged handler already fits it to the work area), and the button
    shows only while the window is not maximised, however it got maximised
    (this button, the title bar, a double click, Win+Up). Returns the sync
    function; it runs once now and on every `StateChanged`."""
    def sync(s=None, e=None):
        btn.Visibility = _fullscreen_vis(win.WindowState)

    def go(s, e):
        win.WindowState = WindowState.Maximized

    btn.Click += go
    win.StateChanged += sync
    sync()
    return sync


def _discard_staged(tpl, nodes, filter_rows):
    """A template was unticked: drop what was staged for it AND repaint its
    cells, or re-ticking it brings back "-> Hidden" / PENDING ghosts with
    Apply at 0. A no-op when nothing was staged for it."""
    keys = [k for k in _PENDING if k[0] == tpl.id_val]
    fkeys = [k for k in _FPENDING if k[0] == tpl.id_val]
    for k in keys:
        del _PENDING[k]
    for k in fkeys:
        del _FPENDING[k]
    if not keys and not fkeys:
        return
    ck = u"t{}".format(tpl.id_val)
    for n in list(nodes) + list(filter_rows):
        try:
            if n.Cells.ContainsKey(ck):
                n.Cells[ck].refresh()
        except Exception:
            pass


def _release_open_row(tab, keep):
    """The accordion's open row left the grid (its parent was folded, or a
    search/discipline filter hides it): close it, so nothing stays "open"
    where nobody sees it and the next open does not have to close a ghost."""
    if tab == u"Annotation":
        return
    row = _ACC["Filters" if tab == u"Filters" else "Model"].open
    if row is None or row in keep:
        return
    if isinstance(row, FilterRow):
        row._expanded = False
    else:
        row.ovr_open = False


def _cat_pending_desc(pend):
    states = []
    if pend.vis is not None:
        states.append(u"set to " + (u"Visible" if pend.vis else u"Hidden"))
    ovr = u"reset" if pend.reset else (u"changed" if pend.edit is not None else None)
    return _pending_sentence(states, ovr)


class CatCellVM(INotifyPropertyChanged):
    """One matrix cell of the Model / Annotation tabs: this template's state for
    one category, from `_CACHE`/`_CROWS` overlaid with any staged `_PENDING`.
    Template: strip (`Badge`, `Eye*Vis`) + either the mini table (`MiniVis`,
    `<Key>Row`), a "Loading..." line (`LoadVis`) or, on a collapsed Model row, a
    one-line summary (`Summary*`); `FootVis` is the "Hide" link of an open one.
    Neither the row view-models nor the rows' visual tree exist until a cell is
    opened (the first are created on demand, the second is a lazy template, see
    `_mini_res`): a Model grid has hundreds of rows x N templates of cells and
    most are never opened."""

    def __init__(self, tpl, node):
        self.tpl = tpl
        self.node = node
        self.Badge = u"\u2026"
        self.BadgeBrush = GREY
        self.EyeOpenVis = GONE
        self.EyeShutVis = GONE
        self.Tip = u""
        self.MiniVis = GONE
        self.LoadVis = GONE
        self.FootVis = GONE
        self.SummaryVis = GONE
        self.SummaryText = u""
        self.SummaryPend = GONE
        self._pend_sig = None     # what was staged at the last refresh (auto-expand)
        self._staged = (None, None)   # (edit object, its rows): `_ogs_rows` once per edit
        self._rows = {}
        self._pc_handlers = []

    def add_PropertyChanged(self, value):
        self._pc_handlers.append(value)

    def remove_PropertyChanged(self, value):
        if value in self._pc_handlers:
            self._pc_handlers.remove(value)

    def _raise(self, *props):
        for p in props:
            args = PropertyChangedEventArgs(p)
            for h in list(self._pc_handlers):
                h(self, args)

    def _put(self, **vals):
        changed = []
        for name, value in vals.items():
            if getattr(self, name) != value:
                setattr(self, name, value)
                changed.append(name)
        if changed:
            self._raise(*changed)

    def _row(self, rkey):
        r = self._rows.get(rkey)
        if r is None:
            r = self._rows[rkey] = FRowVM(_ROW_DEFAULT[rkey])
        return r

    def _fill_rows(self, key, pend, annot, cuttable):
        """The rows: current (model) vs staged, field by field. Only a row whose
        fields really differ is pending (the staged OGS is absolute and drags
        the untouched rows too). A row that cannot apply is "na", never pending."""
        model_rows = _CROWS.get(key)
        cur_known = bool(model_rows)
        cur = model_rows if cur_known else _blank_rows()
        staged = None
        if pend is not None and cur_known:
            if pend.reset:
                staged = _blank_rows()
            elif pend.edit is not None:
                if self._staged[0] is not pend.edit:
                    self._staged = (pend.edit,
                                    _ogs_rows(pend.edit, _ANNO_KEYS if annot else None))
                staged = self._staged[1]
        for rkey, _title, _short, default, _kind in (_ANNO_ROWS if annot else FILTER_ROWS):
            rowvm = self._row(rkey)
            if not annot and not cuttable and rkey in _NA_ROWS:
                rowvm.set(u"na", _NA_TEXT, u"", None, _NA_TIP)
                continue
            c = cur.get(rkey) or _STATIC_BLANK[rkey]
            s = staged.get(rkey) if staged is not None else None
            if (s is not None and s[3] is not None and c[3] is not None
                    and s[3] != c[3]):
                before = (c[1] if c[0] else default) + u" \u2192"
                after = s[1] if s[0] else default
                rowvm.set(u"pending", after, before, s[2] if s[0] else None,
                          before + u" " + after)
            else:
                rowvm.set(u"active" if c[0] else u"inactive", c[1], u"",
                          c[2] if c[0] else None,
                          c[1] if c[0] else u"Click to add an override")

    def refresh(self):
        node = self.node
        key = (self.tpl.id_val, node.id_val)
        entry = _CACHE.get(key)
        none = dict(MiniVis=GONE, LoadVis=GONE, FootVis=GONE, SummaryVis=GONE,
                    SummaryPend=GONE, EyeOpenVis=GONE, EyeShutVis=GONE)
        if entry is None:
            # Not read yet: just the "..." placeholder.
            none.update(Badge=u"\u2026", BadgeBrush=GREY, Tip=u"")
            self._put(**none)
            return
        controllable, hidden, tags = entry
        if not controllable:
            none.update(Badge=u"N/A", BadgeBrush=GREY,
                        Tip=u"This template does not control this category.")
            self._put(**none)
            self._pend_sig = None
            return

        pend = _PENDING.get(key)
        # -- strip: Visible / Hidden, with the staged toggle overlaid --
        if hidden:
            badge, brush, eye_open = u"Hidden", RED, False
            tip = u"Hidden in this template. Click to show it."
        else:
            badge, brush, eye_open = u"Visible", GREEN, True
            tip = u"Visible in this template. Click to hide it."
        if pend is not None:
            if pend.vis is not None:
                badge = u"\u2192 Visible" if pend.vis else u"\u2192 Hidden"
                brush = GREEN if pend.vis else RED
                eye_open = bool(pend.vis)
            desc = _cat_pending_desc(pend)
            if desc:
                tip = desc

        annot = node.annot
        expanded = bool(annot or node._ovr)
        loading = False
        if expanded:
            if key in _CROWS:
                self._fill_rows(key, pend, annot, _CUTTABLE.get(node.id_val, True))
            else:
                loading = True
        names, spend = None, False
        if not expanded:
            # Collapsed (Model): summary from what the state read already has,
            # or from the staged OGS (a handful of getters, only on a pending
            # cell). No `_ogs_rows`, no model read.
            if pend is not None and pend.reset:
                names, spend = [], True
            elif pend is not None and pend.edit is not None:
                names, spend = vgrow.read_tags(pend.edit), True
            else:
                names = tags
        self._put(Badge=badge, BadgeBrush=brush, Tip=tip,
                  EyeOpenVis=VIS if eye_open else GONE,
                  EyeShutVis=GONE if eye_open else VIS,
                  MiniVis=VIS if (expanded and not loading) else GONE,
                  LoadVis=VIS if loading else GONE,
                  FootVis=VIS if (expanded and not annot) else GONE,
                  SummaryVis=VIS if not expanded else GONE,
                  SummaryText=(u", ".join([_short_tag(t) for t in names])
                               if names else u"No overrides") if not expanded else u"",
                  SummaryPend=VIS if spend else GONE)

        if loading and _ROWS_REQ["fn"] is not None:
            _ROWS_REQ["fn"](self.tpl, node)

        # A Model row opens ITSELF when an override is newly staged on it (a
        # visibility toggle shows on the strip already, so it does not count),
        # so a pending change is never hidden behind a collapsed row. "New" =
        # differs from the previous refresh; a refresh that finds the same
        # staging leaves the row as the user left it, and a pending that goes
        # away never folds anything.
        sig = None
        if pend is not None and (pend.reset or pend.edit is not None):
            sig = (pend.reset, id(pend.edit) if pend.edit is not None else None)
        prev = self._pend_sig
        self._pend_sig = sig
        if (sig is not None and sig != prev and _AUTO["on"]
                and not annot and not node._ovr):
            node.ovr_open = True       # accordion + refreshes every cell of the row


def _row_prop(rkey):
    return property(lambda self: self._row(rkey))


for _rk in _ROW_DEFAULT:
    setattr(CatCellVM, _rk + "Row", _row_prop(_rk))


# --------------------------------------------------------------------------
# Left picker, 2 levels: VTypeNode (a ViewType, e.g. FloorPlan) groups
# TplRow (a view template of that type). Step 4b dropped the third level
# (TplCatRow, one line per in-scope category nested under an expanded
# template): a template's tick no longer scopes an editable row inside
# itself, it makes the template a COLUMN of the matrix on the right instead.
# VTypeNode still aggregates over its children's `Checked` (a plain bool on
# TplRow) into a tri-state and cascades a tick onto them -- restricted to
# `_shown`, same mechanic as before -- because it IS a grouping, not an
# override target of its own, same distinction CatNode's docstring draws for
# the category tree.
# --------------------------------------------------------------------------
class VTypeNode(INotifyPropertyChanged):
    """Root of the left picker: groups TplRow by ViewType. Unlike CatNode
    (independent ticks, no cascade -- see the note above CatNode), a
    ViewType node IS a grouping, not an override target of its own, so it
    still aggregates over its children's `Checked` (a plain bool on TplRow)
    into a tri-state and cascades a tick onto them -- restricted to `_shown`,
    the subset currently passing the picker's filters (see Checked below).
    No parent to bubble up to."""

    def __init__(self, name):
        self.Name = name
        self.children = []          # TplRow instances of this ViewType
        self._expanded = True       # types start EXPANDED, or the window opens empty
        # What `_tpl_visible_rows()` last computed as this type's matching
        # templates under the search box + "On sheets only" + print-set
        # filters -- None means "no filter narrowed this type, use every
        # child". Set every rebind by `_rebind_tpl()`; read by Checked/
        # Count below so All/None-by-type respects the filter.
        self._shown = None
        self._pc_handlers = []
        self.Indent = INDENT_VTYPE
        self.ScopeVis = VIS
        # Type rows read as headings (feedback of 09-18: "a bit bolder and a
        # bit bigger"); templates keep the body
        # weight/size.
        self.NameWeight = FontWeights.SemiBold
        self.NameSize = 13.5

    def add_PropertyChanged(self, value):
        self._pc_handlers.append(value)

    def remove_PropertyChanged(self, value):
        if value in self._pc_handlers:
            self._pc_handlers.remove(value)

    def _raise(self, *props):
        for p in props:
            args = PropertyChangedEventArgs(p)
            for h in list(self._pc_handlers):
                h(self, args)

    @property
    def Chevron(self):
        return (CHEV_OPEN if self._expanded else CHEV_SHUT) if self.children else u""

    def toggle(self):
        if not self.children:
            return
        self._expanded = not self._expanded
        self._raise("Chevron")

    @property
    def Count(self):
        if self._shown is None or len(self._shown) == len(self.children):
            return u"{} templates".format(len(self.children))
        return u"{} of {} templates".format(len(self._shown), len(self.children))

    @property
    def Checked(self):
        src = self.children if self._shown is None else self._shown
        if not src:
            return False
        first = src[0].Checked
        for t in src:
            if t.Checked != first:
                return None
        return first

    @Checked.setter
    def Checked(self, value):
        # A click on a dash resolves to True (IsThreeState="False" in the
        # XAML), same as CatNode -- but cascades only onto `_shown` (the
        # templates currently passing the search/On-sheets/print-set
        # filters), not every template of this type. "Tick every FloorPlan
        # template that is on a sheet of print set X" is the whole point of
        # the filter (2026-09-18); ticking a filtered-out template would
        # silently widen the scope past what is visible on screen.
        v = bool(value)
        src = self.children if self._shown is None else self._shown
        for t in src:
            if t._checked != v:
                t._checked = v
                t._raise("Checked")
        self._raise("Checked")


def _build_view_type_tree(tpl_rows):
    """Group already-built TplRow objects by ViewType into VTypeNode roots,
    alphabetically by type name (FloorPlan, CeilingPlan, Section...).
    Templates keep their existing state (`_checked`) -- this only reshapes
    how they are grouped for display."""
    groups = {}
    for t in tpl_rows:
        groups.setdefault(t.ViewType, []).append(t)
    roots = []
    for vtype_name in sorted(groups.keys()):
        node = VTypeNode(vtype_name)
        for t in groups[vtype_name]:
            t._parent_vtype = node
            node.children.append(t)
        roots.append(node)
    return roots


class TplRow(INotifyPropertyChanged):
    """One view template -- a LEAF of the left picker (step 4b): no
    chevron, no toggle, nothing to expand any more. `_checked` (whether this
    template is ticked, i.e. a matrix column right now) and the usage
    numbers (`n_views`/`n_on_sheets`/`sheet_ids`, filled once by
    `_apply_usage()`) are its only state, and both survive a tab switch --
    template selection and usage do not depend on which tab is active."""

    def __init__(self, vt):
        self.vt = vt
        self.eid = vt.Id
        self.id_val = _id_val(vt.Id)
        self.Template = vt.Name
        self.ViewType = str(vt.ViewType)
        self.Indent = INDENT_TPL
        self.ScopeVis = VIS
        self.NameWeight = FontWeights.Normal
        self.NameSize = 12.5
        # Usage against sheets ("On sheets only" filter, 2026-09-18): filled
        # once by `_apply_usage()` right after `_collect_templates()` in
        # `open_window()`, never touched again -- a template's placement on
        # sheets does not change while the window is open.
        self.n_views = 0
        self.n_on_sheets = 0
        self.sheet_ids = set()
        # Tick: starts OFF, same as categories -- request of 2026-09-17: "for
        # me they should all be unselected by default and get ticked as you
        # make changes". STEP 4b: ticking a
        # template is what makes it a matrix COLUMN, not what scopes an
        # editable row inside itself any more.
        self._checked = False
        self._parent_vtype = None     # set by _build_view_type_tree; lets this
                                       # row's own tick bubble up so its
                                       # ViewType group's dash/tick redraws
        self._pc_handlers = []

    def add_PropertyChanged(self, value):
        self._pc_handlers.append(value)

    def remove_PropertyChanged(self, value):
        if value in self._pc_handlers:
            self._pc_handlers.remove(value)

    def _raise(self, *props):
        for p in props:
            args = PropertyChangedEventArgs(p)
            for h in list(self._pc_handlers):
                h(self, args)

    @property
    def Name(self):
        return self.Template

    @property
    def Count(self):
        # "unused" beats "0 views" -- a template nobody applies is a
        # different situation than one whose views just aren't on sheets.
        if self.n_views == 0:
            return u"unused"
        if self.n_on_sheets:
            return u"{} views · {} on sheets".format(self.n_views, self.n_on_sheets)
        return u"{} views".format(self.n_views)

    @property
    def Checked(self):
        return self._checked

    @Checked.setter
    def Checked(self, value):
        v = bool(value)
        if v == self._checked:
            return
        self._checked = v
        self._raise("Checked")
        if self._parent_vtype is not None:
            self._parent_vtype._raise("Checked")


def _collect_templates(doc):
    unsupported = _unsupported_view_types()
    views = DB.FilteredElementCollector(doc).OfClass(DB.View).ToElements()
    tpls = [v for v in views if v.IsTemplate and v.ViewType not in unsupported]
    tpls.sort(key=lambda v: v.Name.lower())
    return [TplRow(v) for v in tpls]


def _collect_usage(doc):
    """Views/sheets/print-sets data for the "On sheets only" filter
    (2026-09-18), computed once in `open_window()` right after
    `_collect_templates()`. Returns (views_by_tpl, sheets_by_view,
    sheets_by_set, set_names). Each collector is wrapped on its own so one
    odd document (a corrupt ViewSheetSet, a view that refuses
    ViewTemplateId) never blocks the window from opening -- it just leaves
    that piece empty."""
    views_by_tpl = {}
    try:
        for v in DB.FilteredElementCollector(doc).OfClass(DB.View):
            try:
                if v.IsTemplate:
                    continue
                tid = v.ViewTemplateId
                if tid == DB.ElementId.InvalidElementId:
                    continue
                views_by_tpl.setdefault(_id_val(tid), set()).add(_id_val(v.Id))
            except Exception:
                continue
    except Exception:
        pass

    sheets_by_view = {}
    try:
        for vp in DB.FilteredElementCollector(doc).OfClass(DB.Viewport):
            try:
                sheets_by_view.setdefault(_id_val(vp.ViewId), set()).add(_id_val(vp.SheetId))
            except Exception:
                continue
    except Exception:
        pass

    sheets_by_set = {}
    try:
        # Same recipe as lib/printsetmanager.py (get_print_sets /
        # get_set_contents): a ViewSheetSet's
        # Views can hold non-sheet entries, so filter by isinstance.
        for vss in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheetSet):
            try:
                name = vss.Name
            except Exception:
                continue
            ids = set()
            try:
                members = list(vss.Views)
            except Exception:
                members = []
            for v in members:
                # Per-member guard, same as the two collectors above: one
                # odd entry must not leave this set half-read.
                try:
                    if isinstance(v, DB.ViewSheet):
                        ids.add(_id_val(v.Id))
                except Exception:
                    continue
            sheets_by_set[name] = ids
    except Exception:
        pass

    set_names = sorted(sheets_by_set.keys(), key=lambda n: n.lower())
    return views_by_tpl, sheets_by_view, sheets_by_set, set_names


def _apply_usage(tpl_rows, views_by_tpl, sheets_by_view):
    """Fill each TplRow's n_views/n_on_sheets/sheet_ids from `_collect_usage`'s
    output. Runs once, right after both collectors -- these numbers do not
    change while the window is open."""
    for t in tpl_rows:
        view_ids = views_by_tpl.get(t.id_val, set())
        t.n_views = len(view_ids)
        n_on = 0
        sheet_ids = set()
        for vid in view_ids:
            vs = sheets_by_view.get(vid)
            if vs:
                n_on += 1
                sheet_ids |= vs
        t.n_on_sheets = n_on
        t.sheet_ids = sheet_ids


def _read_state(vt, cat_eid, on_ogs=None):
    """Return (controllable, hidden_or_None, tags) for this (template,
    category) pair -- same recipe as an older category tool's `_read_state`:
    CanCategoryBeHidden is the real gate (GetCategoryHidden can succeed on a
    category the view still refuses to hide). `on_ogs(ogs)`, when given, gets
    the OGS this read already fetched (Annotation builds its rows from it)."""
    try:
        if not vt.CanCategoryBeHidden(cat_eid):
            return (False, None, [])
        hidden = vt.GetCategoryHidden(cat_eid)
    except Exception:
        return (False, None, [])
    tags = []
    ogs = None
    try:
        ogs = vt.GetCategoryOverrides(cat_eid)
        tags = vgrow.read_tags(ogs)
    except Exception:
        tags = []
    if ogs is not None and on_ogs is not None:
        try:
            on_ogs(ogs)
        except Exception:
            pass     # the cell asks for its rows by itself (`_queue_rows`)
    return (True, hidden, tags)


def _read_filters_for_template(tpl, filter_rows):
    """Populate `_FCACHE` for EVERY `FilterRow` against this one ticked
    template, in a single `GetFilters()` read -- step 5's "read per
    TEMPLATE, not per cell" rule. A filter absent from `GetFilters()` still
    gets an entry (`applied=False`), so a later pass never re-reads this
    template unless it is unticked and reticked (there is no reason to:
    `_FPENDING`/Apply is the only thing that can change what is applied)."""
    vt = tpl.vt
    # v5: the mini table names patterns from a cache that can only be filled
    # here, inside the API context this read runs in.
    try:
        _prime_pattern_names(vt.Document)
    except Exception:
        pass
    try:
        # Compared by `.id_val`, not raw ElementId equality/hash, to match
        # every other cache key in this module (see `_id_val`'s gotcha note).
        applied_ids = set(_id_val(fid) for fid in vt.GetFilters())
    except Exception:
        applied_ids = set()

    order_map = {}
    try:
        # ASSUMPTION, not verified live (no Revit access in this step):
        # `View.GetOrderedFilters()` may not exist on every installed
        # Revit version -- see the module docstring, step 5 paragraph.
        for i, fid in enumerate(vt.GetOrderedFilters()):
            order_map[_id_val(fid)] = i
        _FORDER_TOTAL[tpl.id_val] = len(order_map)
    except Exception:
        order_map = {}

    for row in filter_rows:
        if row.id_val not in applied_ids:
            _FCACHE[(tpl.id_val, row.id_val)] = (False, None, None, [], None)
            _FROWS.pop((tpl.id_val, row.id_val), None)
            continue
        try:
            visible = vt.GetFilterVisibility(row.eid)
        except Exception:
            visible = None
        try:
            enabled = vt.GetIsFilterEnabled(row.eid)
        except Exception:
            enabled = None
        tags = []
        try:
            ogs = vt.GetFilterOverrides(row.eid)
            tags = vgrow.read_tags(ogs)
            _FROWS[(tpl.id_val, row.id_val)] = _ogs_rows(ogs)
        except Exception:
            _FROWS.pop((tpl.id_val, row.id_val), None)
        order = order_map.get(row.id_val)
        _FCACHE[(tpl.id_val, row.id_val)] = (True, visible, enabled, tags, order)


def _ensure_cells(tpls, nodes):
    """Create any missing cell VM for every (node, tpl) pair and refresh the
    ones just created (a cell that already exists is kept current by the
    reads, the clicks and Apply, so re-refreshing all of them on every tick
    was pure churn) -- called before a row or a column reaches the grid
    (ticking a template, switching tabs), so the very first `Cells[key]`
    bind always finds a live object. The class depends on the row: a
    `FilterRow` gets a `FilterCellVM`, everything else (a `CatNode`) gets
    a `CatCellVM`. Never reads the model itself: a freshly-created
    cell just shows "…" until `_schedule_reads`'s `work(uiapp)` fills
    `_CACHE`/`_FCACHE` and refreshes it again."""
    for node in nodes:
        cell_cls = FilterCellVM if isinstance(node, FilterRow) else CatCellVM
        for tpl in tpls:
            key = u"t{}".format(tpl.id_val)
            if not node.Cells.ContainsKey(key):
                cell = cell_cls(tpl, node)
                node.Cells[key] = cell
                cell.refresh()


def _refresh_cells(tpls, nodes):
    """Refresh the cell VMs that already exist for (node, tpl) -- used
    after a write (bulk action, badge click, Apply), when no new pair can
    have appeared. Silently skips a pair with no cell yet (its template is
    not ticked, or its category is not in the active tab)."""
    for node in nodes:
        for tpl in tpls:
            key = u"t{}".format(tpl.id_val)
            if node.Cells.ContainsKey(key):
                node.Cells[key].refresh()


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------
_BODY = u"""
  <Grid>
    <!-- No band on top: the Model/Annotation/Filters tabs sit in the matrix
         header, because they choose the ROWS of the matrix (feedback, 09-18:
         "we are not using the whole height of the window"). -->
    <Grid>
      <!-- Left = everything about templates, right = everything about
           categories; the gutter is a GridSplitter so the user sets the
           split with the cursor (feedback, 09-18). -->
      <Grid.ColumnDefinitions>
        <ColumnDefinition x:Name="colLeft" Width="420" MinWidth="260"/>
        <ColumnDefinition Width="14"/>
        <ColumnDefinition Width="*" MinWidth="420"/>
      </Grid.ColumnDefinitions>
      <GridSplitter x:Name="splitter" Grid.Column="1" Width="4" HorizontalAlignment="Center"
                    VerticalAlignment="Stretch" Background="#ECE9E4"
                    ResizeBehavior="PreviousAndNext" ResizeDirection="Columns"
                    Cursor="SizeWE" Focusable="False"/>
      <!-- Collapse tab on the gutter: hides the template pane and shows it
           again, like an IDE sidebar (feedback, 09-18: "it is like pushing it
           to the left, hiding a panel"). Lives on the gutter, not in the
           matrix header, because it belongs to the panel it hides. -->
      <!-- Feedback (2026-10-01): the arrow has to invite. Accent glyph in bold on
           a warm fill with an accent border; the hover is the stronger warm tone
           (ramp hexes only, so the theme pass retones them). 18 x 56 overflows
           the 14 px gutter by 2 px each side (ZIndex 1 keeps it on top); the
           splitter underneath is untouched. `on_focus` swaps the glyph only. -->
      <Button x:Name="btnFocus" Grid.Column="1" Content="◂" Width="18" Height="56"
              MinWidth="0" MinHeight="0" Padding="0" FontSize="18" FontWeight="Bold"
              Foreground="#FF7700" Cursor="Hand"
              VerticalAlignment="Center" HorizontalAlignment="Center" Panel.ZIndex="1"
              ToolTip="Hide the view templates">
        <Button.Template>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" Background="#21FF7700" BorderBrush="#FF7700"
                    BorderThickness="1" CornerRadius="6">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"
                                Margin="0,-2,0,0"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#FFD9B3"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Button.Template>
      </Button>

      <!-- LEFT: template picker (step 4b). VTypeNode > TplRow: ticking a
           template makes it a column of the matrix on the right. -->
      <Grid Grid.Column="0">
        <Grid.RowDefinitions>
          <RowDefinition Height="Auto"/>
          <RowDefinition Height="*"/>
        </Grid.RowDefinitions>
        <Grid Grid.Row="0" Margin="0,0,0,8">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="Auto"/>
            <ColumnDefinition Width="Auto"/>
          </Grid.ColumnDefinitions>
          <TextBox x:Name="txtTplFilter" Grid.Column="0" Height="30"
                   ToolTip="Type to filter view templates"/>
          <CheckBox x:Name="chkOnSheets" Grid.Column="1" Content="On sheets only"
                    VerticalAlignment="Center" Margin="10,0,8,0"/>
          <ComboBox x:Name="cmbPrintSet" Grid.Column="2" Width="150" Height="30"
                    IsEnabled="False" ToolTip="Only sheets in this print set"/>
        </Grid>
        <Border Grid.Row="1" CornerRadius="8" Background="#FFFFFF"
                BorderBrush="#ECE9E4" BorderThickness="1" ClipToBounds="True">
          <Grid>
            <Grid.RowDefinitions>
              <RowDefinition Height="*"/>
              <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>
          <DataGrid x:Name="gridTpl" Grid.Row="0" AutoGenerateColumns="False" HeadersVisibility="None"
                    SelectionMode="Single" CanUserAddRows="False" CanUserDeleteRows="False"
                    CanUserSortColumns="False" CanUserResizeRows="False"
                    CanUserResizeColumns="False">
            <DataGrid.Resources>
              <!-- Same as the Model grid: a selected row must read the same with or
                   without focus. WPF swaps the selection to SystemColors.InactiveSelectionHighlight(Text)Brush
                   as soon as focus lands on something else in the window (a
                   tick box in the row is enough), and those default to the
                   OS light grey with its own ink: a white row with light text
                   in the dark theme (QA, 2026-10-06). Both pairs (focused and
                   not) are the wash and ink of slantisui's DataGridRow
                   selection, so the row cannot change look. Scoped fix; the
                   master of slantisui lives in Design System. -->
              <SolidColorBrush x:Key="{x:Static SystemColors.InactiveSelectionHighlightBrushKey}"     Color="#21FF7700"/>
              <SolidColorBrush x:Key="{x:Static SystemColors.InactiveSelectionHighlightTextBrushKey}" Color="#202022"/>
              <SolidColorBrush x:Key="{x:Static SystemColors.HighlightBrushKey}"                      Color="#21FF7700"/>
              <SolidColorBrush x:Key="{x:Static SystemColors.HighlightTextBrushKey}"                  Color="#202022"/>
            </DataGrid.Resources>
            <DataGrid.Columns>
              <!-- 72, not 46: the cell style pads 12 px each side and the
                   chevron takes 18, so at 46 the 15 px check box had 4 px
                   left and showed as an orange sliver (feedback, 09-17). -->
              <DataGridTemplateColumn Width="72">
                <DataGridTemplateColumn.CellTemplate>
                  <DataTemplate>
                    <Grid>
                      <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="18"/>
                        <ColumnDefinition Width="*"/>
                      </Grid.ColumnDefinitions>
                      <TextBlock Grid.Column="0" Tag="chev" Text="{Binding Chevron}"
                                 FontSize="11" Foreground="#77736C" Cursor="Hand"
                                 VerticalAlignment="Center"/>
                      <CheckBox Grid.Column="1" IsThreeState="False"
                                Visibility="{Binding ScopeVis}"
                                IsChecked="{Binding Checked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                                VerticalAlignment="Center"/>
                    </Grid>
                  </DataTemplate>
                </DataGridTemplateColumn.CellTemplate>
              </DataGridTemplateColumn>
              <DataGridTemplateColumn Width="*">
                <DataGridTemplateColumn.CellTemplate>
                  <DataTemplate>
                    <Grid Margin="{Binding Indent}">
                      <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                      </Grid.ColumnDefinitions>
                      <TextBlock Grid.Column="0" Text="{Binding Name}"
                                 FontSize="{Binding NameSize}" FontWeight="{Binding NameWeight}"
                                 Foreground="#202022" TextTrimming="CharacterEllipsis"
                                 VerticalAlignment="Center"/>
                      <TextBlock Grid.Column="1" Text="{Binding Count}" FontSize="11"
                                 Foreground="#A6A199" Margin="8,0,0,0"
                                 VerticalAlignment="Center"/>
                    </Grid>
                  </DataTemplate>
                </DataGridTemplateColumn.CellTemplate>
              </DataGridTemplateColumn>
            </DataGrid.Columns>
          </DataGrid>
            <!-- Tree bar: the browser's own buttons (fold, All / None), part
                 of the card and not of the tool's header (feedback, 09-18). -->
            <Border Grid.Row="1" Background="#F7F5F2" BorderBrush="#ECE9E4"
                    BorderThickness="0,1,0,0" Padding="8,5">
              <StackPanel Orientation="Horizontal">
                <Button x:Name="btnTplAll" Content="Clear selection" Style="{StaticResource BtnGhost}"
                        Height="24" MinHeight="0" Padding="9,0" FontSize="11" Margin="0,0,6,0"
                        ToolTip="Untick every view template (every column goes away)"/>
                <Button x:Name="btnTplFold" Content="Expand all" Style="{StaticResource BtnGhost}"
                        Height="24" MinHeight="0" Padding="9,0" FontSize="11" Margin="0,0,6,0"
                        ToolTip="Expand or collapse every view type"/>
              </StackPanel>
            </Border>
          </Grid>
        </Border>
      </Grid>

      <!-- RIGHT: the matrix. Rows = category tree of the active tab, fixed
           columns 0/1 below, one DataGridTemplateColumn per ticked template
           appended in code by _rebuild_columns(). FrozenColumnCount keeps
           the fixed columns in place while the templates scroll sideways. -->
      <Grid Grid.Column="2">
        <Grid.RowDefinitions>
          <RowDefinition Height="Auto"/>
          <RowDefinition Height="*"/>
        </Grid.RowDefinitions>
        <!-- Folio tabs (v5, 2026-10-01): the three tabs sit on their own row
             as real tabs, the active one open towards the card below, the
             other two flat. They hang 1 px over the card's top border (the
             -1 margin and the ZIndex), which is what lets the active one
             erase that line under itself. Height 39 = the 30 px search box
             plus its 8 px margin on the left, so both cards start together.
             The styles live here; `_activate_tab` swaps them. -->
        <StackPanel Grid.Row="0" Orientation="Horizontal" Margin="0,0,0,-1" Panel.ZIndex="1">
          <StackPanel.Resources>
            <Style x:Key="FolioTabOff" TargetType="Button">
              <Setter Property="Foreground" Value="#77736C"/>
              <Setter Property="FontSize" Value="12.5"/>
              <Setter Property="FontWeight" Value="SemiBold"/>
              <Setter Property="Cursor" Value="Hand"/>
              <Setter Property="Margin" Value="0,0,4,0"/>
              <Setter Property="Template">
                <Setter.Value>
                  <ControlTemplate TargetType="Button">
                    <Border x:Name="bd" Background="#EFEDE9" BorderBrush="#ECE9E4"
                            BorderThickness="1" CornerRadius="8,8,0,0" Height="39"
                            Padding="20,0">
                      <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                    </Border>
                    <ControlTemplate.Triggers>
                      <Trigger Property="IsMouseOver" Value="True">
                        <Setter TargetName="bd" Property="Background" Value="#F7F5F2"/>
                        <Setter Property="Foreground" Value="#202022"/>
                      </Trigger>
                    </ControlTemplate.Triggers>
                  </ControlTemplate>
                </Setter.Value>
              </Setter>
            </Style>
            <Style x:Key="FolioTabOn" TargetType="Button">
              <Setter Property="Foreground" Value="#202022"/>
              <Setter Property="FontSize" Value="12.5"/>
              <Setter Property="FontWeight" Value="Bold"/>
              <Setter Property="Cursor" Value="Arrow"/>
              <Setter Property="Margin" Value="0,0,4,0"/>
              <Setter Property="Template">
                <Setter.Value>
                  <ControlTemplate TargetType="Button">
                    <Border Background="#FFFFFF" BorderBrush="#ECE9E4"
                            BorderThickness="1,1,1,0" CornerRadius="8,8,0,0" Height="39"
                            Padding="20,0">
                      <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                    </Border>
                  </ControlTemplate>
                </Setter.Value>
              </Setter>
            </Style>
          </StackPanel.Resources>
          <Button x:Name="btnTabModel" Content="Model" Style="{StaticResource FolioTabOn}"/>
          <Button x:Name="btnTabAnno" Content="Annotation" Style="{StaticResource FolioTabOff}"/>
          <Button x:Name="btnTabFilters" Content="Filters" Style="{StaticResource FolioTabOff}"/>
        </StackPanel>
        <!-- Invitation to use the whole screen (feedback, 2026-10-01): text in
             the accent, warm on hover, NEVER solid (Apply is the only solid
             orange). Shown only while the window is not maximised; see
             `_wire_fullscreen`. -->
        <Button x:Name="btnFullScreen" Grid.Row="0" HorizontalAlignment="Right"
                VerticalAlignment="Center" Margin="0,0,6,4" Cursor="Hand"
                Foreground="#FF7700" FontSize="12" FontWeight="SemiBold"
                ToolTip="Use the whole screen to see more templates side by side">
          <Button.Template>
            <ControlTemplate TargetType="Button">
              <Border x:Name="bd" Background="Transparent" CornerRadius="6" Padding="10,5">
                <StackPanel Orientation="Horizontal">
                  <TextBlock FontFamily="Segoe MDL2 Assets" Text="&#xE740;" FontSize="12"
                             VerticalAlignment="Center" Margin="0,0,7,0"/>
                  <TextBlock Text="Full screen" VerticalAlignment="Center"/>
                </StackPanel>
              </Border>
              <ControlTemplate.Triggers>
                <Trigger Property="IsMouseOver" Value="True">
                  <Setter TargetName="bd" Property="Background" Value="#21FF7700"/>
                </Trigger>
              </ControlTemplate.Triggers>
            </ControlTemplate>
          </Button.Template>
        </Button>
        <Border Grid.Row="1" CornerRadius="0,8,8,8" Background="#FFFFFF"
                BorderBrush="#ECE9E4" BorderThickness="1" Padding="10">
         <Grid>
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
          </Grid.RowDefinitions>
          <!-- Search + discipline live inside the folio card, under the tab. -->
          <Grid Grid.Row="0" Margin="0,0,0,8">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="*"/>
              <ColumnDefinition Width="Auto"/>
            </Grid.ColumnDefinitions>
            <TextBox x:Name="txtCatFilter" Grid.Column="0" Height="30"
                     ToolTip="Type to filter categories"/>
            <ComboBox x:Name="cmbDiscipline" Grid.Column="1" Width="128" Height="30"
                      Margin="8,0,0,0"
                      ToolTip="Show only the categories of one discipline, like V/G's Filter list"/>
          </Grid>
        <Border Grid.Row="1" CornerRadius="8" Background="#FFFFFF"
                BorderBrush="#ECE9E4" BorderThickness="1" ClipToBounds="True">
          <!-- FrozenColumnCount is set in code (open_window), not here:
               WPF coerces it against Columns.Count at the moment it is
               SET, and a XAML attribute is applied before the nested
               DataGrid.Columns children are parsed, so it would silently
               clamp to 0 every time. -->
          <Grid>
            <Grid.RowDefinitions>
              <RowDefinition Height="*"/>
              <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>
          <DataGrid x:Name="gridCat" Grid.Row="0" AutoGenerateColumns="False" HeadersVisibility="Column"
                    EnableColumnVirtualization="True"
                    SelectionMode="Single" CanUserAddRows="False" CanUserDeleteRows="False"
                    CanUserSortColumns="False" CanUserResizeRows="False"
                    CanUserResizeColumns="False"
                    ScrollViewer.HorizontalScrollBarVisibility="Auto">
            <DataGrid.Resources>
              <!-- A selected row must read the same with or without focus. WPF swaps
                   the selection to SystemColors.InactiveSelectionHighlight(Text)Brush
                   as soon as focus lands on something else in the window (a
                   tick box in the row is enough), and those default to the
                   OS light grey with its own ink: a white row with light text
                   in the dark theme (QA, 2026-10-06). Both pairs (focused and
                   not) are the wash and ink of slantisui's DataGridRow
                   selection, so the row cannot change look. Scoped fix; the
                   master of slantisui lives in Design System. -->
              <SolidColorBrush x:Key="{x:Static SystemColors.InactiveSelectionHighlightBrushKey}"     Color="#21FF7700"/>
              <SolidColorBrush x:Key="{x:Static SystemColors.InactiveSelectionHighlightTextBrushKey}" Color="#202022"/>
              <SolidColorBrush x:Key="{x:Static SystemColors.HighlightBrushKey}"                      Color="#21FF7700"/>
              <SolidColorBrush x:Key="{x:Static SystemColors.HighlightTextBrushKey}"                  Color="#202022"/>
              <!-- slantisui's ScrollBar style sets Width=8 for EVERY bar, so
                   the horizontal one renders 8 px wide, i.e. invisible
                   (feedback, 09-18: "there is no horizontal slider"). Scoped
                   fix; the library bug is tracked separately. -->
              <Style TargetType="ScrollBar" BasedOn="{StaticResource {x:Type ScrollBar}}">
                <Style.Triggers>
                  <Trigger Property="Orientation" Value="Horizontal">
                    <Setter Property="Width" Value="Auto"/>
                    <Setter Property="Height" Value="10"/>
                  </Trigger>
                </Style.Triggers>
              </Style>
              <!-- Cells of this grid (v5): slantisui's cell centres its content
                   in the row. A Filters row is tall (the mini table), so the
                   fixed columns (chevron, tick, name) hang from the top
                   when the row says so (`RowAlign`), and stay centred for
                   the one-line Model/Annotation rows. -->
              <Style TargetType="DataGridCell" BasedOn="{StaticResource {x:Type DataGridCell}}">
                <Setter Property="VerticalContentAlignment" Value="Center"/>
                <Setter Property="Template">
                  <Setter.Value>
                    <ControlTemplate TargetType="DataGridCell">
                      <Border Padding="{TemplateBinding Padding}" Background="Transparent">
                        <ContentPresenter VerticalAlignment="{TemplateBinding VerticalContentAlignment}"/>
                      </Border>
                    </ControlTemplate>
                  </Setter.Value>
                </Setter>
                <Style.Triggers>
                  <DataTrigger Binding="{Binding RowAlign}" Value="Top">
                    <Setter Property="VerticalContentAlignment" Value="Top"/>
                  </DataTrigger>
                </Style.Triggers>
              </Style>
              <!-- The name cell of a filter row (Filters tab only, set per tab by
                   `_activate_tab`): the whole cell is the expand/collapse
                   target, with a hand cursor and a warm hover so it reads as
                   clickable. Standalone (not BasedOn) so its template is the
                   only one in play. -->
              <Style x:Key="FilterNameCell" TargetType="DataGridCell">
                <Setter Property="Foreground" Value="#202022"/>
                <Setter Property="FontSize" Value="13"/>
                <Setter Property="BorderThickness" Value="0"/>
                <Setter Property="Padding" Value="12,5"/>
                <Setter Property="Cursor" Value="Hand"/>
                <Setter Property="Template">
                  <Setter.Value>
                    <ControlTemplate TargetType="DataGridCell">
                      <Border x:Name="bd" Padding="{TemplateBinding Padding}" Background="Transparent">
                        <ContentPresenter VerticalAlignment="Top"/>
                      </Border>
                      <ControlTemplate.Triggers>
                        <Trigger Property="IsMouseOver" Value="True">
                          <Setter TargetName="bd" Property="Background" Value="#21FF7700"/>
                        </Trigger>
                      </ControlTemplate.Triggers>
                    </ControlTemplate>
                  </Setter.Value>
                </Setter>
              </Style>
              <!-- The Filters matrix columns: no padding (the mini table runs
                   edge to edge, like the mockup) and the content stretched to
                   the row. Applied per column by `_rebuild_columns`. -->
              <Style x:Key="FilterMatrixCell" TargetType="DataGridCell">
                <Setter Property="Foreground" Value="#202022"/>
                <Setter Property="FontSize" Value="13"/>
                <Setter Property="BorderThickness" Value="0"/>
                <Setter Property="Padding" Value="0"/>
                <Setter Property="Template">
                  <Setter.Value>
                    <ControlTemplate TargetType="DataGridCell">
                      <Border Padding="{TemplateBinding Padding}" Background="Transparent">
                        <ContentPresenter VerticalAlignment="Stretch"/>
                      </Border>
                    </ControlTemplate>
                  </Setter.Value>
                </Setter>
              </Style>
            </DataGrid.Resources>
            <DataGrid.Columns>
              <DataGridTemplateColumn Header="" Width="72">
                <DataGridTemplateColumn.CellTemplate>
                  <DataTemplate>
                    <Grid>
                      <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="28"/>
                        <ColumnDefinition Width="*"/>
                      </Grid.ColumnDefinitions>
                      <TextBlock Grid.Column="0" Tag="chev" Text="{Binding Chevron}"
                                 FontSize="{Binding ChevSize}" Foreground="{Binding ChevBrush}"
                                 Cursor="Hand" VerticalAlignment="Center"/>
                      <CheckBox Grid.Column="1" IsThreeState="False"
                                Visibility="{Binding ScopeVis}"
                                IsChecked="{Binding Checked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                                VerticalAlignment="Center"/>
                    </Grid>
                  </DataTemplate>
                </DataGridTemplateColumn.CellTemplate>
              </DataGridTemplateColumn>
              <DataGridTemplateColumn Header="Category" Width="160">
                <DataGridTemplateColumn.CellTemplate>
                  <DataTemplate>
                    <Grid Margin="{Binding Indent}">
                      <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                      </Grid.ColumnDefinitions>
                      <TextBlock Grid.Column="0" Text="{Binding Name}" FontSize="12.5"
                                 Foreground="#202022" TextTrimming="CharacterEllipsis"
                                 VerticalAlignment="Center" ToolTip="{Binding Name}"/>
                      <TextBlock Grid.Column="1" Text="{Binding Count}" FontSize="11"
                                 Foreground="#A6A199" Margin="8,0,0,0"
                                 VerticalAlignment="Center"/>
                    </Grid>
                  </DataTemplate>
                </DataGridTemplateColumn.CellTemplate>
              </DataGridTemplateColumn>
              <!-- One DataGridTemplateColumn per ticked template is appended
                   here in code by _rebuild_columns(), see _cat_cell_template_for,
                   _filter_cell_template_for and _tpl_header. -->
            </DataGrid.Columns>
          </DataGrid>
            <!-- Tree bar: the browser's own buttons (fold, All / None), part
                 of the card and not of the tool's header (feedback, 09-18). -->
            <Border Grid.Row="1" Background="#F7F5F2" BorderBrush="#ECE9E4"
                    BorderThickness="0,1,0,0" Padding="8,5">
              <StackPanel Orientation="Horizontal">
                <Button x:Name="btnCatAll" Content="All / None" Style="{StaticResource BtnGhost}"
                        Height="24" MinHeight="0" Padding="9,0" FontSize="11" Margin="0,0,6,0"
                        ToolTip="Tick or untick every category row shown by the filters"/>
                <Button x:Name="btnCatFold" Content="Expand all" Style="{StaticResource BtnGhost}"
                        Height="24" MinHeight="0" Padding="9,0" FontSize="11" Margin="0,0,6,0"
                        ToolTip="Expand or collapse every category with subcategories"/>
                <!-- Re-reads the model (QA 2026-10-02): the window also follows
                     Revit by itself, this is the manual way out when it did not. -->
                <Button x:Name="btnRefresh" Content="⟳  Refresh" Style="{StaticResource BtnGhost}"
                        Height="24" MinHeight="0" Padding="9,0" FontSize="11" Margin="0,0,6,0"
                        ToolTip="Re-reads the values of the templates, filters and categories listed here (picks up Undo and edits made in Revit). Staged changes are kept. A template or filter created or deleted while this window is open needs closing and reopening it."/>
                <!-- Bulk actions on the ticked rows x the ticked templates: one
                     button, one grouped menu (feedback, 09-19: eight footer
                     buttons hid that they all act on the same thing). Ghost
                     while nothing is ticked, orange (BtnPrimary, swapped in
                     _update_footer) once a row is. The Filter group shows on
                     the Filters tab only (_activate_tab). The items keep the
                     names the handlers were wired to. The menu is templated
                     here because slantisui styles no menu and the WPF default
                     looks like Windows. -->
                <Button x:Name="btnRows" Content="Selected rows  ▾" Style="{StaticResource BtnGhost}"
                        Height="24" MinHeight="0" Padding="9,0" FontSize="11" Margin="0,0,6,0"
                        ToolTip="Actions on the ticked rows, in every ticked template">
                  <Button.Resources>
                    <Style x:Key="MenuCard" TargetType="ContextMenu">
                      <Setter Property="Background" Value="Transparent"/>
                      <Setter Property="HasDropShadow" Value="False"/>
                      <Setter Property="FontSize" Value="12.5"/>
                      <Setter Property="Template">
                        <Setter.Value>
                          <ControlTemplate TargetType="ContextMenu">
                            <Border Background="#FFFFFF" BorderBrush="#D8D4CD" BorderThickness="1"
                                    CornerRadius="8" Padding="6" Margin="0,0,10,10">
                              <Border.Effect>
                                <DropShadowEffect BlurRadius="12" ShadowDepth="2" Opacity="0.14"/>
                              </Border.Effect>
                              <StackPanel IsItemsHost="True"/>
                            </Border>
                          </ControlTemplate>
                        </Setter.Value>
                      </Setter>
                    </Style>
                    <Style x:Key="MenuAction" TargetType="MenuItem">
                      <Setter Property="Foreground" Value="#202022"/>
                      <Setter Property="Padding" Value="12,6"/>
                      <Setter Property="MinWidth" Value="200"/>
                      <Setter Property="Cursor" Value="Hand"/>
                      <Setter Property="Template">
                        <Setter.Value>
                          <ControlTemplate TargetType="MenuItem">
                            <Border x:Name="bd" Background="Transparent" CornerRadius="6"
                                    Padding="{TemplateBinding Padding}">
                              <ContentPresenter ContentSource="Header" VerticalAlignment="Center"
                                                RecognizesAccessKey="True"/>
                            </Border>
                            <ControlTemplate.Triggers>
                              <Trigger Property="IsHighlighted" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="#FFF1E5"/>
                              </Trigger>
                              <Trigger Property="IsEnabled" Value="False">
                                <Setter Property="Foreground" Value="#B8B3AB"/>
                              </Trigger>
                            </ControlTemplate.Triggers>
                          </ControlTemplate>
                        </Setter.Value>
                      </Setter>
                    </Style>
                    <Style x:Key="MenuGroup" TargetType="MenuItem" BasedOn="{StaticResource MenuAction}">
                      <Setter Property="IsEnabled" Value="False"/>
                      <Setter Property="Padding" Value="12,6,12,2"/>
                      <Setter Property="FontSize" Value="10.5"/>
                      <Setter Property="Cursor" Value="Arrow"/>
                    </Style>
                    <Style x:Key="MenuSep" TargetType="Separator">
                      <Setter Property="Template">
                        <Setter.Value>
                          <ControlTemplate TargetType="Separator">
                            <Rectangle Height="1" Fill="#ECE9E4" Margin="6,4"/>
                          </ControlTemplate>
                        </Setter.Value>
                      </Setter>
                    </Style>
                  </Button.Resources>
                  <Button.ContextMenu>
                    <ContextMenu x:Name="mnuRows" Style="{StaticResource MenuCard}">
                      <MenuItem Header="Visibility" Style="{StaticResource MenuGroup}"/>
                      <MenuItem x:Name="btnShowAll" Header="Show" Style="{StaticResource MenuAction}"/>
                      <MenuItem x:Name="btnHideAll" Header="Hide" Style="{StaticResource MenuAction}"/>
                      <Separator Style="{StaticResource MenuSep}"/>
                      <MenuItem Header="Overrides" Style="{StaticResource MenuGroup}"/>
                      <MenuItem x:Name="btnOverride" Header="Edit..." Style="{StaticResource MenuAction}"
                                ToolTip="Set graphic overrides on every selected row × ticked template"/>
                      <MenuItem x:Name="btnResetOvr" Header="Reset" Style="{StaticResource MenuAction}"
                                ToolTip="Clear graphic overrides (visibility is kept)"/>
                      <MenuItem x:Name="btnFCopy" Header="Copy from another filter..." Visibility="Collapsed"
                                Style="{StaticResource MenuAction}"
                                ToolTip="Copy one filter's overrides and visibility onto the selected rows"/>
                      <Separator x:Name="sepFilter" Style="{StaticResource MenuSep}" Visibility="Collapsed"/>
                      <MenuItem x:Name="grpFilter" Header="Filter" Style="{StaticResource MenuGroup}"
                                Visibility="Collapsed"/>
                      <MenuItem x:Name="btnFEnable" Header="Enable" Style="{StaticResource MenuAction}"
                                Visibility="Collapsed"/>
                      <MenuItem x:Name="btnFDisable" Header="Disable" Style="{StaticResource MenuAction}"
                                Visibility="Collapsed"/>
                      <MenuItem x:Name="btnFRemove" Header="Remove from templates" Style="{StaticResource MenuAction}"
                                Visibility="Collapsed"
                                ToolTip="Detach the filter from every ticked template"/>
                    </ContextMenu>
                  </Button.ContextMenu>
                </Button>
              </StackPanel>
            </Border>
          </Grid>
        </Border>
         </Grid>
        </Border>
      </Grid>
    </Grid>
  </Grid>
"""

# One DataTemplate per matrix column, parsed once per ticked template (a
# DataGridTemplateColumn needs one concrete CellTemplate object, and the
# indexer key has to be baked into the {Binding} path text before parsing --
# there is no way to parameterise a WPF binding path at runtime otherwise).
# "TPLKEY" is replaced with "t{tpl.id_val}" by `_filter_cell_template_for` and
# `_cat_cell_template_for` below. (The old two-chip category cell and its
# template went away with the category mini table.)


# --------------------------------------------------------------------------
# The Filters cell (v5). Parsed per ticked template exactly like the one
# above, but it is a DIFFERENT template: strip (Visible/Hidden + Enable) on
# top, then 7 fixed rows (or a one-line summary when the filter row is
# collapsed), or a "+ Add to template" cell when the filter is not applied.
# "TPLKEY" is the indexer key ("t123"); "ROWKEY"/"TITLE"/"SWATCH" are filled
# per row by `_filter_row_xaml`. The three row looks are DataTriggers on the
# row's `State` (inactive / active / pending); the pending one adds a left bar
# that pulses. Hexes are the light /slantis canon and go through ui._theme()
# below, because a template parsed on its own never sees the window's pass.
# Click anywhere on a row: Tag="row:<key>", the same editor for all seven.
# --------------------------------------------------------------------------
_FILTER_RES = u"""
  <DataTemplate.Resources>
    <Style x:Key="FRowBox" TargetType="Border">
      <Setter Property="Background" Value="Transparent"/>
      <Setter Property="BorderBrush" Value="#F0EEE9"/>
      <Setter Property="BorderThickness" Value="0,0,0,1"/>
      <Setter Property="MinHeight" Value="23"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Style.Triggers>
        <DataTrigger Binding="{Binding State}" Value="active">
          <Setter Property="Background" Value="#FFD9B3"/>
        </DataTrigger>
        <DataTrigger Binding="{Binding State}" Value="pending">
          <Setter Property="Background" Value="#21FF7700"/>
        </DataTrigger>
        <!-- na: a row that cannot apply (Cut lines / Cut pattern of a
             category that cannot be cut). Fainter, arrow cursor, and no hover
             (the hover trigger below only matches inactive). -->
        <DataTrigger Binding="{Binding State}" Value="na">
          <Setter Property="Opacity" Value="0.55"/>
          <Setter Property="Cursor" Value="Arrow"/>
        </DataTrigger>
        <MultiDataTrigger>
          <MultiDataTrigger.Conditions>
            <Condition Binding="{Binding State}" Value="inactive"/>
            <Condition Binding="{Binding RelativeSource={RelativeSource Self}, Path=IsMouseOver}" Value="True"/>
          </MultiDataTrigger.Conditions>
          <Setter Property="Background" Value="#0DFF7700"/>
        </MultiDataTrigger>
      </Style.Triggers>
    </Style>
    <Style x:Key="FRowBar" TargetType="Border">
      <Setter Property="Width" Value="4"/>
      <Setter Property="HorizontalAlignment" Value="Left"/>
      <Setter Property="Background" Value="#FF7700"/>
      <Setter Property="Visibility" Value="Collapsed"/>
      <Style.Triggers>
        <DataTrigger Binding="{Binding State}" Value="pending">
          <Setter Property="Visibility" Value="Visible"/>
          <DataTrigger.EnterActions>
            <BeginStoryboard Name="pulse">
              <Storyboard Timeline.DesiredFrameRate="12">
                <DoubleAnimation Storyboard.TargetProperty="Opacity" From="1" To="0.3"
                                 Duration="0:0:1.1" AutoReverse="True"
                                 RepeatBehavior="Forever"/>
              </Storyboard>
            </BeginStoryboard>
          </DataTrigger.EnterActions>
          <DataTrigger.ExitActions>
            <RemoveStoryboard BeginStoryboardName="pulse"/>
          </DataTrigger.ExitActions>
        </DataTrigger>
      </Style.Triggers>
    </Style>
    <Style x:Key="FRowLabel" TargetType="TextBlock">
      <Setter Property="FontSize" Value="10.5"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Foreground" Value="#A6A199"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
      <Style.Triggers>
        <DataTrigger Binding="{Binding State}" Value="active">
          <Setter Property="Foreground" Value="#C46A00"/>
        </DataTrigger>
        <DataTrigger Binding="{Binding State}" Value="pending">
          <Setter Property="Foreground" Value="#202022"/>
          <Setter Property="FontWeight" Value="Bold"/>
        </DataTrigger>
        <DataTrigger Binding="{Binding State}" Value="na">
          <Setter Property="Foreground" Value="#B8B3AB"/>
        </DataTrigger>
      </Style.Triggers>
    </Style>
    <Style x:Key="FRowVal" TargetType="TextBlock">
      <Setter Property="FontSize" Value="10.5"/>
      <Setter Property="Foreground" Value="#A6A199"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
      <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
      <Setter Property="Text" Value="{Binding Val}"/>
      <Style.Triggers>
        <DataTrigger Binding="{Binding State}" Value="active">
          <Setter Property="Foreground" Value="#202022"/>
          <Setter Property="FontWeight" Value="SemiBold"/>
        </DataTrigger>
        <DataTrigger Binding="{Binding State}" Value="pending">
          <Setter Property="Foreground" Value="#202022"/>
          <Setter Property="FontWeight" Value="Bold"/>
        </DataTrigger>
        <DataTrigger Binding="{Binding State}" Value="na">
          <Setter Property="Foreground" Value="#B8B3AB"/>
        </DataTrigger>
        <MultiDataTrigger>
          <MultiDataTrigger.Conditions>
            <Condition Binding="{Binding State}" Value="inactive"/>
            <Condition Binding="{Binding RelativeSource={RelativeSource FindAncestor, AncestorType={x:Type Border}}, Path=IsMouseOver}" Value="True"/>
          </MultiDataTrigger.Conditions>
          <Setter Property="Text" Value="Add override..."/>
          <Setter Property="Foreground" Value="#FF7700"/>
          <Setter Property="FontWeight" Value="SemiBold"/>
        </MultiDataTrigger>
      </Style.Triggers>
    </Style>
    <Style x:Key="FFold" TargetType="Border">
      <Setter Property="Background" Value="Transparent"/>
      <Setter Property="TextElement.Foreground" Value="#FF7700"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Style.Triggers>
        <Trigger Property="IsMouseOver" Value="True">
          <Setter Property="Background" Value="#21FF7700"/>
        </Trigger>
      </Style.Triggers>
    </Style>
    <Style x:Key="FRm" TargetType="Border">
      <Setter Property="Background" Value="Transparent"/>
      <Setter Property="TextElement.Foreground" Value="#77736C"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Style.Triggers>
        <DataTrigger Binding="{Binding Cells[TPLKEY].RmOn}" Value="True">
          <Setter Property="TextElement.Foreground" Value="#C0392B"/>
        </DataTrigger>
        <Trigger Property="IsMouseOver" Value="True">
          <Setter Property="Background" Value="#0DFF7700"/>
          <Setter Property="TextElement.Foreground" Value="#C46A00"/>
        </Trigger>
      </Style.Triggers>
    </Style>
    <Style x:Key="FAddCell" TargetType="Border">
      <Setter Property="Background" Value="Transparent"/>
      <Setter Property="TextElement.Foreground" Value="#A6A199"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Style.Triggers>
        <Trigger Property="IsMouseOver" Value="True">
          <Setter Property="Background" Value="#0DFF7700"/>
          <Setter Property="TextElement.Foreground" Value="#FF7700"/>
        </Trigger>
      </Style.Triggers>
    </Style>
  </DataTemplate.Resources>
"""

_FILTER_ROW_XAML = u"""
        <Border Tag="row:ROWKEY" Style="{StaticResource FRowBox}"
                DataContext="{Binding Cells[TPLKEY].ROWKEYRow}"
                ToolTip="{Binding Tip}">
          <Grid>
            <Border Style="{StaticResource FRowBar}"/>
            <Grid Margin="8,3,8,3">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="*"/>
              </Grid.ColumnDefinitions>
              <TextBlock Grid.Column="0" Text="TITLE" Style="{StaticResource FRowLabel}"/>
              <!-- Right side: [before ->] [swatch] value. The pending row is
                   already marked by the pulsing bar, the warm fill and the bold
                   text, so there is no PENDING chip here (it would fight the
                   values for 240 px); the collapsed summary keeps its chip.
                   The "before" gives way first (max 56 px, ellipsis) and the
                   value column never goes below 44 px. -->
              <Grid Grid.Column="1" Margin="8,0,0,0" HorizontalAlignment="Right">
                <Grid.ColumnDefinitions>
                  <ColumnDefinition Width="Auto"/>
                  <ColumnDefinition Width="Auto"/>
                  <ColumnDefinition Width="*" MinWidth="44"/>
                </Grid.ColumnDefinitions>
                <TextBlock Grid.Column="0" Text="{Binding Before}" Visibility="{Binding BeforeVis}"
                           FontSize="10.5" Foreground="#A6A199" VerticalAlignment="Center"
                           TextTrimming="CharacterEllipsis" MaxWidth="56" Margin="0,0,5,0"/>
                <Grid Grid.Column="1" VerticalAlignment="Center">SWATCH</Grid>
                <TextBlock Grid.Column="2" Style="{StaticResource FRowVal}"
                           HorizontalAlignment="Right"/>
              </Grid>
            </Grid>
          </Grid>
        </Border>
"""

_FILTER_SWATCH = {
    "line": u"""<Border Visibility="{Binding SwVis}" Background="{Binding Sw}" Width="14"
                        Height="4" CornerRadius="2" Margin="0,0,5,0" VerticalAlignment="Center"/>""",
    "box": u"""<Border Visibility="{Binding SwVis}" Background="{Binding Sw}" Width="11"
                       Height="11" CornerRadius="2" BorderBrush="#D8D4CD" BorderThickness="1"
                       Margin="0,0,5,0" VerticalAlignment="Center"/>""",
}


def _is_row_tag(tag):
    """True for the Tag of a mini-table row ("row:Proj") or of the collapsed
    summary ("row:all")."""
    return isinstance(tag, basestring) and tag.startswith(u"row:")


# Mini-table row -> the section of the override editor it belongs to, so the
# editor opens with the section the user came in through highlighted. "all"
# (the collapsed summary) has no entry: nothing to highlight.
_FOCUS_SECTION = {
    u"Proj": u"lines", u"CutL": u"lines",
    u"Surf": u"surface", u"CutP": u"cut",
    u"Trans": u"general", u"Half": u"general", u"Det": u"general",
}


def _rows_xaml(rows):
    out = []
    for rkey, title, _short, _default, kind in rows:
        out.append(_FILTER_ROW_XAML.replace(u"ROWKEY", rkey).replace(u"TITLE", title)
                   .replace(u"SWATCH", _FILTER_SWATCH.get(kind, u"")))
    return u"".join(out)


def _res_with(extra):
    """`_FILTER_RES` plus more resources (they go last, so they can refer to
    the styles above them)."""
    end = u"  </DataTemplate.Resources>"
    assert _FILTER_RES.count(end) == 1
    return _FILTER_RES.replace(end, extra + end)


def _mini_res(rows):
    """The mini table as a LAZY template: the cell holds a `ContentControl`
    with no content, and a trigger on `MiniVis` gives it the rows only while the
    table is open. The 7 rows are ~100 bindings; a collapsed cell (the common
    state of a Model grid with hundreds of rows x N templates) must not build
    them. The rows bind through `Cells[TPLKEY]...` from the row item, so the
    content is the item itself (`{Binding}`)."""
    return (u"""
    <DataTemplate x:Key="MiniRows">
      <StackPanel Orientation="Vertical">
""" + _rows_xaml(rows) + u"""
      </StackPanel>
    </DataTemplate>
    <Style x:Key="MiniHost" TargetType="ContentControl">
      <Style.Triggers>
        <DataTrigger Binding="{Binding Cells[TPLKEY].MiniVis}" Value="Visible">
          <Setter Property="ContentTemplate" Value="{StaticResource MiniRows}"/>
          <Setter Property="Content" Value="{Binding}"/>
        </DataTrigger>
      </Style.Triggers>
    </Style>
""")


_FILTER_CELL_TEMPLATE_XAML = u"""
<DataTemplate xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
              xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">
""" + _res_with(_mini_res(FILTER_ROWS)) + u"""
  <StackPanel Orientation="Vertical" SnapsToDevicePixels="True">

    <!-- Not applied to this template (and nothing staged to attach it) -->
    <Border Tag="badge" Style="{StaticResource FAddCell}" MinHeight="36"
            Visibility="{Binding Cells[TPLKEY].AddVis}"
            ToolTip="{Binding Cells[TPLKEY].Tip}">
      <TextBlock Text="{Binding Cells[TPLKEY].AddText}" FontSize="11" FontWeight="SemiBold"
                 HorizontalAlignment="Center" VerticalAlignment="Center"/>
    </Border>

    <!-- Strip: Visible/Hidden on the left (click toggles), Enable on the right -->
    <Grid Margin="8,5,8,4" Visibility="{Binding Cells[TPLKEY].StripVis}">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="Auto"/>
        <ColumnDefinition Width="Auto"/>
      </Grid.ColumnDefinitions>
      <Border Grid.Column="0" Tag="badge" Cursor="Hand" Background="Transparent"
              HorizontalAlignment="Left" Padding="0,2,8,2"
              ToolTip="{Binding Cells[TPLKEY].Tip}">
        <StackPanel Orientation="Horizontal">
          <Border Visibility="{Binding Cells[TPLKEY].EyeOpenVis}" Width="14" Height="9"
                  CornerRadius="5" BorderThickness="2"
                  BorderBrush="{Binding Cells[TPLKEY].BadgeBrush}"
                  VerticalAlignment="Center" Margin="0,0,6,0">
            <Ellipse Width="3" Height="3" Fill="{Binding Cells[TPLKEY].BadgeBrush}"
                     HorizontalAlignment="Center" VerticalAlignment="Center"/>
          </Border>
          <Rectangle Visibility="{Binding Cells[TPLKEY].EyeShutVis}" Width="14" Height="2"
                     RadiusX="1" RadiusY="1" Fill="{Binding Cells[TPLKEY].BadgeBrush}"
                     VerticalAlignment="Center" Margin="0,0,6,0"/>
          <TextBlock Text="{Binding Cells[TPLKEY].Badge}"
                     Foreground="{Binding Cells[TPLKEY].BadgeBrush}"
                     FontSize="10.5" FontWeight="SemiBold" VerticalAlignment="Center"/>
        </StackPanel>
      </Border>
      <Border Grid.Column="1" Tag="on" Cursor="Hand" Background="Transparent"
              Padding="8,2,0,2" Visibility="{Binding Cells[TPLKEY].OnVis}"
              ToolTip="Click to enable or disable this filter in this template">
        <StackPanel Orientation="Horizontal">
          <Ellipse Width="7" Height="7" Fill="{Binding Cells[TPLKEY].OnBrush}"
                   VerticalAlignment="Center" Margin="0,0,5,0"/>
          <TextBlock Text="{Binding Cells[TPLKEY].OnText}"
                     Foreground="{Binding Cells[TPLKEY].OnTextBrush}"
                     FontSize="10.5" VerticalAlignment="Center"/>
        </StackPanel>
      </Border>
      <!-- Remove from THIS template: always visible in dim grey (a hover-only
           control gets lost), warn colour on hover, red while a remove is staged. -->
      <Border Grid.Column="2" Tag="rm" Style="{StaticResource FRm}" Margin="4,0,0,0"
              Padding="6,0" CornerRadius="4" Visibility="{Binding Cells[TPLKEY].RmVis}"
              ToolTip="{Binding Cells[TPLKEY].RmTip}">
        <TextBlock Text="&#215;" FontSize="18" FontWeight="SemiBold"
                   VerticalAlignment="Center" Margin="0,-2,0,0"/>
      </Border>
    </Grid>

    <!-- Expanded: the 7 fixed rows (their visual tree only exists while open) -->
    <Border BorderBrush="#ECE9E4" BorderThickness="0,1,0,0"
            Visibility="{Binding Cells[TPLKEY].MiniVis}">
      <ContentControl Style="{StaticResource MiniHost}"/>
    </Border>

    <!-- Collapsed: one line with the overrides that are on, and under it an
         explicit way to open the filter (Tag="fold", handled by
         `on_cat_grid_click`); the chevron alone was getting lost. -->
    <StackPanel Orientation="Vertical" Visibility="{Binding Cells[TPLKEY].SummaryVis}">
      <Border Tag="row:all" Cursor="Hand" Background="Transparent" Padding="8,0,8,4"
              ToolTip="{Binding Cells[TPLKEY].SummaryText}">
        <StackPanel Orientation="Horizontal">
          <TextBlock Text="{Binding Cells[TPLKEY].SummaryText}" FontSize="10.5"
                     Foreground="#77736C" MaxWidth="150" TextTrimming="CharacterEllipsis"
                     VerticalAlignment="Center"/>
          <Border Visibility="{Binding Cells[TPLKEY].SummaryPend}" Background="#FF7700"
                  CornerRadius="8" Padding="5,1" Margin="7,0,0,0" VerticalAlignment="Center">
            <TextBlock Text="PENDING" FontSize="7.5" FontWeight="ExtraBold"
                       Foreground="__ON_ACCENT__"/>
          </Border>
        </StackPanel>
      </Border>
      <Border Tag="fold" Style="{StaticResource FFold}" Padding="8,3,8,8"
              ToolTip="Open this filter: show its 7 override rows">
        <TextBlock Text="Show 7 overrides  &#9662;" FontSize="10.5" FontWeight="SemiBold"/>
      </Border>
    </StackPanel>

  </StackPanel>
</DataTemplate>
"""

# A template parsed on its own never sees the window's theme pass, so run it
# here (same translation `ui.parse` applies to the window's own XAML).
try:
    _FILTER_CELL_TEMPLATE_XAML = ui._theme(_FILTER_CELL_TEMPLATE_XAML)
except Exception:
    # A slantisui copy without `_theme`: light canon as written, with the
    # ink-on-accent placeholder resolved to white so the XAML still parses.
    _FILTER_CELL_TEMPLATE_XAML = _FILTER_CELL_TEMPLATE_XAML.replace(u"__ON_ACCENT__", u"#FFFFFF")


def _filter_cell_template_for(key):
    """Parse one instance of the Filters cell with `key` ("t123") baked into
    every `Cells[...]` binding path -- the Filters twin of `_cat_cell_template_for`."""
    return XamlReader.Parse(_FILTER_CELL_TEMPLATE_XAML.replace(u"TPLKEY", key))


# --------------------------------------------------------------------------
# The category cell, Model and Annotation. Same row template and the
# same styles as the Filters cell (`_FILTER_RES`, plus the "Loading..." style
# below), a different frame: the strip is ONLY the Visible/Hidden state (no
# Enable, no x), and
#   Model       collapsed: summary + "Show 7 overrides"; open: the 7 rows +
#               "Hide" at the foot (Tag="fold" on both, per ROW of the grid,
#               not the subcategory chevron);
#   Annotation  always the 2 rows (Projection lines, Halftone), no folding.
# "Loading..." is a one-line placeholder for a cell whose rows are being read;
# it fades in after 250 ms, so a read that lands in a few ms never flashes it.
# --------------------------------------------------------------------------
_CAT_STYLES = u"""
    <Style x:Key="CLoad" TargetType="TextBlock">
      <Setter Property="Opacity" Value="0"/>
      <Style.Triggers>
        <Trigger Property="IsVisible" Value="True">
          <Trigger.EnterActions>
            <BeginStoryboard Name="ld">
              <Storyboard>
                <DoubleAnimation Storyboard.TargetProperty="Opacity" From="0" To="1"
                                 BeginTime="0:0:0.25" Duration="0:0:0.1"/>
              </Storyboard>
            </BeginStoryboard>
          </Trigger.EnterActions>
          <Trigger.ExitActions>
            <RemoveStoryboard BeginStoryboardName="ld"/>
          </Trigger.ExitActions>
        </Trigger>
      </Style.Triggers>
    </Style>
"""
_CAT_RES = _res_with(_CAT_STYLES)
_MODEL_RES = _res_with(_CAT_STYLES + _mini_res(FILTER_ROWS))

_CAT_STRIP = u"""
    <!-- Strip: Visible / Hidden / N/A (click toggles the staged visibility) -->
    <Grid Margin="8,5,8,4">
      <Border Tag="badge" Cursor="Hand" Background="Transparent"
              HorizontalAlignment="Left" Padding="0,2,8,2"
              ToolTip="{Binding Cells[TPLKEY].Tip}">
        <StackPanel Orientation="Horizontal">
          <Border Visibility="{Binding Cells[TPLKEY].EyeOpenVis}" Width="14" Height="9"
                  CornerRadius="5" BorderThickness="2"
                  BorderBrush="{Binding Cells[TPLKEY].BadgeBrush}"
                  VerticalAlignment="Center" Margin="0,0,6,0">
            <Ellipse Width="3" Height="3" Fill="{Binding Cells[TPLKEY].BadgeBrush}"
                     HorizontalAlignment="Center" VerticalAlignment="Center"/>
          </Border>
          <Rectangle Visibility="{Binding Cells[TPLKEY].EyeShutVis}" Width="14" Height="2"
                     RadiusX="1" RadiusY="1" Fill="{Binding Cells[TPLKEY].BadgeBrush}"
                     VerticalAlignment="Center" Margin="0,0,6,0"/>
          <TextBlock Text="{Binding Cells[TPLKEY].Badge}"
                     Foreground="{Binding Cells[TPLKEY].BadgeBrush}"
                     FontSize="10.5" FontWeight="SemiBold" VerticalAlignment="Center"/>
        </StackPanel>
      </Border>
    </Grid>
"""

_CAT_LOADING = u"""
    <TextBlock Text="Loading&#8230;" FontSize="10.5" Foreground="#A6A199" Margin="8,5,8,5"
               Style="{StaticResource CLoad}" Visibility="{Binding Cells[TPLKEY].LoadVis}"/>
"""

def _cat_head(res):
    return (u"""
<DataTemplate xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
              xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">
""" + res + u"""
  <StackPanel Orientation="Vertical" SnapsToDevicePixels="True">
""" + _CAT_STRIP + _CAT_LOADING)


_MODEL_CELL_TEMPLATE_XAML = _cat_head(_MODEL_RES) + u"""
    <!-- Open: the 7 fixed rows (built only while open), then a way back -->
    <Border BorderBrush="#ECE9E4" BorderThickness="0,1,0,0"
            Visibility="{Binding Cells[TPLKEY].MiniVis}">
      <ContentControl Style="{StaticResource MiniHost}"/>
    </Border>
    <Border Tag="fold" Style="{StaticResource FFold}" Padding="8,3,8,6"
            Visibility="{Binding Cells[TPLKEY].FootVis}"
            ToolTip="Hide the 7 override rows">
      <TextBlock Text="Hide  &#9652;" FontSize="10.5" FontWeight="SemiBold"/>
    </Border>

    <!-- Collapsed: one line with the overrides that are on, and under it the
         explicit way to open the row (Tag="fold", handled by `on_cat_grid_click`). -->
    <StackPanel Orientation="Vertical" Visibility="{Binding Cells[TPLKEY].SummaryVis}">
      <Border Tag="row:all" Cursor="Hand" Background="Transparent" Padding="8,0,8,4"
              ToolTip="{Binding Cells[TPLKEY].SummaryText}">
        <StackPanel Orientation="Horizontal">
          <TextBlock Text="{Binding Cells[TPLKEY].SummaryText}" FontSize="10.5"
                     Foreground="#77736C" MaxWidth="150" TextTrimming="CharacterEllipsis"
                     VerticalAlignment="Center"/>
          <Border Visibility="{Binding Cells[TPLKEY].SummaryPend}" Background="#FF7700"
                  CornerRadius="8" Padding="5,1" Margin="7,0,0,0" VerticalAlignment="Center">
            <TextBlock Text="PENDING" FontSize="7.5" FontWeight="ExtraBold"
                       Foreground="__ON_ACCENT__"/>
          </Border>
        </StackPanel>
      </Border>
      <Border Tag="fold" Style="{StaticResource FFold}" Padding="8,3,8,8"
              ToolTip="Show this category's 7 override rows">
        <TextBlock Text="Show 7 overrides  &#9662;" FontSize="10.5" FontWeight="SemiBold"/>
      </Border>
    </StackPanel>

  </StackPanel>
</DataTemplate>
"""

_ANNO_CELL_TEMPLATE_XAML = _cat_head(_CAT_RES) + u"""
    <!-- Always open: Projection lines and Halftone, nothing to fold -->
    <Border BorderBrush="#ECE9E4" BorderThickness="0,1,0,0"
            Visibility="{Binding Cells[TPLKEY].MiniVis}">
      <StackPanel Orientation="Vertical">
""" + _rows_xaml(_ANNO_ROWS) + u"""
      </StackPanel>
    </Border>

  </StackPanel>
</DataTemplate>
"""


def _themed(xaml):
    # Same pass the Filters template gets (see above): a template parsed on its
    # own never sees the window's theme translation.
    try:
        return ui._theme(xaml)
    except Exception:
        return xaml.replace(u"__ON_ACCENT__", u"#FFFFFF")


_MODEL_CELL_TEMPLATE_XAML = _themed(_MODEL_CELL_TEMPLATE_XAML)
_ANNO_CELL_TEMPLATE_XAML = _themed(_ANNO_CELL_TEMPLATE_XAML)


def _cat_cell_template_for(key, annot=False):
    """Parse one instance of the Model (or Annotation) category cell with `key`
    ("t123") baked into every `Cells[...]` binding path."""
    xaml = _ANNO_CELL_TEMPLATE_XAML if annot else _MODEL_CELL_TEMPLATE_XAML
    return XamlReader.Parse(xaml.replace(u"TPLKEY", key))


def _tpl_header(tpl, tab=None):
    """Column header: template name, then its ViewType in TEXT_DIM -- built in
    code, not a XAML string, because a plain bound `Header="..."` cannot mix two
    font weights/sizes on one TextBlock. When the template does not control the
    V/G group of `tab`, a third, amber line says so (full sentence in the
    tooltip)."""
    header = TextBlock()
    name_run = Run(tpl.Template)
    name_run.FontWeight = FontWeights.SemiBold
    type_run = Run(tpl.ViewType)
    type_run.FontSize = 10.5
    type_run.Foreground = DIM
    header.Inlines.Add(name_run)
    header.Inlines.Add(LineBreak())
    header.Inlines.Add(type_run)
    if tab is None or not _not_controlled(tpl, tab):
        return header
    warn = TextBlock()
    warn.Text = _warn_short(tab)
    warn.Foreground = GOLD
    warn.FontSize = 10.5
    warn.FontWeight = FontWeights.SemiBold
    warn.TextWrapping = TextWrapping.Wrap
    warn.MaxWidth = 210
    warn.Margin = Thickness(0, 3, 0, 0)
    box = StackPanel()
    box.Children.Add(header)
    box.Children.Add(warn)
    box.ToolTip = _warn_full(tab)
    return box

_FOOTER = u"""
  <Grid>
    <!-- The bulk actions live in the category tree bar (btnRows); the footer
         only keeps what closes the round (feedback, 09-19). -->
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal" VerticalAlignment="Center">
      <TextBlock x:Name="lblCount" VerticalAlignment="Center" Foreground="#A6A199"
                 FontSize="11.5" Margin="0,0,14,0"/>
      <!-- "Loaded" Apply (v5): while something is staged, the count rides on
           the button as a badge and the same number is spelled out beside it.
           Apply stays the only solid orange button of the footer. -->
      <TextBlock x:Name="lblPending" Visibility="Collapsed" VerticalAlignment="Center"
                 Foreground="#202022" FontSize="11.5" FontWeight="SemiBold"
                 Margin="0,0,12,0"/>
      <Button x:Name="btnApply" Style="{StaticResource BtnPrimary}" Margin="0,0,8,0">
        <StackPanel Orientation="Horizontal">
          <TextBlock Text="Apply" VerticalAlignment="Center"/>
          <Border x:Name="badgeApply" Visibility="Collapsed" Background="__ON_ACCENT__"
                  CornerRadius="9" MinWidth="18" Height="18" Padding="5,0"
                  Margin="9,0,0,0" VerticalAlignment="Center">
            <TextBlock x:Name="txtBadgeApply" Text="0" Foreground="#FF7700" FontSize="10.5"
                       FontWeight="Bold" HorizontalAlignment="Center"
                       VerticalAlignment="Center"/>
          </Border>
        </StackPanel>
      </Button>
      <Button x:Name="btnClose" Content="Close" Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""


def open_window():
    """Show the window (if it is already open, bring it to front and do
    nothing else -- the shape every modeless Magic Tools window follows,
    see lib/modeless.py)."""
    if modeless.focus(TITLE):
        return

    doc = revit.doc
    if doc is None:
        ui.alert(u"Open a project document first.", title=TITLE)
        return

    tpl_rows = _collect_templates(doc)
    if not tpl_rows:
        ui.alert(u"No view templates found in this document.", title=TITLE)
        return
    views_by_tpl, sheets_by_view, sheets_by_set, set_names = _collect_usage(doc)
    _apply_usage(tpl_rows, views_by_tpl, sheets_by_view)
    vtype_roots = _build_view_type_tree(tpl_rows)

    model_roots = _build_cat_tree(doc, DB.CategoryType.Model)
    anno_roots = _build_cat_tree(doc, DB.CategoryType.Annotation)
    if not model_roots and not anno_roots:
        ui.alert(u"No controllable categories found in this document.", title=TITLE)
        return
    # Filters tab (step 5): an empty list is a valid document state (no
    # ParameterFilterElements yet), not an error -- the tab just opens empty,
    # every scope-gated button stays disabled (see _update_footer).
    filter_rows = _build_filter_rows(doc)

    _CACHE.clear()
    _PENDING.clear()
    _FCACHE.clear()
    _FPENDING.clear()
    _FORDER_TOTAL.clear()
    _FROWS.clear()
    _CROWS.clear()
    _NONCTRL.clear()
    _CUTTABLE.clear()
    _PATNAMES.clear()
    _PRIMED["on"] = False
    _SELF["n"] = 0
    for acc in _ACC.values():
        acc.reset()
    _AUTO["on"] = True
    # "cols" = which cell template the grid's columns carry: "model", "anno" or
    # "filters" (see `_tab_kind`).
    # "closing_at" = when a Close chose Apply: the window closes itself once the
    # job has written, and a second X inside that window is ignored.
    # "ticked" = the ids of the templates ticked when the last tick settled, so a
    # cancelled untick (a single template, a whole ViewType group, Clear
    # selection) can put back exactly that set.
    state = {"tab": u"Model", "cols": None, "closing_at": 0.0, "ticked": set()}
    # Every CatNode of both tabs, by id -- lets `on_apply` find which node a
    # `Pending` (it only carries `cat_eid`) belongs to, to refresh its cell
    # after the write. Ids never collide across Model/Annotation.
    node_by_id = {}
    for n in _iter_nodes(model_roots):
        node_by_id[n.id_val] = n
    for n in _iter_nodes(anno_roots):
        node_by_id[n.id_val] = n

    win = ui.parse(
        TITLE, u"{} view templates in this model".format(len(tpl_rows)),
        _BODY, _FOOTER, width=1180, height=720, context=doc.Title,
    )

    btnTabModel = win.FindName("btnTabModel")
    btnTabAnno = win.FindName("btnTabAnno")
    btnTabFilters = win.FindName("btnTabFilters")
    txtCatFilter = win.FindName("txtCatFilter")
    btnCatAll = win.FindName("btnCatAll")
    btnCatFold = win.FindName("btnCatFold")
    btnRefresh = win.FindName("btnRefresh")
    cmbDiscipline = win.FindName("cmbDiscipline")
    cmbDiscipline.Items.Add(u"All disciplines")
    for d in DISCIPLINES:
        cmbDiscipline.Items.Add(d)
    cmbDiscipline.SelectedIndex = 0

    def _active_disc():
        if state["tab"] == u"Filters":
            # No discipline grouping on this tab -- combo is disabled, but
            # ignore whatever index it is still showing from Model/Annotation.
            return None
        idx = cmbDiscipline.SelectedIndex
        if idx <= 0 or idx > len(DISCIPLINES):
            return None
        return DISCIPLINES[idx - 1]
    btnTplFold = win.FindName("btnTplFold")
    btnTplAll = win.FindName("btnTplAll")
    btnFocus = win.FindName("btnFocus")
    _wire_fullscreen(win, win.FindName("btnFullScreen"))
    colLeft = win.FindName("colLeft")
    splitter = win.FindName("splitter")
    focus = {"on": False, "width": None, "minw": colLeft.MinWidth}
    gridCat = win.FindName("gridCat")
    # Set here, not in the XAML: FrozenColumnCount coerces against
    # Columns.Count at the moment it is SET, and by now the two static
    # fixed columns (chevron+tick, Category) are already parsed in, so the
    # value sticks -- see the comment on gridCat's XAML above.
    gridCat.FrozenColumnCount = 2
    txtTplFilter = win.FindName("txtTplFilter")
    chkOnSheets = win.FindName("chkOnSheets")
    cmbPrintSet = win.FindName("cmbPrintSet")
    gridTpl = win.FindName("gridTpl")
    lblCount = win.FindName("lblCount")
    btnShowAll = win.FindName("btnShowAll")
    btnHideAll = win.FindName("btnHideAll")
    btnOverride = win.FindName("btnOverride")
    btnResetOvr = win.FindName("btnResetOvr")
    btnFRemove = win.FindName("btnFRemove")
    btnFEnable = win.FindName("btnFEnable")
    btnFDisable = win.FindName("btnFDisable")
    btnFCopy = win.FindName("btnFCopy")
    btnRows = win.FindName("btnRows")
    mnuRows = win.FindName("mnuRows")
    sepFilter = win.FindName("sepFilter")
    grpFilter = win.FindName("grpFilter")
    btnApply = win.FindName("btnApply")
    badgeApply = win.FindName("badgeApply")
    txtBadgeApply = win.FindName("txtBadgeApply")
    lblPending = win.FindName("lblPending")
    btnClose = win.FindName("btnClose")

    cmbPrintSet.Items.Add(u"Any print set")
    for name in set_names:
        cmbPrintSet.Items.Add(name)
    cmbPrintSet.SelectedIndex = 0
    if not set_names:
        # No print sets in this model -- the combo has nothing to offer, so
        # it stays disabled even once "On sheets only" is ticked.
        cmbPrintSet.ToolTip = u"No print sets in this model"

    def _active_roots():
        if state["tab"] == u"Model":
            return model_roots
        if state["tab"] == u"Annotation":
            return anno_roots
        if state["tab"] == u"Filters":
            return filter_rows
        return []

    def _active_nodes():
        """Every CatNode of the active CATEGORY tab, roots AND
        subcategories -- the matrix has one row per node of the tab,
        independent of ticks. Category-tree-only; `_active_rows_flat` below
        is what callers use, so this stays untouched by step 5."""
        return list(_iter_nodes(_active_roots()))

    def _active_rows_flat():
        """Every row of the active tab, already flat -- the category tree
        walked (root + subs) for Model/Annotation, or the Filters tab's
        already-flat `filter_rows`. The one place `_schedule_reads` and
        `_bulk_scope` tap into, so neither needs its own `if tab ==
        Filters` branch (step 5)."""
        if state["tab"] == u"Filters":
            return filter_rows
        return _active_nodes()

    def _scope_rows():
        """Every row of `_active_rows_flat()` currently ticked -- what the
        footer's bulk actions and the pending count act on."""
        return [n for n in _active_rows_flat() if n.picked]

    def _ticked_templates():
        return [t for t in tpl_rows if t.Checked]

    cat_vis = ObservableCollection[object]()
    gridCat.ItemsSource = cat_vis

    tpl_vis = ObservableCollection[object]()
    gridTpl.ItemsSource = tpl_vis

    col_warn = {}     # matrix column -> whether its header shows the amber warning
    col_tpl = {}      # matrix column object -> TplRow, rebuilt on every tick
    col_order = []    # same TplRows, in column order -- fallback lookup if
                      # a DataGridColumn wrapper does not hash back to the
                      # same `col_tpl` entry

    # Expand all / Collapse all, one button per tree (2026-09-18: "we are
    # missing collapse all and expand all on both trees, and any tree").
    # The label follows the state: while anything foldable is shut
    # the button offers Expand all, otherwise Collapse all.
    def _cat_foldable():
        if state["tab"] == u"Filters":
            # v5: every filter row folds (mini table <-> one-line summary).
            return list(filter_rows)
        return [n for n in _active_roots() if n.children]

    def _tpl_foldable():
        # Only VTypeNode folds now -- a TplRow is a leaf (step 4b).
        return [v for v in vtype_roots if v.children]

    def _update_fold_labels():
        cat_nodes = _cat_foldable()
        btnCatFold.IsEnabled = bool(cat_nodes)
        btnCatFold.Content = (u"Expand all" if any(not n._expanded for n in cat_nodes)
                              else u"Collapse all")
        tpl_nodes = _tpl_foldable()
        btnTplFold.Content = (u"Expand all" if any(not n._expanded for n in tpl_nodes)
                              else u"Collapse all")

    def _fold_all(nodes, expand):
        for n in nodes:
            if n._expanded != expand:
                n._expanded = expand
                n._raise("Chevron")

    def _rebind_cat():
        roots = _active_roots()
        q = (txtCatFilter.Text or u"").strip().lower()
        wanted = _cat_visible(roots, q, _active_disc())
        keep = set(wanted)
        for idx in range(cat_vis.Count - 1, -1, -1):
            if cat_vis[idx] not in keep:
                cat_vis.RemoveAt(idx)
        for pos, row in enumerate(wanted):
            if pos >= cat_vis.Count or cat_vis[pos] is not row:
                cat_vis.Insert(pos, row)
        # A folded parent or a search/discipline filter can hide the row the
        # accordion holds open: release it (see `_release_open_row`).
        _release_open_row(state["tab"], keep)
        _update_fold_labels()

    def _tpl_passes_usage(t, on_sheets_only, sel_set_ids):
        # "On sheets only" (2026-09-18): a template only counts if at least
        # one of its views is on a sheet; the print-set combo narrows that
        # further to sheets belonging to the selected set.
        if not on_sheets_only:
            return True
        if not t.sheet_ids:
            return False
        if sel_set_ids is not None and not (t.sheet_ids & sel_set_ids):
            return False
        return True

    def _tpl_visible_rows():
        """2 levels flattened for the picker: ViewType always shown (if it
        has a match), its templates shown when the type is expanded -- or
        always, while a search/On-sheets/print-set filter is narrowing the
        list, same "a match has to be visible to mean anything" rule as the
        category tree. Also stores, on every VTypeNode, which of its
        children currently pass the filters (`_shown`) so All/None-by-type
        respects it -- see the comment on VTypeNode.Checked for why that
        matters."""
        q = (txtTplFilter.Text or u"").strip().lower()
        searching = bool(q)
        on_sheets_only = bool(chkOnSheets.IsChecked)
        sel_idx = cmbPrintSet.SelectedIndex
        sel_set_ids = None
        if on_sheets_only and sel_idx > 0 and (sel_idx - 1) < len(set_names):
            sel_set_ids = sheets_by_set.get(set_names[sel_idx - 1], set())
        any_filter = searching or on_sheets_only

        out = []
        for node in vtype_roots:
            if any_filter:
                tpls = [t for t in node.children
                        if (not searching or q in t.Template.lower())
                        and _tpl_passes_usage(t, on_sheets_only, sel_set_ids)]
                node._shown = tpls
            else:
                tpls = node.children
                node._shown = None
            node._raise("Count", "Checked")
            if not tpls:
                continue
            out.append(node)
            if not (node._expanded or any_filter):
                continue
            out.extend(tpls)
        return out

    def _rebind_tpl():
        wanted = _tpl_visible_rows()
        keep = set(wanted)
        for idx in range(tpl_vis.Count - 1, -1, -1):
            if tpl_vis[idx] not in keep:
                tpl_vis.RemoveAt(idx)
        for pos, row in enumerate(wanted):
            if pos >= tpl_vis.Count or tpl_vis[pos] is not row:
                tpl_vis.Insert(pos, row)
        _update_fold_labels()

    def _update_footer():
        n_tpl = sum(1 for t in tpl_rows if t.Checked)
        n_cat = len(_scope_rows())
        scope_ok = bool(n_tpl) and bool(n_cat)
        is_filters = state["tab"] == u"Filters"
        # Orange as soon as a row is ticked ("the user understands there is
        # something to do", 09-19); pale orange = rows ticked but no
        # template yet; ghost = nothing ticked. Local Height/Padding/FontSize
        # survive the style swap.
        btnRows.Style = win.FindResource(u"BtnPrimary" if n_cat else u"BtnGhost")
        btnRows.IsEnabled = scope_ok
        btnShowAll.IsEnabled = scope_ok
        btnHideAll.IsEnabled = scope_ok
        btnOverride.IsEnabled = scope_ok
        btnResetOvr.IsEnabled = scope_ok
        btnFRemove.IsEnabled = scope_ok
        btnFEnable.IsEnabled = scope_ok
        btnFDisable.IsEnabled = scope_ok
        btnFCopy.IsEnabled = scope_ok
        staged = bool(_PENDING or _FPENDING)
        n_cats, n_flts, n_changes, _n_tpls = _summarize(
            list(_PENDING.values()), list(_FPENDING.values()))
        btnApply.IsEnabled = staged
        # "Loaded" Apply (v5): same idiom as btnRows above -- the button
        # changes the moment there is something to act on. A badge with the
        # number of changes rides on it and what they touch is spelled out
        # beside it ("1 category · 2 changes pending", QA 2026-10-02: it used
        # to count cells). Apply stays the only solid orange button of the footer.
        badgeApply.Visibility = VIS if staged else GONE
        txtBadgeApply.Text = u"{}".format(n_changes)
        lblPending.Visibility = VIS if staged else GONE
        lblPending.Text = _summary_text(n_cats, n_flts, n_changes) + u" pending"
        noun = u"filters" if is_filters else u"categories"
        if not scope_ok and not staged:
            # Nothing scoped yet and nothing staged -- everything starts
            # unticked, so this is the normal state right after opening, not
            # an error.
            lblCount.Text = u"Tick templates and {} first".format(noun)
        else:
            lblCount.Text = u"{} templates × {} {}".format(n_tpl, n_cat, noun)

    # Lazy rows: a Model cell reads its 7 rows (and its category's
    # IsCuttable) the first time its row opens. The cells ask through
    # `_ROWS_REQ`; see `_make_rows_queue` for why that holds no state.
    _ROWS_REQ["fn"], _rows_todo = _make_rows_queue(
        lambda work: modeless.run(work, doc=doc, title=TITLE))

    def _schedule_reads():
        """Ensure every (active-tab row, ticked template) has a cell VM,
        then read into `_CACHE`/`_FCACHE` whatever is missing for that same
        set and refresh the cells once the read lands. The read itself runs
        inside a valid Revit API context via modeless.run -- a click handler
        on a modeless window is not one (see lib/modeless.py).

        Categories read per (template, node) pair; the Filters tab (step 5)
        reads per TEMPLATE (`_read_filters_for_template` fills every filter
        row of that column in one `GetFilters()` call), so "missing" there
        means "this template has not been read at all yet". Whatever the tab,
        a ticked template also gets its V/G Include flags read once
        (`_read_noncontrolled`, QA 2026-10-02) for the amber column warning."""
        tpls = _ticked_templates()
        nodes = _active_rows_flat()
        _ensure_cells(tpls, nodes)
        filters_tab = state["tab"] == u"Filters"

        missing_tpls, missing = [], []
        if filters_tab:
            for tpl in tpls:
                for node in nodes:
                    if (tpl.id_val, node.id_val) not in _FCACHE:
                        missing_tpls.append(tpl)
                        break
        else:
            missing = [(tpl, node) for tpl in tpls for node in nodes
                       if (tpl.id_val, node.id_val) not in _CACHE]
        need_flags = [t for t in tpls if t.id_val not in _NONCTRL]
        _update_footer()
        if not missing_tpls and not missing and not need_flags:
            return

        def work(uiapp):
            fresh = set()

            def fresh_vt(tpl):
                if tpl.id_val not in fresh:
                    fresh.add(tpl.id_val)
                    _fresh_vt(tpl, doc)

            for tpl in need_flags:
                fresh_vt(tpl)
                _read_noncontrolled(tpl)
            if filters_tab:
                for tpl in missing_tpls:
                    fresh_vt(tpl)
                    _read_filters_for_template(tpl, filter_rows)
                for tpl in missing_tpls:
                    key = u"t{}".format(tpl.id_val)
                    for row in filter_rows:
                        if row.Cells.ContainsKey(key):
                            row.Cells[key].refresh()
            else:
                for tpl, node in missing:
                    fresh_vt(tpl)
                    # Annotation also builds its 2 rows from the same OGS read.
                    _CACHE[(tpl.id_val, node.id_val)] = _read_cell(tpl, node)
                for tpl, node in missing:
                    key = u"t{}".format(tpl.id_val)
                    if node.Cells.ContainsKey(key):
                        node.Cells[key].refresh()
            _refresh_headers()
            _update_footer()

        # The job runs on Revit's next Idling, not on this click: the footer
        # already registered the tick above, the cells follow when the read
        # lands (same "repaint now, rows follow" idiom as step 4's
        # reviewer note, 2026-09-17).
        modeless.run(work, doc=doc, title=TITLE)

    def _tab_kind():
        # Which cell template the active tab's columns carry.
        if state["tab"] == u"Filters":
            return u"filters"
        return u"anno" if state["tab"] == u"Annotation" else u"model"

    def _rebuild_columns():
        """Remove every matrix column past the two fixed ones and add one
        per ticked template, in tree order (VTypeNode > TplRow, same order
        the left picker shows them) -- ticking/unticking a template is the
        only thing that changes which columns exist."""
        while gridCat.Columns.Count > 2:
            gridCat.Columns.RemoveAt(gridCat.Columns.Count - 1)
        col_tpl.clear()
        col_warn.clear()
        del col_order[:]
        # Every tab's cell is a mini table now: 240 px, and a
        # template per tab (Filters / Model / Annotation), so a tab switch
        # rebuilds the columns too (see _activate_tab).
        kind = _tab_kind()
        state["cols"] = kind
        for vtype in vtype_roots:
            for tpl in vtype.children:
                if not tpl.Checked:
                    continue
                col = DataGridTemplateColumn()
                col.Width = DataGridLength(TPL_COL_MAX)
                key = u"t{}".format(tpl.id_val)
                if kind == u"filters":
                    col.CellTemplate = _filter_cell_template_for(key)
                else:
                    col.CellTemplate = _cat_cell_template_for(key, annot=(kind == u"anno"))
                col.CellStyle = gridCat.FindResource(u"FilterMatrixCell")
                col.Header = _tpl_header(tpl, state["tab"])
                col_warn[col] = _not_controlled(tpl, state["tab"])
                gridCat.Columns.Add(col)
                col_tpl[col] = tpl
                col_order.append(tpl)
        _fit_columns()

    def _fit_columns(s=None, e=None):
        """Share the room the matrix has between the template columns: 240 px
        each while they fit, narrower (down to 180) when two or three would be
        cut by the edge. Runs when the grid is resized (window, splitter, full
        screen), when the tab changes the name column, and when columns change."""
        n = gridCat.Columns.Count - 2
        if n <= 0:
            return
        avail = (gridCat.ActualWidth - FIXED_COL_W - NAME_COL_W[state["tab"]] - GRID_SLACK)
        w = _tpl_col_width(avail, n)
        for i in range(2, gridCat.Columns.Count):
            col = gridCat.Columns[i]
            if abs(col.Width.Value - w) > 0.5:
                col.Width = DataGridLength(w)

    def _refresh_headers():
        """The V/G Include flags land after the columns exist (they are read in
        the API context), so repaint the headers whose warning changed."""
        tab = state["tab"]
        for i in range(2, gridCat.Columns.Count):
            col = gridCat.Columns[i]
            tpl = col_tpl.get(col)
            if tpl is None:
                continue
            warn = _not_controlled(tpl, tab)
            if col_warn.get(col) != warn:
                col.Header = _tpl_header(tpl, tab)
                col_warn[col] = warn

    def _activate_tab(name):
        state["tab"] = name
        # Folio tabs (v5): the logic is the same, only the look changed -- the
        # active tab is open towards the card, the other two are flat.
        for btn, tab in ((btnTabModel, u"Model"), (btnTabAnno, u"Annotation"),
                         (btnTabFilters, u"Filters")):
            btn.Style = btn.FindResource(u"FolioTabOn" if name == tab else u"FolioTabOff")
        is_filters = name == u"Filters"
        # No disciplines on the Filters tab: the combo needs an explicit push.
        # (btnCatFold needs none: `_cat_foldable` already answers per tab,
        # and on Filters it folds the filter rows themselves.)
        cmbDiscipline.IsEnabled = not is_filters
        gridCat.Columns[1].Header = u"Filter" if is_filters else u"Category"
        gridCat.Columns[1].Width = DataGridLength(NAME_COL_W[name])
        gridCat.Columns[1].CellStyle = (gridCat.FindResource(u"FilterNameCell")
                                        if is_filters else None)
        for btn in (btnFRemove, btnFEnable, btnFDisable, btnFCopy, sepFilter, grpFilter):
            btn.Visibility = VIS if is_filters else GONE
        _schedule_reads()
        if state["cols"] != _tab_kind():
            # Cells exist (the line above) before the columns that bind them.
            # The old rows go first: a template bound to another tab's cell
            # VMs would only log binding errors, and `_rebind_cat` below
            # repopulates the grid.
            cat_vis.Clear()
            _rebuild_columns()
        _rebind_cat()
        _fit_columns()
        _update_footer()

    def on_tab_model(s, e):
        if state["tab"] != u"Model":
            _activate_tab(u"Model")

    def on_tab_anno(s, e):
        if state["tab"] != u"Annotation":
            _activate_tab(u"Annotation")

    def on_tab_filters(s, e):
        if state["tab"] != u"Filters":
            _activate_tab(u"Filters")

    def _tag_of(src, stop_type):
        """Walk up the visual tree from `src` (the click's OriginalSource)
        until something exposes a `Tag` of "chev" or "badge", or the walk
        reaches `stop_type` -- a click on the chevron TextBlock or the badge
        Border can land on either that element or something nested inside it
        (the badge's own TextBlock), depending on exactly where the pixel
        is. Returns the tag string, or None."""
        node = src
        while node is not None and not isinstance(node, stop_type):
            tag = getattr(node, "Tag", None)
            if tag in ("chev", "badge", "ovr", "on", "fold", "rm"):
                return tag
            # v5: the Filters mini table. Tag="row:<key>" is one of its 7
            # rows ("row:Proj", "row:CutP"...), "row:all" is the collapsed
            # one-line summary. Every one of them opens the same editor.
            if _is_row_tag(tag):
                return tag
            try:
                node = VisualTreeHelper.GetParent(node)
            except Exception:
                # GetParent only takes Visuals; a Run or other ContentElement
                # as OriginalSource would throw. Treat it as "not ours".
                return None
        return None

    def _find_up(src, stop_type):
        """Walk up from `src` to the nearest `stop_type` ancestor (`src`
        itself counts), or None."""
        node = src
        while node is not None and not isinstance(node, stop_type):
            try:
                node = VisualTreeHelper.GetParent(node)
            except Exception:
                return None
        return node

    def on_tpl_tree_click(s, e):
        # TplRow has no chevron/toggle any more (step 4b) -- only VTypeNode
        # folds in the left picker.
        if _tag_of(e.OriginalSource, DataGridRow) != "chev":
            return
        row = getattr(e.OriginalSource, "DataContext", None)
        if isinstance(row, VTypeNode):
            row.toggle()
            _rebind_tpl()

    def _badge_cell(src):
        """A badge click can land on the Border(Tag="badge"/"ovr"/"on")
        itself or its nested TextBlock. Returns the enclosing DataGridCell
        -- so the caller can read `.Column` to know WHICH template's badge
        fired, since one row now hosts one badge per ticked template
        (unlike the old per-row TplCatRow) -- or None for anything else.
        "on" (step 5, the Filters tab's ENABLED chip) is accepted here too,
        even though a category cell's own "on" chip is always GONE and
        therefore never hit-testable."""
        tag = _tag_of(src, DataGridCell)
        if tag not in ("badge", "ovr", "on", "fold", "rm") and not _is_row_tag(tag):
            return None
        return _find_up(src, DataGridCell)

    def _tpl_for_column(col):
        tpl = col_tpl.get(col)
        if tpl is not None:
            return tpl
        idx = gridCat.Columns.IndexOf(col) - 2
        if 0 <= idx < len(col_order):
            return col_order[idx]
        return None

    def _toggle_cell_pending(tpl, node):
        """Badge click on the matrix: toggle this cell's pending visibility
        (current Visible -> pending Hidden, current Hidden -> pending
        Visible, already pending -> clear it back). The template is already
        a column because it is ticked, so -- unlike step 4's cell-inside-
        an-expanded-template -- no auto-scope is needed any more."""
        entry = _CACHE.get((tpl.id_val, node.id_val))
        if entry is None or not entry[0]:
            return
        controllable, hidden, tags = entry
        current_visible = not hidden
        p = _pending_for(tpl, node, create=True)
        p.vis = None if p.vis is not None else (not current_visible)
        _prune_pending(tpl, node)
        key = u"t{}".format(tpl.id_val)
        if node.Cells.ContainsKey(key):
            node.Cells[key].refresh()
        _update_footer()

    def _edit_cell_override(tpl, node, row_key=None):
        """Click on a cell's mini table (any of its rows, or the collapsed
        summary): open the editor prefilled with
        what this template already has for this category, and stage the
        result for this one cell. Runs inside modeless.run for the same two
        reasons as the bulk Override: reading GetCategoryOverrides and the
        pattern collectors the dialog fills its combos with are API calls.
        At Apply the staged OGS is written as-is (step 8b): `apply_intents`
        resolves set/clear/untouched against `current` right here, so an
        override this cell already had CAN be cleared through the dialog --
        that used to be what "reset" was for, before this step. `row_key`
        ("Proj", ... or "all" for the summary) only tells the editor which
        section to highlight, as in the Filters cell."""
        entry = _CACHE.get((tpl.id_val, node.id_val))
        if entry is None or not entry[0]:
            return

        def work(uiapp):
            try:
                model = tpl.vt.GetCategoryOverrides(node.eid)
            except Exception:
                model = DB.OverrideGraphicSettings()
            # Open on what is staged, not on the model: re-editing a pending
            # cell must build on the first edit instead of dropping it.
            current = _prefill_ogs(_PENDING.get((tpl.id_val, node.id_val)),
                                   lambda: model)
            res = _edit_dialog(current, win, doc, focus=_FOCUS_SECTION.get(row_key),
                               hint=_cut_hint([node]))
            if res is None:
                return
            _stage_cat_edit(tpl, node, vgrow.apply_intents(
                current, _cat_intents(node, res.intents)), model)
            key = u"t{}".format(tpl.id_val)
            if node.Cells.ContainsKey(key):
                node.Cells[key].refresh()
            _update_footer()

        modeless.run(work, doc=doc, title=TITLE)

    def _toggle_filter_cell_pending(tpl, row):
        """Click on a filter cell's STATE chip. Four displayed states, four
        actions (step 5): not applied -> stage `add`; Visible -> stage
        `vis=False`; Hidden -> stage `vis=True`; a staged state (add-without-
        vis, vis, or remove) showing on the badge right now -> clear just
        that, same toggle-back idiom `_toggle_cell_pending` uses above."""
        entry = _FCACHE.get((tpl.id_val, row.id_val))
        if entry is None:
            return
        applied, visible = entry[0], entry[1]
        p = _FPENDING.get((tpl.id_val, row.id_val))
        if p is not None and (p.remove or p.vis is not None or (p.add and p.vis is None)):
            if p.remove:
                p.remove = False
            elif p.vis is not None:
                p.vis = None
            elif p.add:
                if applied:
                    p.add = False
                else:
                    # Undoing the attach of a filter that is not in the
                    # template drops the WHOLE staged cell: an edit staged
                    # on top of that add would otherwise linger as a ghost.
                    _FPENDING.pop((tpl.id_val, row.id_val), None)
            _fprune(tpl, row)
        else:
            p = _fpending_for(tpl, row, create=True)
            if not applied:
                p.add = True
            elif visible:
                p.vis = False
            else:
                p.vis = True
        key = u"t{}".format(tpl.id_val)
        if row.Cells.ContainsKey(key):
            row.Cells[key].refresh()
        _update_footer()

    def _edit_filter_cell_override(tpl, row, row_key=None):
        """Click on a filter cell's OVERRIDES (since v5: on any of the 7 rows
        of its mini table): open the editor
        prefilled with what this template already has for this filter
        (blank when it is not applied there yet), and stage the result --
        attaching the filter too (`add`) if it was not applied. Same
        apply_intents contract as `_edit_cell_override` (step 8b): the
        staged OGS is absolute, resolved against `current` at click time.
        `row_key` is the row's key (`row:Proj` -> "Proj", None for the
        collapsed summary): it only tells the editor which section to
        highlight, it never changes what is edited or staged."""
        entry = _FCACHE.get((tpl.id_val, row.id_val))
        if entry is None:
            return
        applied = entry[0]

        def work(uiapp):
            model = DB.OverrideGraphicSettings()
            if applied:
                try:
                    model = tpl.vt.GetFilterOverrides(row.eid)
                except Exception:
                    pass
            # Open on what is staged, not on the model.
            current = _prefill_ogs(_FPENDING.get((tpl.id_val, row.id_val)),
                                   lambda: model)
            res = vgrow.show_edit_dialog(current, owner_win=win, doc=doc,
                                          focus=_FOCUS_SECTION.get(row_key))
            if res is None:
                return
            _stage_filter_edit(tpl, row, vgrow.apply_intents(current, res.intents),
                               model, applied)
            key = u"t{}".format(tpl.id_val)
            if row.Cells.ContainsKey(key):
                row.Cells[key].refresh()
            _update_footer()

        modeless.run(work, doc=doc, title=TITLE)

    def _toggle_filter_cell_remove(tpl, row):
        """Click on a filter cell's little x: stage `remove` for THIS template
        only (same meaning as `on_f_remove`, one pair instead of the whole
        scope), or undo it if it is already staged. Undoing restores nothing
        else: staging a remove already cleared the cell's other staged fields,
        which is what `FilterPending.remove` means."""
        entry = _FCACHE.get((tpl.id_val, row.id_val))
        if entry is None or not entry[0]:
            return
        p = _fpending_for(tpl, row, create=True)
        p.remove = not p.remove
        _fprune(tpl, row)
        key = u"t{}".format(tpl.id_val)
        if row.Cells.ContainsKey(key):
            row.Cells[key].refresh()
        _update_footer()

    def _toggle_cell_enabled(tpl, row):
        """Click on a filter cell's ENABLED chip. Only ever reached for an
        applied cell whose `GetIsFilterEnabled` is known -- `FilterCellVM`
        keeps `OnVis` GONE otherwise, so there is nothing to hit-test."""
        entry = _FCACHE.get((tpl.id_val, row.id_val))
        if entry is None or not entry[0] or entry[2] is None:
            return
        enabled = entry[2]
        p = _fpending_for(tpl, row, create=True)
        p.enabled = None if p.enabled is not None else (not enabled)
        _fprune(tpl, row)
        key = u"t{}".format(tpl.id_val)
        if row.Cells.ContainsKey(key):
            row.Cells[key].refresh()
        _update_footer()

    def on_cat_grid_click(s, e):
        src = e.OriginalSource
        if _tag_of(src, DataGridRow) == "chev":
            row = getattr(src, "DataContext", None)
            if isinstance(row, (CatNode, FilterRow)):
                row.toggle()
                _rebind_cat()
            return
        if state["tab"] == u"Filters":
            # The whole NAME cell of a filter row folds/unfolds it, not just
            # the arrow (feedback: "the arrow gets lost"). Column 0 holds
            # the arrow and the tick; column 1 is the name; the matrix starts
            # at 2 and is handled below.
            ncell = _find_up(src, DataGridCell)
            if (ncell is not None and isinstance(ncell.DataContext, FilterRow)
                    and ncell.Column is not None
                    and gridCat.Columns.IndexOf(ncell.Column) == 1):
                ncell.DataContext.toggle()
                _rebind_cat()
                return
        cell = _badge_cell(src)
        if cell is None:
            return
        tpl = _tpl_for_column(cell.Column)
        if tpl is None:
            return
        node = cell.DataContext
        tag = _tag_of(src, DataGridCell)
        if isinstance(node, FilterRow):
            if _is_row_tag(tag):
                # Any of the 7 rows (or the collapsed summary): same editor.
                _edit_filter_cell_override(tpl, node, row_key=tag[4:])
            elif tag == "fold":
                node.toggle()       # the "Show 7 overrides" link of a collapsed cell
                _rebind_cat()
            elif tag == "rm":
                _toggle_filter_cell_remove(tpl, node)
            elif tag == "ovr":
                _edit_filter_cell_override(tpl, node)
            elif tag == "on":
                _toggle_cell_enabled(tpl, node)
            else:
                _toggle_filter_cell_pending(tpl, node)
            return
        if not isinstance(node, CatNode):
            return
        if tag == "fold":
            node.toggle_ovr()   # "Show 7 overrides" / "Hide": this ROW's table, not the tree
        elif _is_row_tag(tag):
            rkey = tag[4:]
            key = u"t{}".format(tpl.id_val)
            cellvm = node.Cells[key] if node.Cells.ContainsKey(key) else None
            rowvm = getattr(cellvm, rkey + "Row", None) if cellvm is not None else None
            if rowvm is not None and rowvm.State == u"na":
                return          # a row that cannot apply is not clickable
            _edit_cell_override(tpl, node, row_key=rkey)
        else:
            _toggle_cell_pending(tpl, node)

    def _bulk_scope():
        """(tpls, rows) currently in scope: ticked templates × ticked rows
        of the ACTIVE tab -- what every footer bulk action below acts on."""
        return (_ticked_templates(), _scope_rows())

    def _after_bulk():
        tpls, nodes = _bulk_scope()
        # Staging on many rows at once must not open (and, with the accordion,
        # close) them one after another: they keep the PENDING chip instead.
        _AUTO["on"] = False
        try:
            _refresh_cells(tpls, nodes)
        finally:
            _AUTO["on"] = True
        _update_footer()

    # -- Filters-tab staging helpers for the four shared footer buttons
    # (step 5): a filter cell not yet applied gets `add` staged alongside
    # whatever the button is setting -- "5 templates, 2 of which did not
    # have the filter, they get it" is the headline case.
    def _stage_show(t, row):
        entry = _FCACHE.get((t.id_val, row.id_val))
        if entry is None:
            return
        applied, visible = entry[0], entry[1]
        p = _fpending_for(t, row, create=True)
        if not applied:
            p.add = True
            p.vis = True
        else:
            p.vis = None if visible else True
        _fprune(t, row)

    def _stage_hide(t, row):
        entry = _FCACHE.get((t.id_val, row.id_val))
        if entry is None:
            return
        applied, visible = entry[0], entry[1]
        p = _fpending_for(t, row, create=True)
        if not applied:
            p.add = True
            p.vis = False
        else:
            p.vis = None if not visible else False
        _fprune(t, row)

    def _stage_reset(t, row):
        entry = _FCACHE.get((t.id_val, row.id_val))
        if entry is None or not entry[0]:
            return   # nothing to reset on a filter that is not attached
        tags = entry[3]
        staged = _FPENDING.get((t.id_val, row.id_val))
        if not tags and (staged is None or staged.edit is None):
            return   # nothing to reset on this pair
        if not tags:
            # No override in the model, only a staged edit: Reset means
            # "forget that edit", not "write a blank OGS".
            staged.edit = None
            _fprune(t, row)
            return
        p = _fpending_for(t, row, create=True)
        p.reset = True

    def on_show_all(s, e):
        tpls, nodes = _bulk_scope()
        if state["tab"] == u"Filters":
            for t in tpls:
                for row in nodes:
                    _stage_show(t, row)
            _after_bulk()
            return
        for t in tpls:
            for node in nodes:
                entry = _CACHE.get((t.id_val, node.id_val))
                if entry is None or not entry[0]:
                    continue
                hidden = entry[1]
                p = _pending_for(t, node, create=True)
                p.vis = None if not hidden else True
                _prune_pending(t, node)
        _after_bulk()

    def on_hide_all(s, e):
        tpls, nodes = _bulk_scope()
        if state["tab"] == u"Filters":
            for t in tpls:
                for row in nodes:
                    _stage_hide(t, row)
            _after_bulk()
            return
        for t in tpls:
            for node in nodes:
                entry = _CACHE.get((t.id_val, node.id_val))
                if entry is None or not entry[0]:
                    continue
                hidden = entry[1]
                p = _pending_for(t, node, create=True)
                p.vis = None if hidden else False
                _prune_pending(t, node)
        _after_bulk()

    def on_reset_ovr(s, e):
        tpls, nodes = _bulk_scope()
        if state["tab"] == u"Filters":
            for t in tpls:
                for row in nodes:
                    _stage_reset(t, row)
            _after_bulk()
            return
        for t in tpls:
            for node in nodes:
                entry = _CACHE.get((t.id_val, node.id_val))
                if entry is None or not entry[0]:
                    continue
                tags = entry[2]
                staged = _PENDING.get((t.id_val, node.id_val))
                if not tags and (staged is None or staged.edit is None):
                    continue   # nothing to reset on this pair
                if not tags:
                    # No override in the model, only a staged edit: Reset
                    # means "forget that edit", not "write a blank OGS".
                    staged.edit = None
                    _prune_pending(t, node)
                    continue
                p = _pending_for(t, node, create=True)
                p.reset = True
                p.edit = None
        _after_bulk()

    def on_override(s, e):
        tpls, nodes = _bulk_scope()
        if not tpls or not nodes:
            return
        is_filters = state["tab"] == u"Filters"

        def work(uiapp):
            # vgrow.show_edit_dialog() fills its pattern combos with
            # FilteredElementCollectors and pops its own ShowDialog(): a
            # click on this modeless window is not a valid API context for
            # those reads, so the whole thing runs inside the ExternalEvent,
            # exactly as Inspect View's _on_edit does (reviewer, 09-18).
            #
            # Step 8b: the editor opens in MULTI mode (prefill_many) with
            # the CURRENT override of every pair in scope, read here (same
            # cost as _read_state, one GetCategoryOverrides/GetFilterOverrides
            # per cell). A field where those currents disagree shows
            # <varies> and, left untouched, stays untouched per pair --
            # apply_intents resolves set/clear/untouched against each
            # pair's OWN current, not a shared blank, so this finally lets
            # a bulk edit clear an override without wiping what a
            # differing pair already had elsewhere.
            pairs    = []
            currents = []     # what the editor opens on: STAGED first
            models   = []     # what the model has, to drop edits that change nothing
            if is_filters:
                for t in tpls:
                    for row in nodes:
                        entry = _FCACHE.get((t.id_val, row.id_val))
                        if entry is None:
                            continue
                        model = DB.OverrideGraphicSettings()
                        if entry[0]:
                            try:
                                model = t.vt.GetFilterOverrides(row.eid)
                            except Exception:
                                pass
                        pairs.append((t, row))
                        models.append(model)
                        currents.append(_prefill_ogs(
                            _FPENDING.get((t.id_val, row.id_val)), lambda m=model: m))
            else:
                for t in tpls:
                    for node in nodes:
                        entry = _CACHE.get((t.id_val, node.id_val))
                        if entry is None or not entry[0]:
                            continue
                        try:
                            model = t.vt.GetCategoryOverrides(node.eid)
                        except Exception:
                            model = DB.OverrideGraphicSettings()
                        pairs.append((t, node))
                        models.append(model)
                        currents.append(_prefill_ogs(
                            _PENDING.get((t.id_val, node.id_val)), lambda m=model: m))
            if not pairs:
                return

            hint = u""
            if not is_filters:
                hint = _cut_hint(sorted(set(n for (_t, n) in pairs), key=lambda n: n.id_val))
            res = _edit_dialog(currents[0], win, doc, prefill_many=currents, hint=hint)
            if res is None:
                return

            if is_filters:
                for (t, row), cur, model in zip(pairs, currents, models):
                    entry = _FCACHE.get((t.id_val, row.id_val))
                    new = vgrow.apply_intents(cur, res.intents)
                    same = _bulk_same(new, model, res.intents,
                                      _FPENDING.get((t.id_val, row.id_val)))
                    _stage_filter_edit(t, row, new, model, bool(entry and entry[0]),
                                       same=same)
            else:
                for (t, node), cur, model in zip(pairs, currents, models):
                    intents = _cat_intents(node, res.intents)
                    new = vgrow.apply_intents(cur, intents)
                    same = _bulk_same(new, model, intents,
                                      _PENDING.get((t.id_val, node.id_val)))
                    _stage_cat_edit(t, node, new, model, same=same)
            _after_bulk()

        modeless.run(work, doc=doc, title=TITLE)

    def on_f_remove(s, e):
        tpls, rows = _bulk_scope()
        for t in tpls:
            for row in rows:
                entry = _FCACHE.get((t.id_val, row.id_val))
                if entry is None or not entry[0]:
                    continue   # nothing to remove if it is not even applied
                p = _fpending_for(t, row, create=True)
                p.remove = True
        _after_bulk()

    def on_f_enable(s, e):
        tpls, rows = _bulk_scope()
        for t in tpls:
            for row in rows:
                entry = _FCACHE.get((t.id_val, row.id_val))
                if entry is None or entry[2] is None:
                    continue   # not applied, or the API has no enabled flag
                enabled = entry[2]
                p = _fpending_for(t, row, create=True)
                p.enabled = None if enabled else True
                _fprune(t, row)
        _after_bulk()

    def on_f_disable(s, e):
        tpls, rows = _bulk_scope()
        for t in tpls:
            for row in rows:
                entry = _FCACHE.get((t.id_val, row.id_val))
                if entry is None or entry[2] is None:
                    continue
                enabled = entry[2]
                p = _fpending_for(t, row, create=True)
                p.enabled = None if not enabled else False
                _fprune(t, row)
        _after_bulk()

    def on_f_copy(s, e):
        """"Copy overrides from...": pick ONE filter row (a pure WPF modal,
        no Revit API call -- see `ui.pick_list`'s docstring, no
        `modeless.run` needed for the pick itself), then read that source
        filter's overrides/visibility off every ticked template and stage
        them onto every ticked filter row -- adding the target filter where
        it is missing. A template where the SOURCE filter is not applied is
        skipped and counted in the closing summary."""
        tpls, rows = _bulk_scope()
        if not tpls or not rows:
            return
        source = ui.pick_list(
            filter_rows, title=TITLE,
            subtitle=u"Copy this filter's overrides and visibility onto the selected rows",
            button_name=u"Copy", multiselect=False,
            name_fn=lambda r: r.Name, context=doc.Title)
        if source is None:
            return

        def work(uiapp):
            copied, skipped = 0, 0
            for t in tpls:
                vt = t.vt
                try:
                    src_applied = source.eid in vt.GetFilters()
                except Exception:
                    src_applied = False
                if not src_applied:
                    skipped += 1
                    continue
                try:
                    src_ogs = vt.GetFilterOverrides(source.eid)
                    src_vis = vt.GetFilterVisibility(source.eid)
                except Exception:
                    skipped += 1
                    continue
                for row in rows:
                    p = _fpending_for(t, row, create=True)
                    p.edit = DB.OverrideGraphicSettings(src_ogs)
                    p.vis = src_vis
                    entry = _FCACHE.get((t.id_val, row.id_val))
                    if entry is None or not entry[0]:
                        p.add = True
                copied += 1
            _after_bulk()
            ui.alert(u"Copied on {} template(s); {} skipped (source filter "
                     u"not applied there).".format(copied, skipped), title=TITLE)

        modeless.run(work, doc=doc, title=TITLE)

    def _write_staged(items, fitems):
        """Write these frozen lists of staged changes in ONE transaction.
        Returns (committed, failed, problem): `failed` lists the pairs Revit
        refused one by one (the rest still went through), `problem` says why
        nothing was written when `committed` is False. Must run in API context.

        QA 2026-10-02: `Commit()` hands back a `TransactionStatus` and
        the old code threw it away, so a rollback (in a workshared model, an
        element somebody else is editing is enough) looked like a success: the
        staged changes were dropped and the table said "applied". Anything
        other than Committed now keeps the staging and says so."""
        failed = []
        problem = u""
        committed = False
        _SELF["n"] += 1       # our own DocumentChanged is not news (see `_SELF`)
        try:
            with DB.Transaction(doc, u"View Template Manager") as t:
                try:
                    t.Start()
                    for p in items:
                        vt = p.tpl.vt
                        try:
                            if p.vis is not None and vt.CanCategoryBeHidden(p.cat_eid):
                                vt.SetCategoryHidden(p.cat_eid, not p.vis)
                            if p.reset and vt.IsCategoryOverridable(p.cat_eid):
                                vt.SetCategoryOverrides(p.cat_eid, DB.OverrideGraphicSettings())
                            elif p.edit is not None and vt.IsCategoryOverridable(p.cat_eid):
                                # p.edit is already an absolute OGS (step 8b):
                                # staged by apply_intents() against the cell's
                                # current overrides at click time, not a partial
                                # edit to merge against blank at Apply time.
                                vt.SetCategoryOverrides(p.cat_eid, p.edit)
                        except Exception as ex:
                            failed.append(u"{} / {}: {}".format(p.tpl.Template, p.cat_name, ex))

                    # Filters tab (step 5), same transaction. Order per pair:
                    # remove (then skip everything else on it) -> add -> vis ->
                    # enabled -> reset/edit.
                    for fp in fitems:
                        vt = fp.tpl.vt
                        try:
                            applied_now = fp.fid in vt.GetFilters()
                            if fp.remove:
                                if applied_now:
                                    vt.RemoveFilter(fp.fid)
                                continue
                            if fp.add and not applied_now:
                                vt.AddFilter(fp.fid)
                            if fp.vis is not None:
                                vt.SetFilterVisibility(fp.fid, fp.vis)
                            if fp.enabled is not None:
                                try:
                                    vt.SetIsFilterEnabled(fp.fid, fp.enabled)
                                except Exception:
                                    pass    # older API may lack this method
                            if fp.reset:
                                vt.SetFilterOverrides(fp.fid, DB.OverrideGraphicSettings())
                            elif fp.edit is not None:
                                # Same contract as p.edit above -- already
                                # absolute (apply_intents at stage time, or a
                                # verbatim copy from on_f_copy).
                                vt.SetFilterOverrides(fp.fid, fp.edit)
                        except Exception as ex:
                            failed.append(u"{} / {}: {}".format(fp.tpl.Template, fp.name, ex))

                    # Commit even with partial failures -- the pairs that went
                    # through stay applied; `failed` lists the rest.
                    status = t.Commit()
                    committed = (status == DB.TransactionStatus.Committed)
                    if not committed:
                        problem = (u"Revit rolled the changes back (transaction status: {}), so "
                                   u"nothing was written. In a workshared model this can happen "
                                   u"when another user is editing the same templates or filters."
                                   .format(status))
                except Exception as ex:
                    problem = u"Revit could not apply the changes: {}".format(ex)
                    try:
                        if t.HasStarted() and not t.HasEnded():
                            t.RollBack()
                    except Exception:
                        pass
        except Exception as ex:
            committed = False
            problem = problem or u"Revit could not apply the changes: {}".format(ex)
        finally:
            _SELF["n"] -= 1
        return committed, failed, problem

    def _resync_after_write(items, fitems):
        """After a commit: re-read what this Apply touched and drop from the
        staging only what it carried. Must run in API context."""
        touched = set()
        for p in items:
            cat_id = _id_val(p.cat_eid)
            node = node_by_id.get(cat_id)
            ckey = (p.tpl.id_val, cat_id)
            had_rows = ckey in _CROWS
            _CROWS.pop(ckey, None)
            if node is not None:
                _CACHE[ckey] = _read_cell(p.tpl, node)
                # A pair that had rows is re-read (we are in API context);
                # one that never opened stays lazy.
                if had_rows and ckey not in _CROWS:
                    _read_cat_rows(p.tpl, node)
                touched.add((p.tpl, node))
            else:
                _CACHE[ckey] = _read_state(p.tpl.vt, p.cat_eid)
        # Only what this Apply carried (frozen at the click): anything
        # staged since then is still pending, not silently dropped.
        for p in items:
            k = (p.tpl.id_val, _id_val(p.cat_eid))
            if _PENDING.get(k) is p:
                del _PENDING[k]

        touched_tpl_ids = set(fp.tpl.id_val for fp in fitems)
        touched_tpls = [tr for tr in tpl_rows if tr.id_val in touched_tpl_ids]
        for tpl in touched_tpls:
            _read_filters_for_template(tpl, filter_rows)
        for fp in fitems:
            k = (fp.tpl.id_val, _id_val(fp.fid))
            if _FPENDING.get(k) is fp:
                del _FPENDING[k]

        for tpl, node in touched:
            key = u"t{}".format(tpl.id_val)
            if node.Cells.ContainsKey(key):
                node.Cells[key].refresh()
        for tpl in touched_tpls:
            key = u"t{}".format(tpl.id_val)
            for row in filter_rows:
                if row.Cells.ContainsKey(key):
                    row.Cells[key].refresh()

    def _apply_items(items, fitems, after=None, on_fail=None):
        """Queue the write of these staged changes (frozen lists). `after()`
        runs once they are written; `on_fail()` runs when nothing was (the
        staging is kept in that case)."""
        def work(uiapp):
            ok = False
            failed, problem = [], u""
            resync_error = None
            try:
                ok, failed, problem = _write_staged(items, fitems)
                if ok:
                    try:
                        _resync_after_write(items, fitems)
                    except Exception as ex:
                        # Written, but the table could not re-read itself: the
                        # changes are in the model, so they are not "staged"
                        # any more, and Close must not hang on them.
                        resync_error = ex
                        for p in items:
                            _PENDING.pop((p.tpl.id_val, _id_val(p.cat_eid)), None)
                        for fp in fitems:
                            _FPENDING.pop((fp.tpl.id_val, _id_val(fp.fid)), None)
            finally:
                # A Close that chose Apply is waiting for this job: whatever
                # happened, a second X must work again.
                state["closing_at"] = 0.0
                try:
                    _update_footer()
                except Exception:
                    pass
            if resync_error is not None:
                ui.alert(u"The changes were applied, but the table could not re-read "
                         u"them ({}). Use Refresh.".format(resync_error), title=TITLE)
            if not ok:
                ui.alert(u"{}\n\nYour staged changes are still here.".format(
                    problem or u"The changes could not be applied."), title=TITLE)
                if on_fail is not None:
                    on_fail()
                return
            if failed:
                ui.alert(u"{} change(s) could not be applied:\n\n{}".format(
                    len(failed), u"\n".join(failed[:12])), title=TITLE)
            if after is not None:
                after()

        modeless.run(work, doc=doc, title=TITLE)

    def on_apply(s, e):
        if not _PENDING and not _FPENDING:
            return
        items = list(_PENDING.values())
        fitems = list(_FPENDING.values())
        _n_cat, _n_flt, n_c, n_t = _summarize(items, fitems)
        # Same dialog as the 3-way one so the amber "this template does not
        # control ..." lines (QA 2026-10-02) have somewhere to live.
        choice = _ask(TITLE,
                      u"Apply {} on {}?\n\nEvery view using these templates updates.".format(
                          _plural(n_c, u"change", u"changes"),
                          _plural(n_t, u"view template", u"view templates")),
                      [(u"Apply", u"apply", True), (u"Cancel", u"cancel", False)],
                      warnings=_staged_warnings(items, fitems), owner=win)
        if choice != u"apply":
            return
        _apply_items(items, fitems)

    def on_cat_ticked(s, e):
        # CheckBox.ClickEvent, not Checked/Unchecked: those also fire while
        # WPF realises virtualised rows (same reasoning as Rename Families).
        # Ticking a category row only changes bulk-action scope now -- every
        # row of the active tab already has a cell (and a scheduled read)
        # for each ticked template, there is nothing new to read here.
        _update_footer()

    def _after_tpl_change():
        # A template's tick is what makes it a matrix COLUMN (step 4b), so
        # unticking one -- by hand or through its ViewType node -- discards
        # whatever was staged for it, same rule step 4 had: "Hide all on 5,
        # untick 2, Apply" must not still hide the 2 (decision 2026-09-18). Since
        # QA 2026-10-02 the user was asked first (`_guard_staged`).
        for t in tpl_rows:
            if t.Checked:
                continue
            _discard_staged(t, node_by_id.values(), filter_rows)
        _schedule_reads()
        _rebuild_columns()
        _update_footer()

    def _ask_staged(items, fitems):
        """"You have N staged changes not applied yet." -> 'apply', 'discard'
        or 'cancel'."""
        _c, _f, n, _t = _summarize(items, fitems)
        return _ask(u"Staged changes",
                    u"You have {} staged {} not applied yet.".format(
                        n, u"change" if n == 1 else u"changes"),
                    [(u"Apply", u"apply", True), (u"Discard", u"discard", False),
                     (u"Cancel", u"cancel", False)],
                    warnings=_staged_warnings(items, fitems), owner=win)

    def _guard_staged(items, fitems, proceed, on_cancel=None):
        """Run `proceed()` -- an action that would lose `items` / `fitems` --
        after asking. Nothing staged: just go. Apply: write them first, then go
        (and if nothing was written, `on_cancel`). Discard: go (`proceed` drops
        what it must). Cancel: `on_cancel` and stay."""
        if not items and not fitems:
            proceed()
            return
        choice = _ask_staged(items, fitems)
        if choice == u"apply":
            _apply_items(items, fitems, after=proceed, on_fail=on_cancel)
        elif choice == u"discard":
            proceed()
        elif on_cancel is not None:
            on_cancel()

    def _ticked_ids():
        return set(t.id_val for t in tpl_rows if t._checked)

    def _set_ticks(ids):
        """Make exactly `ids` the ticked templates (no rebuild: callers decide)."""
        for t in tpl_rows:
            want = t.id_val in ids
            if t._checked != want:
                t._checked = want
                t._raise("Checked")
        for v in vtype_roots:
            v._raise("Checked")

    def on_tpl_ticked(s, e):
        now = _ticked_ids()
        lost = [t for t in tpl_rows if t.id_val not in now and _has_staged(t.id_val)]
        if not lost:
            state["ticked"] = now
            _after_tpl_change()
            return
        items, fitems = _staged_for(set(t.id_val for t in lost))
        before = set(state["ticked"])

        # The click is undone UNTIL the user decides: the whole set that was
        # ticked before it comes back (a ViewType group takes its untouched
        # templates with it, not only the ones that had staged changes). Discard
        # or a successful Apply then make the click real; Cancel, a failed Apply
        # or an Apply job that never runs leave the window as it was.
        _set_ticks(before)

        def proceed():
            _set_ticks(now)
            state["ticked"] = now
            _after_tpl_change()

        _guard_staged(items, fitems, proceed, on_cancel=_update_footer)

    def on_focus(s, e):
        """Collapse tab: slide the template pane away (and its splitter) so
        the matrix gets every pixel, or bring it back at the width the user
        had, drag included. Ticks and columns are untouched, layout only."""
        if not focus["on"]:
            focus["width"] = GridLength(colLeft.ActualWidth)
            colLeft.MinWidth = 0
            colLeft.Width = GridLength(0)
            splitter.Visibility = GONE       # the gutter stays: the tab lives there
            btnFocus.Content = u"▸"
            btnFocus.ToolTip = u"Show the view templates"
            focus["on"] = True
        else:
            colLeft.Width = focus["width"] or GridLength(420)
            colLeft.MinWidth = focus["minw"]
            splitter.Visibility = VIS
            btnFocus.Content = u"◂"
            btnFocus.ToolTip = u"Hide the view templates"
            focus["on"] = False

    def on_tpl_all(s, e):
        """Clear selection of the template tree: untick every template, so
        every column goes away. Feedback of 09-18: "putting All on the
        templates is a bit much" -- 76 columns is never what anyone wants, so
        the bar offers only the way back. Same path a manual untick takes
        (drop pendings, columns, footer) -- and, since QA 2026-10-02, the same
        question when something is staged."""
        ticked = [t for t in tpl_rows if t._checked]
        if not ticked:
            return

        def clear():
            _set_ticks(set())
            state["ticked"] = set()
            _after_tpl_change()

        items, fitems = _staged_for(set(t.id_val for t in ticked))
        _guard_staged(items, fitems, clear)

    def on_on_sheets_toggled(s, e):
        # Combo enabled follows the checkbox but keeps whatever was already
        # selected -- unchecking and re-checking should not reset the print
        # set the user picked.
        cmbPrintSet.IsEnabled = bool(chkOnSheets.IsChecked) and bool(set_names)
        _rebind_tpl()

    def on_print_set_changed(s, e):
        _rebind_tpl()

    def on_cat_all(s, e):
        roots = _active_roots()
        q = (txtCatFilter.Text or u"").strip().lower()
        disc = _active_disc()
        nodes = _cat_tickable(roots, q, disc)
        turn_on = not (nodes and all(n.picked for n in nodes))
        _QUIET["on"] = True
        try:
            for n in nodes:
                n._set_checked(turn_on)
        finally:
            _QUIET["on"] = False
        for n in _cat_visible(roots, q, disc):
            n._raise("Checked")
        _update_footer()

    timer_cat = DispatcherTimer()
    timer_cat.Interval = TimeSpan.FromMilliseconds(180)

    def on_cat_filter_tick(s, e):
        timer_cat.Stop()
        _rebind_cat()

    def on_cat_filter_typed(s, e):
        timer_cat.Stop()
        timer_cat.Start()

    timer_cat.Tick += on_cat_filter_tick

    timer_tpl = DispatcherTimer()
    timer_tpl.Interval = TimeSpan.FromMilliseconds(180)

    def on_tpl_filter_tick(s, e):
        timer_tpl.Stop()
        _rebind_tpl()

    def on_tpl_filter_typed(s, e):
        timer_tpl.Stop()
        timer_tpl.Start()

    timer_tpl.Tick += on_tpl_filter_tick

    btnTabModel.Click += on_tab_model
    btnTabAnno.Click += on_tab_anno
    btnTabFilters.Click += on_tab_filters
    txtCatFilter.TextChanged += on_cat_filter_typed
    def on_cat_fold(s, e):
        nodes = _cat_foldable()
        expand = any(not n._expanded for n in nodes)
        if expand and state["tab"] == u"Filters":
            # The one exception to the accordion: Expand all opens every
            # filter row (to compare). The next single expand goes back to one.
            _ACC["Filters"].bulk_expand(nodes)
        else:
            _fold_all(nodes, expand)
        _rebind_cat()

    def on_tpl_fold(s, e):
        nodes = _tpl_foldable()
        _fold_all(nodes, any(not n._expanded for n in nodes))
        _rebind_tpl()

    def on_refresh(s, e):
        """The manual way to re-read the model (the window already follows
        Revit by itself): forget every cache entry and read what the active tab
        needs again. Staged changes stay."""
        burst.take()
        _invalidate(everything=True)
        _schedule_reads()

    btnCatAll.Click += on_cat_all
    btnCatFold.Click += on_cat_fold
    btnRefresh.Click += on_refresh
    gridCat.SizeChanged += _fit_columns
    btnTplFold.Click += on_tpl_fold
    btnTplAll.Click += on_tpl_all
    btnFocus.Click += on_focus
    gridCat.AddHandler(UIElement.MouseLeftButtonUpEvent,
                       MouseButtonEventHandler(on_cat_grid_click), True)
    gridCat.AddHandler(_CheckBox.ClickEvent, RoutedEventHandler(on_cat_ticked), True)
    txtTplFilter.TextChanged += on_tpl_filter_typed
    chkOnSheets.Checked += on_on_sheets_toggled
    chkOnSheets.Unchecked += on_on_sheets_toggled
    cmbPrintSet.SelectionChanged += on_print_set_changed

    def on_discipline_changed(s, e):
        _rebind_cat()

    cmbDiscipline.SelectionChanged += on_discipline_changed
    gridTpl.AddHandler(UIElement.MouseLeftButtonUpEvent,
                       MouseButtonEventHandler(on_tpl_tree_click), True)
    gridTpl.AddHandler(_CheckBox.ClickEvent, RoutedEventHandler(on_tpl_ticked), True)
    def on_rows(s, e):
        """Left click opens the same menu the right click does, above the
        button, so it reads as a dropdown and not as a context menu."""
        mnuRows.PlacementTarget = btnRows
        mnuRows.Placement = PlacementMode.Top
        mnuRows.IsOpen = True

    btnRows.Click += on_rows
    btnShowAll.Click += on_show_all
    btnHideAll.Click += on_hide_all
    btnOverride.Click += on_override
    btnResetOvr.Click += on_reset_ovr
    btnFRemove.Click += on_f_remove
    btnFEnable.Click += on_f_enable
    btnFDisable.Click += on_f_disable
    btnFCopy.Click += on_f_copy
    btnApply.Click += on_apply
    btnClose.Click += lambda s, e: win.Close()

    _rebind_tpl()             # left picker: tab-independent, built once
    _activate_tab(u"Model")   # tab style + matrix rows + reads + footer

    def on_closed(s, e):
        # Module state that outlives the window: the rows hook points at this
        # window's closures, and the accordions hold its rows.
        _ROWS_REQ["fn"] = None
        _AUTO["on"] = True
        for acc in _ACC.values():
            acc.reset()

    win.Closed += on_closed

    def on_closing(s, e):
        """Close and the window's X both end here (Window.Close raises Closing).
        With changes staged: Apply (write them, THEN close), Discard (close),
        Cancel (stay)."""
        items = list(_PENDING.values())
        fitems = list(_FPENDING.values())
        if not items and not fitems:
            return
        if time.time() - state["closing_at"] < 5.0:
            e.Cancel = True          # an Apply chosen a moment ago is still on its way
            return
        choice = _ask_staged(items, fitems)
        if choice == u"cancel":
            e.Cancel = True
        elif choice == u"discard":
            _PENDING.clear()         # nothing was ever written; let it close
            _FPENDING.clear()
        else:
            # Writing needs an API context: this close is cancelled now and the
            # job closes the window itself once it has written.
            e.Cancel = True
            state["closing_at"] = time.time()
            _apply_items(items, fitems, after=win.Close,
                         on_fail=lambda: state.update(closing_at=0.0))

    win.Closing += on_closing

    # -- The window follows the model: Application.DocumentChanged marks the
    # templates / filters it lists that changed (an Undo, an edit in V/G, a
    # sync), a timer coalesces the burst, and ONE tick drops their cache entries
    # and queues ONE read (`_schedule_reads`, through modeless.run). The event
    # handler itself only looks at ids; it never touches the model.
    live = {"closed": False}
    burst = _Burst()
    watched_tpl = set(t.id_val for t in tpl_rows)
    watched_flt = set(r.id_val for r in filter_rows)
    timer_doc = DispatcherTimer()
    timer_doc.Interval = TimeSpan.FromMilliseconds(DOC_DEBOUNCE_MS)

    def on_doc_tick(s, e):
        timer_doc.Stop()
        if live["closed"]:
            return
        tpl_ids, flt_ids = burst.take()
        if not tpl_ids and not flt_ids:
            return
        _invalidate(tpl_ids, flt_ids)
        _schedule_reads()

    timer_doc.Tick += on_doc_tick

    def on_doc_changed(sender, args):
        try:
            if live["closed"] or _SELF["n"]:
                return
            try:
                if not args.GetDocument().Equals(doc):
                    return
            except Exception:
                pass
            tpl_hit, flt_hit = _hit_ids(args, watched_tpl, watched_flt)
            if not tpl_hit and not flt_hit:
                return
            burst.add(tpl_hit, flt_hit)
            timer_doc.Stop()        # restart: the burst is over 400 ms after its last event
            timer_doc.Start()
        except Exception:
            pass                    # an event handler must never throw into Revit

    dc_app = None
    dc_handler = None

    def on_live_closed(s, e):
        live["closed"] = True
        timer_doc.Stop()
        if dc_app is not None:
            try:
                dc_app.DocumentChanged -= dc_handler
            except Exception:
                pass

    win.Closed += on_live_closed

    modeless.show(win, TITLE, doc=doc)
    # Subscribed AFTER show(): if the window never appears there is nothing
    # to unsubscribe (Closed would never fire to do it).
    try:
        dc_app = doc.Application
        try:
            from System import EventHandler
            from Autodesk.Revit.DB.Events import DocumentChangedEventArgs
            dc_handler = EventHandler[DocumentChangedEventArgs](on_doc_changed)
        except Exception:
            dc_handler = on_doc_changed     # IronPython converts the function itself
        dc_app.DocumentChanged += dc_handler
    except Exception:
        dc_app = None                       # no live follow; Refresh still works
    txtCatFilter.Focus()
