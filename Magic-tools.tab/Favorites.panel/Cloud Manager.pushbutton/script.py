# -*- coding: utf-8 -*-
__title__ = "Cloud\nManager"
__doc__ = "Manage revision clouds in 4 steps: pick the revisions, choose the view scope, then show, hide, tag, style or delete them."

from pyrevit import revit, DB, script
import clr
import re
import contextlib
import System
import System.Diagnostics

# WinForms and GDI survive for two deliberate exceptions only: the native colour
# picker (ColorDialog) and the native save dialog (SaveFileDialog). There is no
# /slantis equivalent for either, and both are OS dialogs the user already knows.
# Everything else this tool draws goes through slantisui.
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")
from System.Windows.Forms import ColorDialog, DialogResult, SaveFileDialog
from System.Drawing import Color

clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System.Windows import Visibility as WVisibility, RoutedEventHandler
from System.Windows.Controls import (DataGridRow, Button as WButton,
                                     ComboBox as WComboBox)
from System.Windows.Controls.Primitives import ToggleButton
from System.Windows.Input import Key
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Media import (VisualTreeHelper, SolidColorBrush,
                                  Color as WColor)
from System.Collections.ObjectModel import ObservableCollection
from System.ComponentModel import INotifyPropertyChanged, PropertyChangedEventArgs

# The whole look of the four windows comes from here. This script declares no
# style, no hex, no font and no window chrome of its own.
from slantisui import ui
import usage

doc   = revit.doc
uidoc = revit.uidoc


def _id_val(eid):
    # ElementId value compat Revit 2022-2026 (IntegerValue removed in 2026)
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue

def _make_eid(val):
    # ElementId(int) compat Revit 2022-2026: 2026 dropped the Int32 ctor and a
    # bare Python int is ambiguous (BuiltInParameter/BuiltInCategory/Int64)
    try:
        return DB.ElementId(System.Int64(val))
    except Exception:
        return DB.ElementId(System.Int32(val))

# ============================================================
#  Revit window handle
# ============================================================
class RevitWindowHandle(System.Windows.Forms.IWin32Window):
    def __init__(self):
        self._h = System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle
    @property
    def Handle(self): return self._h

_hwnd = RevitWindowHandle()

# ============================================================
#  Model helpers
# ============================================================
def _param_str(elem, name):
    p = elem.LookupParameter(name)
    if not p: return ""
    v = p.AsString()
    return v.strip() if v else ""

def rev_num(rev):
    if rev is None: return ""
    try:
        p = rev.get_Parameter(DB.BuiltInParameter.REVISION_SEQUENCE_NUM)
        if p:
            v = p.AsString()
            if v and v.strip(): return v.strip()
    except Exception: pass
    return _param_str(rev, "Revision Number")

def rev_desc(rev):
    if rev is None: return ""
    try:
        v = rev.Description
        if v and str(v).strip(): return str(v).strip()
    except Exception: pass
    return _param_str(rev, "Revision Description")

def rev_issued_to(rev):
    if rev is None: return ""
    try:
        v = rev.IssuedTo
        if v and str(v).strip(): return str(v).strip()
    except Exception: pass
    for n in ("Issued to", "Issued To", "IssuedTo",
              "Issued To (Project)", "Emitido a"):
        v = _param_str(rev, n)
        if v: return v
    return ""

def rev_issued_by(rev):
    if rev is None: return ""
    try:
        v = rev.IssuedBy
        if v and str(v).strip(): return str(v).strip()
    except Exception: pass
    for n in ("Issued by", "Issued By", "IssuedBy",
              "Issued By (Project)", "Emitido por"):
        v = _param_str(rev, n)
        if v: return v
    return ""

def rev_is_issued(rev):
    """Whether a Revision has been marked Issued -- Revit blocks Delete (and
    other edits) on clouds that belong to an issued revision, silently, with
    no exception raised. Used to explain a 0-of-N delete to the user."""
    if rev is None: return False
    try:
        return bool(rev.Issued)
    except Exception: pass
    for n in ("Issued", "Emitido"):
        v = _param_str(rev, n)
        if v: return v.strip().lower() in ("yes", "true", "1", "si", u"sí")
    return False

def cloud_fields(cloud):
    p = cloud.get_Parameter(DB.BuiltInParameter.REVISION_CLOUD_REVISION)
    if not p: return ("--", "(no revision)", "", "")
    r = doc.GetElement(p.AsElementId())
    if not r: return ("--", "(no revision)", "", "")
    return (rev_num(r) or "--", rev_desc(r) or "--",
            rev_issued_to(r), rev_issued_by(r))

def get_line_patterns():
    try:
        solid_id = DB.LinePatternElement.GetSolidPatternId()
    except Exception:
        solid_id = _make_eid(-3000010)
    pats = {"Solid": solid_id}
    for lp in DB.FilteredElementCollector(doc).OfClass(DB.LinePatternElement):
        pats[lp.Name] = lp.Id
    return pats

def all_views_dict():
    return {
        _id_val(v.Id): v
        for v in DB.FilteredElementCollector(doc)
               .OfClass(DB.View).WhereElementIsNotElementType().ToElements()
        if not v.IsTemplate
    }

# ---------------------------------------------------------------------------
# Revit API compatibility helpers
# ---------------------------------------------------------------------------
_HAS_REVISION_ID = hasattr(DB, 'RevisionId')   # True in Revit 2024+

def _to_rev_id(elem_id):
    """Convert an ElementId to the type expected by SetRevisionVisibility.

    In Revit 2024 Autodesk replaced the bare ElementId parameter in
    View.SetRevisionVisibility / GetRevisionVisibility with a strongly-typed
    RevisionId struct.  Passing an ElementId to the new overload triggers an
    IronPython AttributeError ('type' object has no attribute …) because no
    matching overload can be found.  This helper tries all known construction
    patterns and falls back gracefully.
    """
    if _HAS_REVISION_ID:
        # Pattern 1: plain int constructor (most common in IronPython)
        try:
            return DB.RevisionId(_id_val(elem_id))
        except Exception:
            pass
        # Pattern 2: explicit System.Int32 cast — IronPython sometimes needs this
        try:
            return DB.RevisionId(System.Int32(_id_val(elem_id)))
        except Exception:
            pass
        # Pattern 3: ElementId constructor
        try:
            return DB.RevisionId(elem_id)
        except Exception:
            pass
        # Pattern 4: pull RevisionId directly from the sheet's revision list
        # (avoids the constructor entirely — guaranteed-correct object from the API)
        try:
            for sh in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet):
                rid_list = sh.GetRevisionIds() if hasattr(sh, 'GetRevisionIds') else []
                for rid in rid_list:
                    iv = None
                    try:    iv = rid.Value
                    except Exception: pass
                    if iv is None:
                        try:    iv = rid.IntegerValue
                        except Exception: pass
                    if iv == _id_val(elem_id):
                        return rid
        except Exception:
            pass
        # If all patterns fail we stay with elem_id — SetRevisionVisibility will raise
        # and the caller's except will log it rather than crashing silently.
    return elem_id

# View types that do NOT support SetRevisionVisibility
_UNSUPPORTED_VT_STRINGS = frozenset([
    'Schedule', 'Legend', 'Rendering', 'Walkthrough',
    'SystemBrowser', 'ProjectBrowser', 'Undefined',
])

def _view_supports_rev_vis(view):
    """Return True if this view type supports SetRevisionVisibility.
    Uses isinstance for the most reliable types, then falls back to the
    ViewType string (which avoids enum-comparison pitfalls in IronPython).
    """
    try:
        # ViewSchedule is the most common unsupported type
        if isinstance(view, DB.ViewSchedule):
            return False
        vt_str = str(view.ViewType)
        return vt_str not in _UNSUPPORTED_VT_STRINGS
    except Exception:
        return True   # assume supported if we can't tell

def to_db_color(c):
    return DB.Color(int(c.R), int(c.G), int(c.B))

# ============================================================
#  Shared state
# ============================================================
class AppState(object):
    def __init__(self):
        self.selected_clouds = []
        self.selected_views  = {}

STATE        = AppState()
SCOPE_ACTIVE = "active"
SCOPE_CHOSEN = "chosen"
SCOPE_MODEL  = "model"
ACTION_BACK    = 20
ACTION_RESTART = 21
ACTION_EXIT    = 99

# ============================================================
#  STEP 1 -- Select Clouds  (grouped by revision)   [slantisui]
# ============================================================
#  Replaces the legacy WinForms CloudSelectionDialog. Same contract:
#  one row per revision that owns clouds (+ a synthetic "(no revision)" row),
#  checking a row selects ALL of its clouds, and `selected_clouds` hands back
#  cloud ELEMENTS so Steps 2 and 3 keep working untouched.
# ============================================================
ACTION_NEXT = 1

def _own_to_revit(win):
    """Parent a WPF window to the Revit main window so it cannot end up behind
    Revit. The legacy Forms did this via ShowDialog(_hwnd)."""
    try:
        WindowInteropHelper(win).Owner = \
            System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle
    except Exception:
        pass


def _tok(xaml):
    """Substitute the library tokens into a bespoke XAML literal.

    A tool never writes a colour of its own. The library retone only reaches the
    library's own styles, so any hex left in a body or footer would go stale in
    silence the day ACCENT moves. Every colour below is READ from slantisui; the
    "_T" ones are the 8% ARGB tint of their token, derived here, not typed."""
    return (xaml
            .replace(u'__ACCENT__',     ui.ACCENT)
            .replace(u'__ACCENT_DK__',  ui.ACCENT_DK)
            .replace(u'__ACCENT_T__',   u'#14' + ui.ACCENT[1:])
            .replace(u'__ROW_HOVER__',  ui.ROW_HOVER)
            .replace(u'__ROW_SEL__',    ui.ROW_SEL)
            .replace(u'__WIN_BG__',     ui.WIN_BG)
            .replace(u'__CARD_BG__',    ui.CARD_BG)
            .replace(u'__CARD_BD__',    ui.CARD_BD)
            .replace(u'__INPUT_BD__',   ui.INPUT_BD)
            .replace(u'__CHECK_BD__',   ui.CHECK_BD)
            .replace(u'__TEXT__',       ui.TEXT)
            .replace(u'__TEXT_DIM__',   ui.TEXT_DIM)
            .replace(u'__TEXT_MUTED__', ui.TEXT_MUTED)
            .replace(u'__OK__',         ui.STATUS_OK)
            .replace(u'__OK_T__',       u'#14' + ui.STATUS_OK[1:])
            .replace(u'__WARN__',       ui.STATUS_WARN)
            .replace(u'__WARN_T__',     u'#14' + ui.STATUS_WARN[1:])
            .replace(u'__BAD__',        ui.STATUS_BAD)
            .replace(u'__BAD_T__',      u'#14' + ui.STATUS_BAD[1:]))


def _wpf_brush(hex_str):
    """#RRGGBB to SolidColorBrush, so the status lines read their colour from
    the library tokens instead of naming one."""
    h = hex_str.lstrip(u'#')
    return SolidColorBrush(WColor.FromRgb(int(h[0:2], 16), int(h[2:4], 16),
                                          int(h[4:6], 16)))


# Status-line brushes. Semantics only, all four read from the library:
# DIM neutral, OK done, WARN partial or nothing to do, BAD blocked or
# destructive. They replace the old BRUSH_DIM/GREEN/ORANGE/RED literals.
BRUSH_DIM  = _wpf_brush(ui.TEXT_DIM)
BRUSH_OK   = _wpf_brush(ui.STATUS_OK)
BRUSH_WARN = _wpf_brush(ui.STATUS_WARN)
BRUSH_BAD  = _wpf_brush(ui.STATUS_BAD)


def _brush(dcolor):
    """Bridge a System.Drawing.Color (what ColorDialog hands back) to a WPF
    brush, for the per-cloud colour swatches. That colour is domain data the
    user picked, not theme, so it is the one colour here that is not a token."""
    if dcolor is None:
        return BRUSH_DIM
    try:
        return SolidColorBrush(WColor.FromArgb(dcolor.A, dcolor.R, dcolor.G,
                                               dcolor.B))
    except Exception:
        return BRUSH_DIM

NO_REV_KEY = -1


def _all_clouds(view_id=None):
    """Every revision cloud in the model (or in one view), deduplicated.
    Collectors can hand back the same element twice -- the legacy dialog
    guarded against this and so must we, or the cloud counts come out
    inflated."""
    try:
        col = (DB.FilteredElementCollector(doc, view_id) if view_id
               else DB.FilteredElementCollector(doc))
        raw = (col.OfCategory(DB.BuiltInCategory.OST_RevisionClouds)
                  .WhereElementIsNotElementType().ToElements())
    except Exception:
        return {}   # e.g. a view that cannot own elements
    seen = {}
    for c in raw:
        k = _id_val(c.Id)
        if k not in seen:
            seen[k] = c
    return seen


class RevisionRow(INotifyPropertyChanged):
    """One grid row. Carries its own clouds -- that is the actual payload."""
    PropertyChanged = None  # so WPF can hook the interface event in IronPython

    def __init__(self, key, number, description, issued_to, issued_by, issued, clouds):
        self._selected = False
        self.Key = key                # revision ElementId value, or NO_REV_KEY
        self.RevNumber = number
        self.Description = description
        self.IssuedTo = issued_to
        self.IssuedBy = issued_by
        self.Issued = issued
        # Shown as a Status column so an issued revision (Delete-blocked by
        # Revit) is visible before the user picks it, not only after Delete
        # silently fails in Step 3.
        self.StatusText = u'Issued' if issued else u''
        self.Clouds = clouds          # list[RevisionCloud]
        self.CloudCount = len(clouds)
        self._handlers = []

    def add_PropertyChanged(self, h):
        self._handlers.append(h)

    def remove_PropertyChanged(self, h):
        if h in self._handlers:
            self._handlers.remove(h)

    def _notify(self, name):
        for h in list(self._handlers):
            h(self, PropertyChangedEventArgs(name))

    def get_IsSelected(self):
        return self._selected

    def set_IsSelected(self, value):
        if self._selected != value:
            self._selected = value
            self._notify('IsSelected')

    IsSelected = property(get_IsSelected, set_IsSelected)


def collect_revision_groups():
    """Group every cloud in the model by its revision.

    Returns (rows, total_clouds). Only revisions that actually own clouds get
    a row -- an empty revision has nothing to act on. Clouds with no revision
    assigned land in a single "(no revision)" row so they stay reachable."""
    clouds = _all_clouds()
    total = len(clouds)

    groups = {}
    no_rev = []
    for c in clouds.values():
        p = c.get_Parameter(DB.BuiltInParameter.REVISION_CLOUD_REVISION)
        r = doc.GetElement(p.AsElementId()) if p else None
        if r is None:
            no_rev.append(c)
            continue
        rid = _id_val(r.Id)
        if rid not in groups:
            groups[rid] = {'num': rev_num(r) or u'--',
                           'desc': rev_desc(r) or u'--',
                           'to': rev_issued_to(r),
                           'by': rev_issued_by(r),
                           'issued': rev_is_issued(r),
                           'clouds': []}
        groups[rid]['clouds'].append(c)

    if no_rev:
        groups[NO_REV_KEY] = {'num': u'--', 'desc': u'(no revision)',
                              'to': u'', 'by': u'', 'issued': False,
                              'clouds': no_rev}

    def sort_key(item):
        g = item[1]
        try:
            n = int(g['num'])
        except Exception:
            n = 9999
        return (n, g['desc'])

    rows = []
    for key, g in sorted(groups.items(), key=sort_key):
        rows.append(RevisionRow(key, g['num'], g['desc'],
                                g['to'], g['by'], g['issued'], g['clouds']))
    return rows, total


