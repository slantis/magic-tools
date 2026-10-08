# -*- coding: utf-8 -*-
"""Inspect Model Overrides -- window and every handler live HERE, not in the
pushbutton's script.py.

Why: under rocket mode Magic Tools shares ONE engine across clicks and tears
it down the moment a script.py returns, which kills every handler that script
hung off a modeless window -- the window stays painted, no button responds
(diagnosed 2026-09-17). script.py used to survive this with
`__cleanengine__ = True`,
but a clean engine reimports every lib module (including lib/modeless.py) on
EVERY click, so `modeless._OPEN` starts empty each time and `modeless.focus()`
never finds the window a previous click opened: a second click on the ribbon
button opened the scope dialog again, modal, on top of the open table. A module
in lib/ survives in sys.modules for as long as Revit runs, so hosting the
window and its handlers here (same shape as lib/findroom.py) keeps them alive
with no `__cleanengine__`. Which window is open is kept by lib/modeless.py in
the AppDomain since 2026-10-06, because a module dict did not survive clicks
either (QA round 3).

The door (the .pushbutton's script.py) is three lines: `import inspectmodel` +
`inspectmodel.open_window(<its own bundle folder>)`.
"""
import traceback

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from pyrevit import revit
from Autodesk.Revit.DB import (
    FilteredElementCollector, ElementId, View, ViewType, ViewSheet, ViewSheetSet,
    OverrideGraphicSettings
)
from System import AppDomain
from System.Collections.ObjectModel import ObservableCollection
from System.ComponentModel import INotifyPropertyChanged, PropertyChangedEventArgs
from System.Windows import RoutedEventHandler
from System.Windows.Controls import CheckBox as _CheckBox

from slantisui import ui
import modeless
import launch
import hostecho


def _id_val(eid):
    # ElementId value compat Revit 2022-2026 (IntegerValue removed in 2026)
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def _pb_safe(s):
    # pyRevit re-formats the ProgressBar title on every tick, so braces coming
    # from the model (a view or family name) get re-parsed and blow up. Escape them.
    return (s or u"").replace(u"{", u"{{").replace(u"}", u"}}")



TITLE = u"Inspect Model Overrides"

INVALID_ID = ElementId.InvalidElementId

AUDIT_VIEW_TYPES = set([
    ViewType.FloorPlan, ViewType.CeilingPlan, ViewType.AreaPlan,
    ViewType.EngineeringPlan, ViewType.Elevation, ViewType.Section,
    ViewType.Detail, ViewType.ThreeD,
])


# ── Comparison helpers ────────────────────────────────────────────────────────

def _color_eq(a, b):
    av = a.IsValid if a is not None else False
    bv = b.IsValid if b is not None else False
    if not av and not bv:
        return True
    if av != bv:
        return False
    return a.Red == b.Red and a.Green == b.Green and a.Blue == b.Blue


def is_overridden(ogs):
    d = OverrideGraphicSettings()
    if ogs.Halftone != d.Halftone:                                       return True
    if ogs.Transparency != d.Transparency:                               return True
    if ogs.DetailLevel != d.DetailLevel:                                 return True
    if ogs.ProjectionLineWeight != d.ProjectionLineWeight:               return True
    if ogs.ProjectionLinePatternId != d.ProjectionLinePatternId:         return True
    if not _color_eq(ogs.ProjectionLineColor, d.ProjectionLineColor):    return True
    if ogs.CutLineWeight != d.CutLineWeight:                             return True
    if ogs.CutLinePatternId != d.CutLinePatternId:                       return True
    if not _color_eq(ogs.CutLineColor, d.CutLineColor):                  return True
    if ogs.IsSurfaceForegroundPatternVisible != d.IsSurfaceForegroundPatternVisible: return True
    if ogs.SurfaceForegroundPatternId != d.SurfaceForegroundPatternId:   return True
    if not _color_eq(ogs.SurfaceForegroundPatternColor, d.SurfaceForegroundPatternColor): return True
    if ogs.IsSurfaceBackgroundPatternVisible != d.IsSurfaceBackgroundPatternVisible: return True
    if ogs.SurfaceBackgroundPatternId != d.SurfaceBackgroundPatternId:   return True
    if not _color_eq(ogs.SurfaceBackgroundPatternColor, d.SurfaceBackgroundPatternColor): return True
    if ogs.IsCutForegroundPatternVisible != d.IsCutForegroundPatternVisible: return True
    if ogs.CutForegroundPatternId != d.CutForegroundPatternId:           return True
    if not _color_eq(ogs.CutForegroundPatternColor, d.CutForegroundPatternColor): return True
    if ogs.IsCutBackgroundPatternVisible != d.IsCutBackgroundPatternVisible: return True
    if ogs.CutBackgroundPatternId != d.CutBackgroundPatternId:           return True
    if not _color_eq(ogs.CutBackgroundPatternColor, d.CutBackgroundPatternColor): return True
    return False


