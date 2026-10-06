# -*- coding: utf-8 -*-

__title__ = "Inspect\nView Overrides"
__author__ = 'slantis'
__doc__ = "Lists every element in the active view that has an element-level graphic override (Override Graphics in View > By Element) or is hidden in view (Hide in View > Element). From the grid you can select or show them in the view, edit their overrides (color, weight, halftone, transparency, patterns), copy the overrides of another picked element, or clear overrides and unhide in one step. Only element-level overrides and element hides: category, filter and V/G overrides are not shown."
# The window is modeless (Revit stays usable while it is open), so the engine
# has to outlive the click: see lib/modeless.py.
__persistentengine__ = True
# Rocket mode shares ONE engine between commands and cleans it when the command
# returns, which kills every handler this script attached to the window: it stays
# painted on screen and no click does anything (diagnosed 2026-09-17). A clean
# engine keeps this run's scope alive for as long as the window lives. Not
# needed by tools whose window and handlers are built inside a lib module --
# All Magic Tools is the reference for that shape.
__cleanengine__ = True

import clr
import traceback

from pyrevit import script

from slantisui import ui
import modeless
import hostecho
import vgrow
# This tool runs on a persistent engine (see __persistentengine__ above), so
# sys.modules survives between clicks: script.py is re-read on every run but a
# lib module already imported is NOT. Without this, editing hostecho.py leaves
# the tool calling yesterday's module until Revit restarts -- which is exactly
# how the fold_by_dependents rename blew up on 2026-09-10.
try:
    reload(hostecho)
except Exception:
    pass
try:
    # vgrow holds no live registry (no open windows, no ExternalEvent), so a
    # plain reload is enough -- unlike modeless below, which has to save and
    # restore its globals across a reload.
    reload(vgrow)
except Exception:
    pass
if not hasattr(modeless, 'live'):
    # lib/modeless.py grew live()/close() on 2026-09-15 and a persistent
    # engine that imported it before still holds the old module (same gotcha
    # as hostecho above). Reload it keeping its registry and its ExternalEvent,
    # which a bare reload would wipe: the open windows would be forgotten.
    _keep = (modeless._OPEN, modeless._EVENT, modeless._HANDLER)
    reload(modeless)
    modeless._OPEN, modeless._EVENT, modeless._HANDLER = _keep

clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

import System
from System.Collections.Generic    import List as CList
from System.Collections.ObjectModel import ObservableCollection

from Autodesk.Revit.DB import (
    FilteredElementCollector, ElementId,
    OverrideGraphicSettings,
    Transaction, TransactionStatus, Element, BuiltInParameter
)
from Autodesk.Revit.UI.Selection import ObjectType
from System.Windows import Visibility
from pyrevit import HOST_APP

def _id_val(eid):
    # ElementId value compat Revit 2022-2026 (IntegerValue removed in 2026)
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


doc   = __revit__.ActiveUIDocument.Document   # noqa
uidoc = __revit__.ActiveUIDocument            # noqa

TITLE = u"Inspect View Overrides"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Single source: lib/vgrow.py -- see its docstring.
WEIGHT_BY_CATEGORY = vgrow.WEIGHT_BY_CATEGORY
INVALID_ID         = vgrow.INVALID_ID


# ---------------------------------------------------------------------------
# OGS comparison & summary helpers
# ---------------------------------------------------------------------------

def _color_eq(a, b):
    av = a.IsValid if a is not None else False
    bv = b.IsValid if b is not None else False
    if not av and not bv:
        return True
    if av != bv:
        return False
    return a.Red == b.Red and a.Green == b.Green and a.Blue == b.Blue


def _color_str(c):
    if c is None or not c.IsValid:
        return '<by cat>'
    return 'RGB({},{},{})'.format(c.Red, c.Green, c.Blue)


def is_overridden(ogs):
    d = OverrideGraphicSettings()
    if ogs.Halftone    != d.Halftone:     return True
    if ogs.Transparency != d.Transparency: return True
    if ogs.DetailLevel  != d.DetailLevel:  return True
    if ogs.ProjectionLineWeight    != d.ProjectionLineWeight:    return True
    if ogs.ProjectionLinePatternId != d.ProjectionLinePatternId: return True
    if not _color_eq(ogs.ProjectionLineColor, d.ProjectionLineColor): return True
    if ogs.CutLineWeight    != d.CutLineWeight:    return True
    if ogs.CutLinePatternId != d.CutLinePatternId: return True
    if not _color_eq(ogs.CutLineColor, d.CutLineColor): return True
    if ogs.IsSurfaceForegroundPatternVisible != d.IsSurfaceForegroundPatternVisible: return True
    if ogs.SurfaceForegroundPatternId != d.SurfaceForegroundPatternId:               return True
    if not _color_eq(ogs.SurfaceForegroundPatternColor, d.SurfaceForegroundPatternColor): return True
    if ogs.IsSurfaceBackgroundPatternVisible != d.IsSurfaceBackgroundPatternVisible: return True
    if ogs.SurfaceBackgroundPatternId != d.SurfaceBackgroundPatternId:               return True
    if not _color_eq(ogs.SurfaceBackgroundPatternColor, d.SurfaceBackgroundPatternColor): return True
    if ogs.IsCutForegroundPatternVisible != d.IsCutForegroundPatternVisible: return True
    if ogs.CutForegroundPatternId != d.CutForegroundPatternId:               return True
    if not _color_eq(ogs.CutForegroundPatternColor, d.CutForegroundPatternColor): return True
    if ogs.IsCutBackgroundPatternVisible != d.IsCutBackgroundPatternVisible: return True
    if ogs.CutBackgroundPatternId != d.CutBackgroundPatternId:               return True
    if not _color_eq(ogs.CutBackgroundPatternColor, d.CutBackgroundPatternColor): return True
    return False