_STEP1_BODY = u'''
<Grid>
  <Grid.RowDefinitions>
    <RowDefinition Height="Auto"/>   <!-- toolbar -->
    <RowDefinition Height="Auto"/>   <!-- count line -->
    <RowDefinition Height="*"/>      <!-- grid + empty state -->
  </Grid.RowDefinitions>

  <!-- Toolbar. The three buttons are selection ACTIONS (they tick and untick
       rows), never view filters, so none of them carries a persistent state. -->
  <Grid Grid.Row="0" Margin="0,0,0,10">
    <Grid.ColumnDefinitions>
      <ColumnDefinition Width="*"/><ColumnDefinition Width="Auto"/>
      <ColumnDefinition Width="Auto"/><ColumnDefinition Width="Auto"/>
    </Grid.ColumnDefinitions>
    <Grid Grid.Column="0" Margin="0,0,10,0">
      <TextBox x:Name="Search" Height="32"/>
      <TextBlock x:Name="SearchPh" Text="Search revisions&#8230;"
                 Foreground="__TEXT_MUTED__" IsHitTestVisible="False"
                 VerticalAlignment="Center" Margin="11,0,0,0"/>
    </Grid>
    <Button Grid.Column="1" x:Name="BtnActiveView" Content="Active View"
            Style="{StaticResource BtnGhost}" Margin="0,0,6,0"/>
    <Button Grid.Column="2" x:Name="BtnAllInModel" Content="All in Model"
            Style="{StaticResource BtnGhost}" Margin="0,0,6,0"/>
    <Button Grid.Column="3" x:Name="BtnClear" Content="Clear"
            Style="{StaticResource BtnGhost}"/>
  </Grid>

  <TextBlock Grid.Row="1" x:Name="CountLine" Margin="2,0,2,8"
             Foreground="__TEXT_DIM__"/>

  <Grid Grid.Row="2">
    <DataGrid x:Name="Grid" CanUserAddRows="False" RowHeight="42">
      <DataGrid.Columns>
        <DataGridTemplateColumn Width="46" CanUserResize="False">
          <DataGridTemplateColumn.CellTemplate>
            <DataTemplate>
              <CheckBox Style="{StaticResource BrandCheck}"
                        IsChecked="{Binding IsSelected, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                        HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </DataTemplate>
          </DataGridTemplateColumn.CellTemplate>
        </DataGridTemplateColumn>
        <DataGridTextColumn Header="Rev #" Binding="{Binding RevNumber}" Width="70"/>
        <DataGridTextColumn Header="Description" Binding="{Binding Description}" Width="*">
          <DataGridTextColumn.ElementStyle>
            <Style TargetType="TextBlock">
              <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
            </Style>
          </DataGridTextColumn.ElementStyle>
        </DataGridTextColumn>
        <DataGridTextColumn Header="Issued To" Binding="{Binding IssuedTo}" Width="110"/>
        <DataGridTextColumn Header="Issued By" Binding="{Binding IssuedBy}" Width="100"/>
        <DataGridTextColumn Header="Status" Binding="{Binding StatusText}" Width="70">
          <DataGridTextColumn.ElementStyle>
            <Style TargetType="TextBlock">
              <Setter Property="Foreground" Value="__WARN__"/>
              <Setter Property="FontWeight" Value="Medium"/>
            </Style>
          </DataGridTextColumn.ElementStyle>
        </DataGridTextColumn>
        <DataGridTextColumn Header="# Clouds" Binding="{Binding CloudCount}" Width="80">
          <DataGridTextColumn.ElementStyle>
            <Style TargetType="TextBlock">
              <Setter Property="HorizontalAlignment" Value="Right"/>
            </Style>
          </DataGridTextColumn.ElementStyle>
        </DataGridTextColumn>
      </DataGrid.Columns>
    </DataGrid>

    <!-- Empty state, toggled from code. Sits on top of the empty grid, offset
         by the header height so the column headers stay visible and the window
         keeps its shape. -->
    <TextBlock x:Name="EmptyState" Visibility="Collapsed"
               Text="No revision clouds found in this model."
               Foreground="__TEXT_MUTED__" Margin="0,42,0,0"
               HorizontalAlignment="Center" VerticalAlignment="Center"/>
  </Grid>
</Grid>
'''
_STEP1_BODY = _tok(_STEP1_BODY)

_STEP1_FOOTER = u'''
<Grid>
  <StackPanel Orientation="Horizontal" HorizontalAlignment="Left"
              VerticalAlignment="Center">
    <Button x:Name="BtnCheckAll"   Content="Check All"
            Style="{StaticResource BtnGhost}" Margin="0,0,6,0"/>
    <Button x:Name="BtnUncheckAll" Content="Uncheck All"
            Style="{StaticResource BtnGhost}"/>
  </StackPanel>
  <StackPanel Orientation="Horizontal" HorizontalAlignment="Right"
              VerticalAlignment="Center">
    <Button x:Name="BtnExit" Content="Exit"
            Style="{StaticResource BtnGhost}" Margin="0,0,8,0"/>
    <Button x:Name="BtnNext" Content="Next  &#8594;"
            Style="{StaticResource BtnPrimary}"/>
  </StackPanel>
</Grid>
'''


class CloudPanel(object):
    """Controller for the XAML window. Mirrors the surface the wizard loop
    expects: .ShowDialog(), .result_code, .selected_clouds."""

    def __init__(self, doc, uidoc):
        self.win = ui.parse(
            u'Cloud Manager', u'Step 1 of 4 \u00b7 Select revision clouds',
            _STEP1_BODY, _STEP1_FOOTER, width=900, height=640,
            context=doc.Title)
        self.win.MinWidth  = 720
        self.win.MinHeight = 480

        self.doc = doc
        self.uidoc = uidoc
        self.result_code = ACTION_EXIT
        self.selected_clouds = []

        # resolve named elements
        f = self.win.FindName
        self.Search = f('Search'); self.SearchPh = f('SearchPh')
        self.BtnActiveView = f('BtnActiveView'); self.BtnAllInModel = f('BtnAllInModel')
        self.BtnClear = f('BtnClear')
        self.BtnCheckAll = f('BtnCheckAll'); self.BtnUncheckAll = f('BtnUncheckAll')
        self.BtnExit = f('BtnExit'); self.BtnNext = f('BtnNext')
        self.Grid = f('Grid'); self.EmptyState = f('EmptyState'); self.CountLine = f('CountLine')

        self._own_to_revit()

        # data
        self._all_rows, self._total_clouds = collect_revision_groups()
        self._bulk = False
        for row in self._all_rows:
            row.add_PropertyChanged(self._on_row_changed)
        self._search = u''
        self._visible = ObservableCollection[object]()
        self.Grid.ItemsSource = self._visible

        # events - drag, minimise, maximise and close all belong to the
        # library chrome now. Closing from the X leaves result_code at its
        # default, ACTION_EXIT, which is what the old handler set by hand.
        self.win.PreviewKeyDown += self._on_key

        # events - toolbar. These are SELECTION actions, not view filters:
        # they tick/untick rows, they never hide them. Same as the legacy tool.
        self.Search.TextChanged += self._on_search
        self.BtnActiveView.Click += self._sel_active_view
        self.BtnAllInModel.Click += self._sel_all_model
        self.BtnClear.Click += self._sel_clear

        # events - grid (click anywhere on a row toggles its checkbox)
        self.Grid.PreviewMouseLeftButtonUp += self._on_grid_click

        # events - footer
        self.BtnCheckAll.Click += lambda s, e: self._set_all(True)
        self.BtnUncheckAll.Click += lambda s, e: self._set_all(False)
        self.BtnExit.Click += lambda s, e: self._close(ACTION_EXIT)
        self.BtnNext.Click += self._next

        self._rebuild()

    # ---- public ----
    def ShowDialog(self):
        return self.win.ShowDialog()

    # ---- host integration ----
    def _own_to_revit(self):
        _own_to_revit(self.win)

    def _on_key(self, sender, e):
        if e.Key == Key.Escape:
            self._close(ACTION_EXIT)
        elif e.Key == Key.Enter and self.BtnNext.IsEnabled:
            self._next(sender, e)

    # ---- search (the ONLY thing that hides rows) ----
    def _on_search(self, sender, e):
        self._search = (self.Search.Text or u'').strip().lower()
        self.SearchPh.Visibility = WVisibility.Collapsed if self._search else WVisibility.Visible
        self._rebuild()

    def _matches(self, row):
        if not self._search:
            return True
        hay = u' '.join([row.RevNumber, row.Description,
                         row.IssuedTo, row.IssuedBy]).lower()
        return self._search in hay

    def _rebuild(self):
        self._visible.Clear()
        for row in self._all_rows:
            if self._matches(row):
                self._visible.Add(row)
        empty = self._visible.Count == 0
        if empty:
            self.EmptyState.Text = (u'No revisions match your search.' if self._search
                                    else u'No revision clouds found in this model.')
        self.EmptyState.Visibility = WVisibility.Visible if empty else WVisibility.Collapsed
        # The grid stays visible even with zero rows so the column headers do
        # not vanish; EmptyState is overlaid below them.
        self._update_count()

    # ---- selection actions (toolbar) ----
    def _set_checked(self, predicate):
        """Replace the whole checked set, like the legacy toolbar buttons do."""
        self._bulk = True
        try:
            for row in self._all_rows:
                row.IsSelected = bool(predicate(row))
        finally:
            self._bulk = False
        self._update_count()

    def _sel_active_view(self, sender, e):
        """Check every revision that owns at least one cloud in the active view."""
        try:
            view_id = self.uidoc.ActiveView.Id
        except Exception:
            view_id = None
        ids = set(_all_clouds(view_id).keys()) if view_id else set()
        self._set_checked(lambda r: any(_id_val(c.Id) in ids for c in r.Clouds))

    def _sel_all_model(self, sender, e):
        self._set_checked(lambda r: True)

    def _sel_clear(self, sender, e):
        self._set_checked(lambda r: False)

    # ---- selection (footer + grid) ----
    def _on_row_changed(self, sender, e):
        if not self._bulk:
            self._update_count()

    def _on_grid_click(self, sender, e):
        """Toggle the row under the cursor. Clicks that land on the checkbox
        itself are left alone so the CheckBox does not get toggled twice."""
        node = e.OriginalSource
        while node is not None:
            if isinstance(node, ToggleButton):
                return
            if isinstance(node, DataGridRow):
                item = node.Item
                if item is not None:
                    try:
                        item.IsSelected = not item.IsSelected
                    except Exception:
                        pass
                return
            try:
                node = VisualTreeHelper.GetParent(node)
            except Exception:
                return

    def _set_all(self, value):
        """Check All / Uncheck All act on what is currently listed."""
        self._bulk = True
        try:
            for row in self._visible:
                row.IsSelected = value
        finally:
            self._bulk = False
        self._update_count()

    # ---- status line ----
    def _update_count(self):
        total_revs = len(self._all_rows)
        shown = self._visible.Count
        sel_rows = [r for r in self._all_rows if r.IsSelected]
        n_sel = len(sel_rows)
        n_clouds = sum(r.CloudCount for r in sel_rows)
        flt = (u'  (showing {0:,} of {1:,} revisions)'.format(shown, total_revs)
               if shown != total_revs else u'')

        if total_revs == 0:
            self.CountLine.Text = u'No revision clouds found in this model.'
            self.CountLine.Foreground = BRUSH_BAD
        elif n_sel == 0:
            self.CountLine.Text = u'{0:,} revisions  ·  {1:,} clouds total — none selected.{2}'.format(
                total_revs, self._total_clouds, flt)
            self.CountLine.Foreground = BRUSH_DIM
        else:
            self.CountLine.Text = u'{0:,} revision{1} selected  ·  {2:,} cloud{3} total.{4}'.format(
                n_sel, u's' if n_sel != 1 else u'',
                n_clouds, u's' if n_clouds != 1 else u'', flt)
            self.CountLine.Foreground = BRUSH_OK

        self.BtnNext.IsEnabled = n_sel > 0

    # ---- navigation ----
    def _next(self, sender, e):
        clouds = []
        for row in self._all_rows:
            if row.IsSelected:
                clouds.extend(row.Clouds)
        if not clouds:
            return  # keep the user here until they pick something
        self.selected_clouds = clouds
        self._close(ACTION_NEXT)

    def _close(self, code):
        self.result_code = code
        self.win.Close()


# ============================================================
#  STEP 2 -- Choose Scope
# ============================================================

VIEW_TYPE_NAMES = {
    "FloorPlan":       "Floor Plan",
    "CeilingPlan":     "Ceiling Plan",
    "Elevation":       "Elevation",
    "Section":         "Section",
    "Detail":          "Detail",
    "ThreeD":          "3D View",
    "DraftingView":    "Drafting",
    "Legend":          "Legend",
    "EngineeringPlan": "Engineering Plan",
    "AreaPlan":        "Area Plan",
    "Schedule":        "Schedule",
    "DrawingSheet":    "Sheet",
    "Walkthrough":     "Walkthrough",
    "Rendering":       "Rendering",
}

def _get_sheets_for_clouds(clouds, extra_views=None):
    """Return a dict {sheet_id_int: ViewSheet} of every sheet associated with
    the given clouds.

    A cloud is 'associated' with a sheet if:
      - It is placed directly on that sheet (OwnerViewId == sheet.Id), OR
      - Its owner view (floor plan / elevation / section) is placed on that
        sheet via a Viewport.

    extra_views: optional dict {id_int: view} whose ViewSheet entries are also
    included (e.g. the user's explicit scope selection)."""
    # Build viewport map: owner-view id (int) -> [sheet, ...]
    vp_map = {}
    for vp in DB.FilteredElementCollector(doc).OfClass(DB.Viewport):
        sh = doc.GetElement(vp.SheetId)
        if sh is not None:
            vp_map.setdefault(_id_val(vp.ViewId), []).append(sh)

    sheets = {}
    for c in clouds:
        try:
            owner_v = doc.GetElement(c.OwnerViewId)
            if owner_v is None:
                continue
            if isinstance(owner_v, DB.ViewSheet):
                sheets[_id_val(owner_v.Id)] = owner_v
            else:
                for sh in vp_map.get(_id_val(owner_v.Id), []):
                    sheets[_id_val(sh.Id)] = sh
        except Exception:
            pass

    if extra_views:
        for v in extra_views.values():
            if isinstance(v, DB.ViewSheet):
                sheets[_id_val(v.Id)] = v

    return sheets


def _filter_editable_clouds(clouds):
    """If the model is workshared, split clouds into (editable, blocked).

    editable : list of clouds we can modify (free or already owned by us)
    blocked  : list of (cloud, owner_name) tuples owned by another user

    If the model is NOT workshared, all clouds are returned as editable.
    """
    if not doc.IsWorkshared:
        return list(clouds), []

    editable = []
    blocked  = []
    try:
        from Autodesk.Revit.DB import WorksharingUtils, CheckoutStatus
        for c in clouds:
            try:
                status = WorksharingUtils.GetCheckoutStatus(doc, c.Id)
                if status == CheckoutStatus.OwnedByOtherUser:
                    try:
                        info  = WorksharingUtils.GetWorksharingTooltipInfo(doc, c.Id)
                        owner = info.Owner if info and info.Owner else u"another user"
                    except Exception:
                        owner = u"another user"
                    blocked.append((c, owner))
                else:
                    editable.append(c)
            except Exception:
                editable.append(c)   # if we can't check, try anyway
    except Exception:
        return list(clouds), []      # worksharing API unavailable — process all

    return editable, blocked


def _is_view_editable(view):
    """Return True if we can modify this view (no other user has it checked out).

    When a view is owned by another user, calling SetRevisionVisibility /
    HideElements / UnhideElements on it will make Revit show its native
    worksharing-conflict dialog BEFORE our try/except can intercept.
    Checking first avoids the popup entirely.
    """
    if not doc.IsWorkshared:
        return True
    try:
        from Autodesk.Revit.DB import WorksharingUtils, CheckoutStatus
        status = WorksharingUtils.GetCheckoutStatus(doc, view.Id)
        return status != CheckoutStatus.OwnedByOtherUser
    except Exception:
        return True  # if the check itself fails, try the operation anyway


class _WSFailureSilencer(DB.IFailuresPreprocessor):
    """Intercepts worksharing ownership failures at transaction commit time.

    When GetCheckoutStatus returned 'free' because the local model is stale
    (another user owns the element in central but the local file doesn't know
    yet), Revit raises a FailureSeverity.Error at COMMIT — after our per-line
    try/except has already passed.  This preprocessor catches every such failure
    BEFORE Revit shows its native dialog, extracts the owner name from the
    failure description text, forces the partial commit to proceed silently, and
    fills self.blocked_views so Step 4 can display the details.
    """
    def __init__(self):
        self.blocked_views = []   # list of (view_name, owner_name)

    def PreprocessFailures(self, fa):
        for msg in list(fa.GetFailureMessages()):
            try:
                desc = msg.GetDescriptionText()
                # Message text: "Can't edit the element until '<user name>'
                # resaves the element to central and relinquishes it…"
                m = re.search(r"'([^']+)'", desc)
                owner = m.group(1) if m else u"another user"
                for eid in msg.GetFailingElements():
                    try:
                        elem = doc.GetElement(eid)
                        vname = u"ID:" + str(_id_val(eid))
                        if elem is not None:
                            try:
                                vname = elem.Name
                            except Exception:
                                pass
                        self.blocked_views.append((vname, owner))
                    except Exception:
                        self.blocked_views.append((u"(unknown)", owner))
                sev = msg.GetSeverity()
                if sev == DB.FailureSeverity.Warning:
                    fa.DeleteWarning(msg)
                else:
                    fa.ResolveFailure(msg)
            except Exception:
                pass
        return DB.FailureProcessingResult.ProceedWithCommit


@contextlib.contextmanager
def _ws_transaction(name):
    """Raw DB.Transaction + _WSFailureSilencer.

    Replaces `with revit.Transaction(name):` for operations that touch
    view ownership.  Any stale-lock failures at commit are silently swallowed
    and collected in silencer.blocked_views.

    Usage::

        with _ws_transaction("My Action") as silencer:
            v.HideElements(...)   # may fail silently if view is stale-locked
        bv_details.extend(silencer.blocked_views)
    """
    silencer = _WSFailureSilencer()
    t = DB.Transaction(doc, name)
    t.Start()
    opts = t.GetFailureHandlingOptions()
    opts.SetFailuresPreprocessor(silencer)
    t.SetFailureHandlingOptions(opts)
    try:
        yield silencer
        if t.HasStarted():
            t.Commit()
    except Exception:
        if t.HasStarted():
            t.RollBack()
        raise