def distinct_counts(ovr_ids, hidden_ids):
    """(# overrides, # hidden, # distinct elements) from two sets of element ids.

    An element that is both overridden and hidden is counted in BOTH columns,
    and once in the total: the total is the number of distinct elements, not
    the sum of the two columns.
    """
    ovr = set(ovr_ids)
    hid = set(hidden_ids)
    return len(ovr), len(hid), len(ovr | hid)


def count_dirty_in_view(view, all_elements, dep_index=None):
    """Returns (n_overridden, n_hidden, n_nested, n_distinct)."""
    ovr_ids = set()
    seen = set()
    # No try/except around the collector: if Revit cannot build the view's
    # element set the exception must reach run_audit, which lists the view as
    # "not audited". Swallowing it here used to turn a failed read into a 0.
    for el in (FilteredElementCollector(view.Document, view.Id)
               .WhereElementIsNotElementType()):
        try:
            ogs = view.GetElementOverrides(el.Id)
        except Exception:
            continue
        if ogs is None:
            continue
        if is_overridden(ogs):
            seen.add(_id_val(el.Id))
            ovr_ids.add(_id_val(el.Id))

    hidden = []
    for el in all_elements:
        if _id_val(el.Id) in seen:
            continue
        try:
            if not el.IsHidden(view):
                continue
        except Exception:
            continue
        hidden.append(el)

    # A hidden container makes every sub-element answer IsHidden=True as well,
    # so counting them one by one inflates the view: one click on a curtain
    # wall reads as hundreds of hidden elements (measured: 169 children for
    # one storefront). Count what the user actually hid. See lib/hostecho.py.
    survivors, folded = hostecho.fold_by_dependents(hidden, dep_index)
    n_nested = sum(len(v) for v in folded.values())

    # The collector above is scoped to the view, so it never returns a hidden
    # element: one that is hidden AND carries an override color would be seen
    # only as hidden. Read the override of each hidden row too, so it counts
    # in both columns (Inspect View lists it with both, "Hidden in view,
    # ProjLine[...]"). The folded echoes are not looked at: they are not rows.
    hidden_ids = set()
    for el in survivors:
        hidden_ids.add(_id_val(el.Id))
        try:
            ogs = view.GetElementOverrides(el.Id)
            if ogs is not None and is_overridden(ogs):
                ovr_ids.add(_id_val(el.Id))
        except Exception:
            continue

    n_ovr, n_hid, n_total = distinct_counts(ovr_ids, hidden_ids)
    return n_ovr, n_hid, n_nested, n_total


def get_audit_views(doc):
    out = []
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if v.IsTemplate:
                continue
            if v.ViewType not in AUDIT_VIEW_TYPES:
                continue
            out.append(v)
        except Exception:
            continue
    out.sort(key=lambda v: (str(v.ViewType), v.Name.lower()))
    return out


def view_template_name(v):
    try:
        tid = v.ViewTemplateId
        if tid is None or tid == INVALID_ID:
            return ''
        t = v.Document.GetElement(tid)
        return t.Name if t is not None else ''
    except Exception:
        return ''


# ── Row model ─────────────────────────────────────────────────────────────────

class AuditRow(object):
    def __init__(self, view, n_overrides, n_hidden, n_total=None,
                 not_audited=False):
        self._view_id   = view.Id
        self.ViewType   = str(view.ViewType)
        # A view whose elements could not be read is listed, not dropped and
        # not shown as a clean 0: the name says so and the row is muted.
        self.ViewName   = (view.Name + u"  (not audited)" if not_audited
                           else view.Name)
        self.IsNotAudited = bool(not_audited)
        self.Template   = view_template_name(view)
        self.NOverrides = int(n_overrides)
        self.NHidden    = int(n_hidden)
        # Distinct elements. The sum would count twice an element that is both
        # overridden and hidden.
        self.Total      = int(n_total if n_total is not None
                              else n_overrides + n_hidden)
        self.IsHigh     = self.Total >= 20


# ── XAML ──────────────────────────────────────────────────────────────────────