def _pattern_name(pid):
    if pid is None or pid == INVALID_ID:
        return None
    el = doc.GetElement(pid)
    if el is None:
        return None
    try:
        return el.Name
    except Exception:
        return None


def summarize(ogs):
    parts = []
    d = OverrideGraphicSettings()
    if ogs.Halftone    != d.Halftone:    parts.append('Halftone')
    if ogs.Transparency != d.Transparency: parts.append('Transp={}%'.format(ogs.Transparency))
    if ogs.DetailLevel  != d.DetailLevel:  parts.append('Detail={}'.format(str(ogs.DetailLevel)))

    if (not _color_eq(ogs.ProjectionLineColor, d.ProjectionLineColor)
            or ogs.ProjectionLineWeight != d.ProjectionLineWeight
            or ogs.ProjectionLinePatternId != d.ProjectionLinePatternId):
        sub = []
        if ogs.ProjectionLineColor.IsValid: sub.append(_color_str(ogs.ProjectionLineColor))
        if ogs.ProjectionLineWeight != WEIGHT_BY_CATEGORY: sub.append('w={}'.format(ogs.ProjectionLineWeight))
        nm = _pattern_name(ogs.ProjectionLinePatternId)
        if nm: sub.append('p={}'.format(nm))
        parts.append('ProjLine[{}]'.format(', '.join(sub)))

    if (not _color_eq(ogs.CutLineColor, d.CutLineColor)
            or ogs.CutLineWeight != d.CutLineWeight
            or ogs.CutLinePatternId != d.CutLinePatternId):
        sub = []
        if ogs.CutLineColor.IsValid: sub.append(_color_str(ogs.CutLineColor))
        if ogs.CutLineWeight != WEIGHT_BY_CATEGORY: sub.append('w={}'.format(ogs.CutLineWeight))
        nm = _pattern_name(ogs.CutLinePatternId)
        if nm: sub.append('p={}'.format(nm))
        parts.append('CutLine[{}]'.format(', '.join(sub)))

    def _sfmt(tag, vis, color, pid):
        if not vis:
            parts.append(tag + ' hidden')
        elif (not _color_eq(color, d.ProjectionLineColor)  # just a non-default comparison
              or pid != INVALID_ID):
            sub = []
            if color.IsValid: sub.append(_color_str(color))
            nm = _pattern_name(pid)
            if nm: sub.append('p={}'.format(nm))
            if sub: parts.append('{}[{}]'.format(tag, ', '.join(sub)))

    _sfmt('SurfFG', ogs.IsSurfaceForegroundPatternVisible,
          ogs.SurfaceForegroundPatternColor, ogs.SurfaceForegroundPatternId)
    _sfmt('SurfBG', ogs.IsSurfaceBackgroundPatternVisible,
          ogs.SurfaceBackgroundPatternColor, ogs.SurfaceBackgroundPatternId)
    _sfmt('CutFG',  ogs.IsCutForegroundPatternVisible,
          ogs.CutForegroundPatternColor, ogs.CutForegroundPatternId)
    _sfmt('CutBG',  ogs.IsCutBackgroundPatternVisible,
          ogs.CutBackgroundPatternColor, ogs.CutBackgroundPatternId)

    return ', '.join(parts) if parts else '(none)'


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _type_name(el):
    """Name of an element TYPE, which `el.Name` cannot give you.

    On every ElementType subclass (WallType, PanelType, MullionType, ...)
    `.Name` raises `AttributeError: Name` under IronPython -- the CLR property
    is shadowed. The old code read `type_el.Name` inside a bare `except`, so
    the exception was swallowed and the TYPE column came out empty for every
    element in every project (reported 2026-09-10). Measured on a real
    storefront: `Element.Name.GetValue` gives 'Storefront' / 'Glazed' /
    '2.5" x 5" rectangular' where `.Name` raises.
    """
    if el is None:
        return ''
    try:
        return Element.Name.GetValue(el)
    except Exception:
        pass
    try:
        p = el.get_Parameter(BuiltInParameter.ALL_MODEL_TYPE_NAME)
        if p is not None:
            return p.AsString() or ''
    except Exception:
        pass
    try:
        return el.Name
    except Exception:
        return ''


