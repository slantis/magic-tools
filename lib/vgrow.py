# -*- coding: utf-8 -*-
"""vgrow -- the single OverrideGraphicSettings editor for Magic Tools.

Lifted 2026-09-16 from Inspect View Overrides (`Favorites.panel/Inspect View
Overrides.pushbutton`), which had the most complete copy of
this editor in the extension. Any tool that lets the user build or read an
`OverrideGraphicSettings` -- element overrides, category overrides, filter
overrides -- should import this module instead of growing its own copy.

Consumers so far (2026-09-19, after step 7 of the View Template Manager
work retired two older tools):
  - Inspect View Overrides.pushbutton  -- uses `show_edit_dialog` (the
    editor), `extra_visibility=False` (default): it edits element overrides,
    which have no separate visibility flag. Stays on `.ogs` -- see step 8b
    below.
  - View Template Manager.pushbutton (window in lib/vtm.py, replaced
    an older category tool on 2026-09-19) -- `read_tags` for the cells,
    `show_edit_dialog` + `apply_intents` for category and filter overrides
    across N view templates (step 8b, 2026-09-20); the Filters tab passes
    `extra_visibility` through its own state chip instead.
  - Create Type Filter.pushbutton (step 6, 2026-09-19; migrated to
    `apply_intents` in step 8b) -- `show_edit_dialog(extra_visibility=True)`
    on a filter it has just created; it dropped its own ~330-line copy of
    the dialog.
  - Other tools (not in this repo; migrated to `apply_intents` in step 8b)
    -- same call on the templates that carry a tracked filter.

Embeddable editor (step 8a, 2026-09-19)
-----------------------------------------
`show_edit_dialog` is a modal, own-window dialog -- fine for a caller that
just needs one field edited, but a tool that wants the editor sitting INSIDE
its own single window (one picker + one editor, no extra hop) cannot host a
whole other Window inside a Grid cell. Two new public pieces split what
`show_edit_dialog` used to do in one function:
  - `editor_xaml(extra_visibility=False)` -- the editor's XAML fragment
    (`_EDIT_BODY`, plus the visibility row when asked), for a caller to paste
    into ITS OWN body string, e.g.
    `MY_BODY.replace(u"<!-- EDITOR -->", vgrow.editor_xaml(True))`. The
    `x:Name`s inside are fixed (`chkHalftone`, `cmbProjWeight`, ...) --
    exactly one editor fragment per window.
  - `attach(win, prefill_ogs=None, prefill_many=None, extra_visibility=False,
    doc=None)` -- does every `FindName` on `win`, fills the pattern combos
    (via `_resolve_doc(doc)`), wires every handler and prefills, returning an
    `Editor` controller (`.fill(ogs)`, `.fill_many(ogs_list)`, `.reset()`,
    `.build() -> EditResult or None`).
  - `show_edit_dialog` is now `editor_xaml` + `attach` wearing their own
    window and footer -- same signature, same return contract as always.
  - Create Type Filter.pushbutton (step 8a, 2026-09-19) -- embeds the
    editor via `editor_xaml`/`attach` in its single-window redesign, instead
    of opening `show_edit_dialog` as a second modal stop.

Tri-state intents (step 8b, 2026-09-20)
-----------------------------------------
Until this step the editor only had TWO effective states per property:
blank (never touched) or a value (set). `merge_into` inferred "the user
touched this" by comparing the built OGS against an empty
`OverrideGraphicSettings()` -- so there was no way to express "clear an
override that is already set" (unticking Halftone on a category that has it
ON reads identically to never having opened the dialog), and no way for a
multi-template edit to show that the templates in scope already disagree on
a field.

`Editor` now knows a real THIRD state, UNTOUCHED, on top of SET and CLEAR:
  - `EditResult.intents` -- a dict of 21 keys (one per OverrideGraphicSettings
    property), `{key: (u'set', value)}` or `{key: (u'clear', None)}`. A key
    that is UNTOUCHED is simply absent from the dict.
  - `apply_intents(target_ogs, intents)` -- the new way to turn that dict
    into a concrete `OverrideGraphicSettings`: copies `target_ogs`, and for
    every key present, either calls the setter with the given value (`set`)
    or with the property's value on a fresh blank `OverrideGraphicSettings()`
    (`clear` -- by construction whatever Revit itself considers "no
    override", never a guessed sentinel). Absent keys are left alone.
  - Multi mode (`prefill_many=[...]` instead of `prefill_ogs=`): every
    combo gets a `<varies>` item inserted at index 0, every checkbox becomes
    three-state (`IsChecked = None` = untouched), the six color swatches
    gain a `<varies>` sentinel state, and `txtTransp` shows `<varies>` when
    the values in scope differ. `fill_many([ogs, ...])` loads it: a
    property where every item in the list agrees loads that value (same as
    `fill`); where they differ, the control shows the neutral/varies state.
    A single-element list is exactly `fill`.
  - Single mode is unchanged in spirit (no `<varies>` state is reachable),
    but every field now resolves to an explicit `set`/`clear` at `build()`
    time instead of silently matching blank -- which is what makes clearing
    an existing override possible. The six swatches gained a small "X" in
    their corner (visible only when a real color is set) to go back to
    `<by cat>` explicitly -- the gap `merge_into`'s docstring names below.
  - `.ogs` (built by `_build_ogs`) is kept on `EditResult` for callers that
    have not migrated (Inspect View). It is best-effort in multi mode
    (an untouched/varies field degrades to whatever its blank value is) --
    multi-mode callers must read `.intents`, never `.ogs`.

Fill patterns -- drafting only
-------------------------------
`OverrideGraphicSettings.Set*Overrides` (unlike `Set*PatternId`) throws
"Fill pattern must be a drafting pattern" if handed a model pattern's Id. The
combo below only ever lists drafting patterns. The Inspect View Overrides
copy this was lifted from did NOT filter (it tagged [D]/[M] instead); that
was a live bug and is fixed here, not preserved.

This module is imported by tools running on a persistent engine
(`__persistentengine__ = True`), so every caller must `reload(vgrow)` after
import -- a persistent engine keeps `sys.modules` across clicks, and without
the reload a script would keep calling yesterday's module until Revit
restarts. `vgrow` holds no live registry (no open windows, no ExternalEvent),
so a plain reload is enough -- unlike `lib/modeless.py`, which has to save and
restore its `_OPEN`/`_EVENT`/`_HANDLER` globals across a reload.
"""

import clr

from pyrevit import revit, script

clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')
clr.AddReference('System.Windows.Forms')   # ColorDialog -- deliberate exception
clr.AddReference('System.Drawing')         # SDColor

from System import Array
from System.Windows import Visibility, Thickness
from System.Windows.Media import SolidColorBrush, Color as WpfColor, ColorConverter
from System.Windows.Forms import ColorDialog, DialogResult as WFDialogResult
from System.Drawing import Color as SDColor

from Autodesk.Revit.DB import (
    FilteredElementCollector, ElementId,
    OverrideGraphicSettings, Color as RevitColor,
    LinePatternElement, FillPatternElement, FillPatternTarget,
    ViewDetailLevel,
)

from slantisui import ui


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WEIGHT_BY_CATEGORY = -1
INVALID_ID         = ElementId.InvalidElementId

_LINE_WEIGHTS   = ['<By Category>'] + [str(i) for i in range(1, 17)]
_DETAIL_LEVELS  = (ViewDetailLevel.Undefined, ViewDetailLevel.Coarse,
                   ViewDetailLevel.Medium,    ViewDetailLevel.Fine)

_COLOR_KEYS = ('proj', 'cut', 'sfg', 'sbg', 'cfg', 'cbg')

# Sentinel for "the templates/cells in scope disagree on this color" (multi
# mode only). A plain module-level object, never confused with `None`
# (="<by cat>"/clear) or a real `Color` -- identity-compared everywhere
# (`is _VARIES`), never `==`.
_VARIES = object()


def _hex_to_brush(hex_str):
    h = hex_str.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return SolidColorBrush(WpfColor.FromRgb(r, g, b))


# Fallback swatch fill for "<by category>" (no override color set) -- pulled
# from the library's card token, never a hardcoded hex.
_DEFAULT_SWATCH_BG = _hex_to_brush(ui.CARD_BG)


# ---------------------------------------------------------------------------
# Read-only summary -- which overrides are set on this OGS
# ---------------------------------------------------------------------------