_BODY = """
  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
    </Grid.RowDefinitions>
    <TextBox x:Name="txtFilter" Grid.Row="0" Height="32" Margin="0,0,0,12"
             ToolTip="Filter by type, view, or template"/>
    <DataGrid x:Name="grid" Grid.Row="1"
              CanUserSortColumns="True"
              SelectionMode="Single">
      <DataGrid.RowStyle>
        <Style TargetType="DataGridRow" BasedOn="{StaticResource {x:Type DataGridRow}}">
          <Style.Triggers>
            <DataTrigger Binding="{Binding IsHigh}" Value="True">
              <Setter Property="Foreground" Value="__WARN__"/>
            </DataTrigger>
            <DataTrigger Binding="{Binding IsNotAudited}" Value="True">
              <Setter Property="Foreground" Value="__MUTED__"/>
            </DataTrigger>
          </Style.Triggers>
        </Style>
      </DataGrid.RowStyle>
      <DataGrid.Columns>
        <DataGridTextColumn Header="TYPE"        Width="110" Binding="{Binding ViewType}"/>
        <DataGridTextColumn Header="VIEW NAME"   Width="*"   Binding="{Binding ViewName}"/>
        <DataGridTextColumn Header="TEMPLATE"    Width="160" Binding="{Binding Template}"/>
        <DataGridTextColumn Header="# OVERRIDES" Width="100" Binding="{Binding NOverrides}"/>
        <DataGridTextColumn Header="# HIDDEN"    Width="90"  Binding="{Binding NHidden}"/>
        <DataGridTextColumn Header="# TOTAL"     Width="90"  Binding="{Binding Total}"
                            SortDirection="Descending"/>
      </DataGrid.Columns>
    </DataGrid>
  </Grid>
"""
_BODY = _BODY.replace("__WARN__", ui.STATUS_WARN).replace("__MUTED__", ui.TEXT_MUTED)

_FOOTER = """
  <Grid>
    <TextBlock x:Name="lblCount" VerticalAlignment="Center" Foreground="#A6A199"/>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnRefresh" Content="Refresh"      Style="{StaticResource BtnGhost}" Margin="0,0,8,0"/>
      <Button x:Name="btnOpen"    Content="Open view"    Style="{StaticResource BtnGhost}" Margin="0,0,8,0"
              ToolTip="Make the selected view the active view"/>
      <Button x:Name="btnInspect" Content="Inspect view" Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"
              ToolTip="Open the selected view and run Inspect View on it: every overridden or hidden element, ready to edit or clear"/>
      <Button x:Name="btnClose"   Content="Close"        Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""


# ── Main ──────────────────────────────────────────────────────────────────────

# ── Scope: what to scan ──────────────────────────────────────────

ALL_SETS = u"All views  (no print set)"


def sheet_index(doc):
    """view id -> 'A101  First Floor Plan', for every view placed on a sheet."""
    idx = {}
    for sh in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            label = u"{}  {}".format(sh.SheetNumber, sh.Name)
            for vid in sh.GetAllPlacedViews():
                idx[_id_val(vid)] = label
        except Exception:
            continue
    return idx


def print_sets(doc):
    """Saved print sets, named ones only. An unnamed set is Revit's in-session
    scratch: it has no identity to offer in a dropdown."""
    out = []
    for s in FilteredElementCollector(doc).OfClass(ViewSheetSet):
        try:
            if s.Name:
                out.append(s)
        except Exception:
            continue
    out.sort(key=lambda s: s.Name.lower())
    return out


def views_in_print_set(vss):
    """View ids a print set reaches. A set holds sheets, views, or both: a sheet
    contributes every view placed on it, which is the whole point of asking
    "what am I about to print"."""
    ids = set()
    try:
        for item in vss.Views:
            if isinstance(item, ViewSheet):
                for vid in item.GetAllPlacedViews():
                    ids.add(_id_val(vid))
            else:
                ids.add(_id_val(item.Id))
    except Exception:
        pass
    return ids


class ScopeRow(INotifyPropertyChanged):
    """One candidate view in the scope picker. INotifyPropertyChanged so All /
    None repaint the ticks without rebinding the grid, which would throw away
    the scroll position."""

    def __init__(self, view, sheet_label):
        self._view = view
        self.Id       = _id_val(view.Id)
        self.ViewType = str(view.ViewType)
        self.ViewName = view.Name
        self.Sheet    = sheet_label or u""
        self._checked = True
        self._pc_handlers = []

    @property
    def view(self):
        return self._view

    @property
    def on_sheet(self):
        return bool(self.Sheet)

    def add_PropertyChanged(self, value):
        self._pc_handlers.append(value)

    def remove_PropertyChanged(self, value):
        if value in self._pc_handlers:
            self._pc_handlers.remove(value)

    @property
    def Checked(self):
        return self._checked

    @Checked.setter
    def Checked(self, value):
        value = bool(value)
        if value == self._checked:
            return
        self._checked = value
        args = PropertyChangedEventArgs("Checked")
        for h in list(self._pc_handlers):
            h(self, args)


_SCOPE_BODY = """
  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
    </Grid.RowDefinitions>

    <Grid Grid.Row="0" Margin="0,0,0,12">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="66"/>
        <ColumnDefinition Width="280"/>
        <ColumnDefinition Width="*"/>
      </Grid.ColumnDefinitions>
      <TextBlock Grid.Column="0" Text="Print set" FontSize="13" Foreground="#202022"
                 VerticalAlignment="Center"/>
      <ComboBox x:Name="cboSet" Grid.Column="1" Height="34"
                ToolTip="Narrow the list to what a print set actually prints: the views it holds, plus every view placed on the sheets it holds."/>
      <CheckBox x:Name="chkSheet" Grid.Column="2" Content="Only views placed on sheets"
                VerticalAlignment="Center" Margin="20,0,0,0"
                ToolTip="Drop the working views nobody prints. A view is placed when it sits in a viewport on a sheet."/>
    </Grid>

    <TextBox x:Name="txtFilter" Grid.Row="1" Height="34" Margin="0,0,0,12"
             ToolTip="Filter by type, view name or sheet"/>

    <DataGrid x:Name="grid" Grid.Row="2" CanUserSortColumns="True"
              CanUserAddRows="False" SelectionMode="Single">
      <DataGrid.Columns>
        <DataGridTemplateColumn Width="36" CanUserSort="False">
          <DataGridTemplateColumn.CellTemplate>
            <DataTemplate>
              <CheckBox HorizontalAlignment="Center"
                        IsChecked="{Binding Checked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"/>
            </DataTemplate>
          </DataGridTemplateColumn.CellTemplate>
        </DataGridTemplateColumn>
        <DataGridTextColumn Header="TYPE"      Width="120" Binding="{Binding ViewType}"/>
        <DataGridTextColumn Header="VIEW NAME" Width="*"   Binding="{Binding ViewName}"/>
        <DataGridTextColumn Header="SHEET"     Width="210" Binding="{Binding Sheet}"/>
      </DataGrid.Columns>
    </DataGrid>
  </Grid>