def _blocked_summary(blocked):
    """Build a short feedback string from a list of (cloud, owner) tuples."""
    if not blocked:
        return u""
    owners = {}
    for c, owner in blocked:
        owners.setdefault(owner, 0)
        owners[owner] += 1
    parts = [u"{} ({:,})".format(o, n) for o, n in owners.items()]
    return u"  |  {:,} cloud(s) locked by: {}".format(
        len(blocked), u", ".join(parts))


class StatusRow(object):
    """One line of the Final Status table. Plain object -- nothing is editable."""
    def __init__(self, rev_num=u"", rev_desc=u"", view_name=u"", owner=u"",
                 cloud_id=u"", is_view_row=False, is_separator=False):
        self.RevNum = rev_num
        self.RevDesc = rev_desc
        self.ViewName = view_name
        self.Owner = owner
        self.CloudId = cloud_id
        self.IsViewRow = is_view_row
        self.IsSeparator = is_separator


_STATUS_BODY = u'''
<Grid>
  <Grid.RowDefinitions>
    <RowDefinition Height="Auto"/>   <!-- headline strip -->
    <RowDefinition Height="*"/>      <!-- grid -->
  </Grid.RowDefinitions>

  <!-- Headline strip. These three lines carry meaning, so they keep a colour,
       and every one of those colours is a status token. -->
  <Border Grid.Row="0" Margin="0,0,0,12" CornerRadius="10" Padding="14,12"
          Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1">
    <StackPanel>
      <TextBlock x:Name="LblOk" FontWeight="Medium" Foreground="__OK__"/>
      <TextBlock x:Name="LblBlocked" Margin="0,5,0,0" Foreground="__TEXT_DIM__"/>
      <TextBlock x:Name="LblBlockedViews" Margin="0,4,0,0"
                 Visibility="Collapsed" Foreground="__WARN__"/>
    </StackPanel>
  </Border>

  <DataGrid Grid.Row="1" x:Name="Grid" CanUserAddRows="False" RowHeight="36">
    <DataGrid.RowStyle>
      <!-- Bespoke on purpose: the tint is what tells a locked VIEW row apart
           from a locked cloud row, and what greys out the separator. Extends
           the library row so hover and selection survive. -->
      <Style TargetType="DataGridRow" BasedOn="{StaticResource {x:Type DataGridRow}}">
        <Style.Triggers>
          <DataTrigger Binding="{Binding IsViewRow}" Value="True">
            <Setter Property="Background" Value="__WARN_T__"/>
          </DataTrigger>
          <DataTrigger Binding="{Binding IsSeparator}" Value="True">
            <Setter Property="Background" Value="__CARD_BG__"/>
            <Setter Property="FontStyle"  Value="Italic"/>
            <Setter Property="Foreground" Value="__TEXT_DIM__"/>
          </DataTrigger>
        </Style.Triggers>
      </Style>
    </DataGrid.RowStyle>
    <DataGrid.Columns>
      <DataGridTextColumn Header="Rev #" Binding="{Binding RevNum}" Width="70"/>
      <DataGridTextColumn Header="Revision description" Binding="{Binding RevDesc}" Width="*">
        <DataGridTextColumn.ElementStyle>
          <Style TargetType="TextBlock">
            <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
            <Setter Property="ToolTip" Value="{Binding RevDesc}"/>
          </Style>
        </DataGridTextColumn.ElementStyle>
      </DataGridTextColumn>
      <DataGridTextColumn Header="View (owner)" Binding="{Binding ViewName}" Width="220">
        <DataGridTextColumn.ElementStyle>
          <Style TargetType="TextBlock">
            <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
          </Style>
        </DataGridTextColumn.ElementStyle>
      </DataGridTextColumn>
      <DataGridTextColumn Header="Locked by" Binding="{Binding Owner}" Width="220">
        <DataGridTextColumn.ElementStyle>
          <Style TargetType="TextBlock">
            <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
          </Style>
        </DataGridTextColumn.ElementStyle>
      </DataGridTextColumn>
      <DataGridTextColumn Header="Cloud ID" Binding="{Binding CloudId}" Width="90">
        <DataGridTextColumn.ElementStyle>
          <Style TargetType="TextBlock">
            <Setter Property="Foreground" Value="__TEXT_DIM__"/>
          </Style>
        </DataGridTextColumn.ElementStyle>
      </DataGridTextColumn>
    </DataGrid.Columns>
  </DataGrid>
</Grid>
'''
_STATUS_BODY = _tok(_STATUS_BODY)

_STATUS_FOOTER = u'''
<Grid>
  <StackPanel Orientation="Horizontal" HorizontalAlignment="Right"
              VerticalAlignment="Center">
    <Button x:Name="BtnExport" Content="Export Status"
            Style="{StaticResource BtnGhost}" Margin="0,0,8,0"/>
    <Button x:Name="BtnCloseBtn" Content="Close"
            Style="{StaticResource BtnPrimary}"/>
  </StackPanel>
</Grid>
'''


class FinalStatusDialog(object):
    """Step 4 -- lists, line by line, the clouds and views that could not be
    edited because another user owns them in the workshared model.

    Keeps the legacy name and `show()` so _show_final_status needs no edits."""

    def __init__(self, action_label, modified_count, blocked, blocked_views=None):
        """
        action_label  : str   -- name of the action ("Hide", "Tag Only", ...)
        modified_count: int   -- how many clouds were processed OK
        blocked       : list  -- list of (cloud, owner_name)
        blocked_views : list  -- list of (view_name, owner_name)  [optional]
        """
        if blocked_views is None:
            blocked_views = []
        self._action_label   = action_label
        self._modified_count = modified_count
        self._blocked        = blocked
        self._blocked_views  = blocked_views

        self.win = ui.parse(
            u'Cloud Manager', u'Step 4 of 4 \u00b7 Status',
            _STATUS_BODY, _STATUS_FOOTER, width=940, height=620)
        self.win.MinWidth  = 780
        self.win.MinHeight = 460

        f = self.win.FindName
        self.LblOk = f('LblOk'); self.LblBlocked = f('LblBlocked')
        self.LblBlockedViews = f('LblBlockedViews')
        self.Grid = f('Grid')
        self.BtnExport = f('BtnExport'); self.BtnCloseBtn = f('BtnCloseBtn')

        _own_to_revit(self.win)

        self.LblOk.Text = u'✓  {0:,} cloud(s) modified  —  action: {1}'.format(
            modified_count, action_label)
        self.LblBlocked.Text = u'✗  {0:,} cloud(s) not modified (locked by another user)'.format(
            len(blocked))
        if blocked:
            self.LblBlocked.Foreground = BRUSH_BAD
        if blocked_views:
            self.LblBlockedViews.Text = (
                u'✗  {0:,} view(s) locked (revision visibility could not be changed)'.format(
                    len(blocked_views)))
            self.LblBlockedViews.Visibility = WVisibility.Visible

        # ---- rows (same order and same content as the legacy ListView) ----
        items = ObservableCollection[object]()
        for cloud, owner in blocked:
            try:
                rev_n, rev_d, _x, _y = cloud_fields(cloud)
            except Exception:
                rev_n, rev_d = u"--", u"--"
            try:
                ov = doc.GetElement(cloud.OwnerViewId)
                view_name = ov.Name if ov else u"--"
            except Exception:
                view_name = u"--"
            items.Add(StatusRow(rev_n, rev_d, view_name, owner, str(_id_val(cloud.Id))))

        if blocked_views:
            items.Add(StatusRow(u"── LOCKED VIEWS ──",
                                is_separator=True))
            for view_name, owner in blocked_views:
                items.Add(StatusRow(u"", u"(revision visibility)", view_name, owner,
                                    u"view", is_view_row=True))
        self.Grid.ItemsSource = items

        # ---- events (the chrome is the library's) ----
        self.BtnCloseBtn.Click += lambda s, e: self.win.Close()
        self.BtnExport.Click += self._do_export
        self.win.PreviewKeyDown += self._on_key

    # ---- legacy-compatible surface ----
    def show(self):
        self.win.ShowDialog()

    # ---- window plumbing ----
    def _on_key(self, sender, e):
        if e.Key == Key.Escape:
            self.win.Close()

    # ---- export, same report as the legacy dialog ----
    def _do_export(self, s=None, e=None):
        try:
            sfd = SaveFileDialog()
            sfd.Title  = u"Export Final Status"
            sfd.Filter = u"Text files (*.txt)|*.txt|All files (*.*)|*.*"
            sfd.FileName = u"CloudManager_FinalStatus.txt"
            if sfd.ShowDialog() != DialogResult.OK:
                return
            lines = []
            lines.append(u"CloudManager -- Final Status")
            lines.append(u"Action: {}".format(self._action_label))
            lines.append(u"Clouds modified: {:,}".format(self._modified_count))
            lines.append(u"")
            if self._blocked:
                lines.append(u"LOCKED CLOUDS ({:,})".format(len(self._blocked)))
                lines.append(u"-" * 60)
                for cloud, owner in self._blocked:
                    try:
                        rev_n, rev_d, _x, _y = cloud_fields(cloud)
                    except Exception:
                        rev_n, rev_d = u"--", u"--"
                    try:
                        ov = doc.GetElement(cloud.OwnerViewId)
                        vn = ov.Name if ov else u"--"
                    except Exception:
                        vn = u"--"
                    lines.append(u"Rev {:<6} | {:40} | View: {:30} | Owner: {:20} | ID: {}".format(
                        rev_n, rev_d[:40], vn[:30], owner[:20], str(_id_val(cloud.Id))))
                lines.append(u"")
            if self._blocked_views:
                lines.append(u"LOCKED VIEWS ({:,})".format(len(self._blocked_views)))
                lines.append(u"-" * 60)
                for vname, owner in self._blocked_views:
                    lines.append(u"View: {:40} | Owner: {}".format(vname[:40], owner))
                lines.append(u"")
            import System.IO
            System.IO.File.WriteAllLines(sfd.FileName,
                System.Array[System.String](lines),
                System.Text.Encoding.UTF8)
            ui.alert(u"Exported to:\n{}".format(sfd.FileName),
                     title=u"Export OK")
        except Exception as ex:
            ui.alert(u"Export error:\n{}".format(str(ex)), title=u"Error")


def _show_final_status(action_label, modified_count, blocked, blocked_views=None):
    """Open FinalStatusDialog when clouds OR views were locked."""
    if blocked_views is None:
        blocked_views = []
    if not blocked and not blocked_views:
        return
    dlg = FinalStatusDialog(action_label, modified_count, blocked, blocked_views)
    dlg.show()


def _get_rev_ids(clouds):
    """Return list of unique revision ElementIds from selected clouds.
    Uses IntegerValue > 0 comparison to avoid IronPython reference-equality
    pitfalls with DB.ElementId.InvalidElementId (IntegerValue == -1)."""
    seen = {}
    for c in clouds:
        try:
            p = c.get_Parameter(DB.BuiltInParameter.REVISION_CLOUD_REVISION)
            if p:
                rid = p.AsElementId()
                if rid is not None and _id_val(rid) > 0:
                    seen[_id_val(rid)] = rid
        except Exception:
            pass
    return list(seen.values())


def _rev_label(rev_ids):
    """Human-readable list of revision numbers, for the project-wide-change
    confirmation in front of Tag Only / Cloud + Tag."""
    names = []
    for rid in rev_ids:
        rev = doc.GetElement(rid)
        if rev is not None:
            names.append(rev_num(rev) or rev_desc(rev) or u"?")
    return u", ".join(names) if names else u"the selected revision(s)"


def _get_tag_ids_for_clouds(clouds, view):
    """Return ElementIds of revision-cloud tag elements in *view* that tag any of the
    given clouds.  Uses OST_RevisionCloudTags category (catches all tag types) and
    tries both the Revit 2023+ API (GetTaggedLocalElementIds) and the older
    TaggedLocalElementId property so it works across Revit versions."""
    cloud_int_ids = {_id_val(c.Id) for c in clouds}
    tag_ids = []
    try:
        tags = (DB.FilteredElementCollector(doc, view.Id)
                   .OfCategory(DB.BuiltInCategory.OST_RevisionCloudTags)
                   .WhereElementIsNotElementType()
                   .ToElements())
        for tag in tags:
            try:
                matched = False
                if hasattr(tag, 'GetTaggedLocalElementIds'):
                    for tid in tag.GetTaggedLocalElementIds():
                        if _id_val(tid) in cloud_int_ids:
                            matched = True; break
                if not matched and hasattr(tag, 'TaggedLocalElementId'):
                    tid = tag.TaggedLocalElementId
                    if tid is not None and _id_val(tid) in cloud_int_ids:
                        matched = True
                if not matched and hasattr(tag, 'TaggedElementId'):
                    tid = tag.TaggedElementId
                    if tid is not None and _id_val(tid) in cloud_int_ids:
                        matched = True
                if matched:
                    tag_ids.append(tag.Id)
            except Exception:
                pass
    except Exception:
        pass
    return tag_ids


def _bb_intersects_xy(bb1, bb2):
    """Return True if two BoundingBoxXYZ overlap in the XY plane."""
    return (bb1.Max.X >= bb2.Min.X and bb1.Min.X <= bb2.Max.X and
            bb1.Max.Y >= bb2.Min.Y and bb1.Min.Y <= bb2.Max.Y)


def friendly_view_type(v):
    vt = str(v.ViewType)
    return VIEW_TYPE_NAMES.get(vt, vt)

# ============================================================
#  STEP 2 -- Choose Scope   [slantisui]
# ============================================================
#  Port of the legacy WinForms ScopeSelectionDialog, now drawn by slantisui.
#  Every piece of behaviour is preserved:
#    - _build_cloud_view_info is copied verbatim (pure Revit logic, no UI)
#    - the same state lives in the same attributes: _current_scope,
#      _cv_checked_keys, _checked_view_ids, _sorted_cv_data, _all_views
#      (_checked_view_ids tracks view ElementIds, not Names -- fixed
#      2026-09-25, QA had two views sharing a name bleed into each other)
#    - the same strings, the same counts, the same green/orange semantics
#    - `scope` and `selected_views` return exactly what Step 3 expects
# ============================================================

class _CheckRow(INotifyPropertyChanged):
    """Shared INotifyPropertyChanged plumbing for the Step 2 grids."""
    PropertyChanged = None

    def __init__(self):
        self._selected = False
        self._handlers = []
        self._owner = None      # panel notified on toggle

    def add_PropertyChanged(self, h):
        self._handlers.append(h)

    def remove_PropertyChanged(self, h):
        if h in self._handlers:
            self._handlers.remove(h)

    def _notify(self, name):
        for h in list(self._handlers):
            h(self, PropertyChangedEventArgs(name))

    def get_IsSelected(self):
        return self._selected

    def set_IsSelected(self, value):
        if self._selected != value:
            self._selected = value
            self._notify('IsSelected')
            if self._owner is not None:
                self._owner._on_row_toggled(self)

    IsSelected = property(get_IsSelected, set_IsSelected)


class CvRow(_CheckRow):
    """One row of the cloud-distribution table."""
    def __init__(self, data, display):
        _CheckRow.__init__(self)
        self.Data = data
        self.Key = data['_key']
        self.Display = display
        self.RevName = data['rev_name']
        self.Sheet = data['sheet']
        self.TypeName = data['type']
        self.Count = data['count']


class ViewRow(_CheckRow):
    """One row of the all-views picker.

    Identity is the view's ElementId (`ViewId`) -- never its Name. Two views
    can share the exact same name (e.g. a Floor Plan and a Ceiling Plan both
    named "Level 1"), and checking one used to check the other too."""
    def __init__(self, view, type_name):
        _CheckRow.__init__(self)
        self.View = view
        self.ViewId = _id_val(view.Id)
        if isinstance(view, DB.ViewSheet):
            self.Name = u'{0} - {1}'.format(view.SheetNumber, view.Name)
        else:
            self.Name = view.Name
        self.TypeName = type_name