def read_tags(ogs):
    """Return a list of human tags describing which overrides are set.

    Moved from an older category tool (`_override_tags`) 2026-09-16,
    renamed. Read-only: never writes to the model.
    """
    tags = []

    try:
        if (ogs.ProjectionLineColor.IsValid or ogs.ProjectionLineWeight != WEIGHT_BY_CATEGORY
                or ogs.ProjectionLinePatternId != INVALID_ID):
            tags.append(u"Projection lines")
    except Exception:
        pass

    try:
        if (ogs.CutLineColor.IsValid or ogs.CutLineWeight != WEIGHT_BY_CATEGORY
                or ogs.CutLinePatternId != INVALID_ID):
            tags.append(u"Cut lines")
    except Exception:
        pass

    try:
        if (ogs.SurfaceForegroundPatternColor.IsValid
                or ogs.SurfaceForegroundPatternId != INVALID_ID
                or not ogs.IsSurfaceForegroundPatternVisible
                or ogs.SurfaceBackgroundPatternColor.IsValid
                or ogs.SurfaceBackgroundPatternId != INVALID_ID
                or not ogs.IsSurfaceBackgroundPatternVisible):
            tags.append(u"Surface pattern")
    except Exception:
        pass

    try:
        if (ogs.CutForegroundPatternColor.IsValid
                or ogs.CutForegroundPatternId != INVALID_ID
                or not ogs.IsCutForegroundPatternVisible
                or ogs.CutBackgroundPatternColor.IsValid
                or ogs.CutBackgroundPatternId != INVALID_ID
                or not ogs.IsCutBackgroundPatternVisible):
            tags.append(u"Cut pattern")
    except Exception:
        pass

    try:
        if ogs.Transparency and ogs.Transparency > 0:
            tags.append(u"Transparency {}%".format(ogs.Transparency))
    except Exception:
        pass

    try:
        if ogs.Halftone:
            tags.append(u"Halftone")
    except Exception:
        pass

    try:
        if ogs.DetailLevel != ViewDetailLevel.Undefined:
            tags.append(u"Detail level: {}".format(ogs.DetailLevel))
    except Exception:
        pass

    return tags


# ---------------------------------------------------------------------------
# Pattern lookups
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The document, which `revit.doc` stops answering mid-run
# ---------------------------------------------------------------------------
# `revit.doc` is not a stored value: it reads the `__revit__` builtin every
# time, and Magic Tools' FailuresProcessing hook swaps that builtin from
# UIApplication to Application at the end of EVERY transaction. From the first
# commit onwards `revit.doc` is None for the rest of the run (measured
# 2026-09-17).
#
# That is fatal here, because a null document into a FilteredElementCollector
# throws before the editor even draws. It bit Create Type Filter on
# 2026-09-19, which creates its filter in a Transaction and only then opens
# this editor, and another tool carries the same shape further down its flow.
#
# So the caller passes the `doc` it captured in its own header, before any
# transaction, which is the remedy the gotcha prescribes. `_LAST_DOC` is the
# net for the consumers that open this editor BEFORE writing anything and
# reopen it afterwards (Inspect View Overrides, the View Template Manager
# window):
# the first open resolves and remembers, the ones after the commit fall back.
# A reload of this module clears it, so it never outlives a click.

_LAST_DOC = None


def _resolve_doc(doc=None):
    global _LAST_DOC
    if doc is None:
        doc = revit.doc
    if doc is None:
        doc = _LAST_DOC
    if doc is None:
        raise Exception(
            "vgrow: no active document. `revit.doc` reads the __revit__ "
            "builtin, which is swapped at the end of every Transaction -- "
            "pass doc= from the reference the tool captured in its header.")
    _LAST_DOC = doc
    return doc


def get_line_patterns(doc=None):
    doc = _resolve_doc(doc)
    items = [('<By Category>', INVALID_ID)]
    pats  = sorted(
        list(FilteredElementCollector(doc).OfClass(LinePatternElement)),
        key=lambda p: p.Name.lower()
    )
    for p in pats:
        items.append((p.Name, p.Id))
    return items


def _is_drafting_pattern(fp):
    # OverrideGraphicSettings.Set*Overrides rejects model patterns with
    # "Fill pattern must be a drafting pattern" -- only drafting patterns work.
    try:
        return fp.GetFillPattern().Target == FillPatternTarget.Drafting
    except Exception:
        return False


def get_fill_patterns(doc=None):
    doc = _resolve_doc(doc)
    items = [('<By Category>', INVALID_ID)]
    pats  = [p for p in FilteredElementCollector(doc).OfClass(FillPatternElement)
             if _is_drafting_pattern(p)]
    pats.sort(key=lambda p: p.Name.lower())
    for p in pats:
        items.append((p.Name, p.Id))
    return items


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def revit_to_wpf_brush(c):
    if c is None or not c.IsValid:
        return _DEFAULT_SWATCH_BG
    return SolidColorBrush(WpfColor.FromRgb(c.Red, c.Green, c.Blue))


def sd_color_to_revit(c):
    return RevitColor(c.R, c.G, c.B)


# ---------------------------------------------------------------------------
# Merging a partial edit into someone else's OGS
# ---------------------------------------------------------------------------

def _color_eq(a, b):
    av = a is not None and a.IsValid
    bv = b is not None and b.IsValid
    if not av and not bv:
        return True
    if av != bv:
        return False
    return a.Red == b.Red and a.Green == b.Green and a.Blue == b.Blue


def merge_into(target_ogs, edited_ogs, base_ogs):
    """Return a NEW OverrideGraphicSettings = `target_ogs` with only the
    properties where `edited_ogs` differs from `base_ogs` applied on top.

    Why this exists (step 2, 2026-09-16): `build_ogs` (above) sets halftone,
    transparency, detail level and the four `Set*PatternVisible` flags
    UNCONDITIONALLY -- whether the user touched them or not, because the
    dialog has no per-field "leave alone" state yet (that is step 4's job).
    A caller applying to N view templates at once passes the SAME empty
    `OverrideGraphicSettings()` as both the dialog's prefill and `base_ogs`
    here: whatever came back identical to that blank slate was never touched
    by the user, so it must not overwrite whatever a given template already
    had. Comparing colors by validity+components (never by object identity)
    and ElementIds with direct `==`/`!=` (no `.IntegerValue`, no
    `ElementId(<int>)`: neither works on every Revit version).

    Kept as-is (step 8b, 2026-09-20): still used verbatim by any caller that
    has not migrated. `apply_intents` (below) is the new path -- it reads a
    real tri-state (untouched/set/clear) off `EditResult.intents` instead of
    inferring "touched" by diffing against blank, which is what let this
    function's callers clear an override that was already set.
    """
    result = OverrideGraphicSettings(target_ogs)   # copy ctor, confirmed on
                                                    # RevitAPI 2022 (reflection)

    # Projection lines
    if not _color_eq(edited_ogs.ProjectionLineColor, base_ogs.ProjectionLineColor):
        result.SetProjectionLineColor(edited_ogs.ProjectionLineColor)
    if edited_ogs.ProjectionLineWeight != base_ogs.ProjectionLineWeight:
        result.SetProjectionLineWeight(edited_ogs.ProjectionLineWeight)
    if edited_ogs.ProjectionLinePatternId != base_ogs.ProjectionLinePatternId:
        result.SetProjectionLinePatternId(edited_ogs.ProjectionLinePatternId)

    # Cut lines
    if not _color_eq(edited_ogs.CutLineColor, base_ogs.CutLineColor):
        result.SetCutLineColor(edited_ogs.CutLineColor)
    if edited_ogs.CutLineWeight != base_ogs.CutLineWeight:
        result.SetCutLineWeight(edited_ogs.CutLineWeight)
    if edited_ogs.CutLinePatternId != base_ogs.CutLinePatternId:
        result.SetCutLinePatternId(edited_ogs.CutLinePatternId)

    # Surface pattern -- foreground
    if edited_ogs.IsSurfaceForegroundPatternVisible != base_ogs.IsSurfaceForegroundPatternVisible:
        result.SetSurfaceForegroundPatternVisible(edited_ogs.IsSurfaceForegroundPatternVisible)
    if not _color_eq(edited_ogs.SurfaceForegroundPatternColor, base_ogs.SurfaceForegroundPatternColor):
        result.SetSurfaceForegroundPatternColor(edited_ogs.SurfaceForegroundPatternColor)
    if edited_ogs.SurfaceForegroundPatternId != base_ogs.SurfaceForegroundPatternId:
        result.SetSurfaceForegroundPatternId(edited_ogs.SurfaceForegroundPatternId)

    # Surface pattern -- background
    if edited_ogs.IsSurfaceBackgroundPatternVisible != base_ogs.IsSurfaceBackgroundPatternVisible:
        result.SetSurfaceBackgroundPatternVisible(edited_ogs.IsSurfaceBackgroundPatternVisible)
    if not _color_eq(edited_ogs.SurfaceBackgroundPatternColor, base_ogs.SurfaceBackgroundPatternColor):
        result.SetSurfaceBackgroundPatternColor(edited_ogs.SurfaceBackgroundPatternColor)
    if edited_ogs.SurfaceBackgroundPatternId != base_ogs.SurfaceBackgroundPatternId:
        result.SetSurfaceBackgroundPatternId(edited_ogs.SurfaceBackgroundPatternId)

    # Cut pattern -- foreground
    if edited_ogs.IsCutForegroundPatternVisible != base_ogs.IsCutForegroundPatternVisible:
        result.SetCutForegroundPatternVisible(edited_ogs.IsCutForegroundPatternVisible)
    if not _color_eq(edited_ogs.CutForegroundPatternColor, base_ogs.CutForegroundPatternColor):
        result.SetCutForegroundPatternColor(edited_ogs.CutForegroundPatternColor)
    if edited_ogs.CutForegroundPatternId != base_ogs.CutForegroundPatternId:
        result.SetCutForegroundPatternId(edited_ogs.CutForegroundPatternId)

    # Cut pattern -- background
    if edited_ogs.IsCutBackgroundPatternVisible != base_ogs.IsCutBackgroundPatternVisible:
        result.SetCutBackgroundPatternVisible(edited_ogs.IsCutBackgroundPatternVisible)
    if not _color_eq(edited_ogs.CutBackgroundPatternColor, base_ogs.CutBackgroundPatternColor):
        result.SetCutBackgroundPatternColor(edited_ogs.CutBackgroundPatternColor)
    if edited_ogs.CutBackgroundPatternId != base_ogs.CutBackgroundPatternId:
        result.SetCutBackgroundPatternId(edited_ogs.CutBackgroundPatternId)

    # General
    if edited_ogs.Halftone != base_ogs.Halftone:
        result.SetHalftone(edited_ogs.Halftone)
    if edited_ogs.Transparency != base_ogs.Transparency:
        result.SetSurfaceTransparency(edited_ogs.Transparency)
    if edited_ogs.DetailLevel != base_ogs.DetailLevel:
        result.SetDetailLevel(edited_ogs.DetailLevel)

    return result