"""

_SCOPE_FOOTER = """
  <Grid>
    <TextBlock x:Name="lblScope" VerticalAlignment="Center" Foreground="#A6A199"/>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnAll"    Content="All"    Style="{StaticResource BtnGhost}" Margin="0,0,8,0"/>
      <Button x:Name="btnNone"   Content="None"   Style="{StaticResource BtnGhost}" Margin="0,0,8,0"/>
      <Button x:Name="btnScan"   Content="Scan"   Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
      <Button x:Name="btnCancel" Content="Cancel" Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""


def choose_scope(all_views, doc):
    """The first thing the tool shows. Returns the list of views to scan, or
    None if cancelled. Replaces the old confirm + pick_list pair, where picking
    a subset was hidden behind saying No to "scan all"."""
    sheets = sheet_index(doc)
    rows   = [ScopeRow(v, sheets.get(_id_val(v.Id))) for v in all_views]
    psets  = print_sets(doc)

    win = ui.parse(
        TITLE, u"Choose what to scan  •  {} views eligible".format(len(rows)),
        _SCOPE_BODY, _SCOPE_FOOTER, width=940, height=620)

    grid      = win.FindName("grid")
    cboSet    = win.FindName("cboSet")
    chkSheet  = win.FindName("chkSheet")
    txtFilter = win.FindName("txtFilter")
    lblScope  = win.FindName("lblScope")
    btnScan   = win.FindName("btnScan")

    cboSet.Items.Add(ALL_SETS)
    for s in psets:
        cboSet.Items.Add(s.Name)
    cboSet.SelectedIndex = 0
    if not psets:
        cboSet.IsEnabled = False
        cboSet.ToolTip = "This model has no saved print sets."

    state = {"go": False, "scope": list(rows), "set_ids": None}

    def _in_scope(r):
        if state["set_ids"] is not None and r.Id not in state["set_ids"]:
            return False
        if chkSheet.IsChecked and not r.on_sheet:
            return False
        return True

    def _visible():
        q = txtFilter.Text.strip().lower()
        rs = state["scope"]
        if q:
            tokens = q.split()
            rs = [r for r in rs
                  if all(t in u" ".join([r.ViewType, r.ViewName, r.Sheet]).lower()
                         for t in tokens)]
        return rs

    def _count():
        n = len([r for r in state["scope"] if r.Checked])
        btnScan.Content = u"Scan ({})".format(n) if n else u"Scan"
        btnScan.IsEnabled = n > 0
        shown = len(_visible())
        extra = u"" if shown == len(state["scope"]) else u"  •  {} shown".format(shown)
        lblScope.Text = u"{} of {} views selected{}".format(
            n, len(state["scope"]), extra)

    def _rebind():
        c = ObservableCollection[object]()
        for r in _visible():
            c.Add(r)
        grid.ItemsSource = c
        _count()

    def _rescope():
        # Changing the print set or the sheet check redefines WHICH views are on
        # offer, so the selection starts fresh: carrying ticks from a scope you
        # can no longer see is how you end up scanning something you did not mean
        # to. The search box, by contrast, only hides rows -- it never unticks.
        keep = set()
        state["scope"] = []
        for r in rows:
            if _in_scope(r):
                state["scope"].append(r)
                keep.add(r.Id)
        for r in rows:
            r.Checked = r.Id in keep
        _rebind()

    def on_set(s, e):
        i = cboSet.SelectedIndex
        state["set_ids"] = None if i <= 0 else views_in_print_set(psets[i - 1])
        if state["set_ids"] is not None:
            # A print set is already a statement about sheets; leaving the check
            # on top of it would only subtract views the set explicitly asked for.
            chkSheet.IsChecked = False
        _rescope()

    def on_all(s, e):
        for r in _visible():
            r.Checked = True
        _count()

    def on_none(s, e):
        for r in _visible():
            r.Checked = False
        _count()

    def on_scan(s, e):
        state["go"] = True
        win.Close()

    cboSet.SelectionChanged += on_set
    chkSheet.Click          += lambda s, e: _rescope()
    txtFilter.TextChanged   += lambda s, e: _rebind()
    win.FindName("btnAll").Click    += on_all
    win.FindName("btnNone").Click   += on_none
    btnScan.Click                   += on_scan
    win.FindName("btnCancel").Click += lambda s, e: win.Close()
    # ClickEvent, not Checked/Unchecked: those also fire while WPF realises
    # virtualised rows, which would recount the whole list on every scroll.
    grid.AddHandler(_CheckBox.ClickEvent,
                    RoutedEventHandler(lambda s, e: _count()), True)

    _rebind()
    txtFilter.Focus()
    win.ShowDialog()

    if not state["go"]:
        return None
    return [r.view for r in state["scope"] if r.Checked]