_SCOPE_BODY = u'''
<Grid>
  <Grid.Resources>
    <!-- ScopeCard: a RadioButton drawn as a selectable card. RadioButton is
         one of the controls the library does not style, so this template is
         legitimately bespoke; every colour inside it is a marker filled in
         from ui.* by _tok(), never a hex typed here. -->
    <Style x:Key="ScopeCard" TargetType="RadioButton">
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="RadioButton">
            <Border x:Name="card" CornerRadius="10" Padding="14,12"
                    Background="__WIN_BG__" BorderBrush="__INPUT_BD__"
                    BorderThickness="1">
              <StackPanel Orientation="Horizontal">
                <Grid Width="16" Height="16" VerticalAlignment="Top" Margin="0,2,10,0">
                  <Ellipse x:Name="ring" Stroke="__CHECK_BD__" StrokeThickness="1.5"
                           Fill="__WIN_BG__"/>
                  <Ellipse x:Name="dot" Width="8" Height="8" Fill="__ACCENT__"
                           Visibility="Collapsed"/>
                </Grid>
                <StackPanel>
                  <TextBlock x:Name="ttl" Text="{TemplateBinding Content}"
                             FontWeight="Medium"/>
                  <TextBlock x:Name="sub" Text="{TemplateBinding Tag}" FontSize="12"
                             Foreground="__TEXT_DIM__" Margin="0,3,0,0"
                             TextWrapping="Wrap"/>
                </StackPanel>
              </StackPanel>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="card" Property="Background" Value="__ROW_HOVER__"/>
              </Trigger>
              <Trigger Property="IsChecked" Value="True">
                <Setter TargetName="card" Property="Background"  Value="__ROW_SEL__"/>
                <Setter TargetName="card" Property="BorderBrush" Value="__ACCENT__"/>
                <Setter TargetName="ring" Property="Stroke"      Value="__ACCENT__"/>
                <Setter TargetName="dot"  Property="Visibility"  Value="Visible"/>
                <Setter TargetName="ttl"  Property="Foreground"  Value="__ACCENT_DK__"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
  </Grid.Resources>

  <Grid.RowDefinitions>
    <RowDefinition Height="Auto"/>   <!-- 0 step-1 summary -->
    <RowDefinition Height="Auto"/>   <!-- 1 scope cards -->
    <RowDefinition Height="Auto"/>   <!-- 2 distribution header + search -->
    <RowDefinition Height="*"/>      <!-- 3 distribution grid -->
    <RowDefinition Height="Auto"/>   <!-- 4 distribution actions -->
    <RowDefinition Height="Auto"/>   <!-- 5 specific-views panel -->
    <RowDefinition Height="Auto"/>   <!-- 6 scope status -->
  </Grid.RowDefinitions>

  <!-- Step-1 summary. Green because it reports something that went well. -->
  <Border Grid.Row="0" CornerRadius="10" Padding="14,11" Background="__OK_T__">
    <TextBlock x:Name="LblStep1" FontWeight="Medium" Foreground="__OK__"/>
  </Border>

  <StackPanel Grid.Row="1" Margin="0,16,0,0">
    <TextBlock Text="WHERE SHOULD ACTIONS APPLY?"
               Style="{StaticResource SectionHead}" Margin="0,0,0,8"/>
    <Grid>
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/><ColumnDefinition Width="*"/>
        <ColumnDefinition Width="*"/>
      </Grid.ColumnDefinitions>
      <RadioButton Grid.Column="0" x:Name="ScActive" Style="{StaticResource ScopeCard}"
                   GroupName="scope" Content="Active View Only"
                   Tag="Current open view only" Margin="0,0,6,0" IsChecked="True"/>
      <RadioButton Grid.Column="1" x:Name="ScChosen" Style="{StaticResource ScopeCard}"
                   GroupName="scope" Content="Select Specific Views"
                   Tag="Choose from the list below" Margin="6,0"/>
      <RadioButton Grid.Column="2" x:Name="ScModel" Style="{StaticResource ScopeCard}"
                   GroupName="scope" Content="All Views in Model"
                   Tag="Every non-template view" Margin="6,0,0,0"/>
    </Grid>
  </StackPanel>

  <Grid Grid.Row="2" Margin="0,16,0,8">
    <Grid.ColumnDefinitions>
      <ColumnDefinition Width="Auto"/><ColumnDefinition Width="*"/>
    </Grid.ColumnDefinitions>
    <TextBlock Grid.Column="0" x:Name="LblCvHeader" FontWeight="Medium"
               VerticalAlignment="Center"/>
    <Grid Grid.Column="1" Margin="16,0,0,0">
      <TextBox x:Name="CvSearch" Height="32"/>
      <TextBlock x:Name="CvSearchPh" Text="Search views&#8230;"
                 Foreground="__TEXT_MUTED__" IsHitTestVisible="False"
                 VerticalAlignment="Center" Margin="11,0,0,0"/>
    </Grid>
  </Grid>

  <Grid Grid.Row="3">
    <DataGrid x:Name="CvGrid" MinHeight="150" CanUserAddRows="False" RowHeight="38">
      <DataGrid.Columns>
        <DataGridTemplateColumn Width="46" CanUserResize="False">
          <DataGridTemplateColumn.CellTemplate>
            <DataTemplate>
              <CheckBox Style="{StaticResource BrandCheck}"
                        IsChecked="{Binding IsSelected, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                        HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </DataTemplate>
          </DataGridTemplateColumn.CellTemplate>
        </DataGridTemplateColumn>
        <DataGridTextColumn Header="View Name" Binding="{Binding Display}" Width="*">
          <DataGridTextColumn.ElementStyle>
            <Style TargetType="TextBlock">
              <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
            </Style>
          </DataGridTextColumn.ElementStyle>
        </DataGridTextColumn>
        <DataGridTextColumn Header="Revision" Binding="{Binding RevName}" Width="230">
          <DataGridTextColumn.ElementStyle>
            <Style TargetType="TextBlock">
              <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
            </Style>
          </DataGridTextColumn.ElementStyle>
        </DataGridTextColumn>
        <DataGridTextColumn Header="Sheet" Binding="{Binding Sheet}" Width="160">
          <DataGridTextColumn.ElementStyle>
            <Style TargetType="TextBlock">
              <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
            </Style>
          </DataGridTextColumn.ElementStyle>
        </DataGridTextColumn>
        <DataGridTextColumn Header="Type" Binding="{Binding TypeName}" Width="110"/>
        <DataGridTextColumn Header="# Clouds" Binding="{Binding Count}" Width="80">
          <DataGridTextColumn.ElementStyle>
            <Style TargetType="TextBlock">
              <Setter Property="HorizontalAlignment" Value="Right"/>
            </Style>
          </DataGridTextColumn.ElementStyle>
        </DataGridTextColumn>
      </DataGrid.Columns>
    </DataGrid>
    <TextBlock x:Name="CvEmpty" Visibility="Collapsed" Margin="0,40,0,0"
               Text="No views match your search." Foreground="__TEXT_MUTED__"
               HorizontalAlignment="Center" VerticalAlignment="Center"/>
  </Grid>

  <Grid Grid.Row="4" Margin="0,10,0,0">
    <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
      <Button x:Name="BtnCvAll"  Content="Check All"
              Style="{StaticResource BtnGhost}" Margin="0,0,6,0"/>
      <Button x:Name="BtnCvNone" Content="Uncheck All"
              Style="{StaticResource BtnGhost}" Margin="0,0,12,0"/>
      <TextBlock x:Name="LblCvStatus" VerticalAlignment="Center"
                 Foreground="__TEXT_DIM__"/>
    </StackPanel>
    <StackPanel Orientation="Horizontal" HorizontalAlignment="Right"
                VerticalAlignment="Center">
      <Button x:Name="BtnExport" Content="Export to Excel"
              Style="{StaticResource BtnGhost}" Margin="0,0,6,0"/>
      <Button x:Name="BtnUseCv" Content="Apply to checked views  &#8594;"
              Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>

  <!-- Specific-views panel, shown only for the "Select Specific Views" scope -->
  <Border Grid.Row="5" x:Name="DetailPanel" Visibility="Collapsed"
          Margin="0,14,0,0" Padding="14" CornerRadius="10"
          Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1">
    <Grid>
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/><RowDefinition Height="200"/>
        <RowDefinition Height="Auto"/>
      </Grid.RowDefinitions>
      <Grid Grid.Row="0" Margin="0,0,0,10">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/><ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <Grid Grid.Column="0" Margin="0,0,10,0">
          <TextBox x:Name="VwSearch" Height="32"/>
          <TextBlock x:Name="VwSearchPh" Text="Search all views&#8230;"
                     Foreground="__TEXT_MUTED__" IsHitTestVisible="False"
                     VerticalAlignment="Center" Margin="11,0,0,0"/>
        </Grid>
        <ComboBox Grid.Column="1" x:Name="CmbType" Width="190"/>
      </Grid>

      <DataGrid Grid.Row="1" x:Name="VwGrid" CanUserAddRows="False" RowHeight="34">
        <DataGrid.Columns>
          <DataGridTemplateColumn Width="46" CanUserResize="False">
            <DataGridTemplateColumn.CellTemplate>
              <DataTemplate>
                <CheckBox Style="{StaticResource BrandCheck}"
                          IsChecked="{Binding IsSelected, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                          HorizontalAlignment="Center" VerticalAlignment="Center"/>
              </DataTemplate>
            </DataGridTemplateColumn.CellTemplate>
          </DataGridTemplateColumn>
          <DataGridTextColumn Header="View Name" Binding="{Binding Name}" Width="*">
            <DataGridTextColumn.ElementStyle>
              <Style TargetType="TextBlock">
                <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
              </Style>
            </DataGridTextColumn.ElementStyle>
          </DataGridTextColumn>
          <DataGridTextColumn Header="Type" Binding="{Binding TypeName}" Width="170"/>
        </DataGrid.Columns>
      </DataGrid>

      <Grid Grid.Row="2" Margin="0,10,0,0">
        <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
          <Button x:Name="BtnVwActive" Content="Check Active View"
                  Style="{StaticResource BtnGhost}" Margin="0,0,6,0"/>
          <Button x:Name="BtnVwAll" Content="Check All"
                  Style="{StaticResource BtnGhost}" Margin="0,0,6,0"/>
          <Button x:Name="BtnVwNone" Content="Uncheck All"
                  Style="{StaticResource BtnGhost}"/>
        </StackPanel>
        <TextBlock x:Name="LblVwCount" HorizontalAlignment="Right"
                   VerticalAlignment="Center" Foreground="__TEXT_DIM__"/>
      </Grid>
    </Grid>
  </Border>

  <TextBlock Grid.Row="6" x:Name="LblScope" Margin="0,14,0,0" FontWeight="Medium"
             Foreground="__TEXT_DIM__"/>
</Grid>
'''
_SCOPE_BODY = _tok(_SCOPE_BODY)

_SCOPE_FOOTER = u'''
<Grid>
  <Button x:Name="BtnBack" Content="&#8592;  Back" Style="{StaticResource BtnGhost}"
          HorizontalAlignment="Left" VerticalAlignment="Center"/>
  <StackPanel Orientation="Horizontal" HorizontalAlignment="Right"
              VerticalAlignment="Center">
    <Button x:Name="BtnExit" Content="Exit"
            Style="{StaticResource BtnGhost}" Margin="0,0,8,0"/>
    <Button x:Name="BtnNext" Content="Next  &#8594;"
            Style="{StaticResource BtnPrimary}"/>
  </StackPanel>
</Grid>
'''