# ---------------------------------------------------------------------------
# Tri-state intents -- the step 8b model (untouched / set / clear)
# ---------------------------------------------------------------------------
# One row per OverrideGraphicSettings property: the key used in
# `EditResult.intents`, the property name to read a "clear" value off a
# fresh blank OGS, and the setter to call. Table-driven so `apply_intents`
# does not need 21 hand-written branches (and can't drift between them).
_INTENT_FIELDS = (
    (u'halftone',     u'Halftone',                          u'SetHalftone'),
    (u'transparency', u'Transparency',                       u'SetSurfaceTransparency'),
    (u'detail',       u'DetailLevel',                         u'SetDetailLevel'),

    (u'proj_color',   u'ProjectionLineColor',                 u'SetProjectionLineColor'),
    (u'proj_weight',  u'ProjectionLineWeight',                u'SetProjectionLineWeight'),
    (u'proj_pattern', u'ProjectionLinePatternId',              u'SetProjectionLinePatternId'),

    (u'cut_color',    u'CutLineColor',                        u'SetCutLineColor'),
    (u'cut_weight',   u'CutLineWeight',                       u'SetCutLineWeight'),
    (u'cut_pattern',  u'CutLinePatternId',                     u'SetCutLinePatternId'),

    (u'sfg_vis',      u'IsSurfaceForegroundPatternVisible',   u'SetSurfaceForegroundPatternVisible'),
    (u'sfg_color',    u'SurfaceForegroundPatternColor',        u'SetSurfaceForegroundPatternColor'),
    (u'sfg_pattern',  u'SurfaceForegroundPatternId',           u'SetSurfaceForegroundPatternId'),

    (u'sbg_vis',      u'IsSurfaceBackgroundPatternVisible',   u'SetSurfaceBackgroundPatternVisible'),
    (u'sbg_color',    u'SurfaceBackgroundPatternColor',        u'SetSurfaceBackgroundPatternColor'),
    (u'sbg_pattern',  u'SurfaceBackgroundPatternId',           u'SetSurfaceBackgroundPatternId'),

    (u'cfg_vis',      u'IsCutForegroundPatternVisible',       u'SetCutForegroundPatternVisible'),
    (u'cfg_color',    u'CutForegroundPatternColor',            u'SetCutForegroundPatternColor'),
    (u'cfg_pattern',  u'CutForegroundPatternId',                u'SetCutForegroundPatternId'),

    (u'cbg_vis',      u'IsCutBackgroundPatternVisible',       u'SetCutBackgroundPatternVisible'),
    (u'cbg_color',    u'CutBackgroundPatternColor',            u'SetCutBackgroundPatternColor'),
    (u'cbg_pattern',  u'CutBackgroundPatternId',                u'SetCutBackgroundPatternId'),
)


def apply_intents(target_ogs, intents):
    """Return a NEW OverrideGraphicSettings = `target_ogs` with every intent
    in `intents` applied on top (step 8b, 2026-09-20) -- the replacement for
    `merge_into` + a blank base_ogs.

    `intents` is `EditResult.intents`: `{key: (u'set', value)}` writes
    `value` through that property's setter; `{key: (u'clear', None)}` writes
    that SAME property's value read off a fresh blank `OverrideGraphicSettings()`
    -- by construction whatever Revit considers "no override", never a
    hand-picked sentinel (no `Color.InvalidColorValue`, no `ElementId(-1)`).
    A key absent from `intents` (untouched) is not read at all -- `target_ogs`
    keeps whatever it already had there.
    """
    result = OverrideGraphicSettings(target_ogs)   # copy ctor
    if not intents:
        return result
    blank = None
    for key, attr, setter_name in _INTENT_FIELDS:
        if key not in intents:
            continue
        kind, value = intents[key]
        setter = getattr(result, setter_name)
        if kind == u'clear':
            if blank is None:
                blank = OverrideGraphicSettings()
            setter(getattr(blank, attr))
        elif kind == u'set':
            setter(value)
    return result


# ---------------------------------------------------------------------------
# ColorDialog custom colors -- persisted across opens (QA 2026-09-24, bug 3)
# ---------------------------------------------------------------------------
# Every call built a brand-new ColorDialog(), so "Add to Custom Colors" never
# survived closing it -- the next open showed the grid empty again. Saved to
# a pyRevit config section named literally (not the default per-script one),
# so it is shared by every vgrow consumer: Inspect View, View Template
# Manager and Create Type Filter all pick from and add to the SAME custom
# colors. `ColorDialog.CustomColors` is a .NET int[] in Windows COLORREF
# format (0x00BBGGRR) -- stored as a plain Python list of ints, which is what
# pyRevit's config file can actually serialize.

_CUSTOM_COLORS_SECTION = "vgrow_color_picker"


def _load_custom_colors():
    try:
        cfg = script.get_config(_CUSTOM_COLORS_SECTION)
        saved = cfg.get_option('custom_colors', None)
        if saved:
            return [int(v) for v in saved]
    except Exception:
        pass
    return None


def _save_custom_colors(colors):
    try:
        cfg = script.get_config(_CUSTOM_COLORS_SECTION)
        cfg.custom_colors = [int(v) for v in colors]
        script.save_config()
    except Exception:
        pass


def _pick_wpf_color(current_revit_color):
    """Open OS ColorDialog and return (SDColor, RevitColor) or (None, None).

    Loads/saves CustomColors from/to `_CUSTOM_COLORS_SECTION` so the "Add to
    Custom Colors" grid survives across opens and across the three tools
    that share this editor (see the module note above).
    """
    dlg = ColorDialog()
    dlg.AllowFullOpen = True
    dlg.AnyColor      = True
    dlg.FullOpen      = True
    saved = _load_custom_colors()
    if saved:
        try:
            dlg.CustomColors = Array[int](saved)
        except Exception:
            pass
    if current_revit_color is not None and current_revit_color.IsValid:
        dlg.Color = SDColor.FromArgb(255, current_revit_color.Red,
                                     current_revit_color.Green,
                                     current_revit_color.Blue)
    is_ok = dlg.ShowDialog() == WFDialogResult.OK
    # Saved regardless of OK/Cancel: "Add to Custom Colors" commits to the
    # grid the moment it's clicked, same as native Windows behaviour, so a
    # Cancel on the final color choice should not wipe colors already added.
    try:
        _save_custom_colors(list(dlg.CustomColors))
    except Exception:
        pass
    if is_ok:
        c = dlg.Color
        return c, sd_color_to_revit(c)
    return None, None


# ---------------------------------------------------------------------------
# Edit Overrides dialog
# ---------------------------------------------------------------------------