class _SafeProgress(object):
    """A progress bar that can never take the scan down with it.

    ui.ProgressBar is pyRevit's, and on every tick it re-anchors itself over
    Revit's window through HOST_APP.uiapp, i.e. the `__revit__` builtin. That
    builtin goes to None after any other tool commits a Transaction on the
    shared engine. Refresh runs inside an ExternalEvent on that engine, so
    creating or ticking the bar can raise. When it does, the bar is dropped
    and the scan carries on without one: a missing bar costs nothing, a dead
    Refresh costs the whole result.
    """

    def __init__(self, title, cancellable=True):
        self._title = title
        self._cancellable = cancellable
        self._pb = None

    def __enter__(self):
        try:
            self._pb = ui.ProgressBar(title=self._title,
                                      cancellable=self._cancellable)
            self._pb.__enter__()
        except Exception:
            self._drop()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._drop()
        return False

    def _drop(self):
        pb, self._pb = self._pb, None
        if pb is not None:
            try:
                pb.__exit__(None, None, None)
            except Exception:
                pass

    @property
    def cancelled(self):
        if self._pb is None:
            return False
        try:
            return bool(self._pb.cancelled)
        except Exception:
            self._drop()
            return False

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, value):
        self._title = value
        if self._pb is None:
            return
        try:
            self._pb.title = value
        except Exception:
            self._drop()

    def update_progress(self, current, total):
        if self._pb is None:
            return
        try:
            self._pb.update_progress(current, total)
        except Exception:
            self._drop()


# A scan in progress, kept in the AppDomain so every engine sees it.
_SCANNING = 'magictools.inspectmodel.scanning'


def scanning():
    try:
        return bool(AppDomain.CurrentDomain.GetData(_SCANNING))
    except Exception:
        return False