class OverriddenRow(object):
    def __init__(self, el, ogs, hidden=False, folded=None):
        self.id     = el.Id
        self.ogs    = ogs
        self.hidden = hidden
        # Sub-elements Revit reports as hidden only because THIS element is
        # (curtain panels of a hidden curtain wall, and the like). They are
        # folded into this row instead of getting one each, but their ids stay
        # here so unhiding this row still reaches them. See lib/hostecho.py.
        self.folded = list(folded) if folded else []
        try:
            self.category = el.Category.Name if el.Category else '<no cat>'
        except Exception:
            self.category = '<no cat>'
        try:
            type_id    = el.GetTypeId()
            type_el    = doc.GetElement(type_id) if type_id != INVALID_ID else None
            self.type_name = _type_name(type_el)
        except Exception:
            self.type_name = ''
        try:
            self.family = el.Symbol.Family.Name
        except Exception:
            self.family = ''
        try:
            self.name = el.Name
        except Exception:
            self.name = ''

        ogs_summary = summarize(ogs)
        if hidden:
            self.summary = ('Hidden in view, ' + ogs_summary
                            if ogs_summary != '(none)' else 'Hidden in view')
        else:
            self.summary = ogs_summary
        self.summary += hostecho.suffix(len(self.folded))

    # PascalCase properties for WPF data binding
    @property
    def Category(self):   return self.category
    @property
    def FamilyOrName(self): return self.family or self.name
    @property
    def TypeName(self):   return self.type_name
    @property
    def IdStr(self):      return str(_id_val(self.id))
    @property
    def IdNum(self):
        # Sort key of the ID column: sorting the text IdStr puts 1000 before 99.
        return System.Int64(_id_val(self.id))
    @property
    def Summary(self):    return self.summary

    @property
    def all_ids(self):
        """This element plus the sub-elements folded into it."""
        return [self.id] + self.folded


def find_overridden_in_view(view):
    result = []
    seen   = set()

    coll1 = FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType()
    for el in coll1:
        try:
            ogs = view.GetElementOverrides(el.Id)
        except Exception:
            continue
        if ogs is None:
            continue
        if is_overridden(ogs):
            seen.add(_id_val(el.Id))
            result.append(OverriddenRow(el, ogs, hidden=False))

    coll2  = FilteredElementCollector(doc).WhereElementIsNotElementType()
    hidden = []
    for el in coll2:
        if _id_val(el.Id) in seen:
            continue
        try:
            if not el.IsHidden(view):
                continue
        except Exception:
            continue
        hidden.append(el)

    # One "Hide in View > Element" on a container makes every sub-element
    # answer IsHidden=True too, so a single click would otherwise become
    # hundreds of rows (measured on a real storefront: 169 children for one
    # wall -- panels, mullions AND grid lines). Fold them onto the container
    # row, keeping their ids so Unhide still works.
    hidden, folded = hostecho.fold_by_dependents(hidden)
    for el in hidden:
        try:
            ogs = view.GetElementOverrides(el.Id)
        except Exception:
            ogs = OverrideGraphicSettings()
        seen.add(_id_val(el.Id))
        result.append(OverriddenRow(el, ogs, hidden=True,
                                    folded=folded.get(_id_val(el.Id))))

    return result


# ---------------------------------------------------------------------------
# Edit Overrides dialog -- lives in lib/vgrow.py (single source, see its
# docstring). Pattern collectors, color helpers, _LINE_WEIGHTS and the WPF
# template strings moved with it; this script only calls
# vgrow.show_edit_dialog(...).
#
# Main form
# ---------------------------------------------------------------------------

_MGR_BODY = """
  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
    </Grid.RowDefinitions>

    <!-- "You are in another view" strip: hidden until the active view stops
         being the one this list was built for. -->
    <Border x:Name="bannerView" Grid.Row="0" Visibility="Collapsed"
            Margin="0,0,0,8" Padding="10,8" CornerRadius="6"
            Background="__CARD_BG__" BorderBrush="__WARN__" BorderThickness="1">
      <Grid>
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <TextBlock x:Name="lblBanner" Grid.Column="0" TextWrapping="Wrap"
                   Foreground="__WARN__" FontSize="12"
                   VerticalAlignment="Center" Margin="0,0,12,0"/>
        <StackPanel Grid.Column="1" Orientation="Horizontal">
          <Button x:Name="btnShowActive" Style="{StaticResource BtnPrimary}" Margin="0,0,6,0"/>
          <Button x:Name="btnBackToList" Style="{StaticResource BtnGhost}"/>
        </StackPanel>
      </Grid>
    </Border>

    <!-- Top bar: filter + count + refresh -->
    <Grid Grid.Row="1" Margin="0,0,0,8">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="Auto"/>
      </Grid.ColumnDefinitions>
      <StackPanel Orientation="Horizontal" Grid.Column="0">
        <TextBox x:Name="txtFilter" Width="280" Margin="0,0,12,0"
                 ToolTip="Type to filter (AND tokens)..."/>
        <TextBlock x:Name="lblCount" Foreground="#A6A199" FontSize="11"
                   VerticalAlignment="Center"/>
      </StackPanel>
      <Button x:Name="btnRefresh" Grid.Column="1" Content="Refresh"
              Style="{StaticResource BtnGhost}"/>
    </Grid>

    <!-- DataGrid -->
    <DataGrid x:Name="grid" Grid.Row="2"
              AutoGenerateColumns="False"
              CanUserAddRows="False"
              CanUserDeleteRows="False"
              CanUserSortColumns="True"
              SelectionMode="Extended"
              HeadersVisibility="Column">
      <DataGrid.Columns>
        <DataGridTextColumn Header="CATEGORY"  Width="130" Binding="{Binding Category}"   IsReadOnly="True"/>
        <DataGridTextColumn Header="FAMILY"    Width="150" Binding="{Binding FamilyOrName}" IsReadOnly="True"/>
        <DataGridTextColumn Header="TYPE"      Width="130" Binding="{Binding TypeName}"   IsReadOnly="True"/>
        <DataGridTextColumn Header="ID"        Width="75"  Binding="{Binding IdStr}"      SortMemberPath="IdNum" IsReadOnly="True"/>
        <DataGridTextColumn Header="OVERRIDES" Width="*"   Binding="{Binding Summary}"    IsReadOnly="True"/>
      </DataGrid.Columns>
    </DataGrid>
  </Grid>
""".replace("__CARD_BG__", ui.CARD_BG).replace("__WARN__", ui.STATUS_WARN)