_EDIT_BODY = """
  <ScrollViewer VerticalScrollBarVisibility="Auto">
    <StackPanel Margin="0,0,6,0">

      __VISIBILITY_BLOCK__
      <!-- GENERAL -->
      <Border x:Name="secGeneral" Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1"
              CornerRadius="4" Margin="0,0,0,10" Padding="12">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="160"/>
            <ColumnDefinition Width="*"/>
          </Grid.ColumnDefinitions>
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>
          <TextBlock Grid.Row="0" Grid.ColumnSpan="2" Text="GENERAL"
                     Foreground="#A6A199" FontSize="10" Margin="0,0,0,10"/>
          <CheckBox x:Name="chkHalftone" Grid.Row="1" Grid.ColumnSpan="2"
                    Content="Halftone" Margin="0,0,0,8"/>
          <TextBlock Grid.Row="2" Grid.Column="0" Text="Transparency (0-100):"
                     Foreground="#A6A199" VerticalAlignment="Center" Margin="0,0,0,8"/>
          <TextBox x:Name="txtTransp" Grid.Row="2" Grid.Column="1"
                   Text="0" Width="70" HorizontalAlignment="Left" Margin="0,0,0,8"/>
          <TextBlock Grid.Row="3" Grid.Column="0" Text="Detail Level:"
                     Foreground="#A6A199" VerticalAlignment="Center"/>
          <ComboBox x:Name="cmbDetail" Grid.Row="3" Grid.Column="1"
                    Width="160" HorizontalAlignment="Left"/>
        </Grid>
      </Border>

      <!-- LINES -->
      <Border x:Name="secLines" Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1"
              CornerRadius="4" Margin="0,0,0,10" Padding="12">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="90"/>
            <ColumnDefinition Width="90"/>
            <ColumnDefinition Width="8"/>
            <ColumnDefinition Width="90"/>
            <ColumnDefinition Width="8"/>
            <ColumnDefinition Width="*"/>
          </Grid.ColumnDefinitions>
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>

          <TextBlock Grid.Row="0" Grid.ColumnSpan="6" Text="LINES"
                     Foreground="#A6A199" FontSize="10" Margin="0,0,0,8"/>
          <TextBlock Grid.Row="1" Grid.Column="1" Text="COLOR"
                     Foreground="#A6A199" FontSize="10" HorizontalAlignment="Center"
                     Margin="0,0,0,4"/>
          <TextBlock Grid.Row="1" Grid.Column="3" Text="WEIGHT"
                     Foreground="#A6A199" FontSize="10" HorizontalAlignment="Center"
                     Margin="0,0,0,4"/>
          <TextBlock Grid.Row="1" Grid.Column="5" Text="PATTERN"
                     Foreground="#A6A199" FontSize="10" Margin="0,0,0,4"/>

          <TextBlock Grid.Row="2" Grid.Column="0" Text="Projection"
                     Foreground="#A6A199" VerticalAlignment="Center" Margin="0,0,0,6"/>
          <Border x:Name="swatchProjLine" Grid.Row="2" Grid.Column="1"
                  Height="26" CornerRadius="3" Cursor="Hand"
                  Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1"
                  VerticalAlignment="Center" Margin="0,0,0,6">
            <Grid>
              <TextBlock x:Name="lblProjColor" Text="&lt;by cat&gt;"
                         Foreground="#A6A199" FontSize="9"
                         HorizontalAlignment="Center" VerticalAlignment="Center"/>
              <TextBlock x:Name="xClearProj" Text="X" Foreground="#A6A199" FontSize="9"
                         FontWeight="Bold" Cursor="Hand" HorizontalAlignment="Right"
                         VerticalAlignment="Center" Margin="0,0,4,0"
                         Visibility="Collapsed"/>
            </Grid>
          </Border>
          <ComboBox x:Name="cmbProjWeight"  Grid.Row="2" Grid.Column="3" Margin="0,0,0,6"/>
          <ComboBox x:Name="cmbProjPattern" Grid.Row="2" Grid.Column="5" Margin="0,0,0,6"/>

          <TextBlock Grid.Row="3" Grid.Column="0" Text="Cut"
                     Foreground="#A6A199" VerticalAlignment="Center"/>
          <Border x:Name="swatchCutLine" Grid.Row="3" Grid.Column="1"
                  Height="26" CornerRadius="3" Cursor="Hand"
                  Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1"
                  VerticalAlignment="Center">
            <Grid>
              <TextBlock x:Name="lblCutColor" Text="&lt;by cat&gt;"
                         Foreground="#A6A199" FontSize="9"
                         HorizontalAlignment="Center" VerticalAlignment="Center"/>
              <TextBlock x:Name="xClearCut" Text="X" Foreground="#A6A199" FontSize="9"
                         FontWeight="Bold" Cursor="Hand" HorizontalAlignment="Right"
                         VerticalAlignment="Center" Margin="0,0,4,0"
                         Visibility="Collapsed"/>
            </Grid>
          </Border>
          <ComboBox x:Name="cmbCutWeight"  Grid.Row="3" Grid.Column="3"/>
          <ComboBox x:Name="cmbCutPattern" Grid.Row="3" Grid.Column="5"/>
        </Grid>
      </Border>

      <!-- SURFACE PATTERNS -->
      <Border x:Name="secSurface" Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1"
              CornerRadius="4" Margin="0,0,0,10" Padding="12">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="90"/>
            <ColumnDefinition Width="30"/>
            <ColumnDefinition Width="90"/>
            <ColumnDefinition Width="8"/>
            <ColumnDefinition Width="*"/>
          </Grid.ColumnDefinitions>
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>

          <TextBlock Grid.Row="0" Grid.ColumnSpan="5" Text="SURFACE PATTERNS"
                     Foreground="#A6A199" FontSize="10" Margin="0,0,0,8"/>
          <TextBlock Grid.Row="1" Grid.Column="1" Text="VIS"
                     Foreground="#A6A199" FontSize="10" HorizontalAlignment="Center"
                     Margin="0,0,0,4"/>
          <TextBlock Grid.Row="1" Grid.Column="2" Text="COLOR"
                     Foreground="#A6A199" FontSize="10" HorizontalAlignment="Center"
                     Margin="0,0,0,4"/>
          <TextBlock Grid.Row="1" Grid.Column="4" Text="PATTERN"
                     Foreground="#A6A199" FontSize="10" Margin="0,0,0,4"/>

          <TextBlock Grid.Row="2" Grid.Column="0" Text="Foreground"
                     Foreground="#A6A199" VerticalAlignment="Center" Margin="0,0,0,6"/>
          <CheckBox x:Name="chkSurfFgVis" Grid.Row="2" Grid.Column="1"
                    IsChecked="True" HorizontalAlignment="Center"
                    VerticalAlignment="Center" Margin="0,0,0,6"/>
          <Border x:Name="swatchSurfFg" Grid.Row="2" Grid.Column="2"
                  Height="26" CornerRadius="3" Cursor="Hand"
                  Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1"
                  VerticalAlignment="Center" Margin="0,0,0,6">
            <Grid>
              <TextBlock x:Name="lblSurfFgColor" Text="&lt;by cat&gt;"
                         Foreground="#A6A199" FontSize="9"
                         HorizontalAlignment="Center" VerticalAlignment="Center"/>
              <TextBlock x:Name="xClearSfg" Text="X" Foreground="#A6A199" FontSize="9"
                         FontWeight="Bold" Cursor="Hand" HorizontalAlignment="Right"
                         VerticalAlignment="Center" Margin="0,0,4,0"
                         Visibility="Collapsed"/>
            </Grid>
          </Border>
          <ComboBox x:Name="cmbSurfFgPattern" Grid.Row="2" Grid.Column="4"
                    Margin="4,0,0,6"/>

          <TextBlock Grid.Row="3" Grid.Column="0" Text="Background"
                     Foreground="#A6A199" VerticalAlignment="Center"/>
          <CheckBox x:Name="chkSurfBgVis" Grid.Row="3" Grid.Column="1"
                    IsChecked="True" HorizontalAlignment="Center"
                    VerticalAlignment="Center"/>
          <Border x:Name="swatchSurfBg" Grid.Row="3" Grid.Column="2"
                  Height="26" CornerRadius="3" Cursor="Hand"
                  Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1"
                  VerticalAlignment="Center">
            <Grid>
              <TextBlock x:Name="lblSurfBgColor" Text="&lt;by cat&gt;"
                         Foreground="#A6A199" FontSize="9"
                         HorizontalAlignment="Center" VerticalAlignment="Center"/>
              <TextBlock x:Name="xClearSbg" Text="X" Foreground="#A6A199" FontSize="9"
                         FontWeight="Bold" Cursor="Hand" HorizontalAlignment="Right"
                         VerticalAlignment="Center" Margin="0,0,4,0"
                         Visibility="Collapsed"/>
            </Grid>
          </Border>
          <ComboBox x:Name="cmbSurfBgPattern" Grid.Row="3" Grid.Column="4"
                    Margin="4,0,0,0"/>
        </Grid>
      </Border>

      <!-- CUT PATTERNS -->
      <Border x:Name="secCut" Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1"
              CornerRadius="4" Padding="12">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="90"/>
            <ColumnDefinition Width="30"/>
            <ColumnDefinition Width="90"/>
            <ColumnDefinition Width="8"/>
            <ColumnDefinition Width="*"/>
          </Grid.ColumnDefinitions>
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>

          <TextBlock Grid.Row="0" Grid.ColumnSpan="5" Text="CUT PATTERNS"
                     Foreground="#A6A199" FontSize="10" Margin="0,0,0,8"/>
          <TextBlock Grid.Row="1" Grid.Column="1" Text="VIS"
                     Foreground="#A6A199" FontSize="10" HorizontalAlignment="Center"
                     Margin="0,0,0,4"/>
          <TextBlock Grid.Row="1" Grid.Column="2" Text="COLOR"
                     Foreground="#A6A199" FontSize="10" HorizontalAlignment="Center"
                     Margin="0,0,0,4"/>
          <TextBlock Grid.Row="1" Grid.Column="4" Text="PATTERN"
                     Foreground="#A6A199" FontSize="10" Margin="0,0,0,4"/>

          <TextBlock Grid.Row="2" Grid.Column="0" Text="Foreground"
                     Foreground="#A6A199" VerticalAlignment="Center" Margin="0,0,0,6"/>
          <CheckBox x:Name="chkCutFgVis" Grid.Row="2" Grid.Column="1"
                    IsChecked="True" HorizontalAlignment="Center"
                    VerticalAlignment="Center" Margin="0,0,0,6"/>
          <Border x:Name="swatchCutFg" Grid.Row="2" Grid.Column="2"
                  Height="26" CornerRadius="3" Cursor="Hand"
                  Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1"
                  VerticalAlignment="Center" Margin="0,0,0,6">
            <Grid>
              <TextBlock x:Name="lblCutFgColor" Text="&lt;by cat&gt;"
                         Foreground="#A6A199" FontSize="9"
                         HorizontalAlignment="Center" VerticalAlignment="Center"/>
              <TextBlock x:Name="xClearCfg" Text="X" Foreground="#A6A199" FontSize="9"
                         FontWeight="Bold" Cursor="Hand" HorizontalAlignment="Right"
                         VerticalAlignment="Center" Margin="0,0,4,0"
                         Visibility="Collapsed"/>
            </Grid>
          </Border>
          <ComboBox x:Name="cmbCutFgPattern" Grid.Row="2" Grid.Column="4"
                    Margin="4,0,0,6"/>

          <TextBlock Grid.Row="3" Grid.Column="0" Text="Background"
                     Foreground="#A6A199" VerticalAlignment="Center"/>
          <CheckBox x:Name="chkCutBgVis" Grid.Row="3" Grid.Column="1"
                    IsChecked="True" HorizontalAlignment="Center"
                    VerticalAlignment="Center"/>
          <Border x:Name="swatchCutBg" Grid.Row="3" Grid.Column="2"
                  Height="26" CornerRadius="3" Cursor="Hand"
                  Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1"
                  VerticalAlignment="Center">
            <Grid>
              <TextBlock x:Name="lblCutBgColor" Text="&lt;by cat&gt;"
                         Foreground="#A6A199" FontSize="9"
                         HorizontalAlignment="Center" VerticalAlignment="Center"/>
              <TextBlock x:Name="xClearCbg" Text="X" Foreground="#A6A199" FontSize="9"
                         FontWeight="Bold" Cursor="Hand" HorizontalAlignment="Right"
                         VerticalAlignment="Center" Margin="0,0,4,0"
                         Visibility="Collapsed"/>
            </Grid>
          </Border>
          <ComboBox x:Name="cmbCutBgPattern" Grid.Row="3" Grid.Column="4"
                    Margin="4,0,0,0"/>
        </Grid>
      </Border>

    </StackPanel>
  </ScrollViewer>
"""