class _ScanLock(object):
    """Nothing else can start from the ribbon while a scan runs.

    The progress bar pumps window messages on every tick (it has to, to
    repaint and to hear Cancel), so a click on the ribbon during a scan was
    dispatched as a new command while this one was still inside the API.
    Clicking Inspect Model Overrides again mid-scan closed Revit outright,
    losing unsaved work (QA round 3, 2026-10-06, Revit 2025). The
    ribbon is disabled for the length of the scan, the way a modal dialog
    disables what is behind it, and the flag lets a click that still gets
    through (a keyboard shortcut) turn itself away. The progress bar and its
    Cancel stay live. Both come back in __exit__ whatever happened.
    """

    def __enter__(self):
        self._ribbon = None
        try:
            AppDomain.CurrentDomain.SetData(_SCANNING, True)
        except Exception:
            pass
        try:
            from pyrevit.api import AdWindows
            ribbon = AdWindows.ComponentManager.Ribbon
            if ribbon is not None and ribbon.IsEnabled:
                ribbon.IsEnabled = False
                self._ribbon = ribbon
        except Exception:
            traceback.print_exc()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._ribbon is not None:
                self._ribbon.IsEnabled = True
        except Exception:
            traceback.print_exc()
        try:
            AppDomain.CurrentDomain.SetData(_SCANNING, None)
        except Exception:
            pass
        return False


def run_audit(views, doc):
    """Returns (list[AuditRow], n_views_total, was_cancelled)."""
    all_elements = list(
        FilteredElementCollector(doc).WhereElementIsNotElementType()
    )
    rows      = []
    cancelled = False
    n_total   = len(views)

    # One index for the whole scan: GetDependentElements is a model fact, not
    # a per-view one, so each element is queried once for all 1000+ views.
    dep_index = hostecho.DependentIndex()

    with _ScanLock(), _SafeProgress(TITLE, cancellable=True) as pb:
        for i, v in enumerate(views):
            if pb.cancelled:
                cancelled = True
                break
            try:
                # A+B: show current item + counter in the title. Reading
                # v.Name is inside the try with the rest of the view: a view
                # deleted since the list was built raises right here.
                pb.title = TITLE + u" - {}/{} - {}".format(
                    i + 1, n_total, _pb_safe(v.Name))
                n_ovr, n_hid, n_nested, n_tot = count_dirty_in_view(
                    v, all_elements, dep_index)
            except Exception:
                # Could not read this view: list it as not audited rather
                # than letting it pass as a clean one.
                try:
                    row = AuditRow(v, 0, 0, 0, not_audited=True)
                    row.n_nested = 0
                    rows.append(row)
                except Exception:
                    pass
                pb.update_progress(i + 1, n_total)
                continue
            if n_ovr or n_hid:
                row = AuditRow(v, n_ovr, n_hid, n_tot)
                # Not a column: it explains the counts, it is not something to
                # sort views by.
                row.n_nested = n_nested
                rows.append(row)
            pb.update_progress(i + 1, n_total)

    rows.sort(key=lambda r: r.Total, reverse=True)
    return rows, n_total, cancelled


def resolve_views(entries, doc):
    """(id, name) pairs saved at scan time -> (live views, names of the gone).

    The View objects of the first scan are stale once the user deletes a view
    from the Project Browser: reading .Name on one raises "The referenced
    object is not valid". So the window keeps ids and names, and a Refresh
    asks the document again. Needs a valid API context (an ExternalEvent job).
    """
    views, gone = [], []
    for vid, name in entries:
        el = None
        try:
            el = doc.GetElement(vid)
        except Exception:
            el = None
        if el is None or not isinstance(el, View):
            gone.append(name)
            continue
        try:
            if el.IsTemplate:
                gone.append(name)
                continue
        except Exception:
            gone.append(name)
            continue
        views.append(el)
    return views, gone


def _gone_message(gone):
    shown = u", ".join(gone[:5])
    if len(gone) > 5:
        shown += u" and {} more".format(len(gone) - 5)
    if len(gone) == 1:
        return u"1 view no longer exists ({}) and was removed from the list.".format(shown)
    return u"{} views no longer exist ({}) and were removed from the list.".format(
        len(gone), shown)


def _n_dirty(rows):
    """Rows that are real findings (the not-audited ones are not)."""
    return sum(1 for r in rows if not getattr(r, 'IsNotAudited', False))