_MGR_FOOTER = """
  <Grid>
    <StackPanel HorizontalAlignment="Left" Orientation="Horizontal" VerticalAlignment="Center">
      <Button x:Name="btnSelect" Content="Select in view" Style="{StaticResource BtnGhost}" Margin="0,0,6,0"/>
      <Button x:Name="btnShow"   Content="Show in view"   Style="{StaticResource BtnGhost}" Margin="0,0,6,0"/>
      <Button x:Name="btnEdit"   Content="Edit overrides" Style="{StaticResource BtnPrimary}" Margin="0,0,6,0"/>
      <Button x:Name="btnCopy"   Content="Copy from..."  Style="{StaticResource BtnGhost}" Margin="0,0,6,0"/>
      <Button x:Name="btnUnhide" Content="Unhide"         Style="{StaticResource BtnGhost}" Margin="0,0,6,0"/>
      <Button x:Name="btnClear"  Content="Clear &amp; unhide" Style="{StaticResource BtnGhost}"/>
    </StackPanel>
    <Button x:Name="btnClose" Content="Close" Style="{StaticResource BtnGhost}"
            HorizontalAlignment="Right"/>
  </Grid>
"""


def _view_name(view):
    try:
        return view.Name or u'<unnamed>'
    except Exception:
        return u'<unnamed>'


def _err_text(ex):
    """Text of an exception that is safe to concatenate into a unicode alert
    under IronPython 2.7. str(ex) raises UnicodeEncodeError when the message
    carries a non-ASCII char (a view name with an accent, quoted by Revit in
    its own message), and that would replace the real error with another."""
    try:
        return unicode(ex)
    except Exception:
        pass
    try:
        return str(ex).decode('utf-8', 'replace')
    except Exception:
        pass
    try:
        return repr(ex)
    except Exception:
        return u'(unprintable error)'


def _short(text, n=30):
    # Long view names would stretch the banner buttons past the window.
    text = text or u''
    return text if len(text) <= n else text[:n - 1] + u'\u2026'


# Pattern fields of an edit: (intent key, OverrideGraphicSettings property,
# which list the editor combo draws from).
_PATTERN_FIELDS = (
    (u'proj_pattern', 'ProjectionLinePatternId',        'line'),
    (u'cut_pattern',  'CutLinePatternId',               'line'),
    (u'sfg_pattern',  'SurfaceForegroundPatternId',     'fill'),
    (u'sbg_pattern',  'SurfaceBackgroundPatternId',     'fill'),
    (u'cfg_pattern',  'CutForegroundPatternId',         'fill'),
    (u'cbg_pattern',  'CutBackgroundPatternId',         'fill'),
)


def _keep_unlisted_patterns(base, intents, line_ok, fill_ok):
    """The editor's pattern combos only list what OverrideGraphicSettings can
    take from a document: the project line patterns and the DRAFTING fill
    patterns. An element whose override uses something else (the built-in
    "Solid" line pattern is the usual one) cannot be shown in the combo, which
    falls back to <By Category> and would read as "the user cleared it". So a
    'clear' on a pattern the element holds but the combo cannot list is
    dropped: Edit leaves it alone, and Clear & unhide is how to remove it.
    `line_ok` / `fill_ok` are sets of the ElementId values the combos list."""
    out = dict(intents)
    for key, attr, kind in _PATTERN_FIELDS:
        if key not in out or out[key][0] != u'clear':
            continue
        listed = line_ok if kind == 'line' else fill_ok
        if _id_val(getattr(base, attr)) not in listed:
            del out[key]
    return out