class ScopePanel(object):

    def __init__(self, selected_clouds):
        self._sel_clouds       = selected_clouds
        self._all_views        = None
        self._filt_views       = []
        self._views_loaded     = False
        self._current_scope    = SCOPE_ACTIVE
        self._model_view_count = None   # cached lazily when MODEL scope is first selected
        self._checked_view_ids = set()  # set of view ElementId values -- not names
        self.result_code       = ACTION_EXIT
        self._bulk             = False  # suppress per-row callbacks during a rebuild
        self._syncing          = False  # suppress radio handlers during programmatic checks

        self.win = ui.parse(
            u'Cloud Manager', u'Step 2 of 4 \u00b7 Choose scope',
            _SCOPE_BODY, _SCOPE_FOOTER, width=980, height=780)
        self.win.MinWidth  = 820
        self.win.MinHeight = 600

        f = self.win.FindName
        self.LblStep1 = f('LblStep1')
        self.ScActive = f('ScActive'); self.ScChosen = f('ScChosen'); self.ScModel = f('ScModel')
        self.LblCvHeader = f('LblCvHeader')
        self.CvSearch = f('CvSearch'); self.CvSearchPh = f('CvSearchPh')
        self.CvGrid = f('CvGrid'); self.CvEmpty = f('CvEmpty')
        self.BtnCvAll = f('BtnCvAll'); self.BtnCvNone = f('BtnCvNone')
        self.LblCvStatus = f('LblCvStatus')
        self.BtnExport = f('BtnExport'); self.BtnUseCv = f('BtnUseCv')
        self.DetailPanel = f('DetailPanel')
        self.VwSearch = f('VwSearch'); self.VwSearchPh = f('VwSearchPh'); self.CmbType = f('CmbType')
        self.VwGrid = f('VwGrid'); self.LblVwCount = f('LblVwCount')
        self.BtnVwActive = f('BtnVwActive'); self.BtnVwAll = f('BtnVwAll'); self.BtnVwNone = f('BtnVwNone')
        self.LblScope = f('LblScope')
        self.BtnBack = f('BtnBack'); self.BtnExit = f('BtnExit'); self.BtnNext = f('BtnNext')

        _own_to_revit(self.win)

        n = len(selected_clouds)
        self.LblStep1.Text = u'{0:,} revision cloud{1} selected in Step 1.'.format(
            n, u's' if n != 1 else u'')

        # ---- cloud distribution (same logic and same sort as the legacy) ----
        self._cloud_view_info = self._build_cloud_view_info()
        n_cv = len(self._cloud_view_info)
        self.LblCvHeader.Text = u'Cloud distribution — found in {0:,} view{1}:'.format(
            n_cv, u's' if n_cv != 1 else u'')

        self._sorted_cv_data = sorted(
            self._cloud_view_info.values(),
            key=lambda d: (d.get('sheet_num', u'~'), -d['count'], d['view'].Name.lower())
        )
        for d in self._sorted_cv_data:
            rev_int = _id_val(d['rev_id']) if d['rev_id'] else -1
            d['_key'] = u'{}_{}'.format(_id_val(d['vid']), rev_int)
        # initially all rows are checked
        self._cv_checked_keys = set(d['_key'] for d in self._sorted_cv_data)

        self._cv_rows = ObservableCollection[object]()
        self.CvGrid.ItemsSource = self._cv_rows
        self._vw_rows = ObservableCollection[object]()
        self.VwGrid.ItemsSource = self._vw_rows

        # ---- events (the chrome is the library's) ----
        self.win.PreviewKeyDown += self._on_key

        self.ScActive.Checked += self._quick_active
        self.ScChosen.Checked += self._quick_chosen
        self.ScModel.Checked  += self._quick_model

        self.CvSearch.TextChanged += self._on_cv_search
        self.CvGrid.PreviewMouseLeftButtonUp += self._on_grid_click
        self.BtnCvAll.Click += self._cv_chk_all
        self.BtnCvNone.Click += self._cv_unchk_all
        self.BtnExport.Click += self._export_cv_info
        self.BtnUseCv.Click += self._use_cloud_views

        self.VwSearch.TextChanged += self._on_view_filter
        self.CmbType.SelectionChanged += self._on_view_filter
        self.VwGrid.PreviewMouseLeftButtonUp += self._on_grid_click
        self.BtnVwActive.Click += self._chk_active_view
        self.BtnVwAll.Click += self._chk_all_views
        self.BtnVwNone.Click += self._unchk_all_views

        self.BtnBack.Click += lambda s, e: self._close(ACTION_BACK)
        self.BtnExit.Click += lambda s, e: self._close(ACTION_EXIT)
        self.BtnNext.Click += lambda s, e: self._close(ACTION_NEXT)

        self.CmbType.Items.Add(u'(All types)')
        self.CmbType.SelectedIndex = 0

        self._rebuild_cv_list()
        self._upd_scope_status()

    # ---- public ----
    def ShowDialog(self):
        return self.win.ShowDialog()

    def _on_key(self, sender, e):
        if e.Key == Key.Escape:
            self._close(ACTION_EXIT)

    def _close(self, code):
        self.result_code = code
        self.win.Close()

    # ---- shared grid behaviour: click a row to toggle it ----
    def _on_grid_click(self, sender, e):
        node = e.OriginalSource
        while node is not None:
            if isinstance(node, ToggleButton):
                return
            if isinstance(node, DataGridRow):
                item = node.Item
                if item is not None:
                    try:
                        item.IsSelected = not item.IsSelected
                    except Exception:
                        pass
                return
            try:
                node = VisualTreeHelper.GetParent(node)
            except Exception:
                return

    def _on_row_toggled(self, row):
        """Mirror of the legacy _on_lv_cv_check / _on_view_check: keep the
        persistent key/name sets in sync so filtering never loses state."""
        if self._bulk:
            return
        if isinstance(row, CvRow):
            if row.IsSelected:
                self._cv_checked_keys.add(row.Key)
            else:
                self._cv_checked_keys.discard(row.Key)
            self._upd_cv_status()
        else:
            if row.IsSelected:
                self._checked_view_ids.add(row.ViewId)
            else:
                self._checked_view_ids.discard(row.ViewId)
            self._upd_scope_status()
            self._upd_view_count()

    # ---- scope selection ----
    def _quick_active(self, s, e):
        if self._syncing: return
        self._current_scope = SCOPE_ACTIVE
        self.DetailPanel.Visibility = WVisibility.Collapsed
        self._upd_scope_status()

    def _quick_chosen(self, s, e):
        if self._syncing: return
        self._current_scope = SCOPE_CHOSEN
        self.DetailPanel.Visibility = WVisibility.Visible
        if not self._views_loaded:
            self._load_views()
        self._upd_scope_status()

    def _quick_model(self, s, e):
        if self._syncing: return
        self._current_scope = SCOPE_MODEL
        self.DetailPanel.Visibility = WVisibility.Collapsed
        if self._model_view_count is None:
            self._model_view_count = len(all_views_dict())
        self._upd_scope_status()

    # ---- all-views picker ----
    def _load_views(self):
        self._views_loaded = True
        raw = sorted(all_views_dict().values(), key=lambda v: v.Name)
        self._all_views = [(v, friendly_view_type(v)) for v in raw]
        vt_set = sorted(set(t for _, t in self._all_views))
        self._syncing = True
        try:
            self.CmbType.Items.Clear()
            self.CmbType.Items.Add(u'(All types)')
            for t in vt_set:
                self.CmbType.Items.Add(t)
            self.CmbType.SelectedIndex = 0
        finally:
            self._syncing = False
        self._rebuild_view_list()

    def _rebuild_view_list(self):
        if not self._views_loaded or self._all_views is None:
            return
        q = (self.VwSearch.Text or u'').strip().lower()
        vt = u''
        if self.CmbType.SelectedItem:
            sel = unicode(self.CmbType.SelectedItem)
            if sel != u'(All types)':
                vt = sel
        self._bulk = True
        try:
            self._vw_rows.Clear()
            self._filt_views = []
            # Use the persistent set -- never read from visible rows (that
            # would lose the state of anything hidden by the filter).
            checked_ids = self._checked_view_ids
            for idx, (v, t) in enumerate(self._all_views):
                if q and q not in v.Name.lower():
                    continue
                if vt and t != vt:
                    continue
                row = ViewRow(v, t)
                row.IsSelected = row.ViewId in checked_ids
                row._owner = self
                self._vw_rows.Add(row)
                self._filt_views.append(idx)
        finally:
            self._bulk = False
        self._upd_view_count()

    def _upd_view_count(self):
        if self._all_views is None:
            return
        shown = len(self._filt_views)
        checked = len(self._checked_view_ids)
        total = len(self._all_views)
        if shown == total:
            self.LblVwCount.Text = u'Showing {0:,} views  |  {1:,} checked'.format(shown, checked)
        else:
            self.LblVwCount.Text = u'Showing {0:,} of {1:,}  |  {2:,} checked'.format(
                shown, total, checked)

    def _on_view_filter(self, s, e):
        if self._syncing: return
        self.VwSearchPh.Visibility = (WVisibility.Collapsed
                                      if (self.VwSearch.Text or u'').strip()
                                      else WVisibility.Visible)
        self._rebuild_view_list()
        self._upd_scope_status()

    def _chk_active_view(self, s, e):
        if not self._views_loaded: return
        self._checked_view_ids = set([_id_val(doc.ActiveView.Id)])
        self._rebuild_view_list()
        self._upd_scope_status()

    def _chk_all_views(self, s, e):
        # Check ALL views in model, not just the visible (filtered) subset
        if not self._views_loaded: return
        self._checked_view_ids = set(_id_val(v.Id) for v, t in self._all_views)
        self._rebuild_view_list()
        self._upd_scope_status()

    def _unchk_all_views(self, s, e):
        if not self._views_loaded: return
        self._checked_view_ids = set()
        self._rebuild_view_list()
        self._upd_scope_status()

    # ---- cloud distribution ----
    def _cv_display_name(self, d):
        v_obj = d['view']
        if isinstance(v_obj, DB.ViewSheet):
            return u'{} - {}'.format(v_obj.SheetNumber, v_obj.Name)
        return v_obj.Name

    def _on_cv_search(self, s, e):
        self.CvSearchPh.Visibility = (WVisibility.Collapsed
                                      if (self.CvSearch.Text or u'').strip()
                                      else WVisibility.Visible)
        self._rebuild_cv_list()

    def _rebuild_cv_list(self):
        q = (self.CvSearch.Text or u'').strip().lower()
        self._bulk = True
        try:
            self._cv_rows.Clear()
            for d in self._sorted_cv_data:
                display_name = self._cv_display_name(d)
                if q:
                    haystack = u' '.join([display_name,
                                          d.get('rev_name', u''),
                                          d.get('sheet', u''),
                                          d.get('type', u'')]).lower()
                    if q not in haystack:
                        continue
                row = CvRow(d, display_name)
                row.IsSelected = d['_key'] in self._cv_checked_keys
                row._owner = self
                self._cv_rows.Add(row)
        finally:
            self._bulk = False
        empty = self._cv_rows.Count == 0
        self.CvEmpty.Visibility = WVisibility.Visible if empty else WVisibility.Collapsed
        self._upd_cv_status()

    def _upd_cv_status(self):
        total = len(self._sorted_cv_data)
        checked = len(self._cv_checked_keys)
        self.LblCvStatus.Text = u'{0:,} of {1:,} view{2} selected'.format(
            checked, total, u's' if total != 1 else u'')
        self.LblCvStatus.Foreground = BRUSH_OK if checked else BRUSH_WARN

    def _cv_chk_all(self, s, e):
        # Legacy behaviour: acts on the rows currently listed
        self._bulk = True
        try:
            for row in self._cv_rows:
                row.IsSelected = True
                self._cv_checked_keys.add(row.Key)
        finally:
            self._bulk = False
        self._upd_cv_status()

    def _cv_unchk_all(self, s, e):
        self._bulk = True
        try:
            for row in self._cv_rows:
                row.IsSelected = False
                self._cv_checked_keys.discard(row.Key)
        finally:
            self._bulk = False
        self._upd_cv_status()

    def _export_cv_info(self, s, e):
        """Export the cloud distribution table to a CSV file (opens in Excel)."""
        dlg = SaveFileDialog()
        dlg.Title  = "Export Cloud Distribution"
        dlg.Filter = "CSV files (*.csv)|*.csv|All files (*.*)|*.*"
        dlg.DefaultExt = "csv"
        dlg.FileName   = "CloudDistribution.csv"
        if dlg.ShowDialog() != DialogResult.OK:
            return
        try:
            import System.IO
            sw = System.IO.StreamWriter(dlg.FileName, False, System.Text.Encoding.UTF8)
            cols = [u"View Name", u"Revision", u"Sheet", u"Type", u"# Clouds"]
            sw.WriteLine(u",".join(u'"{}"'.format(c.replace(u'"', u'""')) for c in cols))
            for row in self._cv_rows:
                vals = [row.Display, row.RevName, row.Sheet, row.TypeName, unicode(row.Count)]
                sw.WriteLine(u",".join(u'"{}"'.format(v.replace(u'"', u'""')) for v in vals))
            sw.Close()
        except Exception as ex:
            ui.alert(str(ex), title=u"Export Error")

    def _use_cloud_views(self, s, e):
        """Set scope to SCOPE_CHOSEN using only the views checked in the distribution
        table. Multiple rows may share a view (different revisions) -- dedupe by vid."""
        checked_int_ids = set()
        for key in self._cv_checked_keys:
            try:
                checked_int_ids.add(int(unicode(key).split(u'_')[0]))
            except Exception:
                pass
        if not checked_int_ids:
            return
        self._current_scope = SCOPE_CHOSEN
        self._syncing = True
        try:
            self.ScChosen.IsChecked = True
        finally:
            self._syncing = False
        # keep the picker hidden -- the selection was already made above
        self.DetailPanel.Visibility = WVisibility.Collapsed
        if not self._views_loaded:
            self._load_views()
        self._checked_view_ids = set(
            _id_val(v.Id) for v, t in self._all_views if _id_val(v.Id) in checked_int_ids)
        self._rebuild_view_list()
        self._upd_scope_status()

    # ---- status ----
    def _upd_scope_status(self):
        if self._current_scope == SCOPE_ACTIVE:
            self.LblScope.Text = u'Scope: active view only — ' + doc.ActiveView.Name + u'.'
            self.LblScope.Foreground = BRUSH_OK
        elif self._current_scope == SCOPE_CHOSEN:
            n = len(self._checked_view_ids)
            if n:
                self.LblScope.Text = u'Scope: {0:,} view{1} selected.'.format(
                    n, u's' if n != 1 else u'')
                self.LblScope.Foreground = BRUSH_OK
            else:
                self.LblScope.Text = u'Scope: no views checked yet.'
                self.LblScope.Foreground = BRUSH_WARN
        else:
            n_all = len(self._all_views) if self._all_views else (self._model_view_count or 0)
            self.LblScope.Text = u'Scope: all {0:,} views in the model.'.format(n_all)
            self.LblScope.Foreground = BRUSH_OK

    # ---- results (identical contract to the legacy dialog) ----
    @property
    def scope(self):
        return self._current_scope

    @property
    def selected_views(self):
        if self._current_scope == SCOPE_ACTIVE:
            v = doc.ActiveView
            return {_id_val(v.Id): v}
        elif self._current_scope == SCOPE_CHOSEN:
            checked = self._checked_view_ids
            result = {}
            if self._all_views:
                for v, t in self._all_views:
                    if _id_val(v.Id) in checked:
                        result[_id_val(v.Id)] = v
            return result
        else:
            return all_views_dict()

    # ---- Revit logic, copied verbatim from the legacy dialog ----
    def _build_cloud_view_info(self):
        """Build a dict keyed by (view_int_id, rev_int_id) so clouds from different
        revisions in the same view appear as separate rows."""

        # --- sheet map (via Viewport for reliable lookup) ---
        # Stores (sheet_number, full_label) per view id
        sheet_map = {}
        try:
            for vp in DB.FilteredElementCollector(doc).OfClass(DB.Viewport).ToElements():
                try:
                    sh = doc.GetElement(vp.SheetId)
                    if sh:
                        num   = sh.SheetNumber or u""
                        title = sh.Name        or u""
                        label = u"{} – {}".format(num, title) if title else num
                        key   = _id_val(vp.ViewId)
                        if key not in sheet_map:
                            sheet_map[key] = (num, label)
                except Exception:
                    pass
        except Exception:
            pass

        # --- dependent-view map ---
        parent_ids = set()
        for c in self._sel_clouds:
            vid = c.OwnerViewId
            if vid and vid != DB.ElementId.InvalidElementId:
                parent_ids.add(_id_val(vid))

        dep_map = {}
        for pid in parent_ids:
            pv = doc.GetElement(_make_eid(pid))
            if pv is None:
                dep_map[pid] = []; continue
            try:
                deps = [doc.GetElement(did) for did in pv.GetDependentViewIds()]
                dep_map[pid] = [dv for dv in deps if dv and not getattr(dv, "IsTemplate", False)]
            except Exception:
                dep_map[pid] = []

        # helper: get revision ElementId from a cloud
        def _rev_id(c):
            try:
                p = c.get_Parameter(DB.BuiltInParameter.REVISION_CLOUD_REVISION)
                if p:
                    rid = p.AsElementId()
                    if rid and rid != DB.ElementId.InvalidElementId:
                        return rid
            except Exception:
                pass
            return None

        def _rev_name(rid):
            try:
                r = doc.GetElement(rid)
                if r:
                    num = r.SequenceNumber
                    desc = r.Description or u""
                    return u"Rev {} {}".format(num, desc).strip()
            except Exception:
                pass
            return u"Unknown"

        # --- assign each cloud to (view, revision) bucket ---
        info = {}   # key: (view_int_id, rev_int_id) -> dict

        def _add(view_obj, view_id, rev_id_obj):
            rev_int = _id_val(rev_id_obj) if rev_id_obj else -1
            key = (_id_val(view_id), rev_int)
            if key not in info:
                sheet_entry = sheet_map.get(_id_val(view_id))
                if sheet_entry:
                    s_num, s_label = sheet_entry
                else:
                    s_num, s_label = u"~", u"—"   # sorts last; display as dash
                info[key] = {
                    "view":      view_obj,
                    "vid":       view_id,
                    "rev_id":    rev_id_obj,
                    "rev_name":  _rev_name(rev_id_obj) if rev_id_obj else u"—",
                    "count":     0,
                    "type":      friendly_view_type(view_obj),
                    "sheet":     s_label,
                    "sheet_num": s_num,
                }
            info[key]["count"] += 1

        for c in self._sel_clouds:
            parent_vid = c.OwnerViewId
            if parent_vid is None or parent_vid == DB.ElementId.InvalidElementId:
                continue
            parent_v = doc.GetElement(parent_vid)
            if parent_v is None or getattr(parent_v, "IsTemplate", False):
                continue
            rid = _rev_id(c)

            deps = dep_map.get(_id_val(parent_vid), [])
            if not deps:
                _add(parent_v, parent_vid, rid)
                continue

            try:
                cloud_bb = c.get_BoundingBox(parent_v)
            except Exception:
                cloud_bb = None

            if cloud_bb is None:
                _add(parent_v, parent_vid, rid)
                continue

            matched = [dv for dv in deps if _bb_intersects_xy(cloud_bb, dv.CropBox) if dv.CropBox]
            if matched:
                for dv in matched:
                    _add(dv, dv.Id, rid)
            else:
                _add(parent_v, parent_vid, rid)

        return info


# ============================================================
#  STEP 3 -- Style sub-dialog  (per-cloud)
# ============================================================
class StyleRow(INotifyPropertyChanged):
    """One row of the style dialog. Keeps `_color` as a System.Drawing.Color
    because _do_style hands it straight to to_db_color()."""
    PropertyChanged = None

    def __init__(self, cloud_info, patterns, weights):
        self._handlers = []
        self._selected = True          # legacy: every row starts checked
        self.cloud     = cloud_info["cloud"]
        self.cloud_id  = cloud_info["id"]
        self.CloudIdText = u"ID {}".format(cloud_info["id"])
        self.RevNum    = cloud_info["rev_num"]
        self.RevDesc   = cloud_info["rev_desc"]
        self.Sheet     = cloud_info["view_name"]
        self.Patterns  = patterns
        self.Weights   = weights
        # Starts empty -- "no change" -- not pre-loaded with a colour/pattern/
        # weight. Accepting the dialog untouched used to paint every cloud red
        # (Color.Red was the old default and get_style() always returned it).
        # None / "" / 0 mean "leave this override alone"; only a value the
        # user actually picked in the grid gets applied by _do_style.
        self._color    = None
        self._pattern  = u""
        self._weight   = 0

    def add_PropertyChanged(self, h):
        self._handlers.append(h)

    def remove_PropertyChanged(self, h):
        if h in self._handlers:
            self._handlers.remove(h)

    def _notify(self, name):
        for h in list(self._handlers):
            h(self, PropertyChangedEventArgs(name))

    def get_IsSelected(self):
        return self._selected

    def set_IsSelected(self, v):
        if self._selected != v:
            self._selected = v
            self._notify('IsSelected')
    IsSelected = property(get_IsSelected, set_IsSelected)

    def get_ColorBrush(self):
        return _brush(self._color)
    ColorBrush = property(get_ColorBrush)

    def get_PatternName(self):
        return self._pattern

    def set_PatternName(self, v):
        if v is not None and self._pattern != v:
            self._pattern = v
            self._notify('PatternName')
    PatternName = property(get_PatternName, set_PatternName)

    def get_Weight(self):
        return self._weight

    def set_Weight(self, v):
        if v is not None and self._weight != v:
            self._weight = v
            self._notify('Weight')
    Weight = property(get_Weight, set_Weight)

    # ---- same surface the legacy CloudStyleRow exposed ----
    def set_color(self, dcolor):
        self._color = dcolor
        self._notify('ColorBrush')

    def set_style(self, color, pattern, weight):
        """Batch-apply a style (called from the Quick-Apply bar)."""
        self.set_color(color)
        self.PatternName = pattern
        self.Weight = int(weight)

    @property
    def is_checked(self):
        return self._selected

    def get_style(self):
        return {"color":        self._color,
                "pattern_name": self._pattern,
                "weight":       int(self._weight)}