def _stats_line(rows, total_views, cancelled, n_shown):
    """Live scan stats for the footer label. The /slantis chrome subtitle is
    set once at construction (title/subtitle args of ui.parse) and is not
    meant to be poked at runtime -- its x:Names are library internals."""
    tag = u"  ⚠ partial scan" if cancelled else u""
    n_unaud = len(rows) - _n_dirty(rows)
    rows = [r for r in rows if not getattr(r, 'IsNotAudited', False)]
    unaud = u"  •  {} not audited".format(n_unaud) if n_unaud else u""
    tot_o = sum(r.NOverrides for r in rows)
    tot_h = sum(r.NHidden    for r in rows)
    # Sub-elements hidden only because their host is are counted on the host,
    # not one by one. Say how many, so the totals are readable.
    tot_n = sum(getattr(r, 'n_nested', 0) for r in rows)
    nested = u" ({} nested folded in)".format(tot_n) if tot_n else u""
    return u"{} of {} dirty views{}  •  {} overridden, {} hidden{}  •  {} audited{}".format(
        n_shown, len(rows), tag, tot_o, tot_h, nested,
        total_views - n_unaud, unaud)


def _doc_key(doc):
    """Identity of a model for win.Tag: the window is built for ONE document."""
    try:
        return u"{}|{}".format(doc.Title, doc.PathName)
    except Exception:
        return u""