class InspectViewForm(object):
    """The window. It is pinned to ONE view (`self._view_id`, fixed when the
    list is built or rebuilt): every read and every write goes to that view,
    never to whatever `doc.ActiveView` is at the moment of the click. QA found
    (2026-10-01) that moving to another view without pressing Refresh made
    Clear & unhide land on the new view while the list still showed the old
    one. When the active view stops being the pinned one, a strip on top says
    so and offers to follow the active view or to go back."""

    def __init__(self):
        view = doc.ActiveView
        self._view_id = view.Id
        # Name of the pinned view, cached: click handlers and the ViewActivated
        # event must not call the API just to print it.
        self._view_label = _view_name(view)
        self._closed  = False
        self._uiapp   = None
        self._ev_handler = None

        self._win = ui.parse(
            TITLE,
            self._subtitle_for(view),
            _MGR_BODY, _MGR_FOOTER,
            width=1000, height=640,
        )
        # Which view this window was built for: the second-click guard at the
        # bottom of the script compares it with the active view. Kept in step
        # with _view_id by _rebind().
        self._win.Tag = _id_val(view.Id)

        self._all_rows = []
        self._rows     = []
        self._items    = ObservableCollection[OverriddenRow]()

        # Get controls
        self.banner          = self._win.FindName("bannerView")
        self.lbl_banner      = self._win.FindName("lblBanner")
        self.btn_show_active = self._win.FindName("btnShowActive")
        self.btn_back        = self._win.FindName("btnBackToList")
        self.txt_filter  = self._win.FindName("txtFilter")
        self.lbl_count   = self._win.FindName("lblCount")
        self.btn_refresh = self._win.FindName("btnRefresh")
        self.grid        = self._win.FindName("grid")
        self.btn_select  = self._win.FindName("btnSelect")
        self.btn_show    = self._win.FindName("btnShow")
        self.btn_edit    = self._win.FindName("btnEdit")
        self.btn_copy    = self._win.FindName("btnCopy")
        self.btn_unhide  = self._win.FindName("btnUnhide")
        self.btn_clear   = self._win.FindName("btnClear")
        self.btn_close   = self._win.FindName("btnClose")

        # Bind grid
        self.grid.ItemsSource = self._items

        # Wire events
        self.txt_filter.TextChanged    += lambda s, e: self._apply_filter()
        self.btn_refresh.Click         += self._on_refresh
        self.btn_show_active.Click     += self._on_show_active
        self.btn_back.Click            += self._on_back_to_list
        self.btn_select.Click          += self._on_select_in_view
        self.btn_show.Click            += self._on_show_in_view
        self.btn_edit.Click            += self._on_edit
        self.btn_copy.Click            += self._on_copy_from
        self.btn_unhide.Click          += self._on_unhide
        self.btn_clear.Click           += self._on_clear
        self.btn_close.Click           += lambda s, e: self._win.Close()
        self.grid.MouseDoubleClick     += self._on_double_click

        self._load()

    def show(self):
        self._subscribe()
        self._win.Closed += self._on_win_closed
        try:
            modeless.show(self._win, TITLE, doc=doc)
        except Exception:
            # The window never opened, so Closed will never fire: undo the
            # ViewActivated subscription here or it stays hooked for the whole
            # Revit session.
            self._on_win_closed(None, None)
            raise

    # ---- The pinned view ---------------------------------------------------

    @staticmethod
    def _subtitle_for(view):
        return u"View: {}".format(_view_name(view))

    def _view(self):
        """The view this window is pinned to, or None if it was deleted."""
        try:
            return doc.GetElement(self._view_id)
        except Exception:
            return None

    def _view_or_alert(self):
        view = self._view()
        if view is None:
            ui.alert(u'The view this list was built for no longer exists. '
                     u'Press Refresh to inspect the active view.', title=TITLE)
        return view

    def _rebind(self, view):
        """Pin the window to `view` and rebuild the list for it: id, Tag,
        subtitle (via _load) and the strip all follow."""
        self._view_id = view.Id
        self._win.Tag = _id_val(view.Id)
        self.banner.Visibility = Visibility.Collapsed
        self._load()

    def _activate(self, uiapp, view):
        """Make `view` the active one (inside an ExternalEvent, where setting
        ActiveView is legal). True when it is active afterwards."""
        uidoc = uiapp.ActiveUIDocument
        cur = uidoc.ActiveView
        if cur is not None and _id_val(cur.Id) == _id_val(view.Id):
            return True
        try:
            uidoc.ActiveView = view
            return True
        except Exception:
            pass
        # The setter is available in every supported Revit (UIDocument.ActiveView
        # is settable since 2012) but Revit refuses it in some states. The
        # fallback is asynchronous: the view changes AFTER this job returns, so
        # the action must not run now on the old view. Say so and stop.
        try:
            uidoc.RequestViewChange(view)
            ui.alert(u'Switched to "{}". Click again to run the action.'.format(
                _view_name(view)), title=TITLE)
            return False
        except Exception as ex:
            ui.alert(u'Could not switch to view "{}":\n{}'.format(
                _view_name(view), _err_text(ex)), title=TITLE)
            return False

    def _existing(self, ids):
        out = []
        for i in ids:
            try:
                if doc.GetElement(i) is not None:
                    out.append(i)
            except Exception:
                pass
        return out

    def _queue(self, fn):
        """modeless.run, plus a banner check at the start of the job: it is
        the safety net for when the ViewActivated subscription is missing."""
        def work(uiapp):
            if self._closed:
                # The window closed between the click and Revit running the job.
                return
            try:
                self._check_banner(uiapp.ActiveUIDocument.ActiveView)
            except Exception:
                pass
            fn(uiapp)
        modeless.run(work, doc=doc, title=TITLE)

    # ---- The "you are in another view" strip -------------------------------

    def _subscribe(self):
        """Follow view changes so the strip shows up the moment the user
        leaves the pinned view, not at the next click. Best effort: without
        the event, _queue() still shows it before the next action runs."""
        self._ev_handler = self._on_view_activated
        for getter in (lambda: __revit__, lambda: HOST_APP.uiapp):   # noqa
            try:
                app = getter()
                if app is not None and hasattr(app, 'ViewActivated'):
                    app.ViewActivated += self._ev_handler
                    self._uiapp = app
                    return
            except Exception:
                pass

    def _on_win_closed(self, sender, args):
        self._closed = True
        if self._uiapp is not None:
            try:
                self._uiapp.ViewActivated -= self._ev_handler
            except Exception:
                pass
            self._uiapp = None

    def _on_view_activated(self, sender, args):
        if self._closed:
            return
        try:
            self._check_banner(args.CurrentActivatedView)
        except Exception:
            pass

    def _check_banner(self, active):
        """Show the strip when `active` (a View) is not the pinned one, hide
        it otherwise. A view of another model hides it: modeless.run already
        refuses actions when another document is active."""
        show = False
        if active is not None and not self._closed:
            try:
                show = (active.Document.Equals(doc)
                        and _id_val(active.Id) != _id_val(self._view_id))
            except Exception:
                show = False
        if not show:
            self.banner.Visibility = Visibility.Collapsed
            return
        lname = self._view_label
        cname = _view_name(active)
        self.lbl_banner.Text = (
            u'You are in {cur} \u00b7 this list is from {lst} \u00b7 '
            u'actions apply to {lst}'.format(cur=cname, lst=lname))
        self.btn_show_active.Content = u'Show ' + _short(cname)
        self.btn_back.Content = u'Back to ' + _short(lname)
        self.banner.Visibility = Visibility.Visible

    def _on_show_active(self, sender, e):
        def work(uiapp):
            active = uiapp.ActiveUIDocument.ActiveView
            if active is None:
                ui.alert(u'No active view.', title=TITLE)
                return
            self._rebind(active)
        modeless.run(work, doc=doc, title=TITLE)

    def _on_back_to_list(self, sender, e):
        def work(uiapp):
            view = self._view_or_alert()
            if view is None:
                return
            if self._activate(uiapp, view):
                self.banner.Visibility = Visibility.Collapsed
        modeless.run(work, doc=doc, title=TITLE)

    # ---- Loading -----------------------------------------------------------

    def _on_refresh(self, sender, e):
        # _load() reads the model (collectors): outside a valid API context
        # on a modeless window, so it runs via the queue. Refresh keeps the
        # pinned view; only if that view is gone does it follow the active one.
        def work(uiapp):
            if self._view() is None:
                active = uiapp.ActiveUIDocument.ActiveView
                if active is None:
                    ui.alert(u'No active view.', title=TITLE)
                    return
                self._rebind(active)
            else:
                self._load()
        self._queue(work)

    def _load(self):
        view = self._view()
        if view is None:
            ui.alert(u'The view this list was built for no longer exists. '
                     u'Press Refresh to inspect the active view.', title=TITLE)
            self._all_rows = []
            self._rows     = []
            self._items.Clear()
            self._update_count()
            return

        self._view_label = _view_name(view)
        self._win.FindName("__slui_subtitle__").Text = self._subtitle_for(view)
        try:
            self._all_rows = find_overridden_in_view(view)
            self._all_rows.sort(key=lambda r: (r.category.lower(),
                                               r.family.lower(),
                                               r.type_name.lower()))
        except Exception as ex:
            ui.alert(u'Error scanning view: ' + _err_text(ex), title='Error')
            self._all_rows = []

        self._apply_filter()

    def _apply_filter(self):
        raw    = (self.txt_filter.Text or u'').strip().lower()
        tokens = [t for t in raw.split() if t]

        def matches(r):
            if not tokens:
                return True
            haystack = u' '.join([
                r.category, r.family, r.type_name, r.name,
                str(_id_val(r.id)), r.summary
            ]).lower()
            return all(t in haystack for t in tokens)

        self._rows = [r for r in self._all_rows if matches(r)]
        self._items.Clear()
        for row in self._rows:
            self._items.Add(row)
        self._update_count()

    def _update_count(self):
        n_hidden = sum(1 for r in self._all_rows if r.hidden)
        # Sub-elements folded onto their host row are still listed, just not as
        # rows of their own. Say so, or they look lost.
        n_nested = sum(len(r.folded) for r in self._all_rows)
        if n_hidden:
            nested = (u', {} nested folded in'.format(n_nested)
                      if n_nested else u'')
            self.lbl_count.Text = (
                u'{} of {} elements ({} hidden{})'.format(
                    len(self._rows), len(self._all_rows), n_hidden, nested))
        else:
            self.lbl_count.Text = u'{} of {} overridden elements'.format(
                len(self._rows), len(self._all_rows))

    # ---- Selection helpers -------------------------------------------------

    def _selected_rows(self):
        return [item for item in self.grid.SelectedItems
                if isinstance(item, OverriddenRow)]

    def _selected_or_warn(self):
        sel = self._selected_rows()
        if not sel:
            ui.alert(u'Select one or more rows first.', title='No selection')
            return None
        return sel

    # ---- Actions -----------------------------------------------------------

    def _select_job(self, ids, show):
        """Select `ids` in the pinned view, switching to it first: a selection
        made while another view is active would not be 'in' the listed view.
        Selection touches the API, so it runs inside the ExternalEvent, with
        the UIDocument Revit hands us."""
        def work(uiapp):
            view = self._view_or_alert()
            if view is None:
                return
            if not self._activate(uiapp, view):
                return
            live = self._existing(ids)
            if not live:
                ui.alert(u'Those elements no longer exist. Press Refresh.',
                         title=TITLE)
                return
            uidoc = uiapp.ActiveUIDocument
            uidoc.Selection.SetElementIds(CList[ElementId](live))
            if show:
                try:
                    uidoc.ShowElements(CList[ElementId](live))
                except Exception:
                    pass
        self._queue(work)

    def _on_select_in_view(self, sender, e):
        sel = self._selected_or_warn()
        if not sel:
            return
        self._select_job([r.id for r in sel], show=False)

    def _on_show_in_view(self, sender, e):
        sel = self._selected_or_warn()
        if not sel:
            return
        self._select_job([r.id for r in sel], show=True)

    def _on_double_click(self, sender, e):
        item = self.grid.SelectedItem
        if not isinstance(item, OverriddenRow):
            return
        self._select_job([item.id], show=True)

    def _on_edit(self, sender, e):
        sel = self._selected_or_warn()
        if not sel:
            return

        def work(uiapp):
            # vgrow.show_edit_dialog() reads pattern collectors and pops its
            # own ShowDialog() (a sub-dialog over this window, stays modal);
            # the whole thing runs inside the ExternalEvent because of those
            # collectors and the Transaction that applies the result.
            # extra_visibility=False (default): element overrides have no
            # filter-visibility flag, so .visibility is always None here.
            # doc= is mandatory: after any Transaction the builtin that
            # revit.doc reads is swapped, so vgrow cannot resolve the
            # document on its own (reported by QA 2026-10-01: Edit overrides
            # stopped opening after a Clear & unhide).
            view = self._view_or_alert()
            if view is None:
                return
            # The editor opens on what the view holds NOW for every selected
            # row (not the snapshot the list was built from), in multi mode:
            # a field where the rows agree shows that value, a field where
            # they differ shows <varies> and is left alone. Applying one
            # row's full OGS to all of them, as this did before, wiped each
            # row's own colors.
            current = []
            for r in sel:
                try:
                    current.append(view.GetElementOverrides(r.id))
                except Exception:
                    current.append(r.ogs)
            line_ok = set(_id_val(i) for _, i in vgrow.get_line_patterns(doc))
            fill_ok = set(_id_val(i) for _, i in vgrow.get_fill_patterns(doc))
            res = vgrow.show_edit_dialog(OverrideGraphicSettings(),
                                         owner_win=self._win, doc=doc,
                                         prefill_many=current)
            if res is None:
                return
            intents = res.intents

            def make(r):
                # Each row gets ONLY what the user set or cleared, on top of
                # its own current override (read again inside the
                # transaction, so a change made in Revit meanwhile survives).
                base = view.GetElementOverrides(r.id)
                return vgrow.apply_intents(
                    base, _keep_unlisted_patterns(base, intents,
                                                  line_ok, fill_ok))

            self._apply_to_rows(sel, u'Edit overrides', make)

        self._queue(work)

    def _on_copy_from(self, sender, e):
        sel = self._selected_or_warn()
        if not sel:
            return

        def work(uiapp):
            view = self._view_or_alert()
            if view is None:
                return
            # The element is picked IN the pinned view, so what the user sees
            # while picking is the override that gets copied.
            if not self._activate(uiapp, view):
                return
            uidoc = uiapp.ActiveUIDocument
            self._win.Hide()
            try:
                try:
                    ref = uidoc.Selection.PickObject(
                        ObjectType.Element,
                        u'Pick the source element to copy overrides from')
                except Exception:
                    return
                src_el  = doc.GetElement(ref.ElementId)
                src_ogs = (view.GetElementOverrides(src_el.Id)
                           if src_el is not None else None)
                if src_ogs is None:
                    ui.alert(u'Cannot read overrides from source element.',
                             title=TITLE)
                    return
                self._apply_to_rows(sel, u'Copy overrides from element',
                                    lambda r: src_ogs)
            finally:
                self._win.Show()

        self._queue(work)

    def _on_unhide(self, sender, e):
        sel = self._selected_or_warn()
        if not sel:
            return
        hidden_rows = [r for r in sel if r.hidden]
        if not hidden_rows:
            ui.alert(u'None of the selected rows are hidden in this view.',
                     title=TITLE)
            return

        def work(uiapp):
            view = self._view_or_alert()
            if view is None:
                return
            flat = []
            for r in hidden_rows:
                flat.extend(r.all_ids)
            flat = self._existing(flat)
            if not flat:
                ui.alert(u'Those elements no longer exist. The list is '
                         u'being refreshed.', title=TITLE)
                self._load()
                return

            t = Transaction(doc, u'{} -- Unhide elements'.format(TITLE))
            t.Start()
            try:
                view.UnhideElements(CList[ElementId](flat))
                self._commit(t, u'Unhide elements')
            except Exception as ex:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
                ui.alert(u'Error unhiding elements:\n' + _err_text(ex), title='Error')
                return

            self._load()

        self._queue(work)

    def _on_clear(self, sender, e):
        sel = self._selected_or_warn()
        if not sel:
            return
        n_hidden = sum(1 for r in sel if r.hidden)
        msg = u'Remove ALL graphic overrides from {} element(s)'.format(len(sel))
        if n_hidden:
            msg += u' and unhide {} hidden one(s)'.format(n_hidden)
        msg += u'?'
        msg += u'\n\nView: {}'.format(self._view_label)
        if not ui.confirm(msg, title='Confirm clear'):
            return

        def work(uiapp):
            self._apply_to_rows(sel, u'Clear overrides',
                                lambda r: OverrideGraphicSettings(),
                                also_unhide=True)

        self._queue(work)

    def _commit(self, t, what):
        """Commit and CHECK the outcome. Commit() does not raise when Revit
        refuses it (an element owned by another user in a workshared model,
        a failure it cannot resolve): it rolls back and returns a status, so
        ignoring the return value reported success for a change that never
        happened. Returns True when committed."""
        status = t.Commit()
        if status == TransactionStatus.Committed:
            return True
        ui.alert(u'Revit rolled back "{}" (transaction status: {}). Nothing '
                 u'was changed.\n\nIn a workshared model this usually means '
                 u'another user owns the view or one of the elements.'.format(
                     what, status), title=TITLE)
        return False

    def _apply_to_rows(self, rows, tx_label, make_ogs, also_unhide=False):
        """Write `make_ogs(row)` as the element override of every row, in the
        PINNED view, in one transaction. A row whose element is gone, or one
        Revit refuses, is skipped and reported instead of sinking the batch."""
        view = self._view_or_alert()
        if view is None:
            return

        live_rows = [r for r in rows if self._existing([r.id])]
        skipped   = [r for r in rows if r not in live_rows]
        failed    = []

        t = Transaction(doc, u'{} -- {}'.format(TITLE, tx_label))
        t.Start()
        try:
            if also_unhide:
                hidden_ids = []
                for r in live_rows:
                    if r.hidden:
                        hidden_ids.extend(r.all_ids)
                hidden_ids = self._existing(hidden_ids)
                if hidden_ids:
                    view.UnhideElements(CList[ElementId](hidden_ids))
            for r in live_rows:
                try:
                    view.SetElementOverrides(r.id, make_ogs(r))
                except Exception:
                    failed.append(r)
            if not live_rows or len(failed) == len(live_rows):
                # Nothing took: do not commit an empty transaction.
                t.RollBack()
            else:
                self._commit(t, tx_label)
        except Exception as ex:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
            ui.alert(u'Error applying overrides:\n' + _err_text(ex), title='Error')
            return

        self._load()

        skipped = skipped + failed
        if skipped:
            ids = u', '.join(str(_id_val(r.id)) for r in skipped[:8])
            if len(skipped) > 8:
                ids += u', ...'
            ui.alert(u'{} of {} element(s) were skipped (deleted, or Revit '
                     u'refused the override): {}'.format(
                         len(skipped), len(rows), ids), title=TITLE)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _show_error(stage, ex):
    msg = (u'Inspect View Overrides failed at stage: {}\n\n'
           u'Error: {}\n\nTraceback:\n{}').format(
        stage, _err_text(ex), traceback.format_exc())
    print(msg)
    try:
        ui.alert(msg, title=TITLE + u' - Error')
    except Exception:
        pass