_STYLE_BODY = u'''
<Grid>
  <Grid.Resources>
    <!-- Swatch: a Button whose whole face IS the colour the user picked, so it
         cannot inherit a background from anywhere. Button is a library-owned
         control, hence BasedOn; only the Template is replaced. Tag="color" is
         load-bearing: _on_grid_button() uses it to tell a swatch click apart
         from any other click bubbling out of the grid. -->
    <Style x:Key="Swatch" TargetType="Button" BasedOn="{StaticResource BtnGhost}">
      <Setter Property="Width"  Value="34"/>
      <Setter Property="Height" Value="22"/>
      <Setter Property="Tag"    Value="color"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="sw" CornerRadius="4" BorderThickness="1"
                    BorderBrush="__INPUT_BD__"
                    Background="{TemplateBinding Background}"/>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="sw" Property="BorderBrush" Value="__ACCENT__"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
  </Grid.Resources>

  <Grid.RowDefinitions>
    <RowDefinition Height="Auto"/>   <!-- caption -->
    <RowDefinition Height="*"/>      <!-- grid -->
    <RowDefinition Height="Auto"/>   <!-- batch bar -->
  </Grid.RowDefinitions>

  <TextBlock Grid.Row="0" x:Name="LblCaption" Margin="0,0,0,10"
             Foreground="__TEXT_DIM__" TextWrapping="Wrap"/>

  <DataGrid Grid.Row="1" x:Name="Grid" CanUserAddRows="False" RowHeight="42">
    <DataGrid.Columns>
      <DataGridTemplateColumn Width="46" CanUserResize="False">
        <DataGridTemplateColumn.CellTemplate>
          <DataTemplate>
            <CheckBox Style="{StaticResource BrandCheck}"
                      IsChecked="{Binding IsSelected, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                      HorizontalAlignment="Center" VerticalAlignment="Center"/>
          </DataTemplate>
        </DataGridTemplateColumn.CellTemplate>
      </DataGridTemplateColumn>
      <DataGridTextColumn Header="Cloud ID" Binding="{Binding CloudIdText}" Width="90">
        <DataGridTextColumn.ElementStyle>
          <Style TargetType="TextBlock">
            <Setter Property="Foreground" Value="__TEXT_DIM__"/>
          </Style>
        </DataGridTextColumn.ElementStyle>
      </DataGridTextColumn>
      <DataGridTextColumn Header="Rev #" Binding="{Binding RevNum}" Width="60"/>
      <DataGridTextColumn Header="Description" Binding="{Binding RevDesc}" Width="*">
        <DataGridTextColumn.ElementStyle>
          <Style TargetType="TextBlock">
            <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
            <Setter Property="ToolTip" Value="{Binding RevDesc}"/>
          </Style>
        </DataGridTextColumn.ElementStyle>
      </DataGridTextColumn>
      <DataGridTextColumn Header="Sheet" Binding="{Binding Sheet}" Width="200">
        <DataGridTextColumn.ElementStyle>
          <Style TargetType="TextBlock">
            <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
            <Setter Property="ToolTip" Value="{Binding Sheet}"/>
          </Style>
        </DataGridTextColumn.ElementStyle>
      </DataGridTextColumn>
      <DataGridTemplateColumn Header="Color" Width="70">
        <DataGridTemplateColumn.CellTemplate>
          <DataTemplate>
            <Button Style="{StaticResource Swatch}" Background="{Binding ColorBrush}"
                    ToolTip="Click to pick a color"/>
          </DataTemplate>
        </DataGridTemplateColumn.CellTemplate>
      </DataGridTemplateColumn>
      <DataGridTemplateColumn Header="Pattern" Width="180">
        <DataGridTemplateColumn.CellTemplate>
          <DataTemplate>
            <ComboBox ItemsSource="{Binding Patterns}"
                      SelectedItem="{Binding PatternName, Mode=TwoWay}"/>
          </DataTemplate>
        </DataGridTemplateColumn.CellTemplate>
      </DataGridTemplateColumn>
      <DataGridTemplateColumn Header="Wt" Width="64">
        <DataGridTemplateColumn.CellTemplate>
          <DataTemplate>
            <ComboBox ItemsSource="{Binding Weights}"
                      SelectedItem="{Binding Weight, Mode=TwoWay}"/>
          </DataTemplate>
        </DataGridTemplateColumn.CellTemplate>
      </DataGridTemplateColumn>
    </DataGrid.Columns>
  </DataGrid>

  <!-- Batch bar -->
  <Border Grid.Row="2" Margin="0,14,0,0" CornerRadius="10" Padding="14,12"
          Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1">
    <StackPanel Orientation="Horizontal">
      <TextBlock Text="Batch style to checked:" FontWeight="Medium"
                 VerticalAlignment="Center" Margin="0,0,12,0"/>
      <Button x:Name="BtnQColor" Style="{StaticResource Swatch}" Width="40" Height="26"
              VerticalAlignment="Center" Margin="0,0,10,0"/>
      <ComboBox x:Name="CmbQPattern" Width="180" VerticalAlignment="Center"
                Margin="0,0,10,0"/>
      <ComboBox x:Name="CmbQWeight" Width="64" VerticalAlignment="Center"
                Margin="0,0,14,0"/>
      <Button x:Name="BtnQApply" Content="Apply to Checked"
              Style="{StaticResource BtnGhost}" VerticalAlignment="Center"
              Margin="0,0,14,0"/>
      <Button x:Name="BtnAll" Content="&#x2713; All" Style="{StaticResource BtnGhost}"
              VerticalAlignment="Center" Margin="0,0,6,0"/>
      <Button x:Name="BtnNone" Content="&#x2717; None" Style="{StaticResource BtnGhost}"
              VerticalAlignment="Center"/>
    </StackPanel>
  </Border>
</Grid>
'''
_STYLE_BODY = _tok(_STYLE_BODY)

_STYLE_FOOTER = u'''
<Grid>
  <TextBlock x:Name="LblCount" HorizontalAlignment="Left" VerticalAlignment="Center"
             Foreground="__TEXT_DIM__"/>
  <StackPanel Orientation="Horizontal" HorizontalAlignment="Right"
              VerticalAlignment="Center">
    <Button x:Name="BtnCancel" Content="Cancel"
            Style="{StaticResource BtnGhost}" Margin="0,0,8,0"/>
    <Button x:Name="BtnOk" Content="Apply Styles"
            Style="{StaticResource BtnPrimary}"/>
  </StackPanel>
</Grid>
'''
_STYLE_FOOTER = _tok(_STYLE_FOOTER)


class StyleSubDialog(object):
    """Per-cloud style assignment window.

    Deliberately keeps the legacy name and a ShowDialog(owner) that returns a
    WinForms DialogResult, so _do_style needs no edits at all."""

    def __init__(self, clouds, pattern_names):
        self._pattern_names = pattern_names
        self._ok = False

        self.win = ui.parse(
            u'Cloud Manager \u00b7 Styles', u'One row per cloud in scope',
            _STYLE_BODY, _STYLE_FOOTER, width=1060, height=700)
        self.win.MinWidth  = 880
        self.win.MinHeight = 480

        f = self.win.FindName
        self.LblCaption = f('LblCaption'); self.LblCount = f('LblCount')
        self.Grid = f('Grid')
        self.BtnQColor = f('BtnQColor'); self.CmbQPattern = f('CmbQPattern')
        self.CmbQWeight = f('CmbQWeight'); self.BtnQApply = f('BtnQApply')
        self.BtnAll = f('BtnAll'); self.BtnNone = f('BtnNone')
        self.BtnOk = f('BtnOk'); self.BtnCancel = f('BtnCancel')

        _own_to_revit(self.win)

        # ---- shared combo sources ----
        self._patterns = ObservableCollection[object]()
        for pn in pattern_names:
            self._patterns.Add(pn)
        self._weights = ObservableCollection[object]()
        for w in range(1, 17):
            self._weights.Add(w)

        # ---- resolve cloud info up-front (verbatim from the legacy dialog) ----
        view_to_sheets = {}
        for vp in DB.FilteredElementCollector(doc).OfClass(DB.Viewport).ToElements():
            sh = doc.GetElement(vp.SheetId)
            if sh:
                view_to_sheets.setdefault(_id_val(vp.ViewId), []).append(sh.Name)

        self._cloud_info = []
        for c in clouds:
            p   = c.get_Parameter(DB.BuiltInParameter.REVISION_CLOUD_REVISION)
            rev = doc.GetElement(p.AsElementId()) if p else None
            ov  = doc.GetElement(c.OwnerViewId)
            if isinstance(ov, DB.ViewSheet):
                sheet_txt = ov.Name
            elif ov is not None:
                names = view_to_sheets.get(_id_val(c.OwnerViewId), [])
                sheet_txt = u", ".join(sorted(names)) if names else u"(no sheet)"
            else:
                sheet_txt = u"(unknown)"
            self._cloud_info.append({
                "cloud":     c,
                "id":        _id_val(c.Id),
                "rev_num":   rev_num(rev)  if rev else u"",
                "rev_desc":  rev_desc(rev) if rev else u"",
                "view_name": sheet_txt,
            })

        self.rows = []
        self._items = ObservableCollection[object]()
        for info in self._cloud_info:
            row = StyleRow(info, self._patterns, self._weights)
            row.add_PropertyChanged(self._on_row_changed)
            self.rows.append(row)
            self._items.Add(row)
        self.Grid.ItemsSource = self._items

        n = len(self.rows)
        self.LblCaption.Text = (
            u'One row per cloud — set color, line pattern and weight, then Apply Styles. '
            u'{0:,} cloud{1} in scope.'.format(n, u's' if n != 1 else u''))

        # ---- batch bar ----
        self._q_color = Color.Red
        self.BtnQColor.Background = _brush(self._q_color)
        for pn in pattern_names:
            self.CmbQPattern.Items.Add(pn)
        if pattern_names:
            self.CmbQPattern.SelectedIndex = 0
        for w in range(1, 17):
            self.CmbQWeight.Items.Add(w)
        self.CmbQWeight.SelectedIndex = 0

        # ---- events. The chrome is the library's: closing from the X leaves
        # self._ok False, exactly like the old BtnClose handler did.
        self.win.PreviewKeyDown += self._on_key

        # Per-row swatch clicks bubble up as Button.Click; catch them here so the
        # DataTemplate needs no code-behind.
        self.Grid.AddHandler(WButton.ClickEvent, RoutedEventHandler(self._on_grid_button))
        self.Grid.PreviewMouseLeftButtonUp += self._on_grid_click

        self.BtnQColor.Click += self._q_pick_color
        self.BtnQApply.Click += self._q_apply
        self.BtnAll.Click += self._chk_all
        self.BtnNone.Click += self._chk_none
        self.BtnOk.Click += lambda s, e: self._done(True)
        self.BtnCancel.Click += lambda s, e: self._done(False)

        self._upd_count()

    # ---- legacy-compatible surface ----
    def ShowDialog(self, owner=None):
        """`owner` is accepted and ignored so the legacy call
        `dlg.ShowDialog(_hwnd)` keeps working untouched."""
        self.win.ShowDialog()
        return DialogResult.OK if self._ok else DialogResult.Cancel

    def get_styles(self):
        """Returns {cloud_id_int: style_dict} for every checked row."""
        return {row.cloud_id: row.get_style()
                for row in self.rows if row.is_checked}

    # ---- window plumbing ----
    def _on_key(self, sender, e):
        if e.Key == Key.Escape:
            self._done(False)

    def _done(self, ok):
        self._ok = ok
        self.win.Close()

    # ---- rows ----
    def _on_row_changed(self, sender, e):
        self._upd_count()

    def _upd_count(self):
        n = sum(1 for r in self.rows if r.is_checked)
        self.LblCount.Text = u'{0:,} of {1:,} cloud{2} checked'.format(
            n, len(self.rows), u's' if len(self.rows) != 1 else u'')
        self.BtnOk.IsEnabled = n > 0

    def _on_grid_button(self, sender, e):
        """A swatch inside the grid was clicked -> pick that row's color."""
        src = e.OriginalSource
        try:
            if getattr(src, 'Tag', None) != 'color':
                return
            row = src.DataContext
        except Exception:
            return
        if not isinstance(row, StyleRow):
            return
        e.Handled = True
        dlg = ColorDialog()
        dlg.FullOpen = True
        dlg.Color = row._color
        if dlg.ShowDialog(_hwnd) == DialogResult.OK:
            row.set_color(dlg.Color)

    def _on_grid_click(self, sender, e):
        """Click a row to toggle its checkbox -- but never over the swatch or a
        combo, which have their own jobs."""
        node = e.OriginalSource
        while node is not None:
            if isinstance(node, (ToggleButton, WComboBox, WButton)):
                return
            if isinstance(node, DataGridRow):
                item = node.Item
                if item is not None:
                    try:
                        item.IsSelected = not item.IsSelected
                    except Exception:
                        pass
                return
            try:
                node = VisualTreeHelper.GetParent(node)
            except Exception:
                return

    # ---- batch bar (same behaviour as the legacy Quick-Apply) ----
    def _q_pick_color(self, s, e):
        dlg = ColorDialog(); dlg.FullOpen = True; dlg.Color = self._q_color
        if dlg.ShowDialog(_hwnd) == DialogResult.OK:
            self._q_color = dlg.Color
            self.BtnQColor.Background = _brush(dlg.Color)

    def _q_apply(self, s, e):
        clr = self._q_color
        pat = self.CmbQPattern.SelectedItem or (
            self._pattern_names[0] if self._pattern_names else u"")
        wt  = int(self.CmbQWeight.SelectedItem or 1)
        for row in self.rows:
            if row.is_checked:
                row.set_style(clr, pat, wt)

    def _chk_all(self, s, e):
        for row in self.rows:
            row.IsSelected = True
        self._upd_count()

    def _chk_none(self, s, e):
        for row in self.rows:
            row.IsSelected = False
        self._upd_count()


# ============================================================
#  STEP 3 -- Action Panel
# ============================================================

def _cloud_in_scope(c, views, dep_cache=None):
    """True if cloud c visually appears in any of the given views.
    Uses bounding-box intersection for segment/dependent views.
    dep_cache: optional {owner_id_int: {dep_id_int: ElementId}} pre-built cache."""
    ov_id = c.OwnerViewId
    if ov_id is None:
        return False
    if _id_val(ov_id) in views:
        return True
    ov = doc.GetElement(ov_id)
    if ov is None:
        return False
    # Use pre-built cache when available, otherwise call API
    if dep_cache is not None:
        dep_ids = dep_cache.get(_id_val(ov_id), {})
    else:
        try:
            dep_ids = {_id_val(d): d for d in ov.GetDependentViewIds()}
        except Exception:
            dep_ids = {}
    for sv_id in views:
        if sv_id in dep_ids:
            sv_obj = doc.GetElement(dep_ids[sv_id])
            if sv_obj is None:
                continue
            try:
                cb = sv_obj.CropBox
                if cb:
                    bb = c.get_BoundingBox(ov)
                    if bb and _bb_intersects_xy(bb, cb):
                        return True
                else:
                    return True
            except Exception:
                return True
    return False