def _open(inspect_view_bundle):
    # Captured here, at click time and before any transaction: the closures
    # below keep using it for as long as the window lives.
    doc = revit.doc

    # A click that reaches here while a scan runs (the ribbon is off, a
    # shortcut is not) is ignored: re-entering the scan is what closed Revit.
    if scanning():
        return

    # Second click on the button: bring the open window to the front, build
    # nothing. lib/modeless.py keeps the window the first click opened.
    # Unless the active model changed since: the table belongs to the model it
    # was scanned in, so it is closed and the normal flow (scope dialog) runs
    # on the new one.
    live = modeless.live(TITLE)
    if live is not None and doc is not None:
        try:
            other_model = live.Tag != _doc_key(doc)
        except Exception:
            other_model = False
        if other_model:
            modeless.close(TITLE)
    if modeless.focus(TITLE):
        return

    if doc is None:
        ui.alert("No active document.", title=TITLE)
        return

    # Pre-scan: choose the scope
    _all_views = get_audit_views(doc)
    if not _all_views:
        ui.alert("No views eligible for audit in this model.", title=TITLE)
        return

    _scan_views = choose_scope(_all_views, doc)
    if not _scan_views:
        return

    all_rows, n_views_total, was_cancelled = run_audit(_scan_views, doc)

    n = len(all_rows)
    if n == 0:
        msg = "No views with local overrides or hidden elements found."
        if was_cancelled:
            msg = u"Scan cancelled - no dirty views found in the scanned portion."
        ui.alert(
            u"{} ({} views audited{}).".format(
                msg, n_views_total,
                u" - partial" if was_cancelled else u""),
            title=TITLE)
        return

    # The counts live in one place, the footer, which Refresh updates. They
    # used to be in the subtitle too, set once at construction, so after a
    # Refresh the two disagreed (QA round 3).
    subtitle = u"Views with element overrides or hidden elements. Double-click opens the view."

    win = ui.parse(TITLE, subtitle, _BODY, _FOOTER,
                   width=980, height=620)
    # Which model this table was scanned in: the next click compares it.
    win.Tag = _doc_key(doc)

    grid      = win.FindName("grid")
    txtFilter = win.FindName("txtFilter")
    lblCount  = win.FindName("lblCount")
    btnRefresh = win.FindName("btnRefresh")
    btnOpen   = win.FindName("btnOpen")
    btnInspect = win.FindName("btnInspect")
    btnClose  = win.FindName("btnClose")

    # Ids and names, not View objects: the objects go stale if a view is
    # deleted while the window is open (see resolve_views).
    state = {"rows": all_rows, "total_views": n_views_total,
             "scan_entries": [(v.Id, v.Name) for v in _scan_views],
             "cancelled": was_cancelled}

    def _to_col(items):
        c = ObservableCollection[object]()
        for it in items:
            c.Add(it)
        return c

    def _refresh_grid():
        q = txtFilter.Text.strip().lower()
        rs = state["rows"]
        if q:
            tokens = q.split()
            def match(r):
                h = " ".join([r.ViewType, r.ViewName, r.Template]).lower()
                return all(t in h for t in tokens)
            rs = [r for r in rs if match(r)]
        grid.ItemsSource = _to_col(rs)
        lblCount.Text = _stats_line(
            state["rows"], state["total_views"], state["cancelled"],
            _n_dirty(rs))

    _refresh_grid()

    def on_filter(s, e):
        _refresh_grid()

    def on_open(s, e):
        sel = grid.SelectedItem
        if sel is None:
            ui.alert("Select a view first.", title=TITLE)
            return
        view_id = sel._view_id

        def work(uiapp):
            # Navigating touches the API: runs inside the ExternalEvent,
            # with the UIDocument Revit hands us.
            uidoc = uiapp.ActiveUIDocument
            try:
                uidoc.RequestViewChange(doc.GetElement(view_id))
            except Exception as ex:
                ui.alert("Could not navigate: " + str(ex), title=TITLE)

        modeless.run(work, doc=doc, title=TITLE)

    def on_inspect(s, e):
        """Open view + Inspect View, chained (2026-09-15: "chain the tools
        into one"). Inspect View reads doc.ActiveView the moment
        it starts, so the switch has to be done by the time it runs."""
        sel = grid.SelectedItem
        if sel is None:
            ui.alert("Select a view first.", title=TITLE)
            return
        view_id = sel._view_id

        def work(uiapp):
            uidoc = uiapp.ActiveUIDocument
            view = doc.GetElement(view_id)
            if view is None:
                ui.alert("That view no longer exists. Refresh the list.", title=TITLE)
                return
            # Not RequestViewChange, which Open view uses: that one is honoured
            # once Revit is idle, after this job has returned. The setter
            # switches now, and inside the ExternalEvent it is legal.
            if uidoc.ActiveView is None or uidoc.ActiveView.Id != view.Id:
                uidoc.ActiveView = view
            uid = launch.uid_for_bundle(inspect_view_bundle)
            if uid is None:
                ui.alert("Inspect View is not loaded in this session.", title=TITLE)
                return
            # Runs the sibling command as a ribbon click would, on Revit's next
            # Idling rather than nested in this Execute: the nested launch is
            # suspected of killing the launched tool's callbacks, and that
            # cause is not closed. Inspect View closes and rebuilds its own
            # window when it finds one built for another view.
            launch.run_when_idle(uid, uiapp, title=TITLE)

        modeless.run(work, doc=doc, title=TITLE)

    def on_doubleclick(s, e):
        sel = grid.SelectedItem
        if sel is None:
            return
        view_id = sel._view_id

        def work(uiapp):
            uidoc = uiapp.ActiveUIDocument
            try:
                uidoc.RequestViewChange(doc.GetElement(view_id))
            except Exception:
                pass

        modeless.run(work, doc=doc, title=TITLE)

    def on_refresh(s, e):
        if scanning():
            return

        def work(uiapp):
            # Hide/Show and the collector-based re-scan all touch the API:
            # the whole cycle runs inside the ExternalEvent.
            gone = []
            win.Hide()
            try:
                # Views deleted since the last scan are dropped first and
                # reported afterwards, instead of crashing the scan.
                views, gone = resolve_views(state["scan_entries"], doc)
                state["scan_entries"] = [(v.Id, v.Name) for v in views]
                if views:
                    rows, total, cancelled = run_audit(views, doc)
                else:
                    rows, total, cancelled = [], 0, False
                rows.sort(key=lambda r: r.Total, reverse=True)
                state["rows"]        = rows
                state["total_views"] = total
                state["cancelled"]   = cancelled
                _refresh_grid()
            except Exception as ex:
                ui.alert(u"Refresh failed: {}".format(ex), title=TITLE)
            finally:
                # Whatever happened above, the window must come back.
                win.Show()
            if gone:
                ui.alert(_gone_message(gone), title=TITLE)

        modeless.run(work, doc=doc, title=TITLE)

    txtFilter.TextChanged    += on_filter
    btnOpen.Click            += on_open
    btnInspect.Click         += on_inspect
    btnClose.Click           += lambda s, e: win.Close()
    btnRefresh.Click         += on_refresh
    grid.MouseDoubleClick    += on_doubleclick

    modeless.show(win, TITLE, doc=doc)


def open_window(inspect_view_bundle):
    """Entry point from the pushbutton's script.py.

    `inspect_view_bundle` is the folder of the sibling "Inspect View
    Overrides.pushbutton", resolved by the door from its own path so that each
    copy of the extension chains into its own Inspect View (lib/launch.py)."""
    try:
        _open(inspect_view_bundle)
    except Exception:
        ui.alert("Unexpected error:\n\n{}".format(traceback.format_exc()),
                 title=TITLE + u" - Error")