try:
    # Second click on the button: bring the open window to the front, build
    # nothing (no collectors, no new window) -- as long as it is looking at
    # the view you are in. Built for another view (you navigated, or Inspect
    # Model handed you one, 2026-09-15), it is closed and rebuilt: its
    # subtitle and rows belong to the old view, and the actions that read
    # doc.ActiveView would act on the wrong one. The window remembers its
    # view in Tag (set in InspectViewForm.__init__, kept in step by _rebind).
    _live = modeless.live(TITLE)
    if _live is not None:
        _here = doc.ActiveView
        if _here is not None and getattr(_live, 'Tag', None) == _id_val(_here.Id):
            modeless.focus(TITLE)
            script.exit()
        modeless.close(TITLE)

    if uidoc is None:
        ui.alert(u'No active document. Open a project first.',
                 title=TITLE)
    elif doc.ActiveView is None:
        ui.alert(u'No active view.', title=TITLE)
    else:
        try:
            form = InspectViewForm()
        except Exception as ex_init:
            _show_error('form construction', ex_init)
            raise

        if not form._all_rows:
            ui.alert(
                (u'No element-level overrides or hidden elements found in '
                 u'view "{}".\n\n'
                 u'This tool only inspects:\n'
                 u'  - Overrides set via Override Graphics in View > By Element\n'
                 u'  - Elements hidden via Hide in View > Element\n\n'
                 u'Category overrides (V/G), filter overrides, category-hide '
                 u'and worksharing display overrides are NOT shown here.').format(
                    doc.ActiveView.Name),
                title=TITLE)
        else:
            form.show()
except Exception as ex_main:
    _show_error('main', ex_main)