# Extra row for callers that batch-apply to N view templates through a
# filter. Filter visibility is `View.SetFilterVisibility`, not a
# member of OverrideGraphicSettings, so it cannot live in `build_ogs`. Tri-
# state because "leave as is" has to be expressible -- a plain checkbox pair
# ("hide" + "force show") is exactly the two checkboxes this step retires.
_VISIBILITY_BLOCK = """
      <Border Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1"
              CornerRadius="4" Margin="0,0,0,10" Padding="12">
        <StackPanel>
          <TextBlock Text="FILTER VISIBILITY" Foreground="#A6A199" FontSize="10"
                     Margin="0,0,0,10"/>
          <ComboBox x:Name="cmbVisibility" Width="260" HorizontalAlignment="Left"
                    SelectedIndex="0">
            <ComboBoxItem Content="Leave as is"/>
            <ComboBoxItem Content="Hide elements matching the filter"/>
            <ComboBoxItem Content="Force show (unhide if hidden)"/>
          </ComboBox>
        </StackPanel>
      </Border>
"""

_EDIT_FOOTER = """
  <Grid>
    <Button x:Name="btnReset"  Content="Reset to defaults"
            Style="{StaticResource BtnGhost}" HorizontalAlignment="Left"/>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnApply"  Content="Apply"  Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
      <Button x:Name="btnCancel" Content="Cancel" Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""


class EditResult(object):
    """What `show_edit_dialog` returns on Apply (step 2, 2026-09-16;
    `.intents` added step 8b, 2026-09-20).

    `.ogs` is the `OverrideGraphicSettings` built from the form -- kept for
    callers that have not migrated (Inspect View Overrides). In multi mode it
    is best-effort (an untouched/varies field degrades to blank): those
    callers must read `.intents` instead.
    `.intents` is the tri-state dict (see `apply_intents`'s docstring) --
    the authoritative read of the form, in both single and multi mode.
    `.visibility` is `None` unless the caller passed `extra_visibility=True`
    AND the user picked something other than "Leave as is" in the extra row:
    `False` for Hide, `True` for Force show. Filter visibility lives on
    `View.SetFilterVisibility`, not on OverrideGraphicSettings, so it can
    never be part of `.ogs`/`.intents`.
    """
    def __init__(self, ogs, visibility, intents=None):
        self.ogs        = ogs
        self.visibility = visibility
        self.intents    = intents if intents is not None else {}


def editor_xaml(extra_visibility=False):
    """Return the editor's XAML fragment, ready to paste into a caller's OWN
    body string (e.g. `MY_BODY.replace(u"<!-- EDITOR -->",
    vgrow.editor_xaml(True))`).

    `extra_visibility=True` adds the "FILTER VISIBILITY" row -- see
    `show_edit_dialog`'s docstring for who needs it and why.

    The `x:Name`s inside are fixed (`chkHalftone`, `cmbProjWeight`, ...):
    exactly ONE editor fragment per window. A second copy in the same window
    would collide on `FindName` in `attach()`. The markup is the same in
    single and multi mode -- the `<varies>` combo item, three-state
    checkboxes and the swatch "X" visibility are all wired at runtime by
    `Editor`, not baked into two versions of this string.
    """
    return (_EDIT_BODY.replace("__VISIBILITY_BLOCK__",
                                _VISIBILITY_BLOCK if extra_visibility else u"")
                       .replace("__CARD_BG__", ui.CARD_BG)
                       .replace("__CARD_BD__", ui.CARD_BD))


class Editor(object):
    """Controller wired onto a window that already has `editor_xaml()`
    pasted into its body (step 8a, 2026-09-19). Built by `attach()` --
    `show_edit_dialog` is this same class wearing its own window and footer,
    so the two can never drift apart.

    `.fill(ogs)` loads a single `OverrideGraphicSettings` into every control
    (single mode). `.fill_many(ogs_list)` loads N of them at once (multi
    mode, step 8b): a property where every item agrees loads that value;
    where they differ, the control shows the neutral/`<varies>` state. Multi
    mode is on whenever the caller passes `prefill_many=` (to `Editor`,
    `attach` or `show_edit_dialog`) instead of `prefill_ogs=` -- there is no
    separate `multi=` flag to keep in sync.
    `.reset()` reloads a blank OGS in single mode, or goes back to
    "all `<varies>`" (= untouched) in multi mode -- "touch nothing", not
    "clear everything", which is what a blank reset would do to N templates
    at once.
    `.build()` reads the form back into an `EditResult` (`.ogs` +
    `.intents`), or `None` if `SetSurfaceTransparency`/friends reject
    something (same error alert the old `on_apply` showed).
    """

    def __init__(self, win, prefill_ogs=None, prefill_many=None,
                 extra_visibility=False, doc=None):
        self.win = win
        self.extra_visibility = extra_visibility
        # Multi mode is INFERRED from prefill_many, not a separate flag --
        # keeps the public signature small (no `multi=` kwarg to forget to
        # pass).
        self._multi = prefill_many is not None
        self._off   = 1 if self._multi else 0

        self._line_pats = get_line_patterns(doc)
        self._fill_pats = get_fill_patterns(doc)

        # --- Get controls ---
        self._chk_halftone   = win.FindName("chkHalftone")
        self._txt_transp     = win.FindName("txtTransp")
        self._cmb_detail     = win.FindName("cmbDetail")

        self._cmb_proj_w     = win.FindName("cmbProjWeight")
        self._cmb_proj_p     = win.FindName("cmbProjPattern")
        self._cmb_cut_w      = win.FindName("cmbCutWeight")
        self._cmb_cut_p      = win.FindName("cmbCutPattern")

        self._chk_sfg_vis    = win.FindName("chkSurfFgVis")
        self._cmb_sfg_p      = win.FindName("cmbSurfFgPattern")
        self._chk_sbg_vis    = win.FindName("chkSurfBgVis")
        self._cmb_sbg_p      = win.FindName("cmbSurfBgPattern")

        self._chk_cfg_vis    = win.FindName("chkCutFgVis")
        self._cmb_cfg_p      = win.FindName("cmbCutFgPattern")
        self._chk_cbg_vis    = win.FindName("chkCutBgVis")
        self._cmb_cbg_p      = win.FindName("cmbCutBgPattern")

        self.cmb_visibility  = win.FindName("cmbVisibility") if extra_visibility else None

        # Swatch / label / clear-X controls, keyed by the same 6 short keys
        # used everywhere else in this class (`_COLOR_KEYS`).
        self._swatch = {
            'proj': win.FindName("swatchProjLine"),
            'cut':  win.FindName("swatchCutLine"),
            'sfg':  win.FindName("swatchSurfFg"),
            'sbg':  win.FindName("swatchSurfBg"),
            'cfg':  win.FindName("swatchCutFg"),
            'cbg':  win.FindName("swatchCutBg"),
        }
        self._lbl_c = {
            'proj': win.FindName("lblProjColor"),
            'cut':  win.FindName("lblCutColor"),
            'sfg':  win.FindName("lblSurfFgColor"),
            'sbg':  win.FindName("lblSurfBgColor"),
            'cfg':  win.FindName("lblCutFgColor"),
            'cbg':  win.FindName("lblCutBgColor"),
        }
        self._x = {
            'proj': win.FindName("xClearProj"),
            'cut':  win.FindName("xClearCut"),
            'sfg':  win.FindName("xClearSfg"),
            'sbg':  win.FindName("xClearSbg"),
            'cfg':  win.FindName("xClearCfg"),
            'cbg':  win.FindName("xClearCbg"),
        }

        # Stored Revit colors: None = "<by cat>"/clear, _VARIES = the
        # cells/templates in scope disagree (multi mode only, until filled).
        self._col = dict((k, _VARIES if self._multi else None) for k in _COLOR_KEYS)

        # --- Populate combos. `<varies>` (multi mode only) always lands at
        # index 0, so `self._off` (0 or 1) is the one number every index
        # lookup below has to subtract -- see the class docstring. ---
        self._populate(self._cmb_detail, [u'<By View>', u'Coarse', u'Medium', u'Fine'])
        self._populate(self._cmb_proj_w, _LINE_WEIGHTS)
        self._populate(self._cmb_cut_w,  _LINE_WEIGHTS)
        _line_pat_names = [nm for nm, _ in self._line_pats]
        self._populate(self._cmb_proj_p, _line_pat_names)
        self._populate(self._cmb_cut_p,  _line_pat_names)
        _fill_pat_names = [nm for nm, _ in self._fill_pats]
        self._populate(self._cmb_sfg_p, _fill_pat_names)
        self._populate(self._cmb_sbg_p, _fill_pat_names)
        self._populate(self._cmb_cfg_p, _fill_pat_names)
        self._populate(self._cmb_cbg_p, _fill_pat_names)

        # --- Three-state checkboxes, multi mode only ---
        for chk in (self._chk_halftone, self._chk_sfg_vis, self._chk_sbg_vis,
                    self._chk_cfg_vis, self._chk_cbg_vis):
            chk.IsThreeState = self._multi
            if self._multi:
                chk.IsChecked = None   # untouched -- overrides the XAML default

        # --- Swatch + clear-X handlers ---
        for key in _COLOR_KEYS:
            self._swatch[key].MouseLeftButtonDown += self._make_color_handler(key)
            self._x[key].MouseLeftButtonDown       += self._make_clear_handler(key)
            self._update_swatch(key)

        if prefill_many is not None:
            self.fill_many(prefill_many)
        elif prefill_ogs is not None:
            self.fill(prefill_ogs)

    # --- Combo population ---
    def _populate(self, cmb, items):
        if self._multi:
            cmb.Items.Add(u"<varies>")
        for it in items:
            cmb.Items.Add(it)
        cmb.SelectedIndex = 0

    # --- Swatch helpers ---
    def _update_swatch(self, key):
        swatch = self._swatch[key]
        lbl    = self._lbl_c[key]
        x      = self._x[key]
        value  = self._col[key]
        if value is _VARIES:
            swatch.Background = _DEFAULT_SWATCH_BG
            lbl.Text = u"<varies>"
            x.Visibility = Visibility.Collapsed
        elif value is not None and value.IsValid:
            swatch.Background = revit_to_wpf_brush(value)
            lbl.Text = u""
            x.Visibility = Visibility.Visible   # only state that can clear
        else:
            swatch.Background = _DEFAULT_SWATCH_BG
            lbl.Text = u"<by cat>"
            x.Visibility = Visibility.Collapsed

    def _make_color_handler(self, key):
        def handler(s, e):
            current = self._col[key]
            current_color = current if (current is not None and current is not _VARIES) else None
            sd, rc = _pick_wpf_color(current_color)
            if rc is not None:
                self._col[key] = rc
                self._update_swatch(key)
        return handler

    def _make_clear_handler(self, key):
        # The gap `merge_into` could never close (its docstring names it):
        # go from "color set" back to "<by cat>" explicitly. Marks the
        # event Handled so the swatch's own MouseLeftButtonDown (the color
        # picker) does not also fire from the same click -- the X sits
        # inside the same Border.
        def handler(s, e):
            e.Handled = True
            self._col[key] = None
            self._update_swatch(key)
        return handler

    def _set_weight(self, cmb, weight):
        if weight == WEIGHT_BY_CATEGORY:
            cmb.SelectedIndex = self._off
        else:
            try:
                cmb.SelectedIndex = self._off + _LINE_WEIGHTS.index(str(weight))
            except ValueError:
                cmb.SelectedIndex = self._off

    def _set_line_pattern(self, cmb, pid):
        if pid is None or pid == INVALID_ID:
            cmb.SelectedIndex = self._off
            return
        for i, (_, eid) in enumerate(self._line_pats):
            if eid == pid:
                cmb.SelectedIndex = self._off + i
                return
        cmb.SelectedIndex = self._off

    def _set_fill_pattern(self, cmb, pid):
        if pid is None or pid == INVALID_ID:
            cmb.SelectedIndex = self._off
            return
        for i, (_, eid) in enumerate(self._fill_pats):
            if eid == pid:
                cmb.SelectedIndex = self._off + i
                return
        cmb.SelectedIndex = self._off

    def fill(self, ogs):
        """Load `ogs` into every control -- same as the old `fill_from`.
        Also what `fill_many` delegates to for a single-element list."""
        self._chk_halftone.IsChecked = ogs.Halftone
        try:
            self._txt_transp.Text = str(int(ogs.Transparency))
        except Exception:
            self._txt_transp.Text = u"0"
        dl = ogs.DetailLevel
        if   dl == ViewDetailLevel.Coarse: self._cmb_detail.SelectedIndex = self._off + 1
        elif dl == ViewDetailLevel.Medium: self._cmb_detail.SelectedIndex = self._off + 2
        elif dl == ViewDetailLevel.Fine:   self._cmb_detail.SelectedIndex = self._off + 3
        else:                              self._cmb_detail.SelectedIndex = self._off

        # Lines
        self._col['proj'] = ogs.ProjectionLineColor if ogs.ProjectionLineColor.IsValid else None
        self._update_swatch('proj')
        self._set_weight(self._cmb_proj_w, ogs.ProjectionLineWeight)
        self._set_line_pattern(self._cmb_proj_p, ogs.ProjectionLinePatternId)

        self._col['cut'] = ogs.CutLineColor if ogs.CutLineColor.IsValid else None
        self._update_swatch('cut')
        self._set_weight(self._cmb_cut_w, ogs.CutLineWeight)
        self._set_line_pattern(self._cmb_cut_p, ogs.CutLinePatternId)

        # Surface
        self._chk_sfg_vis.IsChecked = ogs.IsSurfaceForegroundPatternVisible
        self._col['sfg'] = ogs.SurfaceForegroundPatternColor if ogs.SurfaceForegroundPatternColor.IsValid else None
        self._update_swatch('sfg')
        self._set_fill_pattern(self._cmb_sfg_p, ogs.SurfaceForegroundPatternId)

        self._chk_sbg_vis.IsChecked = ogs.IsSurfaceBackgroundPatternVisible
        self._col['sbg'] = ogs.SurfaceBackgroundPatternColor if ogs.SurfaceBackgroundPatternColor.IsValid else None
        self._update_swatch('sbg')
        self._set_fill_pattern(self._cmb_sbg_p, ogs.SurfaceBackgroundPatternId)

        # Cut
        self._chk_cfg_vis.IsChecked = ogs.IsCutForegroundPatternVisible
        self._col['cfg'] = ogs.CutForegroundPatternColor if ogs.CutForegroundPatternColor.IsValid else None
        self._update_swatch('cfg')
        self._set_fill_pattern(self._cmb_cfg_p, ogs.CutForegroundPatternId)

        self._chk_cbg_vis.IsChecked = ogs.IsCutBackgroundPatternVisible
        self._col['cbg'] = ogs.CutBackgroundPatternColor if ogs.CutBackgroundPatternColor.IsValid else None
        self._update_swatch('cbg')
        self._set_fill_pattern(self._cmb_cbg_p, ogs.CutBackgroundPatternId)

    def fill_many(self, ogs_list):
        """Load N `OverrideGraphicSettings` at once (step 8b): a property
        where every item in `ogs_list` agrees loads that value (same as
        `fill`); where they differ, the control shows the neutral/`<varies>`
        state so the user sees the disagreement instead of a false blank.
        A single-element list is exactly `fill`."""
        if not ogs_list:
            return
        if len(ogs_list) == 1:
            self.fill(ogs_list[0])
            return

        def _same(vals, eq=None):
            eq = eq or (lambda a, b: a == b)
            first = vals[0]
            for v in vals[1:]:
                if not eq(first, v):
                    return False, None
            return True, first

        # Halftone
        ok, v = _same([o.Halftone for o in ogs_list])
        self._chk_halftone.IsChecked = bool(v) if ok else None

        # Transparency
        ok, v = _same([o.Transparency for o in ogs_list])
        self._txt_transp.Text = str(int(v)) if ok else u"<varies>"

        # Detail level
        ok, v = _same([o.DetailLevel for o in ogs_list])
        if ok:
            if   v == ViewDetailLevel.Coarse: self._cmb_detail.SelectedIndex = self._off + 1
            elif v == ViewDetailLevel.Medium: self._cmb_detail.SelectedIndex = self._off + 2
            elif v == ViewDetailLevel.Fine:   self._cmb_detail.SelectedIndex = self._off + 3
            else:                             self._cmb_detail.SelectedIndex = self._off
        else:
            self._cmb_detail.SelectedIndex = 0   # <varies> row

        def _color(key, attr):
            ok, v = _same([getattr(o, attr) for o in ogs_list], eq=_color_eq)
            self._col[key] = (v if (v is not None and v.IsValid) else None) if ok else _VARIES
            self._update_swatch(key)

        def _weight(cmb, attr):
            ok, v = _same([getattr(o, attr) for o in ogs_list])
            self._set_weight(cmb, v) if ok else setattr(cmb, 'SelectedIndex', 0)

        def _line_pattern(cmb, attr):
            ok, v = _same([getattr(o, attr) for o in ogs_list])
            self._set_line_pattern(cmb, v) if ok else setattr(cmb, 'SelectedIndex', 0)

        def _fill_pattern(cmb, attr):
            ok, v = _same([getattr(o, attr) for o in ogs_list])
            self._set_fill_pattern(cmb, v) if ok else setattr(cmb, 'SelectedIndex', 0)

        def _vis(chk, attr):
            ok, v = _same([getattr(o, attr) for o in ogs_list])
            chk.IsChecked = bool(v) if ok else None

        _color('proj', 'ProjectionLineColor')
        _weight(self._cmb_proj_w, 'ProjectionLineWeight')
        _line_pattern(self._cmb_proj_p, 'ProjectionLinePatternId')

        _color('cut', 'CutLineColor')
        _weight(self._cmb_cut_w, 'CutLineWeight')
        _line_pattern(self._cmb_cut_p, 'CutLinePatternId')

        _vis(self._chk_sfg_vis, 'IsSurfaceForegroundPatternVisible')
        _color('sfg', 'SurfaceForegroundPatternColor')
        _fill_pattern(self._cmb_sfg_p, 'SurfaceForegroundPatternId')

        _vis(self._chk_sbg_vis, 'IsSurfaceBackgroundPatternVisible')
        _color('sbg', 'SurfaceBackgroundPatternColor')
        _fill_pattern(self._cmb_sbg_p, 'SurfaceBackgroundPatternId')

        _vis(self._chk_cfg_vis, 'IsCutForegroundPatternVisible')
        _color('cfg', 'CutForegroundPatternColor')
        _fill_pattern(self._cmb_cfg_p, 'CutForegroundPatternId')

        _vis(self._chk_cbg_vis, 'IsCutBackgroundPatternVisible')
        _color('cbg', 'CutBackgroundPatternColor')
        _fill_pattern(self._cmb_cbg_p, 'CutBackgroundPatternId')

    def _reset_to_varies(self):
        """Multi-mode reset target: "touch nothing", every field back to
        untouched -- NOT a blank OGS, which would stage a clear on all 21
        properties for every pair in scope."""
        self._chk_halftone.IsChecked = None
        self._txt_transp.Text = u"<varies>"
        self._cmb_detail.SelectedIndex = 0
        for cmb in (self._cmb_proj_w, self._cmb_cut_w, self._cmb_proj_p, self._cmb_cut_p,
                    self._cmb_sfg_p, self._cmb_sbg_p, self._cmb_cfg_p, self._cmb_cbg_p):
            cmb.SelectedIndex = 0
        self._chk_sfg_vis.IsChecked = None
        self._chk_sbg_vis.IsChecked = None
        self._chk_cfg_vis.IsChecked = None
        self._chk_cbg_vis.IsChecked = None
        for key in _COLOR_KEYS:
            self._col[key] = _VARIES
            self._update_swatch(key)

    def reset(self):
        """Reload a blank `OverrideGraphicSettings` in single mode (same as
        the old `on_reset`), or go back to "all untouched" in multi mode --
        and, if drawn, reset the visibility combo to "Leave as is" either
        way."""
        if self._multi:
            self._reset_to_varies()
        else:
            self.fill(OverrideGraphicSettings())
        if self.cmb_visibility is not None:
            self.cmb_visibility.SelectedIndex = 0

    # --- .ogs (best-effort in multi mode; authoritative in single mode) ---
    def _build_ogs(self):
        ogs = OverrideGraphicSettings()
        ogs.SetHalftone(bool(self._chk_halftone.IsChecked))

        try:
            transp = max(0, min(100, int(self._txt_transp.Text or '0')))
        except Exception:
            transp = 0
        ogs.SetSurfaceTransparency(transp)

        d_eff = self._cmb_detail.SelectedIndex - self._off
        ogs.SetDetailLevel(_DETAIL_LEVELS[d_eff] if d_eff >= 0 else ViewDetailLevel.Undefined)

        if self._col['proj'] not in (None, _VARIES):
            ogs.SetProjectionLineColor(self._col['proj'])
        widx = self._cmb_proj_w.SelectedIndex - self._off
        if widx > 0: ogs.SetProjectionLineWeight(int(_LINE_WEIGHTS[widx]))
        pidx = self._cmb_proj_p.SelectedIndex - self._off
        if pidx > 0: ogs.SetProjectionLinePatternId(self._line_pats[pidx][1])

        if self._col['cut'] not in (None, _VARIES):
            ogs.SetCutLineColor(self._col['cut'])
        widx = self._cmb_cut_w.SelectedIndex - self._off
        if widx > 0: ogs.SetCutLineWeight(int(_LINE_WEIGHTS[widx]))
        pidx = self._cmb_cut_p.SelectedIndex - self._off
        if pidx > 0: ogs.SetCutLinePatternId(self._line_pats[pidx][1])

        ogs.SetSurfaceForegroundPatternVisible(bool(self._chk_sfg_vis.IsChecked))
        if self._col['sfg'] not in (None, _VARIES): ogs.SetSurfaceForegroundPatternColor(self._col['sfg'])
        pidx = self._cmb_sfg_p.SelectedIndex - self._off
        if pidx > 0: ogs.SetSurfaceForegroundPatternId(self._fill_pats[pidx][1])

        ogs.SetSurfaceBackgroundPatternVisible(bool(self._chk_sbg_vis.IsChecked))
        if self._col['sbg'] not in (None, _VARIES): ogs.SetSurfaceBackgroundPatternColor(self._col['sbg'])
        pidx = self._cmb_sbg_p.SelectedIndex - self._off
        if pidx > 0: ogs.SetSurfaceBackgroundPatternId(self._fill_pats[pidx][1])

        ogs.SetCutForegroundPatternVisible(bool(self._chk_cfg_vis.IsChecked))
        if self._col['cfg'] not in (None, _VARIES): ogs.SetCutForegroundPatternColor(self._col['cfg'])
        pidx = self._cmb_cfg_p.SelectedIndex - self._off
        if pidx > 0: ogs.SetCutForegroundPatternId(self._fill_pats[pidx][1])

        ogs.SetCutBackgroundPatternVisible(bool(self._chk_cbg_vis.IsChecked))
        if self._col['cbg'] not in (None, _VARIES): ogs.SetCutBackgroundPatternColor(self._col['cbg'])
        pidx = self._cmb_cbg_p.SelectedIndex - self._off
        if pidx > 0: ogs.SetCutBackgroundPatternId(self._fill_pats[pidx][1])

        return ogs

    # --- .intents (authoritative in both modes) ---
    def _combo_intent(self, cmb, resolve_set):
        """`resolve_set(eff)` returns the SET payload for effective index
        `eff` (>= 1). Effective index 0 is CLEAR, negative (only reachable
        in multi mode, the `<varies>` row) is UNTOUCHED -- `None`."""
        eff = cmb.SelectedIndex - self._off
        if eff < 0:
            return None
        if eff == 0:
            return (u'clear', None)
        return (u'set', resolve_set(eff))

    def _check_intent(self, chk):
        v = chk.IsChecked
        if v is None:   # only reachable in multi mode (IsThreeState)
            return None
        return (u'set', bool(v))

    def _transp_intent(self):
        raw = self._txt_transp.Text
        try:
            v = max(0, min(100, int(raw or u'0')))
        except Exception:
            return None   # unparsed: "<varies>" in multi mode, or garbage
        return (u'set', v)

    def _color_intent(self, key):
        v = self._col[key]
        if v is _VARIES:
            return None
        if v is None:
            return (u'clear', None)
        return (u'set', v)

    def _build_intents(self):
        raw = {
            u'halftone':     self._check_intent(self._chk_halftone),
            u'transparency': self._transp_intent(),
            u'detail':       self._combo_intent(self._cmb_detail, lambda i: _DETAIL_LEVELS[i]),

            u'proj_color':   self._color_intent('proj'),
            u'proj_weight':  self._combo_intent(self._cmb_proj_w, lambda i: int(_LINE_WEIGHTS[i])),
            u'proj_pattern': self._combo_intent(self._cmb_proj_p, lambda i: self._line_pats[i][1]),

            u'cut_color':    self._color_intent('cut'),
            u'cut_weight':   self._combo_intent(self._cmb_cut_w, lambda i: int(_LINE_WEIGHTS[i])),
            u'cut_pattern':  self._combo_intent(self._cmb_cut_p, lambda i: self._line_pats[i][1]),

            u'sfg_vis':      self._check_intent(self._chk_sfg_vis),
            u'sfg_color':    self._color_intent('sfg'),
            u'sfg_pattern':  self._combo_intent(self._cmb_sfg_p, lambda i: self._fill_pats[i][1]),

            u'sbg_vis':      self._check_intent(self._chk_sbg_vis),
            u'sbg_color':    self._color_intent('sbg'),
            u'sbg_pattern':  self._combo_intent(self._cmb_sbg_p, lambda i: self._fill_pats[i][1]),

            u'cfg_vis':      self._check_intent(self._chk_cfg_vis),
            u'cfg_color':    self._color_intent('cfg'),
            u'cfg_pattern':  self._combo_intent(self._cmb_cfg_p, lambda i: self._fill_pats[i][1]),

            u'cbg_vis':      self._check_intent(self._chk_cbg_vis),
            u'cbg_color':    self._color_intent('cbg'),
            u'cbg_pattern':  self._combo_intent(self._cmb_cbg_p, lambda i: self._fill_pats[i][1]),
        }
        return dict((k, v) for k, v in raw.items() if v is not None)

    def build(self):
        """Read the form back into an `EditResult`, or `None` on a bad
        value -- same error alert the old `on_apply` showed, and the same
        contract: the caller only closes its window when this is not None."""
        try:
            ogs = self._build_ogs()
        except Exception as ex:
            ui.alert(u'Error building override settings: ' + str(ex), title='Error')
            return None
        intents = self._build_intents()
        visibility = None
        if self.cmb_visibility is not None:
            idx = self.cmb_visibility.SelectedIndex
            if idx == 1:
                visibility = False   # Hide elements matching the filter
            elif idx == 2:
                visibility = True    # Force show (unhide if hidden)
        return EditResult(ogs, visibility, intents)


def attach(win, prefill_ogs=None, prefill_many=None, extra_visibility=False, doc=None):
    """Wire an `Editor` onto `win`, which must already contain the fragment
    from `editor_xaml(extra_visibility)` pasted somewhere in its body.

    `prefill_many` (step 8b) switches the editor to multi mode -- pass a
    list of `OverrideGraphicSettings` instead of a single `prefill_ogs`.

    `doc` follows the same rule as `show_edit_dialog`: pass the reference the
    caller captured in its own header if the editor opens after a
    Transaction -- `revit.doc` answers None from the first commit onwards
    (see `_resolve_doc`).
    """
    return Editor(win, prefill_ogs=prefill_ogs, prefill_many=prefill_many,
                  extra_visibility=extra_visibility, doc=doc)


_FOCUS_SECTIONS = {
    u"general": u"secGeneral", u"lines": u"secLines",
    u"surface": u"secSurface", u"cut": u"secCut",
}


def _highlight_section(win, focus):
    """Mark one section of the editor (`focus`: "general", "lines",
    "surface" or "cut") as the one the caller came in through: accent
    border, a faint accent wash, and scrolled into view. Cosmetic and
    best-effort -- an unknown name, or any failure, leaves the editor exactly
    as it would have opened without it."""
    try:
        name = _FOCUS_SECTIONS.get(focus)
        sec = win.FindName(name) if name else None
        if sec is None:
            return
        sec.BorderBrush = _hex_to_brush(ui.ACCENT)
        sec.BorderThickness = Thickness(2)
        sec.Background = SolidColorBrush(
            ColorConverter.ConvertFromString(ui.ROW_SEL))
        # Scrolled into view once the window has been laid out: asking
        # before ShowDialog has nothing to scroll yet.
        def _scroll(s, e):
            try:
                sec.BringIntoView()
            except Exception:
                pass
        win.Loaded += _scroll
    except Exception:
        pass


def show_edit_dialog(prefill_ogs, owner_win=None, extra_visibility=False,
                     doc=None, prefill_many=None, focus=None):
    """
    Show the Edit Overrides dialog.

    Returns an `EditResult` (`.ogs`, `.visibility`, `.intents`) if the user
    clicks Apply, else `None` (Cancel) -- same cancel contract as before,
    only the Apply payload grew a field (step 8b, 2026-09-20: `.intents`).

    `extra_visibility=True` draws one more row, "FILTER VISIBILITY" (tri-
    state: leave as is / hide / force show), for callers that apply to a
    Revit filter (Create Type Filter, other tools, and the View Template
    Manager's Filters tab through its own state chip). Inspect View edits ELEMENT overrides,
    which have no such flag, so it calls with the default `False` and never
    sees the row.

    `prefill_many` (step 8b) switches to multi mode: pass a list of
    `OverrideGraphicSettings` (the current override of every cell in scope)
    instead of `prefill_ogs`. `prefill_ogs` stays a required positional arg
    for backward compatibility -- a multi-mode caller passes a throwaway
    blank OGS there, it is ignored once `prefill_many` is given.

    `doc` is MANDATORY for any caller that opens this editor after writing to
    the model: `revit.doc` answers None from the first commit onwards. Pass
    the reference the tool captured in its header. See `_resolve_doc`.

    `focus` (optional, 2026-10-01): "general", "lines", "surface" or "cut".
    Opens the editor with that section highlighted -- the View Template
    Manager's mini table passes the section of the row that was clicked.
    Purely visual; every other caller leaves it out and nothing changes.

    Implemented on top of `editor_xaml()` + `attach()` (step 8a, 2026-09-19)
    -- this function is now just those two wearing their own window and
    footer, so a caller that embeds the editor in its own window (Create
    Type Filter) and a caller that opens it as its own modal (everyone
    else) share the exact same field-by-field behaviour.
    """
    win = ui.parse("Edit Overrides", u"", editor_xaml(extra_visibility), _EDIT_FOOTER,
                   width=560, height=700)
    if owner_win is not None:
        try:
            win.Owner = owner_win
        except Exception:
            pass

    editor = attach(win, prefill_ogs=prefill_ogs, prefill_many=prefill_many,
                     extra_visibility=extra_visibility, doc=doc)
    if focus:
        _highlight_section(win, focus)

    btn_reset       = win.FindName("btnReset")
    btn_apply       = win.FindName("btnApply")
    btn_cancel_edit = win.FindName("btnCancel")

    result = [None]

    def on_apply(s, e):
        res = editor.build()
        if res is None:
            return
        result[0] = res
        win.Close()

    btn_apply.Click       += on_apply
    btn_reset.Click       += lambda s, e: editor.reset()
    btn_cancel_edit.Click += lambda s, e: win.Close()

    win.ShowDialog()
    return result[0]