_ACTION_BODY = u'''
<Grid>
  <Grid.Resources>
    <!-- The one button on this window that needs its own look: red text says
         "this one deletes" before the confirm dialog does. Extends the library
         ghost button (which hardcodes its border inside the template), so only
         the Foreground changes. -->
    <Style x:Key="BtnDanger" TargetType="Button" BasedOn="{StaticResource BtnGhost}">
      <Setter Property="Foreground" Value="__BAD__"/>
    </Style>
  </Grid.Resources>

  <Grid.RowDefinitions>
    <RowDefinition Height="Auto"/>   <!-- summary -->
    <RowDefinition Height="*"/>      <!-- action rows -->
    <RowDefinition Height="Auto"/>   <!-- feedback -->
  </Grid.RowDefinitions>

  <Border Grid.Row="0" CornerRadius="10" Padding="14,12" Background="__CARD_BG__"
          BorderBrush="__CARD_BD__" BorderThickness="1">
    <StackPanel>
      <TextBlock x:Name="LblClouds" TextTrimming="CharacterEllipsis"/>
      <TextBlock x:Name="LblViews" Margin="0,5,0,0"/>
    </StackPanel>
  </Border>

  <!-- Five action cards. The left stripe and the tint are the only colour on
       this window, and they encode the risk of the row: neutral, informational,
       safe, careful, destructive. Every one of them is a status token. -->
  <StackPanel Grid.Row="1" Margin="0,16,0,0">

    <Border CornerRadius="10" Background="__CARD_BG__" BorderBrush="__CARD_BD__"
            BorderThickness="1" Margin="0,0,0,10" MinHeight="60">
      <Grid>
        <Border Width="5" HorizontalAlignment="Left" Background="__TEXT_MUTED__"
                CornerRadius="10,0,0,10"/>
        <StackPanel Orientation="Horizontal" Margin="21,0,16,0" VerticalAlignment="Center">
          <Button x:Name="BtnReset" Content="Reset Overrides" Width="170"
                  Style="{StaticResource BtnGhost}"/>
          <TextBlock Margin="16,0,0,0" VerticalAlignment="Center" TextWrapping="Wrap"
                     Foreground="__TEXT_DIM__"
                     Text="Remove all graphic overrides from the selected clouds in targeted views."/>
        </StackPanel>
      </Grid>
    </Border>

    <Border CornerRadius="10" Background="__ACCENT_T__" BorderBrush="__CARD_BD__"
            BorderThickness="1" Margin="0,0,0,10" MinHeight="60">
      <Grid>
        <Border Width="5" HorizontalAlignment="Left" Background="__ACCENT__"
                CornerRadius="10,0,0,10"/>
        <StackPanel Orientation="Horizontal" Margin="21,0,16,0" VerticalAlignment="Center">
          <Button x:Name="BtnStyle" Content="Apply Styles" Width="170"
                  Style="{StaticResource BtnGhost}"/>
          <TextBlock Margin="16,0,0,0" VerticalAlignment="Center" TextWrapping="Wrap"
                     Foreground="__TEXT_DIM__"
                     Text="Set color, line pattern and weight per revision number."/>
        </StackPanel>
      </Grid>
    </Border>

    <Border CornerRadius="10" Background="__OK_T__" BorderBrush="__CARD_BD__"
            BorderThickness="1" Margin="0,0,0,10" MinHeight="60">
      <Grid>
        <Border Width="5" HorizontalAlignment="Left" Background="__OK__"
                CornerRadius="10,0,0,10"/>
        <StackPanel Orientation="Horizontal" Margin="21,0,16,0" VerticalAlignment="Center">
          <Button x:Name="BtnShow" Content="Show" Width="120"
                  Style="{StaticResource BtnGhost}"/>
          <Button x:Name="BtnHide" Content="Hide" Width="120" Margin="8,0,0,0"
                  Style="{StaticResource BtnGhost}"/>
          <TextBlock Margin="16,0,0,0" VerticalAlignment="Center" TextWrapping="Wrap"
                     Foreground="__TEXT_DIM__"
                     Text="Show or hide the selected revision clouds (Cloud + Tag) in the targeted views."/>
        </StackPanel>
      </Grid>
    </Border>

    <Border CornerRadius="10" Background="__WARN_T__" BorderBrush="__CARD_BD__"
            BorderThickness="1" Margin="0,0,0,10" MinHeight="60">
      <Grid>
        <Border Width="5" HorizontalAlignment="Left" Background="__WARN__"
                CornerRadius="10,0,0,10"/>
        <StackPanel Orientation="Horizontal" Margin="21,0,16,0" VerticalAlignment="Center">
          <Button x:Name="BtnTagOnly" Content="Tag Only" Width="120"
                  Style="{StaticResource BtnGhost}"/>
          <Button x:Name="BtnCloudTag" Content="Cloud + Tag" Width="130" Margin="8,0,0,0"
                  Style="{StaticResource BtnGhost}"/>
          <TextBlock Margin="16,0,0,0" VerticalAlignment="Center" TextWrapping="Wrap"
                     Foreground="__TEXT_DIM__"
                     Text="Revit visibility: Tag only (cloud hidden) or Cloud + Tag (both visible)."/>
        </StackPanel>
      </Grid>
    </Border>

    <Border CornerRadius="10" Background="__BAD_T__" BorderBrush="__CARD_BD__"
            BorderThickness="1" MinHeight="60">
      <Grid>
        <Border Width="5" HorizontalAlignment="Left" Background="__BAD__"
                CornerRadius="10,0,0,10"/>
        <StackPanel Orientation="Horizontal" Margin="21,0,16,0" VerticalAlignment="Center">
          <Button x:Name="BtnDelete" Content="Delete Clouds" Width="170"
                  Style="{StaticResource BtnDanger}"/>
          <TextBlock Margin="16,0,0,0" VerticalAlignment="Center" TextWrapping="Wrap"
                     Foreground="__TEXT_DIM__"
                     Text="Permanently delete the selected revision clouds from the model."/>
        </StackPanel>
      </Grid>
    </Border>
  </StackPanel>

  <!-- Feedback line. Its colour is set from code, from the status tokens. -->
  <Border Grid.Row="2" Margin="0,14,0,0" CornerRadius="10" Padding="14,10"
          Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1">
    <TextBlock x:Name="LblFb" TextWrapping="Wrap" FontWeight="Medium"
               Foreground="__TEXT_DIM__"/>
  </Border>
</Grid>
'''
_ACTION_BODY = _tok(_ACTION_BODY)

_ACTION_FOOTER = u'''
<Grid>
  <StackPanel Orientation="Horizontal" HorizontalAlignment="Left"
              VerticalAlignment="Center">
    <Button x:Name="BtnBack" Content="&#8592;  Change Scope"
            Style="{StaticResource BtnGhost}" Margin="0,0,6,0"/>
    <Button x:Name="BtnRestart" Content="Start Over"
            Style="{StaticResource BtnGhost}"/>
  </StackPanel>
  <StackPanel Orientation="Horizontal" HorizontalAlignment="Right"
              VerticalAlignment="Center">
    <Button x:Name="BtnExit" Content="Exit" Style="{StaticResource BtnGhost}"/>
  </StackPanel>
</Grid>
'''


