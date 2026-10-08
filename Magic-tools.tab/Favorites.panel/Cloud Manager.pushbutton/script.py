# -*- coding: utf-8 -*-
__title__ = "Cloud\nManager"
__doc__ = "Manage revision clouds in 4 steps: pick the revisions, choose the views, then show, hide, tag, style, move or delete them and see what each action did."

# The dashboard is MODELESS: it stays open while you work on the model.
# pyRevit destroys the IronPython engine as soon as a normal script returns,
# and with it every Python handler wired to the window: the window keeps
# painting (WPF is native) but clicks die silently. Asking for a persistent
# engine is what keeps the scope alive.
__persistentengine__ = True
# In Magic Tools that alone is not enough (2026-09-17): under rocket mode
# the persistent engine is SHARED and is cleaned when the script returns,
# killing the handlers this script.py hangs on the window (it stays painted
# but no button responds). Clean engine per click, like the other modeless
# windows in the ribbon.
# Window reuse does not depend on the engine: it lives in pyRevit envvars
# (main() discards it if the build changed or it is not genuinely visible).
__cleanengine__ = True

from pyrevit import revit, DB, script
import clr
import os
import re
import contextlib
import System
import System.Diagnostics

clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")

# Only the system dialogs (color, save) remain from WinForms, plus
# DialogResult, which the WPF sub-dialogs return for compatibility.
# Confirm and alert go through ui.confirm / ui.alert.
from System.Windows.Forms import DialogResult, ColorDialog, SaveFileDialog
from System.Drawing import Color

# --- WPF: the six windows are XAML ----------------------------------------
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")

from System.Windows import (WindowState, Visibility as WVisibility,
                            RoutedEventHandler, SystemParameters)
from System.Windows.Controls import (DataGridRow, Button as WButton,
                                     ComboBox as WComboBox)
from System.Windows.Controls.Primitives import ToggleButton
from System.Windows.Input import Key, Keyboard, ModifierKeys
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Media import (VisualTreeHelper, SolidColorBrush, Brushes,
                                  Color as WColor)
from System.Collections.ObjectModel import ObservableCollection
from System.Windows.Data import CollectionViewSource
from System import Predicate
from System.ComponentModel import INotifyPropertyChanged, PropertyChangedEventArgs
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent

# The whole look of the six windows comes from here: this script declares
# no window chrome, no font and no hex of its own.
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
#  Colours: all read from slantisui
# ============================================================
# A tool never writes a colour of its own: every one below is READ from the
# library tokens, so a retone of slantisui reaches this tool too. The names
# are the original ones the code already passes around (C_* as System.Drawing.Color
# for _brush(), BRUSH_* for status lines), now bound to semantics:
# DIM neutral, OK done, WARN partial or nothing to do, BAD blocked/destructive.
def _dcolor(hex_str):
    h = hex_str.lstrip(u'#')
    return Color.FromArgb(255, int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

C_DARK      = _dcolor(ui.TEXT)
C_MED       = _dcolor(ui.TEXT_DIM)
C_LIGHT     = _dcolor(ui.TEXT_MUTED)
C_BLUE      = _dcolor(ui.ACCENT)          # primary action
C_GREEN     = _dcolor(ui.STATUS_OK)
C_ORANGE    = _dcolor(ui.STATUS_WARN)
C_WARN_DARK = _dcolor(ui.STATUS_WARN)
C_RED       = _dcolor(ui.STATUS_BAD)

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
        # How many step 1 clouds ended up outside the step 2 scope. Only so the
        # dashboard can say it: otherwise "17" looks like 10 were lost.
        self.left_out    = 0
        self.step1_total = 0

STATE        = AppState()
SCOPE_ACTIVE = "active"
SCOPE_CHOSEN = "chosen"
SCOPE_MODEL  = "model"
ACTION_BACK    = 20
ACTION_RESTART = 21
ACTION_EXIT    = 99

# ============================================================
#  STEP 1 -- Select Clouds  (grouped by revision)   [WPF / XAML]
# ============================================================
#  Replaces the legacy WinForms CloudSelectionDialog. Same contract:
#  one row per revision that owns clouds (+ a synthetic "(no revision)" row),
#  checking a row selects ALL of its clouds, and `selected_clouds` hands back
#  cloud ELEMENTS so Steps 2 and 3 keep working untouched.
# ============================================================
ACTION_NEXT = 1

# The six windows are slantisui bodies (ui.parse) further down, each one next
# to the class that drives it: no XAML files beside the script, no fonts.
# _HERE stays: _build_stamp() reads this script's own date and size from it.
_HERE = os.path.dirname(os.path.abspath(__file__))


def _own_to_revit(win):
    """Parent a WPF window to the Revit main window so it cannot end up behind
    Revit. The legacy Forms did this via ShowDialog(_hwnd)."""
    try:
        WindowInteropHelper(win).Owner = \
            System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle
    except Exception:
        pass


def _tok(xaml):
    """Substitute the library tokens into a bespoke XAML literal. Every colour
    in the bodies below is a placeholder read from slantisui; the "_T" ones are
    the 8% ARGB tint of their token, derived here, not typed."""
    return (xaml
            .replace(u'__ACCENT_DK__',  ui.ACCENT_DK)
            .replace(u'__ACCENT_T__',   u'#14' + ui.ACCENT[1:])
            .replace(u'__ACCENT__',     ui.ACCENT)
            .replace(u'__ROW_HOVER__',  ui.ROW_HOVER)
            .replace(u'__ROW_SEL__',    ui.ROW_SEL)
            .replace(u'__WIN_BG__',     ui.WIN_BG)
            .replace(u'__CARD_BG__',    ui.CARD_BG)
            .replace(u'__CARD_BD__',    ui.CARD_BD)
            .replace(u'__INPUT_BD__',   ui.INPUT_BD)
            .replace(u'__CHECK_BD__',   ui.CHECK_BD)
            .replace(u'__TEXT_DIM__',   ui.TEXT_DIM)
            .replace(u'__TEXT_MUTED__', ui.TEXT_MUTED)
            .replace(u'__TEXT__',       ui.TEXT)
            .replace(u'__OK_T__',       u'#14' + ui.STATUS_OK[1:])
            .replace(u'__OK__',         ui.STATUS_OK)
            .replace(u'__WARN_T__',     u'#14' + ui.STATUS_WARN[1:])
            .replace(u'__WARN__',       ui.STATUS_WARN)
            .replace(u'__BAD_T__',      u'#14' + ui.STATUS_BAD[1:])
            .replace(u'__BAD__',        ui.STATUS_BAD))


def _wpf_brush(hex_str):
    h = hex_str.lstrip(u'#')
    return SolidColorBrush(WColor.FromRgb(int(h[0:2], 16), int(h[2:4], 16),
                                          int(h[4:6], 16)))


# Status-line brushes, read from the library (original names kept).
BRUSH_MED    = _wpf_brush(ui.TEXT_DIM)
BRUSH_GREEN  = _wpf_brush(ui.STATUS_OK)
BRUSH_ORANGE = _wpf_brush(ui.STATUS_WARN)
BRUSH_RED    = _wpf_brush(ui.STATUS_BAD)


def _brush(dcolor):
    """Bridge a System.Drawing.Color (the legacy action code passes C_GREEN,
    C_ORANGE, C_RED...) to a WPF brush, so those call sites need no edits.
    None is a colour swatch on "no change": transparent, so the empty box does
    not read as a grey the user picked."""
    if dcolor is None:
        return Brushes.Transparent
    try:
        return SolidColorBrush(WColor.FromArgb(dcolor.A, dcolor.R, dcolor.G, dcolor.B))
    except Exception:
        return BRUSH_MED

NO_REV_KEY = -1


def _row_from_click(e, skip_types):
    """The row under the cursor, or None if the click landed on a control that
    belongs to the cell (checkbox, combo, swatch), which has its own job."""
    node = e.OriginalSource
    while node is not None:
        if isinstance(node, skip_types):
            return None
        if isinstance(node, DataGridRow):
            return node.Item
        try:
            node = VisualTreeHelper.GetParent(node)
        except Exception:
            return None
    return None


def _toggle_rows(rows, anchor, clicked):
    """Toggle the clicked row and return the new anchor.

    With Shift, instead of one row the whole range between the anchor and the
    clicked row is touched, and all of them end up in the same state (the one
    the clicked row was going to take). The anchor does not move while Shift
    is held, so the selection can keep being stretched like in Windows
    Explorer."""
    try:
        value = not clicked.IsSelected
    except Exception:
        return anchor
    try:
        shift = (Keyboard.Modifiers & ModifierKeys.Shift) == ModifierKeys.Shift
    except Exception:
        shift = False

    targets = [clicked]
    if shift and anchor is not None and anchor is not clicked:
        try:
            lst = list(rows)
            i = lst.index(anchor)
            j = lst.index(clicked)
            lo, hi = (i, j) if i <= j else (j, i)
            targets = lst[lo:hi + 1]
        except Exception:
            targets = [clicked]      # the anchor is no longer listed (it was filtered out)

    for r in targets:
        if getattr(r, 'CanChange', True):
            try:
                r.IsSelected = value
            except Exception:
                pass
    return anchor if shift else clicked


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
        self.Issued = issued
        # Shown as a Status column so an issued revision (Delete-blocked by
        # Revit) is visible before the user picks it, not only after Delete
        # skips its clouds in Step 3.
        self.StatusText = u'Issued' if issued else u''
        self.Key = key                # revision ElementId value, or NO_REV_KEY
        self.RevNumber = number
        self.Description = description
        self.IssuedTo = issued_to
        self.IssuedBy = issued_by
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
                           'issued': _rev_is_issued(r),
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


# ---- Step 1 window (slantisui). _REVPICK_RES is the revision picker
# (ComboBox with its own filter box), shared with the Change Revision
# window: the marker goes first, then _tok().
_REVPICK_RES = u'''
    <!-- Item label. Must be an ItemTemplate, not DisplayMemberPath: with a
         custom ComboBox template DisplayMemberPath never reaches the closed
         box and it falls back to ToString(). -->
    <DataTemplate x:Key="RevLabelTemplate">
      <TextBlock Text="{Binding Label}" TextTrimming="CharacterEllipsis"/>
    </DataTemplate>

    <Style x:Key="SlComboToggle" TargetType="ToggleButton">
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="ToggleButton">
            <Border x:Name="b" CornerRadius="8" Background="__WIN_BG__"
                    BorderBrush="__INPUT_BD__" BorderThickness="1"
                    SnapsToDevicePixels="True">
              <Path x:Name="arw" HorizontalAlignment="Right" VerticalAlignment="Center"
                    Margin="0,1,12,0" Fill="__TEXT_DIM__"
                    Data="M 0 0 L 9 0 L 4.5 5.5 Z"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="b" Property="BorderBrush" Value="__ACCENT__"/>
              </Trigger>
              <Trigger Property="IsChecked" Value="True">
                <Setter TargetName="b" Property="BorderBrush" Value="__ACCENT__"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="b" Property="Background" Value="__CARD_BG__"/>
                <Setter TargetName="arw" Property="Fill" Value="__CHECK_BD__"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="SlComboItem" TargetType="ComboBoxItem">
      <Setter Property="Padding" Value="12,7"/>
      <Setter Property="FontSize" Value="12.5"/>
      <Setter Property="Foreground" Value="__TEXT__"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="ComboBoxItem">
            <Border x:Name="b" Background="Transparent" Padding="{TemplateBinding Padding}">
              <ContentPresenter/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="b" Property="Background" Value="__CARD_BG__"/>
              </Trigger>
              <Trigger Property="IsHighlighted" Value="True">
                <Setter TargetName="b" Property="Background" Value="__ROW_SEL__"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter Property="Foreground" Value="__TEXT_MUTED__"/>
                <Setter Property="Cursor" Value="Arrow"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style TargetType="ComboBoxItem" BasedOn="{StaticResource SlComboItem}"/>

    <!-- Target-revision list: issued revisions stay visible but not pickable. -->
    <Style x:Key="RevItem" TargetType="ComboBoxItem" BasedOn="{StaticResource SlComboItem}">
      <Setter Property="IsEnabled" Value="{Binding CanPick}"/>
    </Style>

    <!-- Implicit on purpose: it overrides the library ComboBox for every combo
         under the Grid that holds these resources (see decision 1). -->
    <Style TargetType="ComboBox">
      <Setter Property="Height" Value="32"/>
      <Setter Property="FontSize" Value="13"/>
      <Setter Property="Foreground" Value="__TEXT__"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="ComboBox">
            <Grid>
              <ToggleButton Style="{StaticResource SlComboToggle}" Focusable="False"
                            ClickMode="Press"
                            IsChecked="{Binding IsDropDownOpen, Mode=TwoWay,
                                        RelativeSource={RelativeSource TemplatedParent}}"/>
              <ContentPresenter x:Name="cp" Margin="12,0,30,0" VerticalAlignment="Center"
                                IsHitTestVisible="False"
                                Content="{TemplateBinding SelectionBoxItem}"
                                ContentTemplate="{TemplateBinding SelectionBoxItemTemplate}"
                                ContentStringFormat="{TemplateBinding SelectionBoxItemStringFormat}"/>
              <Popup x:Name="PART_Popup" Placement="Bottom" AllowsTransparency="True"
                     IsOpen="{TemplateBinding IsDropDownOpen}" Focusable="False"
                     MinWidth="{TemplateBinding ActualWidth}">
                <Border CornerRadius="8" Background="__WIN_BG__"
                        BorderBrush="__INPUT_BD__" BorderThickness="1"
                        MaxHeight="380" Margin="0,4,0,10">
                  <Border.Effect>
                    <DropShadowEffect BlurRadius="18" ShadowDepth="5" Direction="270"
                                      Opacity="0.22" Color="__TEXT__"/>
                  </Border.Effect>
                  <Grid>
                    <Grid.RowDefinitions>
                      <RowDefinition Height="Auto"/>
                      <RowDefinition Height="*"/>
                    </Grid.RowDefinitions>
                    <!-- The search lives INSIDE the dropdown, so the combo stays
                         non-editable, the only mode that does not fight WPF
                         over the text. -->
                    <Border Grid.Row="0" Margin="8,8,8,4" CornerRadius="6" Height="30"
                            BorderThickness="1" BorderBrush="__INPUT_BD__"
                            Background="__WIN_BG__">
                      <Grid>
                        <TextBlock x:Name="FilterPh" Text="Type to filter&#8230;"
                                   IsHitTestVisible="False" Margin="10,0" FontSize="12.5"
                                   VerticalAlignment="Center"
                                   Foreground="__TEXT_MUTED__"/>
                        <TextBox x:Name="FilterBox" Background="Transparent"
                                 BorderThickness="0" Padding="10,0" FontSize="12.5"
                                 VerticalContentAlignment="Center"
                                 Foreground="__TEXT__"/>
                      </Grid>
                    </Border>
                    <TextBlock Grid.Row="1" x:Name="FilterEmpty" Text="No revision matches."
                               Margin="12,10,12,12" FontSize="12.5" Visibility="Collapsed"
                               Foreground="__TEXT_DIM__"/>
                    <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
                      <ItemsPresenter/>
                    </ScrollViewer>
                  </Grid>
                </Border>
              </Popup>
            </Grid>
            <ControlTemplate.Triggers>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="cp" Property="Opacity" Value="0.5"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
'''
_STEP1_BODY = u'''
<Grid>
  <Grid.Resources>
__REVPICK_RES__
    <!-- The library ghost button has no disabled look and Move Clouds / Choose
         Clouds are enabled from code, so they get a dimmed variant. -->
    <Style x:Key="BtnGhostDim" TargetType="Button" BasedOn="{StaticResource BtnGhost}">
      <Style.Triggers>
        <Trigger Property="IsEnabled" Value="False">
          <Setter Property="Opacity" Value="0.45"/>
        </Trigger>
      </Style.Triggers>
    </Style>
  </Grid.Resources>

  <Grid.RowDefinitions>
    <RowDefinition Height="Auto"/>   <!-- toolbar -->
    <RowDefinition Height="Auto"/>   <!-- count line -->
    <RowDefinition Height="*"/>      <!-- grid + empty state -->
    <RowDefinition Height="Auto"/>   <!-- change revision -->
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

  <!-- Change revision: acts on the revisions checked above. It does not depend
       on the scope, so it lives in step 1 and not in the dashboard. -->
  <Border Grid.Row="3" Margin="0,12,0,0" CornerRadius="10" Padding="14,12"
          Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1">
    <Grid>
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="Auto"/>
        <ColumnDefinition Width="Auto"/>
      </Grid.ColumnDefinitions>
      <StackPanel Grid.Column="0" VerticalAlignment="Center" Margin="0,0,16,0">
        <Grid MinHeight="24">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="Auto"/>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="Auto"/>
          </Grid.ColumnDefinitions>
          <TextBlock Grid.Column="0" Style="{StaticResource SectionHead}"
                     Text="CHANGE REVISION" Margin="0" VerticalAlignment="Center"/>
          <TextBlock Grid.Column="1" x:Name="LblMoveHint" FontSize="12"
                     Foreground="__TEXT_DIM__" Margin="12,0,8,0"
                     VerticalAlignment="Center" TextTrimming="CharacterEllipsis"
                     ToolTip="{Binding Text, RelativeSource={RelativeSource Self}}"
                     Text="Check revisions above, then pick where their clouds go."/>
          <Button Grid.Column="2" x:Name="BtnMoveDetails" Content="View details &#8594;"
                  Style="{StaticResource BtnGhost}" Padding="10,3" FontSize="11.5"
                  VerticalAlignment="Center" Visibility="Collapsed"/>
        </Grid>
        <ComboBox x:Name="CmbTargetRev" Margin="0,8,0,0"
                  ItemTemplate="{StaticResource RevLabelTemplate}"
                  ItemContainerStyle="{StaticResource RevItem}"/>
      </StackPanel>
      <Button Grid.Column="1" x:Name="BtnApplyRevAll" Content="Move Clouds"
              Style="{StaticResource BtnGhostDim}" Width="140" Height="32"
              VerticalAlignment="Bottom"/>
      <Button Grid.Column="2" x:Name="BtnPickClouds" Content="Choose Clouds&#8230;"
              Style="{StaticResource BtnGhostDim}" Width="140" Height="32"
              Margin="8,0,0,0" VerticalAlignment="Bottom"/>
    </Grid>
  </Border>
</Grid>
'''
_STEP1_BODY = _tok(_STEP1_BODY.replace(u'__REVPICK_RES__', _REVPICK_RES))
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
_STEP1_FOOTER = _tok(_STEP1_FOOTER)

class CloudPanel(object):
    """Controller for the XAML window. Mirrors the surface the wizard loop
    expects: .ShowDialog(), .result_code, .selected_clouds."""

    def __init__(self, doc, uidoc):
        self.win = ui.parse(
            u'Cloud Manager', u'Step 1 of 4 \u00b7 Select revision clouds',
            _STEP1_BODY, _STEP1_FOOTER, width=900, height=740,
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
        self._anchor = None          # last clicked row, for Shift+click
        self.Grid.ItemsSource = self._visible

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

        # Change revision: acts on the revisions ticked in the grid.
        # It lives in step 1 because it does not depend on the scope: it changes the
        # cloud itself.
        # The picker is built before _rebuild, which already updates the help.
        self.CmbTargetRev = f('CmbTargetRev')
        self.BtnMove = f('BtnApplyRevAll'); self.BtnPickClouds = f('BtnPickClouds')
        self.LblMoveHint = f('LblMoveHint'); self.BtnMoveDetails = f('BtnMoveDetails')
        self._all_revs = _all_revisions()
        self._picker = RevPicker(self.CmbTargetRev, self._all_revs, self._upd_move_hint)
        self.BtnMove.Click += self._move_all
        self.BtnPickClouds.Click += self._move_pick
        self.BtnMoveDetails.Click += self._open_details
        if not self._picker.has_revisions:
            self.CmbTargetRev.IsEnabled = False

        self._rebuild()

    # ---- public ----
    def ShowDialog(self):
        return self.win.ShowDialog()

    # ---- host integration ----
    def _own_to_revit(self):
        _own_to_revit(self.win)

    # ---- window chrome ----
    def _on_key(self, sender, e):
        # With the Change revision dropdown open, Enter picks and Escape
        # closes it: they belong to the search box, not to wizard shortcuts.
        try:
            if self.CmbTargetRev.IsDropDownOpen:
                return
        except Exception:
            pass
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
        itself are let through so the row is not toggled twice. Shift extends
        from the last clicked row."""
        item = _row_from_click(e, (ToggleButton,))
        if item is not None:
            self._anchor = _toggle_rows(self._visible, self._anchor, item)

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
            self.CountLine.Foreground = BRUSH_RED
        elif n_sel == 0:
            self.CountLine.Text = u'{0:,} revisions, {1:,} clouds total. None selected.{2}'.format(
                total_revs, self._total_clouds, flt)
            self.CountLine.Foreground = BRUSH_MED
        else:
            self.CountLine.Text = u'{0:,} revision{1} selected ({2:,} cloud{3}).{4}'.format(
                n_sel, u's' if n_sel != 1 else u'',
                n_clouds, u's' if n_clouds != 1 else u'', flt)
            self.CountLine.Foreground = BRUSH_GREEN

        self.BtnNext.IsEnabled = n_sel > 0
        self._upd_move_hint()

    # ---- change revision ----
    def _checked_clouds(self):
        clouds = []
        for row in self._all_rows:
            if row.IsSelected:
                clouds.extend(row.Clouds)
        return clouds

    def _upd_move_hint(self):
        """Card help and enabled buttons according to what is ticked."""
        p = getattr(self, '_picker', None)
        if p is None:
            return
        sel = [r for r in self._all_rows if r.IsSelected]
        n = sum(r.CloudCount for r in sel)
        eid, _label = p.target()
        if not sel:
            txt = u"Check revisions above, then pick where their clouds go."
        else:
            txt = u"Moves the {0:,} cloud{1} of the {2:,} checked revision{3}.".format(
                n, u"s" if n != 1 else u"", len(sel), u"s" if len(sel) != 1 else u"")
        self.LblMoveHint.Text = txt
        self.LblMoveHint.Foreground = BRUSH_MED
        self.BtnMove.IsEnabled = bool(sel) and eid is not None
        self.BtnPickClouds.IsEnabled = bool(sel) and p.has_revisions

    def _move_all(self, sender, e):
        """Move ALL the clouds of the ticked revisions."""
        clouds = self._checked_clouds()
        eid, label = self._picker.target()
        if not clouds or eid is None:
            return
        target_int = _id_val(eid)
        on_target = 0
        issued = 0
        for c in clouds:
            r = _rev_of_cloud(c)
            if r is not None and _id_val(r.Id) == target_int:
                on_target += 1
            elif _rev_is_issued(r):
                issued += 1
        if on_target == len(clouds):
            self.LblMoveHint.Text = u"Every checked cloud is already on that revision."
            self.LblMoveHint.Foreground = _brush(C_WARN_DARK)
            return
        if issued + on_target == len(clouds):
            self.LblMoveHint.Text = (u"Every checked cloud sits on an issued revision: "
                                     u"Revit will not let them move.")
            self.LblMoveHint.Foreground = _brush(C_WARN_DARK)
            return
        note = u""
        if issued:
            note = (u"\n\n{0:,} cloud(s) will be skipped: their revision is issued."
                    .format(issued))
        if on_target:
            note += (u"\n{0:,} cloud(s) are already on that revision."
                     .format(on_target))
        if not ui.confirm(
                u"Move {0:,} cloud(s) to {1}?{2}\n\nCtrl+Z in Revit undoes it.".format(
                    len(clouds) - issued - on_target, label, note),
                title=u"Confirm Change Revision", yes_text=u"Move"):
            return
        self._after_move(apply_revision(clouds, eid, label))

    def _move_pick(self, sender, e):
        """Choose which of the ticked clouds get moved."""
        clouds = self._checked_clouds()
        if not clouds:
            return
        eid, _label = self._picker.target()
        dlg = ChangeRevisionDialog(clouds, self._all_revs,
                                   _id_val(eid) if eid is not None else None)
        if dlg.ShowDialog(_hwnd) != DialogResult.OK:
            return
        picked = dlg.chosen_clouds
        teid, tlabel = dlg.target
        if not picked or teid is None:
            return
        self._after_move(apply_revision(picked, teid, tlabel))

    def _after_move(self, res):
        """To the history, reload the grid (the per-revision counts
        changed) and leave the result in view with the link to the detail."""
        _log_result(res)
        keep = set(r.Key for r in self._all_rows if r.IsSelected)
        self._reload(keep)
        self.LblMoveHint.Text = res.line()
        self.LblMoveHint.Foreground = _brush(_result_color(res))
        self.BtnMoveDetails.Visibility = WVisibility.Visible

    def _reload(self, keep_keys):
        rows, total = collect_revision_groups()
        for row in rows:
            if row.Key in keep_keys:
                row._selected = True          # before hooking it up: no events
            row.add_PropertyChanged(self._on_row_changed)
        self._all_rows, self._total_clouds = rows, total
        self._anchor = None
        self._rebuild()

    def _open_details(self, sender, e):
        StatusDialog(_session_log(), allow_zoom=False).ShowDialog()

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

def _sheet_label_map():
    """{view_id_int: [sheet label]} built from the viewports."""
    m = {}
    try:
        for vp in DB.FilteredElementCollector(doc).OfClass(DB.Viewport).ToElements():
            try:
                sh = doc.GetElement(vp.SheetId)
                if sh is None:
                    continue
                m.setdefault(_id_val(vp.ViewId), []).append(_sheet_label(sh))
            except Exception:
                pass
    except Exception:
        pass
    return m


def _sheet_label(sheet):
    num = u""
    nm = u""
    try:
        num = sheet.SheetNumber or u""
    except Exception:
        pass
    try:
        nm = sheet.Name or u""
    except Exception:
        pass
    if num and nm:
        return u"{0} - {1}".format(num, nm)
    return num or nm or u"(unnamed sheet)"


def _cloud_sheet_text(cloud, vp_map):
    """On which sheet(s) a cloud is visible.

    Looking only at the owner view is not enough: with dependent views the
    cloud usually belongs to the parent view while the sheet holds the
    segment, and the reverse also happens. All three paths are walked: the
    owner itself, its dependents (downwards) and its primary (upwards).  With
    no sheet the view is returned, which still locates it and is more useful
    than a "(no sheet)"."""
    try:
        ov = doc.GetElement(cloud.OwnerViewId) if cloud.OwnerViewId else None
    except Exception:
        ov = None
    if ov is None:
        return u"(unknown)"
    if isinstance(ov, DB.ViewSheet):
        return _sheet_label(ov)

    names = []
    seen = set()

    def _add(vid_int):
        for lbl in vp_map.get(vid_int, []):
            if lbl not in seen:
                seen.add(lbl)
                names.append(lbl)

    _add(_id_val(ov.Id))
    try:
        for did in ov.GetDependentViewIds():
            _add(_id_val(did))
    except Exception:
        pass
    try:
        pid = ov.GetPrimaryViewId()
        if pid is not None and _id_val(pid) > 0:
            _add(_id_val(pid))
    except Exception:
        pass

    if names:
        return u", ".join(sorted(names))
    try:
        return u"(view: {0})".format(ov.Name)
    except Exception:
        return u"(not on a sheet)"


def _locked_by(eid):
    """Who has the element checked out in the central, or None if it is free.

    Touching an element (or a view) of another user makes Revit show its
    native conflict dialog before any try/except can see it, so the check is
    done first and that element is skipped with the owner's name."""
    if not doc.IsWorkshared:
        return None
    try:
        from Autodesk.Revit.DB import WorksharingUtils, CheckoutStatus
        if WorksharingUtils.GetCheckoutStatus(doc, eid) != CheckoutStatus.OwnedByOtherUser:
            return None
        try:
            info = WorksharingUtils.GetWorksharingTooltipInfo(doc, eid)
            if info and info.Owner:
                return info.Owner
        except Exception:
            pass
        return u"another user"
    except Exception:
        return None      # if the check itself fails, try anyway


class _WSFailureSilencer(DB.IFailuresPreprocessor):
    """OBSERVES the commit failures: keeps their text and deletes warnings.

    Since 2026-09-30 it no longer resolves errors. Doing so (ResolveFailure on
    each one and moving on) was the 09/28 regression: if the default
    resolution was to roll back, the change was lost silently in every
    action. Errors are now handled by Revit as usual. What follows is the
    original design, kept as history: the lock case it describes is covered
    by `_locked_by` before writing.

    Original: Intercepts worksharing ownership failures at transaction commit time.

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
        # The TEXT of everything Revit reports, whether or not it carries
        # elements. It used to keep only (name, owner) of those that carried
        # elements, and a failure without elements was resolved and vanished
        # without a trace.
        self.messages = []
        self.status = None        # what Commit() returned; set by _ws_transaction

    def PreprocessFailures(self, fa):
        for msg in list(fa.GetFailureMessages()):
            try:
                desc = msg.GetDescriptionText()
                try:
                    self.messages.append(u"{0}".format(desc))
                except Exception:
                    pass
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
                    # A warning never rolls anything back: deleting it only avoids noise.
                    fa.DeleteWarning(msg)
                # An ERROR is not touched. Until 09/28 ResolveFailure was called here
                # on each one and it moved on: if the default resolution was to
                # roll back, the change was lost silently. That was the regression
                # of "can't do anything". Now Revit handles it as usual.
            except Exception as ex:
                # Swallowing this meant losing the trail exactly when it was needed most.
                try:
                    self.messages.append(u"(could not process a failure: {0})".format(ex))
                except Exception:
                    pass
        # Continue = let Revit go on with its default handling. With no errors it
        # commits normally; with errors it shows its dialog, as before 09/28.
        return DB.FailureProcessingResult.Continue


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
            # Commit returns a TransactionStatus. If Revit rolls back while processing
            # the failures it returns RolledBack WITHOUT raising an exception, and the
            # caller believes it committed. It is kept so _silencer_note can say so.
            silencer.status = t.Commit()
    except Exception:
        if t.HasStarted():
            t.RollBack()
        raise


# ============================================================
#  Result of each action (Step 4)
# ============================================================
#  Each action leaves one row per cloud (or per cloud and view, when the action
#  is per view) with what really happened. The "before" is read before opening
#  the transaction and the "after" is READ from the already committed model:
#  never deduced from the call not raising an error. If a cloud or a view
#  cannot be touched, ONLY that one is skipped, the reason is noted and the
#  action goes on with the rest.
#
#  Cheap reads only: IsHidden, GetElementOverrides, Revision.Visibility,
#  the Revision parameter. No get_BoundingBox per view and no per-view
#  collectors, which regenerate; the crop diagnosis is on demand (Where is it?).
#  The history keeps ints and text, never Revit elements.

R_CHANGED   = u"Changed"
R_UNCHANGED = u"Unchanged"
R_SKIPPED   = u"Skipped"
R_FAILED    = u"Failed"
_RESULT_ORDER = {R_FAILED: 0, R_SKIPPED: 1, R_UNCHANGED: 2, R_CHANGED: 3}

# Row cap per action. With scope "All views in model" a per-view action can
# cross many clouds with thousands of views: past the cap, Changed/Unchanged
# rows are summarised per view. Skipped/Failed always go one by one, since
# those are the ones that matter.
_DETAIL_CAP = 5000

_ENVVAR_LOG = 'cloudmanager_log'
_LOG_MAX    = 30


def _err(ex):
    """First line of the error, short, for the Reason column."""
    try:
        txt = u"{0}".format(ex)
    except Exception:
        txt = u"error"
    txt = txt.strip().splitlines()[0] if txt.strip() else u"error"
    return txt[:200]


def _alive(eid_int):
    try:
        return doc.GetElement(_make_eid(eid_int)) is not None
    except Exception:
        return False


def _cloud_rev_txt(cloud):
    r = _rev_of_cloud(cloud)
    return (rev_num(r) if r is not None else u"") or u"--"


def _silencer_note(sil):
    """What really happened on commit, in one sentence.

    Priority order: if Revit rolled back the transaction that is the news; if
    not, what it said; and only then who had it checked out. A Step 4 that says
    "Revit kept it on X" with no reason is of no use to anyone."""
    texts = []
    try:
        for m in getattr(sil, 'messages', []) or []:
            m = (m or u"").strip()
            if m and m not in texts:
                texts.append(m)
    except Exception:
        texts = []
    said = u" | ".join(t[:160] for t in texts[:3])

    try:
        status = getattr(sil, 'status', None)
        rolled = status is not None and str(status) != u"Committed"
    except Exception:
        rolled = False
    if rolled:
        if said:
            return u" (Revit rolled the change back: {0})".format(said)
        return u" (Revit rolled the change back, status {0})".format(status)
    if said:
        return u" (Revit said: {0})".format(said)

    try:
        owners = sorted(set(o for _n, o in sil.blocked_views))
    except Exception:
        owners = []
    if not owners:
        return u""
    return u" (at commit Revit reported it locked by {0})".format(u", ".join(owners))


def _viewport_sheet_map():
    """{view_id_int: [ViewSheet]} built once from the viewports."""
    m = {}
    try:
        for vp in DB.FilteredElementCollector(doc).OfClass(DB.Viewport).ToElements():
            try:
                sh = doc.GetElement(vp.SheetId)
                if sh is not None:
                    m.setdefault(_id_val(vp.ViewId), []).append(sh)
            except Exception:
                pass
    except Exception:
        pass
    return m


def _ogs_sig(o):
    """Comparable signature of the overrides the tool writes (lines and
    halftone). Used for the before/after without comparing Revit objects."""
    def _col(cc):
        try:
            return (cc.Red, cc.Green, cc.Blue) if cc.IsValid else None
        except Exception:
            return None
    try:
        return (_col(o.ProjectionLineColor), o.ProjectionLineWeight,
                _id_val(o.ProjectionLinePatternId),
                _col(o.CutLineColor), o.CutLineWeight,
                _id_val(o.CutLinePatternId), bool(o.Halftone))
    except Exception:
        return None


class ResultRow(object):
    """A Step 4 row. Plain object: nothing is edited."""

    def __init__(self, result, cloud_int, rev, where, reason,
                 view_int=None, cloud_txt=None, diagnose=True):
        self.Result    = result
        self.cloud_int = cloud_int
        self.view_int  = view_int
        self.CloudId   = cloud_txt or (u"{0}".format(cloud_int) if cloud_int else u"--")
        self.Rev       = rev or u"--"
        try:
            self.RevSort = int(self.Rev)
        except Exception:
            self.RevSort = 99999
        self.Where     = where or u"--"
        self.Reason    = reason or u""
        # "Where is it?" only makes sense if there is a specific cloud and it
        # did not end up as requested.
        self.CanDiagnose = bool(diagnose and cloud_int and result != R_CHANGED)


class ActionResult(object):
    """What an action did: title, time, rows and a one-line summary."""

    def __init__(self, title, note=u""):
        self.Title   = title
        self.Time    = System.DateTime.Now.ToString("HH:mm")
        self.note    = note
        self.rows    = []
        self.Summary = u""

    def add(self, result, cloud_int, rev, where, reason, view_int=None,
            cloud_txt=None, diagnose=True):
        row = ResultRow(result, cloud_int, rev, where, reason, view_int,
                        cloud_txt, diagnose)
        self.rows.append(row)
        return row

    def counts(self):
        c = {R_CHANGED: 0, R_UNCHANGED: 0, R_SKIPPED: 0, R_FAILED: 0}
        for r in self.rows:
            c[r.Result] = c.get(r.Result, 0) + 1
        return c

    def _compress(self):
        """Past the cap, Changed/Unchanged are grouped by view and reason."""
        keep = []
        groups = {}
        for r in self.rows:
            if r.Result in (R_SKIPPED, R_FAILED):
                keep.append(r)
                continue
            k = (r.Result, r.Where, r.Reason)
            groups.setdefault(k, []).append(r)
        for (res, where, reason), rs in groups.items():
            keep.append(ResultRow(res, None, u"--", where, reason,
                                  cloud_txt=u"{0:,} clouds".format(len(rs)),
                                  diagnose=False))
        self.rows = keep
        self.note = (self.note + u"  Grouped by view: over {0:,} rows.".format(
            _DETAIL_CAP)).strip()

    def finish(self):
        """Sort (what failed first) and build the summary. Idempotent: the
        counts are taken once, before grouping."""
        counts = getattr(self, '_counts', None)
        if counts is None:
            counts = self.counts()
            if len(self.rows) > _DETAIL_CAP:
                self._compress()
        self.rows.sort(key=lambda r: (_RESULT_ORDER.get(r.Result, 9), r.Where, r.CloudId))
        parts = []
        for key, word in ((R_CHANGED, u"changed"), (R_UNCHANGED, u"unchanged"),
                          (R_SKIPPED, u"skipped"), (R_FAILED, u"failed")):
            if counts.get(key):
                parts.append(u"{0:,} {1}".format(counts[key], word))
        self.Summary = u", ".join(parts) if parts else u"nothing to do"
        self._counts = counts
        return self

    def final_counts(self):
        return getattr(self, '_counts', None) or self.counts()

    def line(self):
        return u"{0}: {1}.".format(self.Title, self.Summary)


def _result_color(res):
    c = res.final_counts()
    if c.get(R_FAILED):
        return C_RED
    if c.get(R_SKIPPED):
        return C_WARN_DARK
    if c.get(R_CHANGED):
        return C_GREEN
    return C_MED


def _session_log():
    """Session history. Lives in an envvar: the persistent engine re-runs the
    module on every button click and a global list would be lost."""
    log = None
    try:
        log = script.get_envvar(_ENVVAR_LOG)
    except Exception:
        log = None
    if log is None:
        log = []
        try:
            script.set_envvar(_ENVVAR_LOG, log)
        except Exception:
            pass
    return log


def _log_result(res):
    res.finish()
    log = _session_log()
    log.insert(0, res)
    del log[_LOG_MAX:]
    return res


def _reset_session_log():
    try:
        script.set_envvar(_ENVVAR_LOG, [])
    except Exception:
        pass


# ------------------------------------------------------------
#  Change revision (step 1)
# ------------------------------------------------------------
def apply_revision(clouds, rev_id, label):
    """Move the clouds to `rev_id` in ONE transaction (a Ctrl+Z reverts the
    batch) and return the ActionResult. Normal transaction, NOT _ws_transaction:
    that silencer resolves the failures and goes on with the commit, which for
    writing a parameter turns a real failure into a silent success. Clouds
    of another user are skipped beforehand, with their name."""
    res = ActionResult(u"Change revision", u"Target: {0}".format(label))
    target_int = _id_val(rev_id)
    tseq = rev_num(doc.GetElement(rev_id)) or u"--"
    lbls = _sheet_label_map()

    todo = []
    for c in clouds:
        cid = _id_val(c.Id)
        r = _rev_of_cloud(c)
        seq = (rev_num(r) if r is not None else u"") or u"--"
        where = _cloud_sheet_text(c, lbls)
        if r is not None and _id_val(r.Id) == target_int:
            res.add(R_UNCHANGED, cid, seq, where, u"Already on Seq. {0}".format(tseq))
            continue
        if _rev_is_issued(r):
            res.add(R_SKIPPED, cid, seq, where,
                    u"Its revision (Seq. {0}) is issued: Revit locks it".format(seq))
            continue
        lock = _locked_by(c.Id)
        if lock:
            res.add(R_SKIPPED, cid, seq, where, u"Locked by {0}".format(lock))
            continue
        todo.append((c, cid, seq, where))

    errs = {}
    if todo:
        try:
            with revit.Transaction("Change Cloud Revision"):
                for c, cid, _s, _w in todo:
                    ok, err = _set_cloud_revision(c, rev_id)
                    if not ok:
                        errs[cid] = err or u"Revit refused the change"
        except Exception as ex:
            whole = u"Transaction rolled back: " + _err(ex)
            for _c, cid, _s, _w in todo:
                errs.setdefault(cid, whole)

        # --- the truth is read from the model, already committed ---
        for c, cid, seq, where in todo:
            moved = False
            try:
                rv = _rev_of_cloud(c)
                moved = rv is not None and _id_val(rv.Id) == target_int
            except Exception:
                moved = False
            if moved and cid not in errs:
                res.add(R_CHANGED, cid, seq, where,
                        u"Seq. {0} -> Seq. {1}".format(seq, tseq))
            else:
                res.add(R_FAILED, cid, seq, where,
                        errs.get(cid) or u"Reported success but kept its revision")
    return res


# ------------------------------------------------------------
#  Zoom to (from Step 4)
# ------------------------------------------------------------
def _zoom_to_cloud(cloud_int, dep_cache=None, view_int=None):
    """Select the cloud and zoom. Uses the row's view if the cloud is visible
    there; otherwise the active one if it contains it; otherwise opens the
    owner. Switching views is only valid in an API context: it is called from
    the ExternalEvent. Returns (ok, message)."""
    c = None
    try:
        c = doc.GetElement(_make_eid(cloud_int))
    except Exception:
        c = None
    if c is None:
        return False, u"Cloud {0} no longer exists in the model.".format(cloud_int)
    owner = None
    try:
        owner = doc.GetElement(c.OwnerViewId)
    except Exception:
        owner = None
    if owner is None:
        return False, u"Cloud {0} has no owner view.".format(cloud_int)

    target = owner
    candidates = []
    if view_int:
        try:
            v = doc.GetElement(_make_eid(view_int))
            if isinstance(v, DB.View) and not isinstance(v, DB.ViewSheet):
                candidates.append(v)
        except Exception:
            pass
    try:
        if uidoc.ActiveView is not None:
            candidates.append(uidoc.ActiveView)
    except Exception:
        pass
    for v in candidates:
        try:
            if _cloud_in_scope(c, {_id_val(v.Id): v}, dep_cache):
                target = v
                break
        except Exception:
            pass

    try:
        if _id_val(uidoc.ActiveView.Id) != _id_val(target.Id):
            uidoc.ActiveView = target
    except Exception as ex:
        return False, u"Could not open {0}: {1}".format(_safe_name(target), _err(ex))
    try:
        uidoc.Selection.SetElementIds(
            System.Collections.Generic.List[DB.ElementId]([c.Id]))
    except Exception:
        pass
    try:
        uidoc.ShowElements(c.Id)
    except Exception as ex:
        return False, u"Cloud {0} selected in {1}, but Revit could not zoom to it: {2}".format(
            cloud_int, _safe_name(target), _err(ex))
    return True, u"Cloud {0} selected and zoomed in {1}.".format(cloud_int, _safe_name(target))


# ------------------------------------------------------------
#  Step 4 -- the window
# ------------------------------------------------------------
# ---- Step 4 window (slantisui)
_STATUS_BODY = u'''
<Grid>
  <Grid.Resources>
    <!-- Count chip, and at the same time grid filter. ToggleButton is a
         control the library does not style: own template, all with tokens.
         Each chip sets its Background/Foreground (the _T and the token of its
         semantics); here only shape, typography and the "chosen" border. -->
    <Style x:Key="Chip" TargetType="ToggleButton">
      <Setter Property="Margin" Value="0,0,8,0"/>
      <Setter Property="Padding" Value="12,5"/>
      <Setter Property="FontSize" Value="12"/>
      <Setter Property="FontWeight" Value="Medium"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="BorderBrush" Value="Transparent"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="ToggleButton">
            <Border x:Name="b" CornerRadius="14" Padding="{TemplateBinding Padding}"
                    Background="{TemplateBinding Background}" BorderThickness="1.5"
                    BorderBrush="{TemplateBinding BorderBrush}">
              <ContentPresenter/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsChecked" Value="True">
                <Setter TargetName="b" Property="BorderBrush" Value="__TEXT__"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="b" Property="Opacity" Value="0.4"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <!-- History: one card per action. ListBoxItem is not styled by the library
         either. The chosen one carries the selection orange of the rows. -->
    <Style x:Key="HistItem" TargetType="ListBoxItem">
      <Setter Property="Margin" Value="0,0,0,6"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="ListBoxItem">
            <Border x:Name="b" CornerRadius="10" Background="__WIN_BG__"
                    BorderBrush="__CARD_BD__" BorderThickness="1.5" Padding="12,10">
              <ContentPresenter/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="b" Property="BorderBrush" Value="__INPUT_BD__"/>
              </Trigger>
              <Trigger Property="IsSelected" Value="True">
                <Setter TargetName="b" Property="Background" Value="__ROW_SEL__"/>
                <Setter TargetName="b" Property="BorderBrush" Value="__ACCENT__"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="CellText" TargetType="TextBlock">
      <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
    </Style>
  </Grid.Resources>

  <Grid.ColumnDefinitions>
    <ColumnDefinition Width="230"/>
    <ColumnDefinition Width="*"/>
  </Grid.ColumnDefinitions>

  <!-- Session history -->
  <Grid Grid.Column="0" Margin="0,0,16,0">
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
    </Grid.RowDefinitions>
    <TextBlock Text="THIS SESSION" Style="{StaticResource SectionHead}"
               Margin="2,0,0,8"/>
    <ListBox x:Name="HistList" Grid.Row="1" BorderThickness="0" Background="Transparent"
             ItemContainerStyle="{StaticResource HistItem}"
             ScrollViewer.HorizontalScrollBarVisibility="Disabled">
      <ListBox.ItemTemplate>
        <DataTemplate>
          <StackPanel>
            <Grid>
              <TextBlock Text="{Binding Title}" FontWeight="Medium" Margin="0,0,40,0"
                         TextTrimming="CharacterEllipsis"/>
              <TextBlock Text="{Binding Time}" FontSize="11.5" HorizontalAlignment="Right"
                         Foreground="__TEXT_DIM__"/>
            </Grid>
            <TextBlock Text="{Binding Summary}" FontSize="11.5" TextWrapping="Wrap"
                       Foreground="__TEXT_DIM__" Margin="0,3,0,0"/>
          </StackPanel>
        </DataTemplate>
      </ListBox.ItemTemplate>
    </ListBox>
  </Grid>

  <!-- Detail of the chosen action -->
  <Grid Grid.Column="1">
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>   <!-- title + note -->
      <RowDefinition Height="Auto"/>   <!-- chips -->
      <RowDefinition Height="*"/>      <!-- grid -->
      <RowDefinition Height="Auto"/>   <!-- diagnosis -->
    </Grid.RowDefinitions>

    <Grid Grid.Row="0" Margin="0,0,0,10">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="Auto"/>
        <ColumnDefinition Width="*"/>
      </Grid.ColumnDefinitions>
      <TextBlock x:Name="LblTitle" Text="No actions yet" FontSize="15" FontWeight="Medium"/>
      <TextBlock x:Name="LblNote" Grid.Column="1" FontSize="12" Foreground="__TEXT_DIM__"
                 Margin="12,0,0,1" VerticalAlignment="Bottom"
                 TextTrimming="CharacterEllipsis"/>
    </Grid>

    <!-- The five chips: the colour is the semantics of the result, and each one is
         a state token (the neutral of Unchanged is the card border). -->
    <StackPanel Grid.Row="1" Orientation="Horizontal" Margin="0,0,0,12">
      <ToggleButton x:Name="ChipAll" Style="{StaticResource Chip}" IsChecked="True"
                    Background="__WIN_BG__" Foreground="__TEXT__"
                    BorderBrush="__INPUT_BD__"/>
      <ToggleButton x:Name="ChipChanged" Style="{StaticResource Chip}"
                    Background="__OK_T__" Foreground="__OK__"/>
      <ToggleButton x:Name="ChipUnchanged" Style="{StaticResource Chip}"
                    Background="__CARD_BD__" Foreground="__TEXT_DIM__"/>
      <ToggleButton x:Name="ChipSkipped" Style="{StaticResource Chip}"
                    Background="__WARN_T__" Foreground="__WARN__"/>
      <ToggleButton x:Name="ChipFailed" Style="{StaticResource Chip}"
                    Background="__BAD_T__" Foreground="__BAD__"/>
    </StackPanel>

    <Grid Grid.Row="2">
      <DataGrid x:Name="Grid" CanUserAddRows="False" RowHeight="36"
                SelectionMode="Single" EnableRowVirtualization="True">
        <DataGrid.Columns>
          <DataGridTemplateColumn Header="Result" Width="110" SortMemberPath="Result">
            <DataGridTemplateColumn.CellTemplate>
              <DataTemplate>
                <Border x:Name="pill" CornerRadius="10" Padding="9,2" HorizontalAlignment="Left"
                        Background="__OK_T__">
                  <TextBlock x:Name="pt" Text="{Binding Result}" FontSize="11.5"
                             FontWeight="Medium" Foreground="__OK__"/>
                </Border>
                <DataTemplate.Triggers>
                  <DataTrigger Binding="{Binding Result}" Value="Unchanged">
                    <Setter TargetName="pill" Property="Background" Value="__CARD_BD__"/>
                    <Setter TargetName="pt" Property="Foreground" Value="__TEXT_DIM__"/>
                  </DataTrigger>
                  <DataTrigger Binding="{Binding Result}" Value="Skipped">
                    <Setter TargetName="pill" Property="Background" Value="__WARN_T__"/>
                    <Setter TargetName="pt" Property="Foreground" Value="__WARN__"/>
                  </DataTrigger>
                  <DataTrigger Binding="{Binding Result}" Value="Failed">
                    <Setter TargetName="pill" Property="Background" Value="__BAD_T__"/>
                    <Setter TargetName="pt" Property="Foreground" Value="__BAD__"/>
                  </DataTrigger>
                </DataTemplate.Triggers>
              </DataTemplate>
            </DataGridTemplateColumn.CellTemplate>
          </DataGridTemplateColumn>
          <DataGridTemplateColumn Header="Cloud" Width="92" SortMemberPath="CloudId">
            <DataGridTemplateColumn.CellTemplate>
              <DataTemplate>
                <TextBlock Style="{StaticResource CellText}" Text="{Binding CloudId}"/>
              </DataTemplate>
            </DataGridTemplateColumn.CellTemplate>
          </DataGridTemplateColumn>
          <DataGridTemplateColumn Header="Rev" Width="56" SortMemberPath="RevSort">
            <DataGridTemplateColumn.CellTemplate>
              <DataTemplate>
                <TextBlock Style="{StaticResource CellText}" Text="{Binding Rev}"/>
              </DataTemplate>
            </DataGridTemplateColumn.CellTemplate>
          </DataGridTemplateColumn>
          <DataGridTemplateColumn Header="Sheet / View" Width="190" SortMemberPath="Where">
            <DataGridTemplateColumn.CellTemplate>
              <DataTemplate>
                <TextBlock Style="{StaticResource CellText}" Text="{Binding Where}"
                           ToolTip="{Binding Where}"/>
              </DataTemplate>
            </DataGridTemplateColumn.CellTemplate>
          </DataGridTemplateColumn>
          <DataGridTemplateColumn Header="Reason" Width="*" SortMemberPath="Reason">
            <DataGridTemplateColumn.CellTemplate>
              <DataTemplate>
                <TextBlock Style="{StaticResource CellText}" Text="{Binding Reason}"
                           ToolTip="{Binding Reason}" Foreground="__TEXT_DIM__"/>
              </DataTemplate>
            </DataGridTemplateColumn.CellTemplate>
          </DataGridTemplateColumn>
          <DataGridTemplateColumn Header="" Width="116" CanUserSort="False">
            <DataGridTemplateColumn.CellTemplate>
              <DataTemplate>
                <!-- Tag="diag" is what StatusDialog._on_grid_button uses to
                     tell this button apart from any other grid click. -->
                <Button x:Name="bw" Style="{StaticResource BtnGhost}" Content="Where is it?"
                        Tag="diag" Padding="12,3" FontSize="12" Visibility="Collapsed"/>
                <DataTemplate.Triggers>
                  <DataTrigger Binding="{Binding CanDiagnose}" Value="True">
                    <Setter TargetName="bw" Property="Visibility" Value="Visible"/>
                  </DataTrigger>
                </DataTemplate.Triggers>
              </DataTemplate>
            </DataGridTemplateColumn.CellTemplate>
          </DataGridTemplateColumn>
        </DataGrid.Columns>
      </DataGrid>

      <!-- Empty state, turned on by the code. Shifted by the header height
           so the column headings stay in view. -->
      <TextBlock x:Name="LblEmpty" Visibility="Collapsed" Margin="0,40,0,0"
                 HorizontalAlignment="Center" VerticalAlignment="Center"
                 Foreground="__TEXT_MUTED__" Text="Nothing to show."/>
    </Grid>

    <!-- Diagnosis of the chosen row (Where is it?). Replaces the MessageBox
         of the old Find cloud row. -->
    <Border x:Name="DiagPanel" Grid.Row="3" Margin="0,12,0,0" CornerRadius="10"
            Padding="16,12" Background="__WIN_BG__" BorderBrush="__CARD_BD__"
            BorderThickness="1" Visibility="Collapsed">
      <Grid>
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <Grid.RowDefinitions>
          <RowDefinition Height="Auto"/>
          <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>
        <TextBlock x:Name="DiagTitle" FontWeight="Medium" TextWrapping="Wrap"
                   Margin="0,0,16,0"/>
        <StackPanel Grid.Column="1" Grid.RowSpan="2" VerticalAlignment="Top">
          <Button x:Name="BtnZoom" Style="{StaticResource BtnGhost}" Content="Zoom to"
                  Width="120"/>
          <Button x:Name="BtnDiagClose" Style="{StaticResource BtnGhost}" Content="Close"
                  Width="120" Margin="0,8,0,0"/>
        </StackPanel>
        <TextBox x:Name="DiagText" Grid.Row="1" Margin="0,8,16,0" IsReadOnly="True"
                 BorderThickness="0" Background="Transparent" Padding="0" FontSize="12.5"
                 TextWrapping="Wrap" MaxHeight="170" VerticalScrollBarVisibility="Auto"/>
      </Grid>
    </Border>
  </Grid>
</Grid>
'''
_STATUS_BODY = _tok(_STATUS_BODY)
_STATUS_FOOTER = u'''
<Grid>
  <Grid.ColumnDefinitions>
    <ColumnDefinition Width="*"/>
    <ColumnDefinition Width="Auto"/>
  </Grid.ColumnDefinitions>
  <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
    <Button x:Name="BtnExport" Content="Export CSV" Style="{StaticResource BtnGhost}"/>
    <TextBlock x:Name="LblExport" Margin="14,0,0,0" VerticalAlignment="Center"
               FontSize="12" Foreground="__TEXT_DIM__"/>
  </StackPanel>
  <Button x:Name="BtnBack" Grid.Column="1" Content="&#8592;  Back"
          Style="{StaticResource BtnPrimary}" VerticalAlignment="Center"/>
</Grid>
'''
_STATUS_FOOTER = _tok(_STATUS_FOOTER)

class StatusDialog(object):
    """Step 4. Modal: opened from the dashboard (inside the ExternalEvent) or
    from step 1. Zoom to does not zoom in here: it notes the cloud and closes,
    and whoever opened the dialog does the zoom with the window already
    closed."""

    _CHIPS = (('ChipAll', None, u"All"), ('ChipChanged', R_CHANGED, u"Changed"),
              ('ChipUnchanged', R_UNCHANGED, u"Unchanged"),
              ('ChipSkipped', R_SKIPPED, u"Skipped"), ('ChipFailed', R_FAILED, u"Failed"))

    def __init__(self, history, views=None, vp_map=None, sheet_lbls=None,
                 allow_zoom=True):
        self.zoom_cloud = None
        self.zoom_view  = None
        self._views      = views or {}
        self._vp_map     = vp_map
        self._sheet_lbls = sheet_lbls
        self._cur        = None
        self._filter     = None
        self._diag_row   = None

        self.win = ui.parse(
            u'Cloud Manager', u'Step 4 of 4 \u00b7 Status',
            _STATUS_BODY, _STATUS_FOOTER, width=1060, height=760,
            context=doc.Title)
        self.win.MinWidth  = 900
        self.win.MinHeight = 620

        f = self.win.FindName
        self.HistList = f('HistList')
        self.LblTitle = f('LblTitle'); self.LblNote = f('LblNote')
        self.Grid = f('Grid'); self.LblEmpty = f('LblEmpty')
        self.DiagPanel = f('DiagPanel'); self.DiagTitle = f('DiagTitle')
        self.DiagText = f('DiagText'); self.BtnZoom = f('BtnZoom')
        self.BtnDiagClose = f('BtnDiagClose')
        self.BtnExport = f('BtnExport'); self.LblExport = f('LblExport')
        self.BtnBack = f('BtnBack')
        self._chips = [(f(name), key, word) for name, key, word in self._CHIPS]

        _own_to_revit(self.win)

        self._history = list(history)
        hist = ObservableCollection[object]()
        for r in self._history:
            hist.Add(r)
        self.HistList.ItemsSource = hist
        self._items = ObservableCollection[object]()
        self.Grid.ItemsSource = self._items

        if not allow_zoom:
            self.BtnZoom.Visibility = WVisibility.Collapsed

        # ---- events ----
        self.BtnBack.Click += lambda s, e: self.win.Close()
        self.win.PreviewKeyDown += self._on_key
        self.HistList.SelectionChanged += self._on_hist
        for chip, key, _w in self._chips:
            chip.Click += self._make_chip_handler(key)
        self.Grid.AddHandler(WButton.ClickEvent, RoutedEventHandler(self._on_grid_button))
        self.BtnZoom.Click += self._zoom
        self.BtnDiagClose.Click += lambda s, e: self._hide_diag()
        self.BtnExport.Click += self._export

        if self._history:
            self.HistList.SelectedIndex = 0
        else:
            self._show(None)

    def ShowDialog(self):
        self.win.ShowDialog()

    # ---- window plumbing ----
    def _on_key(self, sender, e):
        if e.Key == Key.Escape:
            self.win.Close()

    # ---- which action is shown ----
    def _on_hist(self, sender, e):
        self._show(self.HistList.SelectedItem)

    def _show(self, res):
        self._cur = res
        self._filter = None
        self._hide_diag()
        if res is None:
            self.LblTitle.Text = u"No actions yet"
            self.LblNote.Text = u"Run an action in step 3 and its result shows up here."
            counts = {}
        else:
            self.LblTitle.Text = res.Title
            self.LblNote.Text = res.note or u""
            counts = res.final_counts()
        total = sum(counts.values()) if counts else 0
        for chip, key, word in self._chips:
            n = total if key is None else counts.get(key, 0)
            chip.Content = u"{0}  {1:,}".format(word, n)
            chip.IsChecked = (key is None)
            chip.IsEnabled = (key is None) or n > 0
        self._fill()

    def _make_chip_handler(self, key):
        def _h(s, e):
            self._filter = key
            for chip, k, _w in self._chips:
                chip.IsChecked = (k == key)
            self._fill()
        return _h

    def _fill(self):
        self._items.Clear()
        rows = self._cur.rows if self._cur is not None else []
        for r in rows:
            if self._filter is None or r.Result == self._filter:
                self._items.Add(r)
        self.LblEmpty.Visibility = (WVisibility.Visible if self._items.Count == 0
                                    else WVisibility.Collapsed)
        self._hide_diag()

    # ---- Where is it? ----
    def _on_grid_button(self, sender, e):
        src = e.OriginalSource
        try:
            if getattr(src, 'Tag', None) != 'diag':
                return
            row = src.DataContext
        except Exception:
            return
        if not isinstance(row, ResultRow):
            return
        e.Handled = True
        self._diagnose(row)

    def _hide_diag(self):
        self._diag_row = None
        self.DiagPanel.Visibility = WVisibility.Collapsed

    def _diagnose(self, row):
        self._diag_row = row
        self.DiagPanel.Visibility = WVisibility.Visible
        self.BtnZoom.IsEnabled = False
        cloud = None
        try:
            cloud = doc.GetElement(_make_eid(row.cloud_int))
        except Exception:
            cloud = None
        if cloud is None:
            self.DiagTitle.Text = u"Cloud {0} no longer exists in the model.".format(row.cloud_int)
            self.DiagText.Text = u""
            return

        views = {}
        if row.view_int:
            v = self._views.get(row.view_int)
            if v is None:
                try:
                    v = doc.GetElement(_make_eid(row.view_int))
                except Exception:
                    v = None
            if v is not None:
                views[row.view_int] = v
        if not views:
            views = dict(self._views)
        if not views:
            try:
                ov = doc.GetElement(cloud.OwnerViewId)
                if ov is not None:
                    views[_id_val(ov.Id)] = ov
            except Exception:
                pass

        if self._vp_map is None:
            self._vp_map = _viewport_sheet_map()
        if self._sheet_lbls is None:
            self._sheet_lbls = _sheet_label_map()
        try:
            short, report = _diagnose_cloud(cloud, views, self._vp_map, self._sheet_lbls)
        except Exception as ex:
            self.DiagTitle.Text = u"Could not diagnose cloud {0}: {1}".format(row.cloud_int, _err(ex))
            self.DiagText.Text = u""
            return
        self.DiagTitle.Text = u"Cloud {0}: {1}".format(row.cloud_int, short)
        self.DiagText.Text = report
        self.BtnZoom.IsEnabled = True

    def _zoom(self, s, e):
        row = self._diag_row
        if row is None or not row.cloud_int:
            return
        self.zoom_cloud = row.cloud_int
        self.zoom_view  = row.view_int
        self.win.Close()

    # ---- export ----
    def _export(self, s, e):
        """CSV of the WHOLE session history, one row per result."""
        import System.IO
        try:
            sfd = SaveFileDialog()
            sfd.Title = u"Export CloudManager status"
            sfd.Filter = u"CSV files (*.csv)|*.csv|All files (*.*)|*.*"
            sfd.FileName = u"CloudManager_Status_{0}.csv".format(
                System.DateTime.Now.ToString("yyyy-MM-dd_HHmm"))
            if sfd.ShowDialog() != DialogResult.OK:
                return

            def _q(v):
                return u'"{0}"'.format(u"{0}".format(v).replace(u'"', u'""'))

            lines = [u",".join(_q(h) for h in (u"Action", u"Time", u"Result", u"Cloud",
                                                u"Rev", u"Sheet / View", u"Reason"))]
            n = 0
            for res in self._history:
                for r in res.rows:
                    lines.append(u",".join(_q(x) for x in (
                        res.Title, res.Time, r.Result, r.CloudId, r.Rev, r.Where, r.Reason)))
                    n += 1
            System.IO.File.WriteAllLines(sfd.FileName,
                                         System.Array[System.String](lines),
                                         System.Text.UTF8Encoding(True))
            self.LblExport.Text = u"{0:,} rows exported to {1}".format(
                n, System.IO.Path.GetFileName(sfd.FileName))
            self.LblExport.Foreground = BRUSH_MED
        except Exception as ex:
            self.LblExport.Text = u"Export failed: {0}".format(_err(ex))
            self.LblExport.Foreground = BRUSH_RED



def _bb_intersects_xy(bb1, bb2):
    """Return True if two BoundingBoxXYZ overlap in the XY plane."""
    return (bb1.Max.X >= bb2.Min.X and bb1.Min.X <= bb2.Max.X and
            bb1.Max.Y >= bb2.Min.Y and bb1.Min.Y <= bb2.Max.Y)


def friendly_view_type(v):
    vt = str(v.ViewType)
    return VIEW_TYPE_NAMES.get(vt, vt)

# ============================================================
#  STEP 2 -- Choose Scope   [WPF / XAML]
# ============================================================
#  Port of the legacy WinForms ScopeSelectionDialog. The presentation layer is
#  new; every piece of behaviour is preserved:
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


# ---- Step 2 window (slantisui)
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
_SCOPE_FOOTER = _tok(_SCOPE_FOOTER)

class ScopePanel(object):

    def __init__(self, selected_clouds):
        self._sel_clouds       = selected_clouds
        self._all_views        = None
        self._filt_views       = []
        self._views_loaded     = False
        self._current_scope    = SCOPE_ACTIVE
        self._model_view_count = None   # cached lazily when MODEL scope is first selected
        self._checked_view_ids = set()
        self.result_code       = ACTION_EXIT
        self._bulk             = False  # suppress per-row callbacks during a rebuild
        self._syncing          = False  # suppress radio handlers during programmatic checks

        self.win = ui.parse(
            u'Cloud Manager', u'Step 2 of 4 \u00b7 Choose scope',
            _SCOPE_BODY, _SCOPE_FOOTER, width=980, height=780,
            context=doc.Title)
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
        self.LblCvHeader.Text = u'Cloud distribution: found in {0:,} view{1}:'.format(
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

        self._cv_anchor = None       # Shift anchor, one per grid
        self._vw_anchor = None
        self._cv_rows = ObservableCollection[object]()
        self.CvGrid.ItemsSource = self._cv_rows
        self._vw_rows = ObservableCollection[object]()
        self.VwGrid.ItemsSource = self._vw_rows

        # ---- events ----
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

    # ---- window chrome ----
    def _on_key(self, sender, e):
        if e.Key == Key.Escape:
            self._close(ACTION_EXIT)

    def _close(self, code):
        self.result_code = code
        self.win.Close()

    # ---- shared grid behaviour: click a row to toggle it ----
    def _on_grid_click(self, sender, e):
        """The two grids of the step share a handler, so each one keeps
        its own Shift anchor."""
        item = _row_from_click(e, (ToggleButton,))
        if item is None:
            return
        if sender is self.CvGrid:
            self._cv_anchor = _toggle_rows(self._cv_rows, self._cv_anchor, item)
        else:
            self._vw_anchor = _toggle_rows(self._vw_rows, self._vw_anchor, item)

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
        # Ticked rows that the search box hides stay ticked, and "Apply to
        # checked views" uses them. Without saying so, searching "A3.20" showed
        # 1 ticked row out of 8 and it looked like only one was selected.
        try:
            shown = set(r.Key for r in self._cv_rows)
            hidden = len([k for k in self._cv_checked_keys if k not in shown])
        except Exception:
            hidden = 0
        txt = u'{0:,} of {1:,} view{2} selected'.format(
            checked, total, u's' if total != 1 else u'')
        if hidden:
            txt += (u' ({0:,} hidden by the search; Apply uses only the ones '
                    u'shown)'.format(hidden))
        self.LblCvStatus.Text = txt
        self.LblCvStatus.Foreground = (BRUSH_ORANGE if (hidden or not checked)
                                       else BRUSH_GREEN)

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
            ui.alert(_err(ex), title=u"Export Error")

    def _use_cloud_views(self, s, e):
        """Set scope to SCOPE_CHOSEN using only the views checked in the distribution
        table. Multiple rows may share a view (different revisions) -- dedupe by vid."""
        # With the search box set, ONLY the ticked rows that are visible are
        # applied. Before, the hidden ticked rows also counted: searching
        # "A3.20", seeing one row and applying gave all 8 views and the 27
        # clouds of step 1.
        keys = list(self._cv_checked_keys)
        if (self.CvSearch.Text or u'').strip():
            shown = set(r.Key for r in self._cv_rows)
            keys = [k for k in keys if k in shown]
        checked_int_ids = set()
        for key in keys:
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
            self.LblScope.Text = u'Scope: active view only (' + doc.ActiveView.Name + u').'
            self.LblScope.Foreground = BRUSH_GREEN
        elif self._current_scope == SCOPE_CHOSEN:
            n = len(self._checked_view_ids)
            if n:
                self.LblScope.Text = u'Scope: {0:,} view{1} selected.'.format(
                    n, u's' if n != 1 else u'')
                self.LblScope.Foreground = BRUSH_GREEN
            else:
                self.LblScope.Text = u'Scope: no views checked yet.'
                self.LblScope.Foreground = BRUSH_ORANGE
        else:
            n_all = len(self._all_views) if self._all_views else (self._model_view_count or 0)
            self.LblScope.Text = u'Scope: all {0:,} views in the model.'.format(n_all)
            self.LblScope.Foreground = BRUSH_GREEN
        # With no ticked views there is nowhere to go: Next is off, same as in
        # step 1. It used to let it through, a native MessageBox popped up and
        # step 2 was redone from scratch.
        self.BtnNext.IsEnabled = not (self._current_scope == SCOPE_CHOSEN
                                      and not self._checked_view_ids)

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
                        label = u"{} - {}".format(num, title) if title else num
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
                    s_num, s_label = u"~", u"--"   # sorts last; display as dash
                info[key] = {
                    "view":      view_obj,
                    "vid":       view_id,
                    "rev_id":    rev_id_obj,
                    "rev_name":  _rev_name(rev_id_obj) if rev_id_obj else u"--",
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
# First item of the batch bar's pattern and weight combos: leave that field
# alone on the checked rows.
_NO_CHANGE = u'<No change>'


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
        # user actually picked in the grid gets applied by _run_style.
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

    @property
    def is_checked(self):
        return self._selected

    def get_style(self):
        return {"color":        self._color,
                "pattern_name": self._pattern,
                "weight":       int(self._weight)}


# ---- Apply Styles window (slantisui)
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

        # ---- resolve cloud info up-front ----
        vp_map = _sheet_label_map()
        self._cloud_info = []
        for c in clouds:
            p   = c.get_Parameter(DB.BuiltInParameter.REVISION_CLOUD_REVISION)
            rev = doc.GetElement(p.AsElementId()) if p else None
            sheet_txt = _cloud_sheet_text(c, vp_map)
            self._cloud_info.append({
                "cloud":     c,
                "id":        _id_val(c.Id),
                "rev_num":   rev_num(rev)  if rev else u"",
                "rev_desc":  rev_desc(rev) if rev else u"",
                "view_name": sheet_txt,
            })

        self.rows = []
        self._anchor = None          # last clicked row, for Shift+click
        self._items = ObservableCollection[object]()
        for info in self._cloud_info:
            row = StyleRow(info, self._patterns, self._weights)
            row.add_PropertyChanged(self._on_row_changed)
            self.rows.append(row)
            self._items.Add(row)
        self.Grid.ItemsSource = self._items

        n = len(self.rows)
        self.LblCaption.Text = (
            u'One row per cloud: set color, line pattern and weight, then Apply Styles. '
            u'{0:,} cloud{1} in scope.'.format(n, u's' if n != 1 else u''))

        # ---- batch bar ----
        # Starts on "no change" like the rows: no colour, and the two combos
        # on _NO_CHANGE. It used to start on red + first pattern + weight 1,
        # so Apply to Checked without looking painted every checked row red.
        self._q_color = None
        self.BtnQColor.Background = _brush(self._q_color)
        self.BtnQColor.ToolTip = u'No change. Click to pick a color'
        self.CmbQPattern.Items.Add(_NO_CHANGE)
        for pn in pattern_names:
            self.CmbQPattern.Items.Add(pn)
        self.CmbQPattern.SelectedIndex = 0
        self.CmbQWeight.Items.Add(_NO_CHANGE)
        for w in range(1, 17):
            self.CmbQWeight.Items.Add(w)
        self.CmbQWeight.SelectedIndex = 0

        # ---- events ----
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
        # A row starts with no colour ("no change"); ColorDialog.Color does
        # not take None, so only seed it when the row already has one.
        if row._color is not None:
            dlg.Color = row._color
        if dlg.ShowDialog(_hwnd) == DialogResult.OK:
            row.set_color(dlg.Color)

    def _on_grid_click(self, sender, e):
        """Click on the row to toggle the checkbox, except on the swatch or
        a combo, which have their own behaviour. Shift extends the range."""
        item = _row_from_click(e, (ToggleButton, WComboBox, WButton))
        if item is not None:
            self._anchor = _toggle_rows(self._items, self._anchor, item)

    # ---- batch bar (same behaviour as the legacy Quick-Apply) ----
    def _q_pick_color(self, s, e):
        dlg = ColorDialog(); dlg.FullOpen = True
        if self._q_color is not None:
            dlg.Color = self._q_color
        if dlg.ShowDialog(_hwnd) == DialogResult.OK:
            self._q_color = dlg.Color
            self.BtnQColor.Background = _brush(dlg.Color)
            self.BtnQColor.ToolTip = u'Click to pick a color'

    def _q_apply(self, s, e):
        """Copy to the checked rows only the fields picked in the bar; a field
        left on "no change" keeps whatever each row already has."""
        clr = self._q_color
        pat = self.CmbQPattern.SelectedItem
        wt  = self.CmbQWeight.SelectedItem
        for row in self.rows:
            if not row.is_checked:
                continue
            if clr is not None:
                row.set_color(clr)
            if pat is not None and pat != _NO_CHANGE:
                row.PatternName = pat
            if wt is not None and wt != _NO_CHANGE:
                row.Weight = int(wt)

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


# ============================================================
#  Change Revision -- shared helpers
# ============================================================

class RevItem(object):
    """One entry of a target-revision picker.

    `CanPick` is False for issued revisions: Revit refuses to move a cloud into
    an issued revision, so the entry stays visible (you can see it exists) but
    the ComboBoxItem style greys it out."""

    def __init__(self, data):
        self.data    = data
        self.Label   = data['label']
        self.CanPick = not data['issued']

    def ToString(self):
        """Safety net: if some WPF path falls back to ToString instead
        of using the ItemTemplate, show the label and not
        IronPython.NewTypes.System.Object_1$1."""
        return self.Label

    def __str__(self):
        return self.Label


class RevPicker(object):
    """The only dropdown: pick the target revision.

    It used to be two controls (a small filter box + the combo) and that was
    confusing: the filter looked like part of the target.  With `IsEditable`
    the ComboBox's own text box is the search box, like Revit's list.

    Three things to know so as not to break it:
      - `Items.Clear()` wipes the typed text, so it has to be put back with
        the guard on (otherwise the filter eats every keystroke).
      - while filtering, `SelectedItem` stays None (the text matches no
        item), so the confirmed choice is kept separately in
        `_picked`.
      - when the dropdown closes the full list is restored and the box keeps
        the chosen revision, not the fragment it was searched with.
    """

    def __init__(self, combo, revisions, on_pick=None):
        self.combo    = combo
        self.items    = [RevItem(d) for d in revisions]
        self._on_pick = on_pick
        self._guard   = False
        self._picked  = None
        self._needle  = u""

        # Items are loaded once and the filter goes through the view.
        # NOTE: it has to be a CollectionView over ItemsSource. Setting
        # `combo.Items.Filter` directly accepts the predicate (CanFilter
        # returns True) and filters NOTHING; measured, not assumed.
        self._src = ObservableCollection[object]()
        for it in self.items:
            self._src.Add(it)
        cvs = CollectionViewSource()
        cvs.Source = self._src
        self._view = cvs.View
        self._view.Filter = Predicate[object](self._match)
        combo.ItemsSource = self._view
        # With a CollectionView as ItemsSource, the ComboBox syncs its
        # selection with the view's CurrentItem: when filtering it auto-selects
        # what is left and that REWRITES the text being typed. Turn it off.
        combo.IsSynchronizedWithCurrentItem = False

        combo.SelectionChanged += self._on_sel
        combo.DropDownOpened   += self._on_opened
        combo.DropDownClosed   += self._on_closed
        # The filter TextBox is inside the template: we have to wait until it
        # is applied. TextChanged is hooked once, the first time it opens.
        self._hooked = False

        for it in self.items:
            if it.CanPick:
                self._picked = it      # the last pickable == highest sequence
        self._reset()

    # ---- what the panel uses ----
    @property
    def has_revisions(self):
        return bool(self.items)

    def target(self):
        """(ElementId, label) of the chosen revision, or (None, None)."""
        if self._picked is None:
            return None, None
        return self._picked.data['id'], self._picked.data['label']

    def preselect(self, rev_int):
        for it in self.items:
            if it.data['int'] == rev_int and it.CanPick:
                self._picked = it
                self._reset()
                return

    # ---- internal ----
    def _match(self, item):
        """What decides whether an item goes into the visible list."""
        if not self._needle:
            return True
        try:
            return self._needle in item.Label.lower()
        except Exception:
            return True

    def _visible_count(self):
        n = 0
        for _ in self.combo.Items:
            n += 1
        return n

    def _part(self, name):
        try:
            return self.combo.Template.FindName(name, self.combo)
        except Exception:
            return None

    def _hook_filter(self):
        """Hook up the popup's search box. It only exists once the template
        is applied, that is, the first time the dropdown opens."""
        if self._hooked:
            return
        box = self._part('FilterBox')
        if box is None:
            return
        box.TextChanged += self._on_text
        self._hooked = True

    def _upd_empty(self):
        """A "no match" sign instead of an empty popup."""
        empty = self._part('FilterEmpty')
        if empty is not None:
            empty.Visibility = (WVisibility.Visible if self._visible_count() == 0
                                else WVisibility.Collapsed)

    def _refilter(self, needle):
        self._needle = (needle or u"").strip().lower()
        try:
            self._view.Refresh()
        except Exception:
            pass

    def _reset(self):
        """Full list and the confirmed choice in the closed box."""
        self._guard = True
        try:
            self._refilter(u"")
            self.combo.SelectedItem = self._picked
        finally:
            self._guard = False

    def _clear_filter(self):
        box = self._part('FilterBox')
        if box is not None and box.Text:
            self._guard = True
            try:
                box.Text = u""
            finally:
                self._guard = False
        ph = self._part('FilterPh')
        if ph is not None:
            ph.Visibility = WVisibility.Visible

    def _on_text(self, sender, e):
        if self._guard:
            return
        box = self._part('FilterBox')
        txt = (box.Text or u"") if box is not None else u""
        ph = self._part('FilterPh')
        if ph is not None:
            ph.Visibility = (WVisibility.Collapsed if txt
                             else WVisibility.Visible)
        self._refilter(txt)
        self._upd_empty()

    def _on_opened(self, sender, e):
        """Each opening starts with the whole list and focus on the search box."""
        self._hook_filter()
        self._clear_filter()
        self._refilter(u"")
        self._upd_empty()
        box = self._part('FilterBox')
        if box is not None:
            try:
                box.Focus()
            except Exception:
                pass

    def _on_sel(self, sender, e):
        if self._guard:
            return
        it = self.combo.SelectedItem
        if it is not None:
            self._picked = it
            if self._on_pick is not None:
                self._on_pick()

    def _on_closed(self, sender, e):
        self._clear_filter()
        self._reset()
        if self._on_pick is not None:
            self._on_pick()


def _all_revisions():
    """Every revision in the model, in sequence order.

    Mirrors the dropdown Revit itself shows in a cloud's Identity Data:
    `Seq. <SequenceNumber> - <Description>`.  Revision.GetAllRevisionIds already
    returns them sequence-ordered; the category collector is the fallback for
    builds where the static method is missing."""
    ids = None
    try:
        ids = list(DB.Revision.GetAllRevisionIds(doc))
    except Exception:
        ids = None
    if not ids:
        try:
            ids = [r.Id for r in DB.FilteredElementCollector(doc)
                   .OfCategory(DB.BuiltInCategory.OST_Revisions)
                   .WhereElementIsNotElementType().ToElements()]
        except Exception:
            ids = []
    out = []
    for rid in ids:
        r = doc.GetElement(rid)
        if r is None:
            continue
        try:
            seq = int(r.SequenceNumber)
        except Exception:
            seq = 0
        try:
            issued = bool(r.Issued)
        except Exception:
            issued = False
        desc = rev_desc(r) or u"(no description)"
        d = {'id': rid, 'int': _id_val(rid), 'seq': seq, 'issued': issued,
             'num': rev_num(r) or u"", 'desc': desc}
        d['label'] = u"Seq. {0} - {1}{2}".format(
            seq, desc, u"   (issued)" if issued else u"")
        out.append(d)
    out.sort(key=lambda x: x['seq'])
    return out


def _rev_of_cloud(cloud):
    """The Revision element a cloud belongs to, or None."""
    try:
        p = cloud.get_Parameter(DB.BuiltInParameter.REVISION_CLOUD_REVISION)
        return doc.GetElement(p.AsElementId()) if p else None
    except Exception:
        return None


def _rev_title(rev):
    """A revision the way the user knows it: its description in quotes (what
    step 1 shows), else its number. Never the bare sequence number alone."""
    d = rev_desc(rev) if rev is not None else u""
    if d:
        return u"'{0}'".format(d)
    n = rev_num(rev) if rev is not None else u""
    return u"revision {0}".format(n) if n else u"the revision"


def _rev_is_issued(rev):
    if rev is None:
        return False
    try:
        return bool(rev.Issued)
    except Exception:
        return False


def _set_cloud_revision(cloud, rev_id):
    """Move a cloud to `rev_id`. Returns (ok, detail).

    Verifies RIGHT AWAY. Revit not complaining on write does not guarantee the
    value went in: the property may accept the assignment with no effect and
    `Parameter.Set` may return True anyway. So after each attempt the
    parameter is read back, and if the value is not there, it says which way
    was taken and what each one answered. A named failure beats a dubious
    success."""
    want = _id_val(rev_id)
    tried = []

    def _current():
        try:
            p = cloud.get_Parameter(DB.BuiltInParameter.REVISION_CLOUD_REVISION)
            if p is not None:
                return _id_val(p.AsElementId())
        except Exception:
            pass
        return None

    if _current() == want:
        return True, None                      # it was already there

    # --- way 1: the documented property ---
    try:
        cloud.RevisionId = rev_id
        if _current() == want:
            return True, None
        tried.append(u"RevisionId accepted but did not stick")
    except Exception as ex:
        tried.append(u"RevisionId: {0}".format(ex))

    # --- way 2: the parameter ---
    try:
        p = cloud.get_Parameter(DB.BuiltInParameter.REVISION_CLOUD_REVISION)
        if p is None:
            tried.append(u"no Revision parameter")
        elif p.IsReadOnly:
            tried.append(u"Revision parameter is read-only")
        elif not p.Set(rev_id):
            tried.append(u"Parameter.Set refused the value")
        elif _current() != want:
            tried.append(u"Parameter.Set accepted but did not stick")
        else:
            return True, None
    except Exception as ex:
        tried.append(u"parameter: {0}".format(ex))

    return False, u"; ".join(tried)


class RevPickRow(INotifyPropertyChanged):
    """One row of the Change Revision grid.

    Only IsSelected and NewRev change after construction, so those are the only
    notified properties.  A cloud sitting on an issued revision cannot move:
    CanChange is False, which disables its checkbox and dims the row."""
    PropertyChanged = None

    def __init__(self, cloud, rev, sheet_txt):
        self._handlers = []
        self._selected = False
        self._new      = u""
        self.cloud     = cloud
        self.cloud_id  = _id_val(cloud.Id)
        self.rev_int   = _id_val(rev.Id) if rev is not None else NO_REV_KEY
        self.CanChange = not _rev_is_issued(rev)
        self.CloudIdText = u"{0}".format(self.cloud_id)
        self.Sheet       = sheet_txt
        if rev is None:
            self.rev_seq    = -1
            self.SeqText    = u"--"
            self.CurrentRev = u"(no revision)"
            self.RevLabel   = u"(no revision)"
        else:
            try:
                seq = int(rev.SequenceNumber)
            except Exception:
                seq = 0
            desc = rev_desc(rev) or u"(no description)"
            self.rev_seq    = seq
            self.SeqText    = u"{0}".format(seq)
            self.CurrentRev = desc + (u"" if self.CanChange else u"   (issued)")
            self.RevLabel   = u"Seq. {0} - {1}".format(seq, desc)

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
        if not self.CanChange:
            value = False
        if self._selected != value:
            self._selected = value
            self._notify('IsSelected')

    IsSelected = property(get_IsSelected, set_IsSelected)

    def get_NewRev(self):
        return self._new

    NewRev = property(get_NewRev)

    def set_new_rev(self, label):
        lbl = label or u""
        if self._new != lbl:
            self._new = lbl
            self._notify('NewRev')


# ---- Change Revision window (slantisui); the picker comes from _REVPICK_RES
_REV_BODY = u'''
<Grid>
  <Grid.Resources>
    <!-- Grid row: extends the library row (hover, selection, divider) and adds
         the only thing of its own: the clouds of an already issued revision
         cannot be moved, so they stay visible but dimmed. -->
    <Style x:Key="RevRow" TargetType="DataGridRow"
           BasedOn="{StaticResource {x:Type DataGridRow}}">
      <Style.Triggers>
        <DataTrigger Binding="{Binding CanChange}" Value="False">
          <Setter Property="Opacity" Value="0.62"/>
        </DataTrigger>
      </Style.Triggers>
    </Style>
  </Grid.Resources>

  <Grid.RowDefinitions>
    <RowDefinition Height="Auto"/>   <!-- caption -->
    <RowDefinition Height="*"/>      <!-- grid -->
    <RowDefinition Height="Auto"/>   <!-- batch bar -->
  </Grid.RowDefinitions>

  <TextBlock Grid.Row="0" x:Name="LblCaption" Margin="0,0,0,10"
             Foreground="__TEXT_DIM__" TextWrapping="Wrap"/>

  <DataGrid Grid.Row="1" x:Name="Grid" CanUserAddRows="False" RowHeight="42"
            RowStyle="{StaticResource RevRow}">
    <DataGrid.Columns>
      <DataGridTemplateColumn Width="46" CanUserResize="False">
        <DataGridTemplateColumn.CellTemplate>
          <DataTemplate>
            <CheckBox Style="{StaticResource BrandCheck}"
                      IsChecked="{Binding IsSelected, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                      IsEnabled="{Binding CanChange}"
                      HorizontalAlignment="Center" VerticalAlignment="Center"/>
          </DataTemplate>
        </DataGridTemplateColumn.CellTemplate>
      </DataGridTemplateColumn>

      <DataGridTextColumn Header="Cloud ID" Binding="{Binding CloudIdText}" Width="92">
        <DataGridTextColumn.ElementStyle>
          <Style TargetType="TextBlock">
            <Setter Property="Foreground" Value="__TEXT_DIM__"/>
          </Style>
        </DataGridTextColumn.ElementStyle>
      </DataGridTextColumn>

      <DataGridTextColumn Header="Seq." Binding="{Binding SeqText}" Width="58">
        <DataGridTextColumn.ElementStyle>
          <Style TargetType="TextBlock">
            <Setter Property="FontWeight" Value="Medium"/>
          </Style>
        </DataGridTextColumn.ElementStyle>
      </DataGridTextColumn>

      <DataGridTextColumn Header="Current revision" Binding="{Binding CurrentRev}" Width="*">
        <DataGridTextColumn.ElementStyle>
          <Style TargetType="TextBlock">
            <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
            <Setter Property="ToolTip" Value="{Binding CurrentRev}"/>
          </Style>
        </DataGridTextColumn.ElementStyle>
      </DataGridTextColumn>

      <DataGridTextColumn Header="Sheet" Binding="{Binding Sheet}" Width="210">
        <DataGridTextColumn.ElementStyle>
          <Style TargetType="TextBlock">
            <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
            <Setter Property="ToolTip" Value="{Binding Sheet}"/>
          </Style>
        </DataGridTextColumn.ElementStyle>
      </DataGridTextColumn>

      <DataGridTextColumn Header="&#8594; New revision" Binding="{Binding NewRev}" Width="230">
        <DataGridTextColumn.ElementStyle>
          <Style TargetType="TextBlock">
            <Setter Property="FontWeight" Value="Medium"/>
            <Setter Property="Foreground" Value="__ACCENT_DK__"/>
            <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
            <Setter Property="ToolTip" Value="{Binding NewRev}"/>
          </Style>
        </DataGridTextColumn.ElementStyle>
      </DataGridTextColumn>
    </DataGrid.Columns>
  </DataGrid>

  <!-- Batch bar, two lines. Top: which clouds. Bottom: the target. -->
  <Border Grid.Row="2" Margin="0,14,0,0" CornerRadius="10" Padding="14,12"
          Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1">
    <StackPanel>
      <!-- Only shown if the selection spans more than one revision (the code
           decides). It is a library ComboBox, without a search box:
           that is why the picker resource is NOT pasted up here. -->
      <StackPanel x:Name="FilterLine" Orientation="Horizontal" Margin="0,0,0,10">
        <TextBlock Text="Current revision:" FontWeight="Medium"
                   VerticalAlignment="Center" Margin="0,0,10,0"/>
        <ComboBox x:Name="CmbFilterRev" Width="360" VerticalAlignment="Center"
                  Margin="0,0,10,0"/>
        <Button x:Name="BtnCheckFiltered" Content="&#x2713; Check filtered"
                Style="{StaticResource BtnGhost}" VerticalAlignment="Center"/>
      </StackPanel>

      <StackPanel Orientation="Horizontal">
        <StackPanel.Resources>
          <!-- Resources of the revision picker (RevLabelTemplate, RevItem, the
               ComboBox template with FilterBox/FilterPh/FilterEmpty): the
               integrator pastes them from the shared literal. Scoped to this
               row on purpose, see doubts (a) in the file header. -->
          __REVPICK_RES__
        </StackPanel.Resources>
        <TextBlock Text="Move to:" FontWeight="Medium"
                   VerticalAlignment="Center" Margin="0,0,10,0"/>
        <ComboBox x:Name="CmbTargetRev" Width="470" VerticalAlignment="Center"
                  ItemContainerStyle="{StaticResource RevItem}"
                  ItemTemplate="{StaticResource RevLabelTemplate}" Margin="0,0,18,0"/>
        <Button x:Name="BtnAll" Content="&#x2713; All" Style="{StaticResource BtnGhost}"
                VerticalAlignment="Center" Margin="0,0,6,0"/>
        <Button x:Name="BtnNone" Content="&#x2717; None" Style="{StaticResource BtnGhost}"
                VerticalAlignment="Center"/>
      </StackPanel>
    </StackPanel>
  </Border>
</Grid>
'''
_REV_BODY = _tok(_REV_BODY.replace(u'__REVPICK_RES__', _REVPICK_RES))
_REV_FOOTER = u'''
<Grid>
  <Grid.ColumnDefinitions>
    <ColumnDefinition Width="*"/>
    <ColumnDefinition Width="Auto"/>
  </Grid.ColumnDefinitions>
  <TextBlock x:Name="LblCount" VerticalAlignment="Center" Margin="0,0,16,0"
             Foreground="__TEXT_DIM__" TextTrimming="CharacterEllipsis"/>
  <StackPanel Grid.Column="1" Orientation="Horizontal" VerticalAlignment="Center">
    <Button x:Name="BtnCancel" Content="Cancel" Style="{StaticResource BtnGhost}"
            Margin="0,0,8,0"/>
    <Button x:Name="BtnOk" Content="Change Revision" Style="{StaticResource BtnPrimary}"/>
  </StackPanel>
</Grid>
'''
_REV_FOOTER = _tok(_REV_FOOTER)

class ChangeRevisionDialog(object):
    """Pick WHICH clouds move and WHERE to.

    The batch bar answers "all the X" (filter by current revision, then Check
    filtered); the row checkboxes answer "only some X".  Returns a WinForms
    DialogResult so the caller reads like the other sub-dialogs."""

    def __init__(self, clouds, revisions, preselect_int=None):
        self._ok = False

        self.win = ui.parse(
            u'Cloud Manager \u00b7 Change revision',
            u'Move the checked clouds to another revision',
            _REV_BODY, _REV_FOOTER, width=1080, height=700)
        self.win.MinWidth  = 940
        self.win.MinHeight = 500

        f = self.win.FindName
        self.LblCaption = f('LblCaption'); self.LblCount = f('LblCount')
        self.Grid = f('Grid')
        self.FilterLine = f('FilterLine')
        self.CmbFilterRev = f('CmbFilterRev')
        self.BtnCheckFiltered = f('BtnCheckFiltered')
        self.CmbTargetRev = f('CmbTargetRev')
        self.BtnAll = f('BtnAll'); self.BtnNone = f('BtnNone')
        self.BtnOk = f('BtnOk'); self.BtnCancel = f('BtnCancel')

        _own_to_revit(self.win)

        # ---- one row per cloud ----
        vp_map = _sheet_label_map()
        self.rows = []
        for c in clouds:
            row = RevPickRow(c, _rev_of_cloud(c), _cloud_sheet_text(c, vp_map))
            row.add_PropertyChanged(self._on_row_changed)
            self.rows.append(row)

        self._anchor = None          # last clicked row, for Shift+click
        self._items = ObservableCollection[object]()
        self.Grid.ItemsSource = self._items

        n = len(self.rows)
        locked = self.locked_count

        # ---- which revision(s) the selection comes from ----
        counts = {}
        for r in self.rows:
            if r.rev_int not in counts:
                counts[r.rev_int] = [0, r.RevLabel, r.rev_seq]
            counts[r.rev_int][0] += 1
        self._filter_keys = [None]
        mixed = len(counts) > 1

        cap = u"One row per cloud. "
        if mixed:
            cap += (u"Filter by current revision, check the ones you want, pick the "
                    u"target revision, then Apply. {0:,} cloud{1} in scope from {2:,} "
                    u"revisions.".format(n, u"s" if n != 1 else u"", len(counts)))
        else:
            only = counts[list(counts.keys())[0]][1]
            cap += (u"Check the ones you want, pick the target revision, then Apply. "
                    u"{0:,} cloud{1} in scope, all on {2}.".format(
                        n, u"s" if n != 1 else u"", only))
        if locked:
            cap += (u"  {0:,} of them sit on an issued revision and cannot move.".format(locked))
        self.LblCaption.Text = cap

        # The current-revision chooser only earns screen space if there is more
        # than one: with a single one the header says it and the dropdown is
        # redundant.
        if mixed:
            self.CmbFilterRev.Items.Add(u"All ({0:,})".format(n))
            for key in sorted(counts.keys(), key=lambda k: counts[k][2]):
                cnt, label, _seq = counts[key]
                self.CmbFilterRev.Items.Add(u"{0}  ({1:,})".format(label, cnt))
                self._filter_keys.append(key)
            self.CmbFilterRev.SelectedIndex = 0
        else:
            self.FilterLine.Visibility = WVisibility.Collapsed

        # ---- the picker (all the model's revisions) ----
        self._picker = RevPicker(self.CmbTargetRev, revisions, self._on_target_changed)
        if preselect_int is not None:
            self._picker.preselect(preselect_int)

        # ---- events ----
        self.win.PreviewKeyDown += self._on_key
        self.Grid.PreviewMouseLeftButtonUp += self._on_grid_click
        self.CmbFilterRev.SelectionChanged += self._on_filter_changed
        self.BtnCheckFiltered.Click += self._check_filtered
        self.BtnAll.Click += self._chk_all
        self.BtnNone.Click += self._chk_none
        self.BtnOk.Click += lambda s, e: self._done(True)
        self.BtnCancel.Click += lambda s, e: self._done(False)

        self._rebuild_grid()
        self._upd_count()

    # ---- surface the caller uses ----
    def ShowDialog(self, owner=None):
        """`owner` is accepted and ignored, like StyleSubDialog."""
        self.win.ShowDialog()
        return DialogResult.OK if self._ok else DialogResult.Cancel

    @property
    def locked_count(self):
        return sum(1 for r in self.rows if not r.CanChange)

    @property
    def target(self):
        """(ElementId, label) of the chosen revision, or (None, None)."""
        return self._picker.target()

    @property
    def chosen_clouds(self):
        return [r.cloud for r in self.rows if r.IsSelected and r.CanChange]

    # ---- window plumbing ----
    def _on_key(self, sender, e):
        if e.Key == Key.Escape:
            self._done(False)

    def _done(self, ok):
        eid, _label = self.target
        if ok and eid is None:
            return
        self._ok = ok
        self.win.Close()

    # ---- grid + current-revision filter ----
    def _current_filter_key(self):
        i = self.CmbFilterRev.SelectedIndex
        if i < 0 or i >= len(self._filter_keys):
            return None
        return self._filter_keys[i]

    def _visible_rows(self):
        key = self._current_filter_key()
        if key is None:
            return list(self.rows)
        return [r for r in self.rows if r.rev_int == key]

    def _rebuild_grid(self):
        self._items.Clear()
        for r in self._visible_rows():
            self._items.Add(r)

    def _on_filter_changed(self, s, e):
        self._rebuild_grid()
        self._upd_count()

    def _on_grid_click(self, sender, e):
        """Click on the row to toggle the checkbox, except on a control
        that has its own behaviour. Shift extends the range; rows that cannot
        be moved (issued revision) are left out, also within the range."""
        item = _row_from_click(e, (ToggleButton, WComboBox, WButton))
        if item is not None and getattr(item, 'CanChange', True):
            self._anchor = _toggle_rows(self._items, self._anchor, item)

    # ---- target picker ----
    def _on_target_changed(self):
        self._refresh_new_rev()
        self._upd_count()

    def _refresh_new_rev(self):
        _eid, label = self.target
        for r in self.rows:
            r.set_new_rev(label if (r.IsSelected and r.CanChange) else u"")

    # ---- checking ----
    def _on_row_changed(self, sender, e):
        # Guard against the NewRev notification bouncing back in here.
        try:
            if e.PropertyName != 'IsSelected':
                return
        except Exception:
            return
        _eid, label = self.target
        try:
            sender.set_new_rev(label if (sender.IsSelected and sender.CanChange) else u"")
        except Exception:
            pass
        self._upd_count()

    def _check_filtered(self, s, e):
        for r in self._visible_rows():
            if r.CanChange:
                r.IsSelected = True
        self._after_bulk()

    def _chk_all(self, s, e):
        for r in self.rows:
            if r.CanChange:
                r.IsSelected = True
        self._after_bulk()

    def _chk_none(self, s, e):
        for r in self.rows:
            r.IsSelected = False
        self._after_bulk()

    def _after_bulk(self):
        self._refresh_new_rev()
        self._upd_count()

    def _upd_count(self):
        n = sum(1 for r in self.rows if r.IsSelected and r.CanChange)
        eid, label = self.target
        txt = u"{0:,} of {1:,} cloud{2} checked".format(
            n, len(self.rows), u"s" if len(self.rows) != 1 else u"")
        if label:
            txt += u"  ->  moving to {0}".format(label)
        locked = self.locked_count
        if locked:
            txt += u"   ({0:,} locked: issued)".format(locked)
        self.LblCount.Text = txt
        self.BtnOk.IsEnabled = (n > 0 and eid is not None)
        # without a filter line there is nothing "check filtered" can do
        self.BtnCheckFiltered.IsEnabled = (
            self.FilterLine.Visibility == WVisibility.Visible)


# ============================================================
#  Find Cloud -- why a cloud is not drawn where it should be (2026-09-18)
# ============================================================
#  Read-only. Answers the question that used to be answered by hand with
#  Reveal Hidden Elements + Select by ID + looking at two crops: if a cloud
#  does not show up in a view of the scope, it is because it is hidden (Show
#  fixes it), because a crop covers it (the model crop or the annotation crop,
#  which are TWO), because the category or a filter turns it off, or because
#  the revision is Hidden at project level or on the sheet. The tool only
#  looked at the model crop to build the step 2 list, so it could list a
#  cloud "in SEGMENT D" that Revit would never draw there.

_PLAN_TYPES = ("FloorPlan", "CeilingPlan", "AreaPlan", "EngineeringPlan")


def _safe_name(v):
    try:
        if isinstance(v, DB.ViewSheet):
            return _sheet_label(v)
        return v.Name or u"--"
    except Exception:
        return u"--"


def _cat_id(bic):
    try:
        return DB.ElementId(bic)
    except Exception:
        return _make_eid(int(bic))


def _bb_xy(bb):
    """(minx, miny, maxx, maxy) in model coordinates, or None. Applies the
    box's Transform: the CropBox of a rotated plan comes in view axes, not
    model axes."""
    if bb is None:
        return None
    try:
        t = bb.Transform
        pts = [t.OfPoint(bb.Min), t.OfPoint(bb.Max),
               t.OfPoint(DB.XYZ(bb.Min.X, bb.Max.Y, bb.Min.Z)),
               t.OfPoint(DB.XYZ(bb.Max.X, bb.Min.Y, bb.Min.Z))]
        xs = [p.X for p in pts]; ys = [p.Y for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))
    except Exception:
        try:
            return (bb.Min.X, bb.Min.Y, bb.Max.X, bb.Max.Y)
        except Exception:
            return None


def _loop_xy(loop):
    """(minx, miny, maxx, maxy) of a CurveLoop (the annotation crop)."""
    xs = []; ys = []
    try:
        for crv in loop:
            for i in (0, 1):
                p = crv.GetEndPoint(i)
                xs.append(p.X); ys.append(p.Y)
    except Exception:
        return None
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _xy_relation(inner, outer):
    """'inside' if inner fits entirely in outer, 'crosses' if it touches the
    edge, 'outside' if they do not touch. The difference matters: Revit hides
    an annotation that merely TOUCHES the annotation crop, not only the one
    that falls outside."""
    if inner is None or outer is None:
        return None
    ix0, iy0, ix1, iy1 = inner
    ox0, oy0, ox1, oy1 = outer
    if ix1 < ox0 or ix0 > ox1 or iy1 < oy0 or iy0 > oy1:
        return 'outside'
    if ix0 >= ox0 and iy0 >= oy0 and ix1 <= ox1 and iy1 <= oy1:
        return 'inside'
    return 'crosses'


def _rel_label(rel):
    return {'inside': u"inside", 'crosses': u"TOUCHES THE BOUNDARY",
            'outside': u"OUTSIDE"}.get(rel, u"?")


def _vis_label(s):
    return {u"Hidden": u"Hidden", u"TagVisible": u"Tag only",
            u"CloudAndTagVisible": u"Cloud and Tag"}.get(s, s or u"?")


def _diagnose_cloud(cloud, views, vp_sheets, sheet_lbls):
    """Why a cloud is or is not visible in each view of the scope.

    Returns (short verdict, full report). Writes nothing.
    vp_sheets: {view_id_int: [ViewSheet]} (the panel's _vp_map).
    sheet_lbls: {view_id_int: [label]} (for _cloud_sheet_text)."""
    lines = []
    cid = _id_val(cloud.Id)
    lines.append(u"Cloud ID {0}".format(cid))

    rev = _rev_of_cloud(cloud)
    rev_vis = None
    if rev is None:
        lines.append(u"Revision: (none)")
    else:
        try:
            rev_vis = str(rev.Visibility)
        except Exception:
            rev_vis = None
        lines.append(u"Revision: Seq. {0} - {1}".format(
            rev_num(rev) or u"?", rev_desc(rev) or u"(no description)"))
        lines.append(u"Issued: {0}    Project visibility: {1}".format(
            u"yes" if _rev_is_issued(rev) else u"no", _vis_label(rev_vis)))

    owner = None
    try:
        owner = doc.GetElement(cloud.OwnerViewId)
    except Exception:
        owner = None
    if owner is None:
        lines.append(u"Owner view: (unknown)")
        return (u"Owner view unknown: Revit cannot tell where this cloud lives.",
                u"\n".join(lines))
    owner_int = _id_val(owner.Id)
    lines.append(u"Owner view: {0} ({1})".format(_safe_name(owner), friendly_view_type(owner)))
    lines.append(u"On sheets: {0}".format(_cloud_sheet_text(cloud, sheet_lbls)))

    bb = None
    try:
        bb = cloud.get_BoundingBox(owner)
    except Exception:
        bb = None
    if bb is None:
        try:
            bb = cloud.get_BoundingBox(None)
        except Exception:
            bb = None
    cloud_xy = _bb_xy(bb)

    dep_ints = set()
    try:
        dep_ints = set(_id_val(d) for d in owner.GetDependentViewIds())
    except Exception:
        pass
    owner_primary = None
    try:
        pid = owner.GetPrimaryViewId()
        if pid is not None and _id_val(pid) > 0:
            owner_primary = _id_val(pid)
    except Exception:
        pass
    family = set([owner_int]) | dep_ints
    if owner_primary is not None:
        family.add(owner_primary)

    verdicts = []
    for vid_int, v in sorted(views.items(), key=lambda kv: _safe_name(kv[1])):
        vname = _safe_name(v)
        lines.append(u"")

        # -- sheet: the cloud arrives through the viewport of its view (or its family)
        if isinstance(v, DB.ViewSheet):
            on_sheet = (owner_int == vid_int)
            for fid in family:
                for sh in vp_sheets.get(fid, []):
                    if _id_val(sh.Id) == vid_int:
                        on_sheet = True
            lines.append(u"Sheet {0}:".format(vname))
            if not on_sheet:
                lines.append(u"  none of the cloud's views is placed on this sheet")
                verdicts.append(u"{0}: not on this sheet".format(vname))
                continue
            if rev is not None:
                sv = None
                try:
                    sv = str(v.GetRevisionVisibility(_to_rev_id(rev.Id)))
                except Exception:
                    sv = None
                lines.append(u"  revision on this sheet: {0}".format(_vis_label(sv)))
                if sv == u"Hidden":
                    lines.append(u"  => NOT drawn: the revision is Hidden in Revisions on Sheet")
                    verdicts.append(u"{0}: revision set to Hidden on this sheet".format(vname))
                    continue
            if rev_vis == u"Hidden":
                lines.append(u"  => NOT drawn: revision visibility is Hidden at project level (Cloud + Tag fixes it)")
                verdicts.append(u"{0}: revision Hidden at project level".format(vname))
                continue
            lines.append(u"  => reaches this sheet through its viewport; check the view itself below or in scope")
            verdicts.append(u"{0}: should be visible through its viewport".format(vname))
            continue

        # -- view: must be the owner, a dependent or the primary
        if vid_int == owner_int:
            rel = u"owner view"
        elif vid_int in dep_ints:
            rel = u"dependent of the owner view"
        elif owner_primary is not None and vid_int == owner_primary:
            rel = u"primary of the owner (cloud placed in a dependent)"
        else:
            lines.append(u"View {0}: unrelated to the owner view".format(vname))
            lines.append(u"  a cloud only appears in its owner view, its dependents or its primary")
            verdicts.append(u"{0}: cannot appear here (different view)".format(vname))
            continue
        lines.append(u"View {0} ({1}):".format(vname, rel))

        problems = []

        if rev_vis == u"Hidden":
            problems.append(u"revision visibility is Hidden at project level (Cloud + Tag fixes it)")

        try:
            if v.AreAnnotationCategoriesHidden:
                lines.append(u"  hide annotation categories: ON")
                problems.append(u"'Hide annotation categories' is on in this view")
        except Exception:
            pass

        tmpl = u""
        try:
            tid = v.ViewTemplateId
            if tid is not None and _id_val(tid) > 0:
                t = doc.GetElement(tid)
                tmpl = u" (view template: {0})".format(_safe_name(t) if t else u"?")
        except Exception:
            pass
        cat_hidden = None
        try:
            cat_hidden = bool(v.GetCategoryHidden(_cat_id(DB.BuiltInCategory.OST_RevisionClouds)))
        except Exception:
            cat_hidden = None
        lines.append(u"  Revision Clouds category: {0}{1}".format(
            u"HIDDEN" if cat_hidden else (u"visible" if cat_hidden is not None else u"?"), tmpl))
        if cat_hidden:
            problems.append(u"Revision Clouds category is off in V/G{0}".format(tmpl))

        hiding = []
        try:
            cat_int = _id_val(_cat_id(DB.BuiltInCategory.OST_RevisionClouds))
            for fid in v.GetFilters():
                try:
                    if v.GetFilterVisibility(fid):
                        continue
                    pfe = doc.GetElement(fid)
                    if cat_int in [_id_val(c) for c in pfe.GetCategories()]:
                        hiding.append(pfe.Name)
                except Exception:
                    pass
        except Exception:
            pass
        lines.append(u"  filters hiding clouds: {0}".format(u", ".join(hiding) if hiding else u"none"))
        if hiding:
            problems.append(u"filter(s) {0} hide the category".format(u", ".join(hiding)))

        try:
            if v.IsInTemporaryViewMode(DB.TemporaryViewMode.TemporaryHideIsolate):
                lines.append(u"  temporary hide/isolate: ACTIVE")
                problems.append(u"temporary hide/isolate is active")
        except Exception:
            pass

        hid = None
        try:
            hid = bool(cloud.IsHidden(v))
        except Exception:
            hid = None
        lines.append(u"  hidden in view: {0}".format(
            u"YES" if hid else (u"no" if hid is not None else u"?")))
        if hid:
            problems.append(u"hidden by element in this view (Show fixes it)")

        is_plan = str(v.ViewType) in _PLAN_TYPES
        crop_on = False
        try:
            crop_on = bool(v.CropBoxActive)
        except Exception:
            crop_on = False
        if not crop_on:
            lines.append(u"  model crop: not active")
        elif not is_plan:
            lines.append(u"  model crop: active (position not checked, plan views only)")
        else:
            rel_m = _xy_relation(cloud_xy, _bb_xy(v.CropBox))
            lines.append(u"  model crop: {0}".format(_rel_label(rel_m)))
            if rel_m == 'outside':
                problems.append(u"outside the crop region")

        ann_on = False
        try:
            p = v.get_Parameter(DB.BuiltInParameter.VIEWER_ANNOTATION_CROP_ACTIVE)
            ann_on = bool(p is not None and p.AsInteger() == 1)
        except Exception:
            ann_on = False
        if not ann_on:
            lines.append(u"  annotation crop: not active")
        elif not is_plan:
            lines.append(u"  annotation crop: active (position not checked, plan views only)")
        else:
            ann_xy = None
            try:
                ann_xy = _loop_xy(v.GetCropRegionShapeManager().GetAnnotationCropShape())
            except Exception:
                ann_xy = None
            rel_a = _xy_relation(cloud_xy, ann_xy)
            lines.append(u"  annotation crop: {0}".format(_rel_label(rel_a)))
            if rel_a == 'outside':
                problems.append(u"outside the annotation crop (the dashed inner line)")
            elif rel_a == 'crosses':
                problems.append(u"touching the annotation crop: Revit hides any annotation that "
                                u"crosses that dashed line, even partially")

        if problems:
            lines.append(u"  => NOT drawn here: " + u"; ".join(problems))
            verdicts.append(u"{0}: {1}".format(vname, problems[0]))
        else:
            lines.append(u"  => nothing hides it here. If you still cannot see it, use Zoom to: "
                         u"it may be tiny or under other graphics.")
            verdicts.append(u"{0}: should be visible".format(vname))

    if len(verdicts) == 1:
        short = verdicts[0]
    else:
        short = u"{0} view(s) checked, see the report.".format(len(verdicts))
    return short, u"\n".join(lines)


# ---- Step 3 window, the dashboard (slantisui)
_ACTION_BODY = u'''
<Grid>
  <Grid.Resources>
    <!-- Library ghost pill at the fixed action size, so every card lines up
         in two button columns. -->
    <Style x:Key="BtnAction" TargetType="Button" BasedOn="{StaticResource BtnGhost}">
      <Setter Property="Width"  Value="150"/>
      <Setter Property="Height" Value="36"/>
    </Style>
    <!-- The one button that says "this deletes". Red text only: the ghost
         template keeps its own border. -->
    <Style x:Key="BtnDanger" TargetType="Button" BasedOn="{StaticResource BtnAction}">
      <Setter Property="Foreground" Value="__BAD__"/>
    </Style>

    <!-- Flat clickable text (step 4 of the stepper, View details). The library
         has no borderless button, so this template is bespoke; colours are
         markers only. -->
    <Style x:Key="LinkButton" TargetType="Button">
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" CornerRadius="8" Padding="8,4" Background="Transparent">
              <ContentPresenter VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="__ROW_HOVER__"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="bd" Property="Background" Value="Transparent"/>
                <Setter Property="Cursor" Value="Arrow"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <!-- Neutral action card: the colour no longer encodes risk. -->
    <Style x:Key="ActionCard" TargetType="Border">
      <Setter Property="CornerRadius"    Value="10"/>
      <Setter Property="Background"      Value="__WIN_BG__"/>
      <Setter Property="BorderBrush"     Value="__CARD_BD__"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding"         Value="16,10"/>
      <Setter Property="Margin"          Value="0,0,0,8"/>
      <Setter Property="MinHeight"       Value="64"/>
    </Style>
    <Style x:Key="CardTitle" TargetType="TextBlock">
      <Setter Property="FontSize"   Value="13.5"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
    </Style>
    <Style x:Key="CardHint" TargetType="TextBlock">
      <Setter Property="FontSize"     Value="12"/>
      <Setter Property="Foreground"   Value="__TEXT_DIM__"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
      <Setter Property="Margin"       Value="0,3,0,0"/>
    </Style>
  </Grid.Resources>

  <Grid.RowDefinitions>
    <RowDefinition Height="Auto"/>   <!-- 0 stepper -->
    <RowDefinition Height="Auto"/>   <!-- 1 summary -->
    <RowDefinition Height="*"/>      <!-- 2 action cards (scrolls if short) -->
    <RowDefinition Height="Auto"/>   <!-- 3 feedback -->
  </Grid.RowDefinitions>

  <!-- Stepper. Steps 1 and 2 are done, 3 is this window, 4 opens the status of
       what the actions did and is off until the first action runs (Python
       flips IsEnabled / Opacity / ToolTip). -->
  <Grid Grid.Row="0" Margin="4,0,4,14">
    <Grid.ColumnDefinitions>
      <ColumnDefinition Width="Auto"/><ColumnDefinition Width="*"/>
      <ColumnDefinition Width="Auto"/><ColumnDefinition Width="*"/>
      <ColumnDefinition Width="Auto"/><ColumnDefinition Width="*"/>
      <ColumnDefinition Width="Auto"/>
    </Grid.ColumnDefinitions>

    <StackPanel Grid.Column="0" Orientation="Horizontal" VerticalAlignment="Center">
      <Border Width="26" Height="26" CornerRadius="13" Background="__ACCENT_T__">
        <TextBlock Text="&#x2713;" Foreground="__ACCENT_DK__" FontSize="12.5"
                   FontWeight="SemiBold" HorizontalAlignment="Center"
                   VerticalAlignment="Center"/>
      </Border>
      <TextBlock Text="Cloud" Margin="9,0,0,0" VerticalAlignment="Center"
                 FontWeight="Medium"/>
    </StackPanel>
    <Border Grid.Column="1" Height="2" Background="__INPUT_BD__" Margin="14,0"/>

    <StackPanel Grid.Column="2" Orientation="Horizontal" VerticalAlignment="Center">
      <Border Width="26" Height="26" CornerRadius="13" Background="__ACCENT_T__">
        <TextBlock Text="&#x2713;" Foreground="__ACCENT_DK__" FontSize="12.5"
                   FontWeight="SemiBold" HorizontalAlignment="Center"
                   VerticalAlignment="Center"/>
      </Border>
      <TextBlock Text="Scope" Margin="9,0,0,0" VerticalAlignment="Center"
                 FontWeight="Medium"/>
    </StackPanel>
    <Border Grid.Column="3" Height="2" Background="__INPUT_BD__" Margin="14,0"/>

    <StackPanel Grid.Column="4" Orientation="Horizontal" VerticalAlignment="Center">
      <Border Width="26" Height="26" CornerRadius="13" Background="__ACCENT_T__"
              BorderBrush="__ACCENT__" BorderThickness="1.5">
        <TextBlock Text="3" Foreground="__ACCENT_DK__" FontSize="12.5"
                   FontWeight="SemiBold" HorizontalAlignment="Center"
                   VerticalAlignment="Center"/>
      </Border>
      <TextBlock Text="Action" Margin="9,0,0,0" VerticalAlignment="Center"
                 FontWeight="SemiBold"/>
    </StackPanel>
    <Border Grid.Column="5" Height="2" Background="__INPUT_BD__" Margin="14,0"/>

    <Button x:Name="BtnStepStatus" Grid.Column="6" Style="{StaticResource LinkButton}"
            IsEnabled="False" ToolTip="Run an action to see its status">
      <StackPanel x:Name="StepStatusPanel" Orientation="Horizontal" Opacity="0.5">
        <Border Width="26" Height="26" CornerRadius="13" Background="__WIN_BG__"
                BorderBrush="__CHECK_BD__" BorderThickness="1.5">
          <TextBlock Text="4" Foreground="__TEXT_DIM__" FontSize="12.5"
                     FontWeight="SemiBold" HorizontalAlignment="Center"
                     VerticalAlignment="Center"/>
        </Border>
        <TextBlock Text="Status" Margin="9,0,0,0" VerticalAlignment="Center"
                   FontWeight="Medium"/>
      </StackPanel>
    </Button>
  </Grid>

  <!-- Summary: what is selected and how many views hold it. -->
  <Border Grid.Row="1" CornerRadius="10" Padding="14,12" Background="__CARD_BG__"
          BorderBrush="__CARD_BD__" BorderThickness="1">
    <StackPanel>
      <TextBlock x:Name="LblClouds" TextTrimming="CharacterEllipsis"/>
      <TextBlock x:Name="LblViews" Margin="0,5,0,0"/>
    </StackPanel>
  </Border>

  <ScrollViewer Grid.Row="2" Margin="0,16,0,0" VerticalScrollBarVisibility="Auto">
    <StackPanel>

      <StackPanel Orientation="Horizontal" Margin="2,0,0,8">
        <TextBlock Text="IN THE TARGETED VIEWS" Style="{StaticResource SectionHead}"
                   Margin="0" VerticalAlignment="Center"/>
        <TextBlock x:Name="LblScopeNote" Text="Uses the scope from step 2."
                   FontSize="12" Foreground="__TEXT_DIM__" Margin="12,0,0,0"
                   VerticalAlignment="Center"/>
      </StackPanel>

      <!-- Graphics -->
      <Border Style="{StaticResource ActionCard}">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="Auto"/>
            <ColumnDefinition Width="Auto"/>
          </Grid.ColumnDefinitions>
          <StackPanel Grid.Column="0" VerticalAlignment="Center" Margin="0,0,16,0">
            <TextBlock Style="{StaticResource CardTitle}" Text="Graphics"/>
            <TextBlock Style="{StaticResource CardHint}"
                       Text="Color, line pattern and weight per revision."/>
          </StackPanel>
          <Button x:Name="BtnStyle" Grid.Column="1" Style="{StaticResource BtnAction}"
                  Content="Apply Styles"/>
          <Button x:Name="BtnReset" Grid.Column="2" Style="{StaticResource BtnAction}"
                  Content="Reset Overrides" Margin="8,0,0,0"/>
        </Grid>
      </Border>

      <!-- Visibility in views -->
      <Border Style="{StaticResource ActionCard}">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="Auto"/>
            <ColumnDefinition Width="Auto"/>
          </Grid.ColumnDefinitions>
          <StackPanel Grid.Column="0" VerticalAlignment="Center" Margin="0,0,16,0">
            <TextBlock Style="{StaticResource CardTitle}" Text="Visibility"/>
            <TextBlock Style="{StaticResource CardHint}"
                       Text="Hide or unhide the selected clouds."/>
          </StackPanel>
          <Button x:Name="BtnShow" Grid.Column="1" Style="{StaticResource BtnAction}"
                  Content="Show"/>
          <Button x:Name="BtnHide" Grid.Column="2" Style="{StaticResource BtnAction}"
                  Content="Hide" Margin="8,0,0,0"/>
        </Grid>
      </Border>

      <!-- Revision display -->
      <Border Style="{StaticResource ActionCard}">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="Auto"/>
            <ColumnDefinition Width="Auto"/>
          </Grid.ColumnDefinitions>
          <StackPanel Grid.Column="0" VerticalAlignment="Center" Margin="0,0,16,0">
            <TextBlock Style="{StaticResource CardTitle}" Text="Revision display"/>
            <TextBlock Style="{StaticResource CardHint}"
                       Text="Show only the tag, or cloud and tag. Applies to the whole revision."/>
          </StackPanel>
          <Button x:Name="BtnTagOnly" Grid.Column="1" Style="{StaticResource BtnAction}"
                  Content="Tag Only"/>
          <Button x:Name="BtnCloudTag" Grid.Column="2" Style="{StaticResource BtnAction}"
                  Content="Cloud + Tag" Margin="8,0,0,0"/>
        </Grid>
      </Border>

      <!-- Delete: the only card with colour, because it is the only destructive
           one. Tint plus the 09-03 left stripe. -->
      <Border Style="{StaticResource ActionCard}" Background="__BAD_T__"
              Padding="0" Margin="0">
        <Grid>
          <Border Width="5" HorizontalAlignment="Left" Background="__BAD__"
                  CornerRadius="10,0,0,10"/>
          <Grid Margin="21,10,16,10">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="*"/>
              <ColumnDefinition Width="Auto"/>
              <ColumnDefinition Width="Auto"/>
            </Grid.ColumnDefinitions>
            <StackPanel Grid.Column="0" VerticalAlignment="Center" Margin="0,0,16,0">
              <TextBlock Style="{StaticResource CardTitle}" Text="Delete"/>
              <TextBlock Style="{StaticResource CardHint}"
                         Text="Permanently delete the selected clouds placed in these views."/>
            </StackPanel>
            <Button x:Name="BtnDelete" Grid.Column="2" Style="{StaticResource BtnDanger}"
                    Content="Delete Clouds"/>
          </Grid>
        </Grid>
      </Border>

    </StackPanel>
  </ScrollViewer>

  <!-- Feedback: one line with the result of the last action and the link to the
       detail (step 4). LblFb colour is set from code from the BRUSH_* tokens. -->
  <Border Grid.Row="3" Margin="0,14,0,0" CornerRadius="10" Padding="14,10"
          Background="__CARD_BG__" BorderBrush="__CARD_BD__" BorderThickness="1"
          MinHeight="40">
    <Grid>
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="Auto"/>
      </Grid.ColumnDefinitions>
      <TextBlock x:Name="LblFb" Grid.Column="0" FontSize="12.5" FontWeight="Medium"
                 TextWrapping="Wrap" VerticalAlignment="Center"
                 Foreground="__TEXT_DIM__"/>
      <Button x:Name="BtnDetails" Grid.Column="1" Style="{StaticResource LinkButton}"
              Margin="16,0,0,0" Visibility="Collapsed">
        <TextBlock Text="View details &#8594;" Foreground="__ACCENT_DK__"
                   FontWeight="SemiBold"/>
      </Button>
    </Grid>
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
_ACTION_FOOTER = _tok(_ACTION_FOOTER)

class ActionPanel(object):
    """Step 3, the dashboard. All its actions respect the step 2 scope.
    Change revision lives in step 1 (it does not depend on the scope) and Find
    cloud became the "Where is it?" of Step 4."""

    def __init__(self, clouds, views):
        # Deduplicate clouds by ElementId (Revit collectors can return duplicates)
        seen = {}
        for c in clouds:
            iid = _id_val(c.Id)
            if iid not in seen:
                seen[iid] = c
        self._clouds = list(seen.values())
        # The ids are the truth: an element deleted from outside cannot even be
        # touched, but its id can be looked up safely.
        self._ids = list(seen.keys())
        self._views  = views
        self._result_code = ACTION_EXIT

        # --- Performance cache (built once, reused by every action button) ---
        self._vp_map = _viewport_sheet_map()            # view_int -> [ViewSheet]
        self._sheet_views = {}                           # sheet_int -> [view_int]
        for _vint, _sheets in self._vp_map.items():
            for _sh in _sheets:
                self._sheet_views.setdefault(_id_val(_sh.Id), []).append(_vint)
        self._sheet_lbls = None                          # lazy, see _lbls()
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

        # --- modeless ---
        # The handler is set by _open_dashboard. This panel's data belongs to the
        # document where the clouds were chosen: if the user moves to another
        # model, acting would mean writing to the wrong one.
        self._handler  = None
        self._busy     = False
        try:
            self.doc_path = doc.PathName
        except Exception:
            self.doc_path = None

        self.win = ui.parse(
            u'Cloud Manager', u'Step 3 of 4 \u00b7 Choose an action',
            _ACTION_BODY, _ACTION_FOOTER, width=980, height=720,
            context=doc.Title)
        self.win.MinWidth  = 880
        self.win.MinHeight = 680

        f = self.win.FindName
        self.lbl_clouds = f('LblClouds'); self.lbl_views = f('LblViews'); self.lbl_fb = f('LblFb')
        self.LblScopeNote = f('LblScopeNote')
        self.BtnDetails = f('BtnDetails')
        self.BtnStepStatus = f('BtnStepStatus'); self.StepStatusPanel = f('StepStatusPanel')

        _own_to_revit(self.win)

        self.win.PreviewKeyDown += self._on_key

        f('BtnStyle').Click    += self._do_style
        f('BtnReset').Click    += self._do_reset
        f('BtnShow').Click     += self._do_show
        f('BtnHide').Click     += self._do_hide
        f('BtnTagOnly').Click  += self._do_tag_only
        f('BtnCloudTag').Click += self._do_cloud_and_tag
        f('BtnDelete').Click   += self._do_delete
        self.BtnDetails.Click    += self._do_status
        self.BtnStepStatus.Click += self._do_status

        f('BtnBack').Click    += self._go_back
        f('BtnRestart').Click += self._go_restart
        f('BtnExit').Click    += self._go_exit

        self._refresh_summary()
        self._refresh_status_link()
        self._feedback(u"Ready.", C_MED)

    # ---- modeless: every action crosses the ExternalEvent ----
    def _request(self, action):
        """Queue the action and wake Revit up. Returning right away is what
        keeps the window alive; the work happens in Execute, already in context."""
        if self._busy:
            return
        h = self._handler
        if h is None or h.event is None:
            # Without an ExternalEvent (should not happen) it runs directly: at
            # worst Revit complains about the transaction, and a visible error is
            # better than a button that does nothing.
            self._dispatch(action)
            return
        self._busy = True
        h.panel  = self
        h.action = action
        self._feedback(u"Working...", C_MED)
        h.event.Raise()

    def _dispatch(self, action):
        try:
            fn = getattr(self, '_run_' + action, None)
            if fn is not None:
                fn()
        finally:
            self._busy = False

    # ---- window plumbing ----
    def ShowDialog(self):
        return self.win.ShowDialog()

    def _on_key(self, sender, e):
        if e.Key == Key.Escape:
            self._go_exit(sender, e)

    def _refresh_summary(self):
        nc = len(self._ids); nv = len(self._views)
        ids = u", ".join(str(i) for i in list(self._ids)[:30])
        if nc > 30:
            ids += u" ... and {:,} more".format(nc - 30)
        out = getattr(STATE, 'left_out', 0) or 0
        tot = getattr(STATE, 'step1_total', 0) or 0
        note = (u"  ({0:,} of the {1:,} from step 1 are outside this scope)".format(out, tot)
                if out and tot else u"")
        self.lbl_clouds.Text = u"Clouds:  {:,} selected{}".format(nc, note)
        # Raw ElementIds mean nothing to the user; they stay on hover.
        self.lbl_clouds.ToolTip = u"IDs: {}".format(ids) if ids else None
        # Count the views that hold these clouds, not every view in scope:
        # "whole model" targets 100+ views for a couple of clouds (QA 10-06).
        owners = set()
        for i in self._ids:
            try:
                el = doc.GetElement(_make_eid(i))
                if el is not None and el.OwnerViewId is not None:
                    owners.add(_id_val(el.OwnerViewId))
            except Exception:
                pass
        nw = len(owners)
        self.lbl_views.Text  = u"Scope:   {:,} view{} with these clouds".format(
            nw, "s" if nw != 1 else "")
        self.lbl_views.ToolTip = u"{:,} view{} targeted in Step 2".format(
            nv, "s" if nv != 1 else "")
        if self.LblScopeNote is not None:
            self.LblScopeNote.Text = u"Uses the scope from step 2 ({0:,} view{1}).".format(
                nv, u"s" if nv != 1 else u"")

    def _refresh_status_link(self):
        """Step 4 of the stepper lights up as soon as there is something to show."""
        has = bool(_session_log())
        self.BtnStepStatus.IsEnabled = has
        self.StepStatusPanel.Opacity = 1.0 if has else 0.5
        self.BtnStepStatus.ToolTip = (u"See what each action did" if has
                                      else u"Run an action to see its status")

    def _feedback(self, msg, color=None):
        self.lbl_fb.Text = msg
        self.lbl_fb.Foreground = _brush(color or C_MED)

    def _finish(self, res):
        """Every action ends here: to the history, a line in the feedback and
        the link to the detail."""
        _log_result(res)
        self._feedback(res.line(), _result_color(res))
        self.BtnDetails.Visibility = WVisibility.Visible
        self._refresh_status_link()

    def _lbls(self):
        if self._sheet_lbls is None:
            self._sheet_lbls = _sheet_label_map()
        return self._sheet_lbls

    def _where(self, c):
        try:
            return _cloud_sheet_text(c, self._lbls())
        except Exception:
            return u"--"

    def _live_clouds(self, res):
        """The clouds that still exist, re-read from the model. One deleted
        from outside (native Delete, Ctrl+Z, another user's sync) while the
        dashboard is open is noted as Skipped and drops out of the list, instead
        of blowing up the action when touched."""
        self._live_views(res)
        alive = []
        gone = []
        for iid in list(self._ids):
            el = None
            try:
                el = doc.GetElement(_make_eid(iid))
            except Exception:
                el = None
            if el is None:
                gone.append(iid)
            else:
                alive.append(el)
        for iid in gone:
            self._ids.remove(iid)
            if res is not None:
                res.add(R_SKIPPED, iid, u"--", u"--",
                        u"Cloud no longer exists in the model", diagnose=False)
        self._clouds = alive
        if gone:
            STATE.selected_clouds = list(alive)
            STATE.scoped_clouds = list(alive)
            self._refresh_summary()
        return alive

    def _live_views(self, res):
        """Drop from the scope the views deleted from outside since step 2.
        Touching the .Id or the CropBox of a deleted view raises outside any
        try and used to bring down the whole action; this way it is noted once
        and the action goes on."""
        gone = []
        for vid, v in list(self._views.items()):
            ok = False
            try:
                ok = bool(v.IsValidObject) and doc.GetElement(_make_eid(vid)) is not None
            except Exception:
                ok = False
            if not ok:
                gone.append(vid)
                del self._views[vid]
        if gone:
            if res is not None:
                for vid in gone:
                    res.add(R_SKIPPED, None, u"--", u"View {0}".format(vid),
                            u"View no longer exists in the model",
                            cloud_txt=u"--", diagnose=False)
            STATE.selected_views = self._views
            self._refresh_summary()

    def _views_for_visibility(self):
        """Return the union of user-selected scope views and the primary (parent)
        views of any dependent/segment views in scope.

        Hide/Unhide only takes visual effect in the view where the cloud was
        placed (OwnerViewId). If the scope view is a dependent or segment view,
        clouds may be owned by the parent view, so we add the parent. We do NOT
        add owner views from unrelated parts of the model."""
        views = dict(self._views)
        primary_ids = set()
        for v in self._views.values():
            try:
                pid = v.GetPrimaryViewId()
                if pid is not None and pid != DB.ElementId.InvalidElementId:
                    primary_ids.add(_id_val(pid))
            except Exception:
                pass
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

    def _cloud_views(self, c, views):
        """The views of `views` where the cloud can appear: its owner and the
        owner's dependents whose crop touches it. The cloud's bbox is requested
        ONCE per cloud and only if needed."""
        out = []
        try:
            ov_id = c.OwnerViewId
            ov_int = _id_val(ov_id)
        except Exception:
            return out
        if ov_int in views:
            out.append(views[ov_int])
        deps = self._dep_cache.get(ov_int) or {}
        bb = None
        bb_done = False
        for dint in deps:
            if dint == ov_int or dint not in views:
                continue
            v = views[dint]
            try:
                cb = v.CropBox
            except Exception:
                cb = None
            if not cb:
                out.append(v)
                continue
            if not bb_done:
                bb_done = True
                try:
                    bb = c.get_BoundingBox(doc.GetElement(ov_id))
                except Exception:
                    bb = None
            if bb is not None and _bb_intersects_xy(bb, cb):
                out.append(v)
        return out

    # ================================================================
    #  Show / Hide  (per cloud and per view)
    # ================================================================
    def _do_show(self, s, e):
        self._request('show')

    def _do_hide(self, s, e):
        self._request('hide')

    def _run_show(self, s=None, e=None):
        self._run_visibility(False)

    def _run_hide(self, s=None, e=None):
        self._run_visibility(True)

    def _run_visibility(self, hide):
        """Show = Unhide in View > Elements; Hide = Hide in View > Elements.
        One row per cloud and view. View checked out by another user: that view
        is skipped. If the batch fails in a view, it is retried one cloud at a
        time to find out which one does not allow it, and the rest go on."""
        title = u"Hide" if hide else u"Show"
        res = ActionResult(title)
        clouds = self._live_clouds(res)
        # Sheets stay in: a cloud drawn straight on a sheet (not through a
        # viewport) is owned by the sheet, and _cloud_views only hands a sheet
        # the clouds it owns -- HideElements rejects ids from other views (QA 09-25).
        views = dict((k, v) for k, v in self._views_for_visibility().items()
                     if _view_supports_rev_vis(v))
        res.note = u"{0:,} clouds, {1:,} views. Read back from the model after the change.".format(
            len(clouds), len(views))

        view_lock = {}
        todo = {}            # vid -> (view, [cloud])
        for c in clouds:
            cid = _id_val(c.Id)
            rv = _cloud_rev_txt(c)
            cv = self._cloud_views(c, views)
            if not cv:
                res.add(R_SKIPPED, cid, rv, self._where(c),
                        u"Not placed in any targeted view")
                continue
            for v in cv:
                vid = _id_val(v.Id)
                vname = _safe_name(v)
                if vid not in view_lock:
                    view_lock[vid] = _locked_by(v.Id)
                if view_lock[vid]:
                    res.add(R_SKIPPED, cid, rv, vname,
                            u"View locked by {0}".format(view_lock[vid]), vid)
                    continue
                try:
                    was = bool(c.IsHidden(v))
                except Exception as ex:
                    res.add(R_FAILED, cid, rv, vname,
                            u"Could not read its state: " + _err(ex), vid)
                    continue
                if was == hide:
                    res.add(R_UNCHANGED, cid, rv, vname,
                            u"Already hidden" if hide else u"Already visible", vid)
                    continue
                if hide:
                    try:
                        can = bool(c.CanBeHidden(v))
                    except Exception:
                        can = True
                    if not can:
                        res.add(R_SKIPPED, cid, rv, vname,
                                u"Revit does not allow hiding it in this view", vid)
                        continue
                todo.setdefault(vid, (v, []))[1].append(c)

        failed = {}          # (cid, vid) -> reason
        note = u""
        if todo:
            List = System.Collections.Generic.List[DB.ElementId]
            try:
                with _ws_transaction(title + u" Revision Clouds") as sil:
                    for vid, (v, cs) in todo.items():
                        try:
                            if hide:
                                v.HideElements(List([c.Id for c in cs]))
                            else:
                                v.UnhideElements(List([c.Id for c in cs]))
                        except Exception:
                            # The whole batch fails if ONE cloud does not allow it:
                            # one at a time, to know which and go on with the rest.
                            for c in cs:
                                try:
                                    if hide:
                                        v.HideElements(List([c.Id]))
                                    else:
                                        v.UnhideElements(List([c.Id]))
                                except Exception as ex1:
                                    failed[(_id_val(c.Id), vid)] = _err(ex1)
                note = _silencer_note(sil)
            except Exception as ex:
                whole = u"Transaction rolled back: " + _err(ex)
                for vid, (v, cs) in todo.items():
                    for c in cs:
                        failed.setdefault((_id_val(c.Id), vid), whole)

            # --- the truth is read from the model ---
            for vid, (v, cs) in todo.items():
                vname = _safe_name(v)
                for c in cs:
                    cid = _id_val(c.Id)
                    rv = _cloud_rev_txt(c)
                    if (cid, vid) in failed:
                        res.add(R_FAILED, cid, rv, vname, failed[(cid, vid)], vid)
                        continue
                    try:
                        now = bool(c.IsHidden(v))
                    except Exception:
                        now = None
                    if now == hide:
                        res.add(R_CHANGED, cid, rv, vname,
                                u"Hidden in view" if hide else u"Unhidden in view", vid)
                    else:
                        res.add(R_FAILED, cid, rv, vname,
                                (u"Revit kept it visible" if hide else u"Revit kept it hidden") + note,
                                vid)
        try:
            uidoc.RefreshActiveView()
        except Exception:
            pass
        self._finish(res)

    # ================================================================
    #  Revision display: Tag Only / Cloud + Tag  (per revision, project)
    # ================================================================
    def _do_tag_only(self, s, e):
        self._request('tag_only')

    def _do_cloud_and_tag(self, s, e):
        self._request('cloud_and_tag')

    def _run_tag_only(self, s=None, e=None):
        self._run_revision_display(DB.RevisionVisibility.TagVisible, u"Tag Only")

    def _run_cloud_and_tag(self, s=None, e=None):
        self._run_revision_display(DB.RevisionVisibility.CloudAndTagVisible, u"Cloud + Tag")

    def _run_revision_display(self, target, title):
        """Revision.Visibility belongs to the REVISION and applies to the whole
        project: same as in Revit's Sheet Issues/Revisions. The scope only
        decides which revisions get touched. One row per revision."""
        res = ActionResult(title, u"Applies to the whole revision in the project, "
                                  u"not only to the targeted views.")
        clouds = self._live_clouds(res)
        target_s = str(target)
        by_rev = {}
        for c in clouds:
            cid = _id_val(c.Id)
            if not _cloud_in_scope(c, self._views, self._dep_cache):
                res.add(R_SKIPPED, cid, _cloud_rev_txt(c), self._where(c),
                        u"Not in the targeted views")
                continue
            r = _rev_of_cloud(c)
            if r is None:
                res.add(R_SKIPPED, cid, u"--", self._where(c), u"Cloud has no revision")
                continue
            by_rev.setdefault(_id_val(r.Id), []).append(c)

        todo = []            # (rev, n_clouds, before)
        for rint, cs in by_rev.items():
            r = doc.GetElement(_make_eid(rint))
            seq = rev_num(r) or u"--"
            txt = u"{0:,} cloud{1}".format(len(cs), u"s" if len(cs) != 1 else u"")
            try:
                before = str(r.Visibility)
            except Exception as ex:
                res.add(R_FAILED, None, seq, u"Whole project",
                        u"Could not read its visibility: " + _err(ex), cloud_txt=txt)
                continue
            if before == target_s:
                res.add(R_UNCHANGED, None, seq, u"Whole project",
                        u"Already " + _vis_label(target_s), cloud_txt=txt)
                continue
            # An issued revision is greyed out in Sheet Issues/Revisions: the
            # Visibility setter raises no exception and does nothing. Without
            # this check it ended in a generic Failed ("Revit kept it on ...")
            # that did not say what to do.
            if _rev_is_issued(r):
                res.add(R_SKIPPED, None, seq, u"Whole project",
                        u"Revision is issued: Revit locks its visibility. "
                        u"Un-issue it in Sheet Issues/Revisions to change this.",
                        cloud_txt=txt)
                continue
            lock = _locked_by(r.Id)
            if lock:
                res.add(R_SKIPPED, None, seq, u"Whole project",
                        u"Revision locked by {0}".format(lock), cloud_txt=txt)
                continue
            todo.append((r, txt, before))

        # Revision.Visibility is a project-level property of the Revision: it
        # changes every view of the project, whatever the scope of step 2. The
        # user has to know that before, not read it in the result (QA 09-25).
        if todo and not ui.confirm(
                u"This changes {0} in every view of the project, not only the "
                u"view(s) you chose in Step 2.\n\nContinue?".format(
                    u", ".join(_rev_title(r) for r, _t, _b in todo)),
                title=u"Confirm " + title, yes_text=u"Apply"):
            self._feedback(title + u" cancelled.", C_MED)
            return

        errs = {}
        note = u""
        if todo:
            try:
                with _ws_transaction(title + u" - Revision Clouds") as sil:
                    for r, _t, _b in todo:
                        try:
                            r.Visibility = target
                        except Exception as ex:
                            errs[_id_val(r.Id)] = _err(ex)
                note = _silencer_note(sil)
            except Exception as ex:
                whole = u"Transaction rolled back: " + _err(ex)
                for r, _t, _b in todo:
                    errs.setdefault(_id_val(r.Id), whole)
            for r, txt, before in todo:
                rint = _id_val(r.Id)
                seq = rev_num(r) or u"--"
                if rint in errs:
                    res.add(R_FAILED, None, seq, u"Whole project", errs[rint], cloud_txt=txt)
                    continue
                try:
                    now = str(r.Visibility)
                except Exception:
                    now = None
                if now == target_s:
                    res.add(R_CHANGED, None, seq, u"Whole project",
                            u"{0} -> {1}".format(_vis_label(before), _vis_label(target_s)),
                            cloud_txt=txt)
                else:
                    res.add(R_FAILED, None, seq, u"Whole project",
                            u"Revit kept it on {0} (the revision is not issued "
                            u"and not locked){1}".format(_vis_label(now), note),
                            cloud_txt=txt)
        try:
            uidoc.RefreshActiveView()
        except Exception:
            pass
        self._finish(res)

    # ================================================================
    #  Graphics: Apply Styles / Reset Overrides  (per cloud and per view)
    # ================================================================
    def _do_style(self, s, e):
        self._request('style')

    def _do_reset(self, s, e):
        self._request('reset')

    def _override_targets(self, c):
        """Where the overrides of a cloud go: its owner view and the
        dependents of that view (the viewports inherit from there). If the cloud
        is placed directly on a sheet, the views of its viewports."""
        owner_v = None
        try:
            owner_v = doc.GetElement(c.OwnerViewId)
        except Exception:
            owner_v = None
        if owner_v is None:
            return []
        seen = set([_id_val(owner_v.Id)])
        targets = [owner_v]
        for dint, did in (self._dep_cache.get(_id_val(owner_v.Id)) or {}).items():
            if dint not in seen:
                dv = doc.GetElement(did)
                if dv is not None:
                    seen.add(dint)
                    targets.append(dv)
        if isinstance(owner_v, DB.ViewSheet):
            for vint in self._sheet_views.get(_id_val(owner_v.Id), []):
                if vint not in seen:
                    vw = doc.GetElement(_make_eid(vint))
                    if vw is not None:
                        seen.add(vint)
                        targets.append(vw)
        return targets

    def _run_style(self, s=None, e=None):
        res = ActionResult(u"Apply Styles")
        clouds = self._live_clouds(res)
        scoped = []
        for c in clouds:
            if _cloud_in_scope(c, self._views, self._dep_cache):
                scoped.append(c)
            else:
                res.add(R_SKIPPED, _id_val(c.Id), _cloud_rev_txt(c), self._where(c),
                        u"Not in the targeted views")
        if not scoped:
            self._finish(res)
            return
        pats = get_line_patterns()
        if not pats:
            self._feedback(u"No line patterns found in the model.", C_ORANGE)
            return
        dlg = StyleSubDialog(scoped, sorted(pats.keys()))
        if dlg.ShowDialog(_hwnd) != DialogResult.OK:
            self._feedback(u"Apply Styles cancelled.", C_MED)
            return
        styles = dlg.get_styles()   # {cloud_id_int: style_dict}
        if not styles:
            self._feedback(u"No clouds checked: nothing applied.", C_ORANGE)
            return

        def _build(style):
            """A function of the overrides the view already has: only the
            fields the user picked change, the ones left on "no change" keep
            their current value (the rows start empty since the QA of 09-25)."""
            pat_id = pats.get(style["pattern_name"]) if style["pattern_name"] else None

            def merge(current):
                try:
                    ogs = DB.OverrideGraphicSettings(current)
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
                return ogs
            return merge

        # Clouds that were not ticked in the dialog are not part of the action.
        # A ticked one with nothing chosen is not either: it stays Unchanged.
        plan = []
        for c in scoped:
            st = styles.get(_id_val(c.Id))
            if st is None:
                continue
            if st["color"] is None and not st["pattern_name"] and not st["weight"]:
                res.add(R_UNCHANGED, _id_val(c.Id), _cloud_rev_txt(c), self._where(c),
                        u"No style picked for this cloud")
                continue
            plan.append((c, _build(st)))
        self._apply_overrides(res, plan, u"Style Revision Clouds",
                              u"Style applied", u"Already has this style")

    def _run_reset(self, s=None, e=None):
        res = ActionResult(u"Reset Overrides")
        clouds = self._live_clouds(res)
        plan = []
        for c in clouds:
            if _cloud_in_scope(c, self._views, self._dep_cache):
                plan.append((c, DB.OverrideGraphicSettings()))
            else:
                res.add(R_SKIPPED, _id_val(c.Id), _cloud_rev_txt(c), self._where(c),
                        u"Not in the targeted views")
        self._apply_overrides(res, plan, u"Reset Revision Cloud Overrides",
                              u"Overrides cleared", u"Had no overrides")

    def _apply_overrides(self, res, plan, tx_name, done_txt, same_txt):
        """plan: [(cloud, OverrideGraphicSettings or f(current) -> OGS)].
        Before/after by signature. With a function, the override is built per
        view on top of what that view already has (Apply Styles with untouched
        fields)."""
        view_lock = {}
        todo = []            # (cloud, view, ogs, want)
        for c, plan_ogs in plan:
            cid = _id_val(c.Id)
            rv = _cloud_rev_txt(c)
            ogs = plan_ogs
            want = None if callable(plan_ogs) else _ogs_sig(plan_ogs)
            targets = self._override_targets(c)
            if not targets:
                res.add(R_SKIPPED, cid, rv, self._where(c), u"Owner view not found")
                continue
            for v in targets:
                vid = _id_val(v.Id)
                vname = _safe_name(v)
                if vid not in view_lock:
                    view_lock[vid] = _locked_by(v.Id)
                if view_lock[vid]:
                    res.add(R_SKIPPED, cid, rv, vname,
                            u"View locked by {0}".format(view_lock[vid]), vid)
                    continue
                try:
                    cur = v.GetElementOverrides(c.Id)
                    before = _ogs_sig(cur)
                except Exception as ex:
                    res.add(R_FAILED, cid, rv, vname,
                            u"Could not read its overrides: " + _err(ex), vid)
                    continue
                if callable(plan_ogs):
                    try:
                        ogs = plan_ogs(cur)
                        want = _ogs_sig(ogs)
                    except Exception as ex:
                        res.add(R_FAILED, cid, rv, vname,
                                u"Could not build its overrides: " + _err(ex), vid)
                        continue
                if before is not None and before == want:
                    res.add(R_UNCHANGED, cid, rv, vname, same_txt, vid)
                    continue
                todo.append((c, v, ogs, want))

        errs = {}
        note = u""
        if todo:
            try:
                with _ws_transaction(tx_name) as sil:
                    for c, v, ogs, _w in todo:
                        try:
                            v.SetElementOverrides(c.Id, ogs)
                        except Exception as ex:
                            errs[(_id_val(c.Id), _id_val(v.Id))] = _err(ex)
                note = _silencer_note(sil)
            except Exception as ex:
                whole = u"Transaction rolled back: " + _err(ex)
                for c, v, _o, _w in todo:
                    errs.setdefault((_id_val(c.Id), _id_val(v.Id)), whole)
            for c, v, _o, want in todo:
                cid = _id_val(c.Id); vid = _id_val(v.Id)
                rv = _cloud_rev_txt(c); vname = _safe_name(v)
                if (cid, vid) in errs:
                    res.add(R_FAILED, cid, rv, vname, errs[(cid, vid)], vid)
                    continue
                try:
                    now = _ogs_sig(v.GetElementOverrides(c.Id))
                except Exception:
                    now = None
                if now is not None and now == want:
                    res.add(R_CHANGED, cid, rv, vname, done_txt, vid)
                else:
                    res.add(R_FAILED, cid, rv, vname,
                            u"Revit did not keep the overrides" + note, vid)
        try:
            uidoc.RefreshActiveView()
        except Exception:
            pass
        self._finish(res)

    # ================================================================
    #  Delete
    # ================================================================
    def _do_delete(self, s, e):
        self._request('delete')

    def _run_delete(self, s=None, e=None):
        """Delete the clouds that appear in the views of the scope (also
        when the scope is a segment: it used to filter by exact OwnerViewId and
        with a dependent it found nothing)."""
        res = ActionResult(u"Delete")
        clouds = self._live_clouds(res)
        cands = []
        issued = []          # names of the issued revisions met, for the alert
        for c in clouds:
            cid = _id_val(c.Id)
            rv = _cloud_rev_txt(c)
            where = self._where(c)
            if not _cloud_in_scope(c, self._views, self._dep_cache):
                res.add(R_SKIPPED, cid, rv, where, u"Not in the targeted views")
                continue
            rev_c = _rev_of_cloud(c)
            if _rev_is_issued(rev_c):
                name = _rev_title(rev_c)
                if name not in issued:
                    issued.append(name)
                res.add(R_SKIPPED, cid, rv, where,
                        u"{0} is issued: Revit does not delete its clouds".format(name))
                continue
            lock = _locked_by(c.Id)
            if lock:
                res.add(R_SKIPPED, cid, rv, where, u"Locked by {0}".format(lock))
                continue
            cands.append((c, cid, rv, where))

        if not cands:
            self._finish(res)
            self._delete_alert(res, issued)
            return
        skipped = len(res.rows)
        skip_note = (u"\n\n{0:,} other cloud(s) will be skipped (see Status).".format(skipped)
                     if skipped else u"")
        if not ui.confirm(
                u"Permanently delete {0:,} cloud(s) from the targeted view(s)?{1}\n\n"
                u"Ctrl+Z in Revit undoes it.".format(len(cands), skip_note),
                title=u"Confirm Delete", yes_text=u"Delete"):
            self._feedback(u"Delete cancelled.", C_MED)
            return

        errs = {}
        List = System.Collections.Generic.List[DB.ElementId]
        try:
            with _ws_transaction(u"Delete Revision Clouds"):
                try:
                    doc.Delete(List([c.Id for c, _i, _r, _w in cands]))
                except Exception:
                    # One that does not allow it brings down the batch: one at a time, and go on.
                    for c, cid, _r, _w in cands:
                        try:
                            if _alive(cid):
                                doc.Delete(c.Id)
                        except Exception as ex1:
                            errs[cid] = _err(ex1)
        except Exception as ex:
            whole = u"Transaction rolled back: " + _err(ex)
            for _c, cid, _r, _w in cands:
                errs.setdefault(cid, whole)

        for _c, cid, rv, where in cands:
            if not _alive(cid):
                res.add(R_CHANGED, cid, rv, where, u"Deleted", diagnose=False)
            else:
                res.add(R_FAILED, cid, rv, where, errs.get(cid) or u"Revit kept it")
        self._live_clouds(None)      # remove the deleted ones from the panel's list
        self._finish(res)
        self._delete_alert(res, issued)

    def _delete_alert(self, res, issued):
        """A Delete that did not delete everything is easy to miss in the
        status line (QA 10-06): say it in an alert, naming the issued
        revision(s), and point to the step 4 detail."""
        # Count only the clouds that were part of the order: rows with no cloud
        # (a view that no longer exists) or outside the scope are not "not deleted".
        rows = [r for r in res.rows
                if r.cloud_int and r.Reason != u"Not in the targeted views"]
        done = sum(1 for r in rows if r.Result == R_CHANGED)
        total = len(rows)
        if not total or done == total:
            return
        msg = u"{0:,} of {1:,} cloud{2} deleted.".format(
            done, total, u"s" if total != 1 else u"")
        if issued:
            msg += u"\n\n{0} {1} issued: Revit does not delete {2} clouds.".format(
                u", ".join(issued), u"is" if len(issued) == 1 else u"are",
                u"its" if len(issued) == 1 else u"their")
        msg += u"\n\nView details shows each cloud and why."
        ui.alert(msg, title=u"Delete")

    # ================================================================
    #  Step 4
    # ================================================================
    def _do_status(self, s, e):
        self._request('status')

    def _run_status(self, s=None, e=None):
        dlg = StatusDialog(_session_log(), self._views, self._vp_map, self._lbls(),
                           allow_zoom=True)
        dlg.ShowDialog()
        if dlg.zoom_cloud:
            ok, msg = _zoom_to_cloud(dlg.zoom_cloud, self._dep_cache, dlg.zoom_view)
            self._feedback(msg, C_GREEN if ok else C_ORANGE)
            return
        # _request left "Working...": restore the last result line.
        log = _session_log()
        if log:
            self._feedback(log[0].line(), _result_color(log[0]))
        else:
            self._feedback(u"Ready.", C_MED)

    # ================================================================
    #  Navigation
    # ================================================================
    # Going back reopens steps 1 and 2, which read the model: it has to
    # go through the ExternalEvent like any other action. Exiting, instead,
    # just closes a window and needs no context.
    def _go_back(self, s, e):
        self._request('back')

    def _go_restart(self, s, e):
        self._request('restart')

    def _go_exit(self, s, e):
        self._result_code = ACTION_EXIT
        self.win.Close()

    def _run_back(self, s=None, e=None):
        self._result_code = ACTION_BACK
        self.win.Close()
        run_wizard(2)

    def _run_restart(self, s=None, e=None):
        self._result_code = ACTION_RESTART
        self.win.Close()
        run_wizard(1)

    @property
    def result_code(self):
        return self._result_code


# ============================================================
#  Modeless plumbing
# ============================================================
# Keys where the window and its ExternalEvent are parked so they survive
# the end of the script run (see main()).
_ENVVAR_WIN   = 'cloudmanager_window'
_ENVVAR_KEEP  = 'cloudmanager_event'
_ENVVAR_BUILD = 'cloudmanager_build'


def _build_stamp():
    """Stamp of the running script: date and size of script.py.

    With a persistent engine a parked window survives the reload, and with it
    the code it was built with. Without this stamp, after a deploy the button
    returned the old window, and Back / Start Over ran the old wizard: the
    fix "did not work" even though it was installed."""
    try:
        p = os.path.join(_HERE, 'script.py')
        return u"{0}:{1}".format(os.path.getmtime(p), os.path.getsize(p))
    except Exception:
        return None


class _CMHandler(IExternalEventHandler):
    """Run a dashboard action inside a valid API context.

    Everything that reads or writes the model enters here: applying an action,
    and also going back to steps 1 and 2, which re-read the model to rebuild
    their lists.

    This does NOT make the action run in the background: the Revit API is
    single-threaded, so Revit is busy while it is applied. What is gained is
    all the time between one action and the next."""

    def __init__(self):
        self.panel  = None
        self.action = None
        self.event  = None      # assigned right after ExternalEvent.Create

    def Execute(self, uiapp):
        panel, action = self.panel, self.action
        self.action = None
        if panel is None or not action:
            return
        try:
            cur = None
            try:
                if uiapp.ActiveUIDocument is not None:
                    cur = uiapp.ActiveUIDocument.Document
            except Exception:
                cur = None
            if (panel.doc_path and cur is not None
                    and cur.PathName != panel.doc_path):
                # The clouds belong to another document: acting would be writing
                # to the wrong model.
                panel._busy = False
                panel._feedback(
                    u"These clouds belong to another model. Switch back to it "
                    u"to use this window.", C_ORANGE)
                return
            panel._dispatch(action)
        except Exception as ex:
            try:
                panel._busy = False
                panel._feedback(u"Action failed: {0}".format(ex), C_RED)
            except Exception:
                pass

    def GetName(self):
        return 'CloudManager'


def _open_dashboard(clouds, views):
    """Build step 3, park it and show it WITHOUT blocking Revit."""
    prev = script.get_envvar(_ENVVAR_WIN)
    if prev is not None:
        try:
            prev.Close()
        except Exception:
            pass
        script.set_envvar(_ENVVAR_WIN, None)

    # The handler is also reused between runs: only if it is from the same build.
    # One from an earlier build would run its old Execute with the new panel.
    stamp = _build_stamp()
    keep = script.get_envvar(_ENVVAR_KEEP)
    handler = keep[0] if keep else None
    if handler is not None and stamp and script.get_envvar(_ENVVAR_BUILD) != stamp:
        handler = None
    if handler is None:
        handler = _CMHandler()
        handler.event = ExternalEvent.Create(handler)
        script.set_envvar(_ENVVAR_KEEP, (handler, handler.event))
    script.set_envvar(_ENVVAR_BUILD, stamp)

    panel = ActionPanel(clouds, views)
    panel._handler = handler
    handler.panel = panel

    win = panel.win

    def _on_closed(sender, e):
        if script.get_envvar(_ENVVAR_WIN) is win:
            script.set_envvar(_ENVVAR_WIN, None)

    win.Closed += _on_closed
    _own_to_revit(win)          # otherwise the window goes behind Revit
    script.set_envvar(_ENVVAR_WIN, win)
    win.Show()
    return win


def run_wizard(start_step=1):
    """Steps 1 and 2 are modal (they pick, do not touch the model, are short),
    step 3 is modeless. It is called on start and from Back / Start Over, which
    run inside the ExternalEvent, that is, already in a valid context."""
    step = start_step
    while True:
        if step == 1:
            # WPF window. Next is disabled while nothing is checked, so the
            # legacy "nothing selected" MessageBox is no longer reachable.
            dlg = CloudPanel(doc, uidoc)
            dlg.ShowDialog()
            if dlg.result_code != ACTION_NEXT:
                return
            STATE.selected_clouds = dlg.selected_clouds
            step = 2

        if step == 2:
            dlg2 = ScopePanel(STATE.selected_clouds)
            dlg2.ShowDialog()
            if dlg2.result_code == ACTION_EXIT:
                return
            if dlg2.result_code == ACTION_BACK:
                step = 1
                continue
            views = dlg2.selected_views
            if not views:
                ui.alert(u"Pick at least one view before continuing.",
                         title=u"No views selected")
                continue
            STATE.selected_views = views
            # Only the clouds that fall in the chosen scope go to the dashboard.
            # Until 2026-10-01 ALL of step 1's went in: a view with 17 was
            # chosen and the dashboard still said 27. The fear behind the old
            # comment was a per-view collector, which skips hidden clouds (just
            # the ones you want to show). _cloud_in_scope is not that: it looks
            # at the owner view and its dependents, so a hidden cloud still gets
            # in. And it is the same filter the actions already apply: it does
            # not change what is touched, only makes the number tell the truth.
            inside = []
            for c in STATE.selected_clouds:
                try:
                    if _cloud_in_scope(c, views):
                        inside.append(c)
                except Exception:
                    inside.append(c)     # when in doubt, inside: the action decides
            if not inside:
                ui.alert(
                    u"None of the {0:,} clouds picked in step 1 appear in the chosen "
                    u"views.\n\nPick other views, or go back and pick other "
                    u"clouds.".format(len(STATE.selected_clouds)),
                    title=u"Cloud Manager")
                continue
            STATE.scoped_clouds = inside
            STATE.step1_total = len(STATE.selected_clouds)
            STATE.left_out = STATE.step1_total - len(inside)
            step = 3

        if step == 3:
            _open_dashboard(STATE.scoped_clouds, STATE.selected_views)
            return


def _nudge_onscreen(win):
    """Bring the window to the center if it ended up off screen.

    It really happens: it was opened on a second monitor and then that was
    disconnected, or it kept old coordinates. The window exists and reports
    itself visible, but cannot be seen. If it is not repositioned, `main()`
    reuses it, returns, and the button looks dead."""
    try:
        vl = SystemParameters.VirtualScreenLeft
        vt = SystemParameters.VirtualScreenTop
        vw = SystemParameters.VirtualScreenWidth
        vh = SystemParameters.VirtualScreenHeight
        w = win.Width if win.Width > 0 else 900
        h = win.Height if win.Height > 0 else 600
        off = (win.Left + w < vl + 60 or win.Left > vl + vw - 60 or
               win.Top + h < vt + 60 or win.Top > vt + vh - 60)
        if off:
            win.Left = vl + (vw - w) / 2
            win.Top = vt + (vh - h) / 2
    except Exception:
        pass


def _drop_parked(win):
    """Discard the parked window. Close it, do not just forget it: an orphan
    stays alive and gets in the way again on the next click."""
    try:
        if win is not None:
            win.Close()
    except Exception:
        pass
    script.set_envvar(_ENVVAR_WIN, None)


def main():
    # A modeless window survives the script run, so it has to be reused
    # instead of opening a second one. The check is paranoid on purpose: with a
    # persistent engine the envvar survives EVEN a pyRevit reload, so a zombie
    # reference (window closed behind the scenes, off screen, or created by a
    # run whose engine died) would make the button bring "nothing" to the
    # front and return: dead without a single visible error, and
    # reloading would not fix it. Only a genuinely VISIBLE, on-screen window is
    # reused; anything else is closed and rebuilt.
    prev = script.get_envvar(_ENVVAR_WIN)

    # Escape hatch: Shift + click discards what is parked and starts from
    # zero. It is the way out when the window got into an odd state, without
    # closing Revit.
    try:
        force_new = (Keyboard.Modifiers & ModifierKeys.Shift) == ModifierKeys.Shift
    except Exception:
        force_new = False
    if force_new and prev is not None:
        _drop_parked(prev)
        prev = None

    # If the script changed (a deploy) the parked window is from another build:
    # reusing it means going on running the old code. It is discarded along with
    # its handler and rebuilt from zero.
    stamp = _build_stamp()
    if prev is not None and stamp and script.get_envvar(_ENVVAR_BUILD) != stamp:
        _drop_parked(prev)
        script.set_envvar(_ENVVAR_KEEP, None)
        prev = None

    if prev is not None:
        reused = False
        try:
            if prev.IsLoaded and prev.IsVisible:
                if prev.WindowState == WindowState.Minimized:
                    prev.WindowState = WindowState.Normal
                _nudge_onscreen(prev)
                prev.Activate()
                reused = True
        except Exception:
            reused = False
        if reused:
            return
        _drop_parked(prev)

    # A real start (not a window reuse): Step 4 starts empty.
    _reset_session_log()
    try:
        run_wizard(1)
    except Exception as ex:
        run.error()
        # A modeless window that fails to open leaves no trace: the command
        # runs, nothing appears, and the button reads as dead. Say it loudly.
        ui.alert(u"Cloud Manager could not open:\n\n{0}".format(ex),
                 title=u"Cloud Manager")


# Called without a __main__ guard: pyRevit runs this file as a command and
# with a persistent engine the module name need not be '__main__'. With the
# guard in place the script would load, define everything and do absolutely
# nothing.
with usage.tool_run(__file__) as run:
    main()