class ActionPanel(object):

    def __init__(self, clouds, views):
        # Deduplicate clouds by ElementId (Revit collectors can return duplicates)
        seen = {}
        for c in clouds:
            iid = _id_val(c.Id)
            if iid not in seen:
                seen[iid] = c
        self._clouds = list(seen.values())
        self._views  = views
        self._result_code = ACTION_EXIT

        # --- Performance cache (built once, reused by every action button) ---
        # Viewport map: view_id_int -> [ViewSheet]
        self._vp_map = {}
        for _vp in DB.FilteredElementCollector(doc).OfClass(DB.Viewport).ToElements():
            _sh = doc.GetElement(_vp.SheetId)
            if _sh is not None:
                self._vp_map.setdefault(_id_val(_vp.ViewId), []).append(_sh)
        # Dependent-view cache: owner_view_id_int -> {dep_id_int: ElementId}
        self._dep_cache = {}
        for _c in self._clouds:
            _ov_id = _c.OwnerViewId
            if _ov_id is None:
                continue
            _ov_int = _id_val(_ov_id)
            if _ov_int not in self._dep_cache:
                _ov = doc.GetElement(_ov_id)
                if _ov is not None:
                    try:
                        self._dep_cache[_ov_int] = {
                            _id_val(_d): _d for _d in _ov.GetDependentViewIds()}
                    except Exception:
                        self._dep_cache[_ov_int] = {}
                else:
                    self._dep_cache[_ov_int] = {}

        self._last_blocked       = []   # stores (cloud, owner) from last action
        self._last_blocked_views = []   # stores (view_name, owner) from last action

        self.win = ui.parse(
            u'Cloud Manager', u'Step 3 of 4 \u00b7 Choose an action',
            _ACTION_BODY, _ACTION_FOOTER, width=920, height=700)
        self.win.MinWidth  = 780
        self.win.MinHeight = 600

        f = self.win.FindName
        self.lbl_clouds = f('LblClouds'); self.lbl_views = f('LblViews'); self.lbl_fb = f('LblFb')

        _own_to_revit(self.win)

        # The chrome is the library's. Closing from the X leaves _result_code
        # at ACTION_EXIT, which is what the old BtnClose handler set.
        self.win.PreviewKeyDown += self._on_key

        # Same five rows, same handlers, same order as the legacy panel.
        f('BtnReset').Click    += self._do_reset
        f('BtnStyle').Click    += self._do_style
        f('BtnShow').Click     += self._do_show
        f('BtnHide').Click     += self._do_hide
        f('BtnTagOnly').Click  += self._do_tag_only
        f('BtnCloudTag').Click += self._do_cloud_and_tag
        f('BtnDelete').Click   += self._do_delete

        f('BtnBack').Click    += self._go_back
        f('BtnRestart').Click += self._go_restart
        f('BtnExit').Click    += self._go_exit

        self._refresh_summary()

    # ---- window plumbing ----
    def ShowDialog(self):
        return self.win.ShowDialog()

    def _on_key(self, sender, e):
        if e.Key == Key.Escape:
            self._go_exit(sender, e)

    def _refresh_summary(self):
        nc = len(self._clouds); nv = len(self._views)
        ids = u", ".join(str(_id_val(c.Id)) for c in self._clouds)
        self.lbl_clouds.Text = u"Clouds:  {:,} selected  [IDs: {}]".format(nc, ids)
        self.lbl_clouds.ToolTip = self.lbl_clouds.Text   # the ID list can be long
        self.lbl_views.Text  = u"Scope:   {:,} view{} targeted".format(nv, "s" if nv != 1 else "")

    def _feedback(self, msg, brush=None):
        """`brush` is one of the BRUSH_* status tokens; None means neutral."""
        self.lbl_fb.Text = msg
        self.lbl_fb.Foreground = brush or BRUSH_DIM

    def _views_for_visibility(self):
        """Return the union of user-selected scope views and the primary (parent)
        views of any dependent/segment views in scope.

        SetRevisionVisibility only takes visual effect in the view where the
        cloud was placed (OwnerViewId).  If the active view is a dependent or
        segment view, clouds may be owned by the parent view — so we add the
        parent.  We do NOT add owner views from unrelated parts of the model,
        which would inflate the count when "Active View Only" is selected."""
        views = dict(self._views)
        # Collect primary view IDs of each scope view (parent of dependent views)
        primary_ids = set()
        for v in self._views.values():
            try:
                pid = v.GetPrimaryViewId()
                if pid is not None and pid != DB.ElementId.InvalidElementId:
                    primary_ids.add(_id_val(pid))
            except Exception:
                pass
        # Only add the cloud's owner view if it's the primary/parent of a scope view
        for c in self._clouds:
            try:
                ov_id = c.OwnerViewId
                if ov_id is None or _id_val(ov_id) <= 0:
                    continue
                ov_int = _id_val(ov_id)
                if ov_int not in views and ov_int in primary_ids:
                    ov = doc.GetElement(ov_id)
                    if ov and isinstance(ov, DB.View) and not ov.IsTemplate:
                        views[ov_int] = ov
            except Exception:
                pass
        return views

    def _do_show(self, s, e):
        """Show — identical to Revit's native right-click > Unhide in View > Elements.
        Calls UnhideElements in every scope view. Skips clouds blocked by other users.
        A sheet only accepts the clouds it actually owns (drawn straight on the
        sheet, not through a viewport) -- HideElements/UnhideElements rejects
        ids that do not belong to that view."""
        if not self._clouds or not self._views:
            self._feedback(u"Nothing to show -- no clouds or views.", BRUSH_WARN); return
        editable, blocked = _filter_editable_clouds(self._clouds)
        if not editable:
            self._feedback(u"All clouds are locked." + _blocked_summary(blocked), BRUSH_WARN); return
        cloud_id_list = System.Collections.Generic.List[DB.ElementId](
            [c.Id for c in editable])
        vis_views = self._views_for_visibility()
        errors = []; shown_in = 0
        with revit.Transaction("Show Revision Clouds"):
            for v in vis_views.values():
                if not _view_supports_rev_vis(v): continue
                if isinstance(v, DB.ViewSheet):
                    sheet_ids = [c.Id for c in editable
                                 if c.OwnerViewId is not None
                                 and _id_val(c.OwnerViewId) == _id_val(v.Id)]
                    if not sheet_ids: continue  # nothing drawn directly on this sheet
                    id_list = System.Collections.Generic.List[DB.ElementId](sheet_ids)
                else:
                    id_list = cloud_id_list
                try:
                    v.UnhideElements(id_list)
                    shown_in += 1
                except Exception as ex:
                    errors.append(u"UnhideElements in {}: {}".format(str(v.ViewType), str(ex)))
        uidoc.RefreshActiveView()
        msg = u"Shown in {:,} view(s).  {} error(s).".format(shown_in, len(errors))
        if errors: msg += u"  | " + errors[0][:80]
        msg += _blocked_summary(blocked)
        self._feedback(msg, BRUSH_OK if not errors else BRUSH_WARN)

    def _do_cloud_and_tag(self, s, e):
        """Cloud + Tag — sets rev.Visibility = CloudAndTagVisible at project level.
        Identical behavior to selecting 'Cloud and Tag' in Revit's native
        Revision Manager. Does not touch per-sheet or element-level visibility."""
        scoped = [c for c in self._clouds if _cloud_in_scope(c, self._views, self._dep_cache)]
        if not scoped:
            self._feedback(u"No clouds found in the targeted view(s).", BRUSH_WARN); return
        scoped, blocked = _filter_editable_clouds(scoped)
        if not scoped:
            self._feedback(u"All clouds are locked." + _blocked_summary(blocked), BRUSH_WARN); return
        rev_ids = _get_rev_ids(scoped)
        if not rev_ids:
            self._feedback(u"No revisions found for selected clouds.", BRUSH_WARN); return
        # Revision.Visibility is a project-level property of the Revision
        # element, not of a view -- it always changes every sheet that shows
        # this revision, no matter which scope was chosen in Step 2.
        if not ui.confirm(
                u"This changes revision {0} across the WHOLE project (every "
                u"sheet showing it), not only the view(s) you chose in Step 2."
                u"\n\nContinue?".format(_rev_label(rev_ids)),
                title=u"Confirm Cloud + Tag", yes_text=u"Apply"):
            return
        errors = []; done = 0
        with revit.Transaction("Cloud + Tag - Revision Clouds"):
            for rid in rev_ids:
                try:
                    rev = doc.GetElement(rid)
                    if rev is not None:
                        rev.Visibility = DB.RevisionVisibility.CloudAndTagVisible
                        done += 1
                except Exception as ex:
                    errors.append(str(ex))
        uidoc.RefreshActiveView()
        msg = u"Cloud + Tag: {:,} revision(s) | {} error(s).".format(done, len(errors))
        if errors: msg += u"  | " + errors[0][:150]
        msg += _blocked_summary(blocked)
        self._feedback(msg, BRUSH_OK if done > 0 and not errors else BRUSH_WARN)
        self._set_blocked(blocked)

    def _do_tag_only(self, s, e):
        """Tag Only — project-level rev.Visibility = TagVisible."""
        scoped = [c for c in self._clouds if _cloud_in_scope(c, self._views, self._dep_cache)]
        if not scoped:
            self._feedback(u"No clouds found in the targeted view(s).", BRUSH_WARN); return
        scoped, blocked = _filter_editable_clouds(scoped)
        if not scoped:
            self._feedback(u"All clouds are locked by other users." + _blocked_summary(blocked), BRUSH_WARN); return
        rev_ids = _get_rev_ids(scoped)
        if not rev_ids:
            self._feedback(u"No revisions found for selected clouds.", BRUSH_WARN); return
        # Revision.Visibility is a project-level property of the Revision
        # element, not of a view -- it always changes every sheet that shows
        # this revision, no matter which scope was chosen in Step 2.
        if not ui.confirm(
                u"This changes revision {0} across the WHOLE project (every "
                u"sheet showing it), not only the view(s) you chose in Step 2."
                u"\n\nContinue?".format(_rev_label(rev_ids)),
                title=u"Confirm Tag Only", yes_text=u"Apply"):
            return
        errors = []; done = 0
        with revit.Transaction("Tag Only - Revision Clouds"):
            for rid in rev_ids:
                try:
                    rev = doc.GetElement(rid)
                    if rev is not None:
                        rev.Visibility = DB.RevisionVisibility.TagVisible
                        done += 1
                except Exception as ex:
                    errors.append(str(ex))
        uidoc.RefreshActiveView()
        msg = u"Tag Only: {:,} revision(s) | {} error(s).".format(done, len(errors))
        if errors: msg += u"  | " + errors[0][:150]
        msg += _blocked_summary(blocked)
        self._feedback(msg, BRUSH_OK if done > 0 and not errors else BRUSH_WARN)
        self._set_blocked(blocked)
        _show_final_status(u"Tag Only", done, blocked)

    def _do_show_hide(self, s, e):
        """Toggle Cloud+Tag <-> Hidden.
        - Sheet views: SetRevisionVisibility — revision-level, never touches project-level
          Revision.Visibility in the Revision Manager.
        - Non-sheet views: HideElements / UnhideElements directly on cloud elements so
          the toggle is always visually effective."""
        rev_ids = _get_rev_ids(self._clouds)
        if not rev_ids or not self._views:
            return

        vis_views = self._views_for_visibility()
        cloud_id_list = System.Collections.Generic.List[DB.ElementId](
            [c.Id for c in self._clouds])

        # Determine toggle direction: try GetRevisionVisibility on the first sheet;
        # for non-sheet views fall back to checking CanBeHidden to guess current state.
        going_hidden = True
        try:
            first_rid = rev_ids[0]
            first_sheet = next(
                (v for v in vis_views.values()
                 if isinstance(v, DB.ViewSheet) and _view_supports_rev_vis(v)), None)
            if first_sheet:
                cur = first_sheet.GetRevisionVisibility(_to_rev_id(first_rid))
                going_hidden = (cur != DB.RevisionVisibility.Hidden)
            else:
                # Non-sheet only: check if the cloud element is hidden in the first view
                first_view = next(v for v in vis_views.values() if _view_supports_rev_vis(v))
                c0 = self._clouds[0]
                going_hidden = not first_view.IsElementHidden(c0)
        except Exception:
            going_hidden = True

        label  = u"Hidden" if going_hidden else u"Cloud + Tag visible"
        errors = []; bv_details = []

        with _ws_transaction("Show/Hide Revision Clouds") as silencer:
            for v in vis_views.values():
                if not _view_supports_rev_vis(v): continue
                if not _is_view_editable(v):
                    try:
                        from Autodesk.Revit.DB import WorksharingUtils
                        info  = WorksharingUtils.GetWorksharingTooltipInfo(doc, v.Id)
                        owner = info.Owner if info and info.Owner else u"another user"
                    except Exception:
                        owner = u"another user"
                    vname = u"--"
                    try: vname = v.Name
                    except Exception: pass
                    bv_details.append((vname, owner))
                    continue
                if isinstance(v, DB.ViewSheet):
                    if hasattr(v, 'SetRevisionVisibility'):
                        target = DB.RevisionVisibility.Hidden if going_hidden \
                                 else DB.RevisionVisibility.CloudAndTagVisible
                        for rid in rev_ids:
                            try:
                                v.SetRevisionVisibility(_to_rev_id(rid), target)
                            except Exception as ex:
                                errors.append(str(ex))
                else:
                    try:
                        if going_hidden:
                            v.HideElements(cloud_id_list)
                        else:
                            v.UnhideElements(cloud_id_list)
                    except Exception as ex:
                        errors.append(u"{} in {}: {}".format(
                            "HideElements" if going_hidden else "UnhideElements",
                            str(v.ViewType), str(ex)))

        # Merge stale-lock failures caught at commit time
        bv_details.extend(silencer.blocked_views)
        uidoc.RefreshActiveView()
        clr = BRUSH_WARN if going_hidden else BRUSH_OK
        msg = u"{} in {:,} view(s).  {} error(s).".format(label, len(vis_views), len(errors))
        if bv_details:
            msg += u"  |  {:,} view(s) locked by another user.".format(len(bv_details))
        if errors: msg += u"  | " + errors[0][:80]
        self._feedback(msg, clr)
        self._set_blocked([], bv_details)
        if bv_details:
            _show_final_status(label, 0, [], bv_details)
    def _do_tag(self, s, e):
        """Place an IndependentTag on each selected cloud in each target view.
        Works with both parent views and dependent segment views."""
        errors = []; tagged = 0
        with revit.Transaction("Tag Revision Clouds"):
            for c in self._clouds:
                for vid, target_view in self._views.items():
                    try:
                        # Get the cloud's bounding box in the target view.
                        # For dependent views the bbox is clipped to the crop region.
                        bb = c.get_BoundingBox(target_view)
                        if bb is None:
                            # Fall back: bbox in the owner (parent) view
                            pv = doc.GetElement(c.OwnerViewId)
                            if pv:
                                bb = c.get_BoundingBox(pv)
                        if bb is None:
                            bb = c.get_BoundingBox(None)
                        if bb is None:
                            errors.append("No bbox – cloud {}".format(_id_val(c.Id)))
                            continue
                        mid = DB.XYZ((bb.Min.X + bb.Max.X) / 2.0,
                                     (bb.Min.Y + bb.Max.Y) / 2.0,
                                     (bb.Min.Z + bb.Max.Z) / 2.0)
                        DB.IndependentTag.Create(
                            doc, target_view.Id, DB.Reference(c),
                            False,
                            DB.TagMode.TM_ADDBY_CATEGORY,
                            DB.TagOrientation.Horizontal,
                            mid)
                        tagged += 1
                    except Exception as ex:
                        errors.append(str(ex))
        clr = BRUSH_OK if not errors else BRUSH_WARN
        self._feedback("Tagged {:,} cloud(s).  {} error(s).".format(tagged, len(errors)), clr)

    def _do_hide(self, s, e):
        """Hide — identical to Revit's native right-click > Hide in View > Elements.
        Calls HideElements in every scope view. Skips clouds blocked by other users.
        A sheet only accepts the clouds it actually owns (drawn straight on the
        sheet, not through a viewport) -- HideElements/UnhideElements rejects
        ids that do not belong to that view."""
        if not self._clouds or not self._views:
            self._feedback(u"Nothing to hide -- no clouds or views.", BRUSH_WARN); return
        editable, blocked = _filter_editable_clouds(self._clouds)
        if not editable:
            self._feedback(u"All clouds are locked." + _blocked_summary(blocked), BRUSH_WARN); return
        cloud_id_list = System.Collections.Generic.List[DB.ElementId](
            [c.Id for c in editable])
        vis_views = self._views_for_visibility()
        errors = []; hidden_in = 0
        with revit.Transaction("Hide Revision Clouds"):
            for v in vis_views.values():
                if not _view_supports_rev_vis(v): continue
                if isinstance(v, DB.ViewSheet):
                    sheet_ids = [c.Id for c in editable
                                 if c.OwnerViewId is not None
                                 and _id_val(c.OwnerViewId) == _id_val(v.Id)]
                    if not sheet_ids: continue  # nothing drawn directly on this sheet
                    id_list = System.Collections.Generic.List[DB.ElementId](sheet_ids)
                else:
                    id_list = cloud_id_list
                try:
                    v.HideElements(id_list)
                    hidden_in += 1
                except Exception as ex:
                    errors.append(u"HideElements in {}: {}".format(str(v.ViewType), str(ex)))
        uidoc.RefreshActiveView()
        msg = u"Hidden in {:,} view(s).  {} error(s).".format(hidden_in, len(errors))
        if errors: msg += u"  | " + errors[0][:80]
        msg += _blocked_summary(blocked)
        self._feedback(msg, BRUSH_WARN)

    def _do_delete(self, s, e):
        # Only delete clouds that belong to (were placed in) the targeted views
        target_int_ids = set(self._views.keys())
        to_delete = [c for c in self._clouds
                     if c.OwnerViewId is not None
                     and _id_val(c.OwnerViewId) in target_int_ids]
        if not to_delete:
            self._feedback("No clouds belong to the targeted view(s) -- nothing deleted.", BRUSH_WARN)
            return
        skipped = len(self._clouds) - len(to_delete)
        skip_note = "  ({:,} cloud(s) in other views will NOT be deleted.)".format(skipped) if skipped else ""
        # No "this cannot be undone" here -- it is a Transaction like any other
        # in this tool, Ctrl+Z reverts it same as everything else.
        if not ui.confirm(
                u"Permanently delete {:,} cloud(s) from the targeted view(s)?{}"
                .format(len(to_delete), skip_note),
                title=u"Confirm Delete", yes_text=u"Delete"):
            return
        # Capture id -> cloud BEFORE deletion -- elements become invalid after
        # doc.Delete only for whichever ones Revit actually deletes.
        id_to_cloud = {_id_val(c.Id): c for c in to_delete}
        ids = System.Collections.Generic.List[DB.ElementId]([c.Id for c in to_delete])
        n = len(to_delete); errors = []
        actually_deleted_int = set()
        with revit.Transaction("Delete Revision Clouds"):
            try:
                result = doc.Delete(ids)
                # Revit silently refuses to delete clouds belonging to an issued
                # revision -- no exception, it just leaves them out of `result`.
                # Trust the return value, never assume the request succeeded.
                actually_deleted_int = set(_id_val(rid) for rid in result)
            except Exception as ex:
                errors.append(str(ex))
        deleted_int = actually_deleted_int & set(id_to_cloud.keys())
        not_deleted_int = set(id_to_cloud.keys()) - deleted_int
        self._clouds = [c for c in self._clouds if _id_val(c.Id) not in deleted_int]
        STATE.selected_clouds = self._clouds
        self._refresh_summary()
        n_ok = len(deleted_int)
        if n_ok == n and not errors:
            self._feedback(u"{:,} cloud(s) deleted.".format(n_ok), BRUSH_BAD)
            return
        reason = u""
        for cid in not_deleted_int:
            c = id_to_cloud.get(cid)
            if c is None: continue
            try:
                p = c.get_Parameter(DB.BuiltInParameter.REVISION_CLOUD_REVISION)
                rev = doc.GetElement(p.AsElementId()) if p else None
            except Exception:
                rev = None
            if rev is not None and rev_is_issued(rev):
                reason = u"  {0} is issued: Revit did not delete its cloud(s).".format(
                    rev_num(rev) or rev_desc(rev) or u"Revision")
                break
        if not reason and errors:
            reason = u"  " + errors[0][:150]
        self._feedback(u"{0:,} of {1:,} cloud(s) deleted.{2}".format(n_ok, n, reason),
                        BRUSH_WARN if n_ok else BRUSH_BAD)

    def _do_style(self, s, e):
        # Apply graphic overrides to the owner view of each cloud (+ dependent views).
        # Applying to the owner view is sufficient: sheet viewports inherit the
        # owner view's element overrides, so the cloud will appear overridden on
        # every sheet that displays the owner view through a viewport.
        # Trying to SetElementOverrides on a ViewSheet for a cloud that lives inside
        # a viewport often fails silently or has no visual effect -- so we skip sheets.
        #
        # Scope filter: only style clouds in the user-selected views.
        # Use self._views directly — NOT _views_for_visibility() which adds all
        # cloud owner views (designed for show/hide, defeats scope for styling).
        # Also include clouds in dependent views (segment views) of scoped views.
        # Expand scope in both directions:
        #  down -> dependent (segment/callout) views of scoped views
        #  up   -> primary (parent) view if a scoped view is itself a dependent
        # This is critical because OwnerViewId always points to the PARENT floor plan
        # even when the cloud visually lives only in a segment (dependent) view.

        scoped = [c for c in self._clouds if _cloud_in_scope(c, self._views, self._dep_cache)]
        if not scoped:
            self._feedback(u"No clouds found in the targeted view(s).", BRUSH_WARN); return
        if not self._clouds:
            self._feedback(u"No clouds selected.", BRUSH_WARN); return
        pats = get_line_patterns()
        if not pats:
            self._feedback(u"Warning -- no line patterns found in model.", BRUSH_WARN); return
        dlg = StyleSubDialog(scoped, sorted(pats.keys()))
        if dlg.ShowDialog(_hwnd) != DialogResult.OK: return
        styles = dlg.get_styles()   # {cloud_id_int: style_dict}
        if not styles:
            self._feedback(u"No clouds checked -- nothing applied.", BRUSH_WARN); return

        errors = []; applied = 0; clouds_done = 0; no_change = 0
        with revit.Transaction("Style Revision Clouds"):
            for c in scoped:
                cid = _id_val(c.Id)
                style = styles.get(cid)
                if style is None: continue
                # A checked row the user never touched (color/pattern/weight
                # all still "no change") has nothing to apply -- skip it
                # instead of stamping a default (that default used to be red).
                if style["color"] is None and not style["pattern_name"] and not style["weight"]:
                    no_change += 1
                    continue
                pat_id = pats.get(style["pattern_name"]) if style["pattern_name"] else None
                owner_v = doc.GetElement(c.OwnerViewId)
                if owner_v is None: continue
                # Build target list with ID-based deduplication
                # (IronPython 'in' uses reference equality on Revit objects)
                seen_ids = set()
                targets = []
                seen_ids.add(_id_val(owner_v.Id))
                targets.append(owner_v)
                try:
                    for did in owner_v.GetDependentViewIds():
                        dv = doc.GetElement(did)
                        if dv is not None and _id_val(dv.Id) not in seen_ids:
                            seen_ids.add(_id_val(dv.Id))
                            targets.append(dv)
                except Exception:
                    pass
                if isinstance(owner_v, DB.ViewSheet):
                    for vp in DB.FilteredElementCollector(doc).OwnedByView(owner_v.Id).OfClass(DB.Viewport):
                        vw = doc.GetElement(vp.ViewId)
                        if vw is not None and _id_val(vw.Id) not in seen_ids:
                            seen_ids.add(_id_val(vw.Id))
                            targets.append(vw)
                cloud_ok = False
                for tv in targets:
                    try:
                        # Seed from what is already applied so an untouched
                        # field (color/pattern/weight the user left blank)
                        # keeps its current value instead of resetting.
                        try:
                            ogs = tv.GetElementOverrides(c.Id)
                        except Exception:
                            ogs = DB.OverrideGraphicSettings()
                        if style["color"] is not None:
                            col = to_db_color(style["color"])
                            ogs.SetProjectionLineColor(col)
                            ogs.SetCutLineColor(col)
                        if pat_id:
                            ogs.SetProjectionLinePatternId(pat_id)
                            ogs.SetCutLinePatternId(pat_id)
                        if style["weight"]:
                            ogs.SetProjectionLineWeight(style["weight"])
                            ogs.SetCutLineWeight(style["weight"])
                        tv.SetElementOverrides(c.Id, ogs)
                        applied += 1
                        cloud_ok = True
                    except Exception as ex:
                        vname = getattr(tv, 'Name', None) or str(_id_val(tv.Id))
                        errors.append(u"Cloud {} / view \"{}\": {}".format(cid, vname, str(ex)))
                if cloud_ok:
                    clouds_done += 1
        uidoc.RefreshActiveView()
        if clouds_done == 0 and not errors:
            note = u"  ({:,} cloud(s) checked with no changes.)".format(no_change) if no_change else u""
            self._feedback(u"No style changes selected -- nothing applied.{0}".format(note), BRUSH_WARN)
            return
        msg = u"Styles applied to {:,} cloud(s) ({:,} override(s)).  {} error(s).".format(
            clouds_done, applied, len(errors))
        if no_change:
            msg += u"  {:,} cloud(s) had no changes.".format(no_change)
        if errors:
            msg += u"  | " + errors[0][:120]
        self._feedback(msg, BRUSH_OK if clouds_done > 0 and not errors else BRUSH_WARN)

    def _set_blocked(self, blocked, blocked_views=None):
        """Keep the blocked lists around so _show_final_status can use them."""
        self._last_blocked       = blocked or []
        self._last_blocked_views = blocked_views or []

    def _do_reset(self, s, e):
        # Reset overrides — same scope filter as _do_style.
        scoped = [c for c in self._clouds if _cloud_in_scope(c, self._views, self._dep_cache)]
        if not scoped:
            self._feedback(u"No clouds found in the targeted view(s).", BRUSH_WARN); return
        errors = []; processed = 0
        ogs_empty = DB.OverrideGraphicSettings()
        with revit.Transaction("Reset Revision Cloud Overrides"):
            for c in scoped:
                owner_v = doc.GetElement(c.OwnerViewId)
                if owner_v is None: continue
                seen_ids = set()
                targets = []
                seen_ids.add(_id_val(owner_v.Id))
                targets.append(owner_v)
                try:
                    for did in owner_v.GetDependentViewIds():
                        dv = doc.GetElement(did)
                        if dv is not None and _id_val(dv.Id) not in seen_ids:
                            seen_ids.add(_id_val(dv.Id))
                            targets.append(dv)
                except Exception:
                    pass
                if isinstance(owner_v, DB.ViewSheet):
                    for vp in DB.FilteredElementCollector(doc).OwnedByView(owner_v.Id).OfClass(DB.Viewport):
                        vw = doc.GetElement(vp.ViewId)
                        if vw is not None and _id_val(vw.Id) not in seen_ids:
                            seen_ids.add(_id_val(vw.Id))
                            targets.append(vw)
                for tv in targets:
                    try:
                        tv.SetElementOverrides(c.Id, ogs_empty)
                        processed += 1
                    except Exception as ex:
                        vname = getattr(tv, 'Name', None) or str(_id_val(tv.Id))
                        errors.append(u"Cloud {} / view \"{}\": {}".format(
                            _id_val(c.Id), vname, str(ex)))
        uidoc.RefreshActiveView()
        msg = u"Reset: {:,} override(s) cleared.  {} error(s).".format(processed, len(errors))
        if errors:
            msg += u"  | " + errors[0][:120]
        self._feedback(msg, BRUSH_OK if not errors else BRUSH_WARN)

    def _go_back(self, s, e):
        self._result_code = ACTION_BACK
        self.win.Close()

    def _go_restart(self, s, e):
        self._result_code = ACTION_RESTART
        self.win.Close()

    def _go_exit(self, s, e):
        self._result_code = ACTION_EXIT
        self.win.Close()

    @property
    def result_code(self):
        return self._result_code


# ============================================================
#  Main wizard loop
# ============================================================
with usage.tool_run(__file__) as run:
    step = 1
    while True:
        if step == 1:
            # WPF window. Next is disabled while nothing is checked, so the
            # legacy "nothing selected" MessageBox is no longer reachable.
            dlg = CloudPanel(doc, uidoc)
            dlg.ShowDialog()
            if dlg.result_code != ACTION_NEXT: script.exit()
            STATE.selected_clouds = dlg.selected_clouds
            step = 2

        if step == 2:
            dlg2 = ScopePanel(STATE.selected_clouds)
            dlg2.ShowDialog()
            if dlg2.result_code == ACTION_EXIT: script.exit()
            if dlg2.result_code == ACTION_BACK: step = 1; continue
            views = dlg2.selected_views
            if not views:
                ui.alert(u"Pick at least one view before continuing.",
                         title=u"No Views Selected")
                continue
            STATE.selected_views = views
            # Pass ALL selected clouds to the ActionPanel.
            # Previously this used a view-based FilteredElementCollector to filter
            # to "visible" clouds only, but that collector skips hidden elements --
            # which is exactly the wrong behaviour when the user wants to *show*
            # clouds that are currently hidden.  The user already chose which clouds
            # to act on in Step 1, so we honour that selection in full.
            STATE.scoped_clouds = list(STATE.selected_clouds)
            step = 3

        if step == 3:
            dlg3 = ActionPanel(STATE.scoped_clouds, STATE.selected_views)
            dlg3.ShowDialog()
            code = dlg3.result_code
            if code == ACTION_BACK:    step = 2; continue
            if code == ACTION_RESTART: step = 1; continue
            break
