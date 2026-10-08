# -*- coding: utf-8 -*-
"""Print Set Manager -- window, handlers and Revit API calls.

Hosted here, not in the tool's script.py: Magic Tools runs under rocket
mode, which shares ONE IronPython engine across clicks and cleans it up
when the command returns, tearing down anything defined in script.py's own
scope -- including every button handler a modeless window relies on. A lib
module stays in sys.modules for as long as Revit runs, so this door does NOT
need __cleanengine__ -- the same shape lib/vtm.py (View Template Manager)
uses, proved working in Revit on 2026-09-18.

Why not __cleanengine__ (the fix nine other modeless tools carry instead,
Print Set Manager included until this file existed): it keeps the window
responsive, but it reimports every lib/ module -- modeless.py too -- on
EVERY click, so modeless._OPEN is always empty and modeless.focus() can
never find the window a previous click opened. That is exactly the "second
click opens another window" bug reported by QA on 2026-09-24. Each approach
is a trade-off; this tool takes the one that keeps focus() working.
"""

import re

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

import System
from System.Windows import RoutedEventHandler, Visibility
from System.Windows.Controls import CheckBox as WpfCheckBox
from System.Collections.ObjectModel import ObservableCollection

from pyrevit import revit

from Autodesk.Revit.DB import (
    FilteredElementCollector, ViewSheet, View, ViewSheetSet, ViewSet,
    PrintRange, Transaction, StorageType, BuiltInParameter
)

from slantisui import ui
import modeless

TITLE = u"Print Set Manager"

# Set by open_window() before anything else runs, once per window build --
# never read mid-run off __revit__/revit.doc again (do not read revit.doc in
# the body of a function; take the document from the caller). Every write path
# below passes doc=doc to modeless.run/show, so
# Revit refuses the job with an on-screen message if another document is
# active when it actually runs -- see _Handler.Execute / _is_active in
# modeless.py. That check is what keeps Save from ever writing into a
# different open model (investigated for QA point 6: no code change needed,
# the guard already existed and every call site in this tool already used
# it).
doc = None


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _read_appears_in_sheet_list(sheet):
    try:
        p = sheet.LookupParameter('Appears in Sheet List')
        if p is None:
            p = sheet.get_Parameter(BuiltInParameter.SHEET_SCHEDULED)
        if p is None:
            return True
        return p.AsInteger() == 1
    except Exception:
        return True


class SheetRow(object):
    """Lightweight wrapper around a ViewSheet for the DataGrid."""

    def __init__(self, sheet):
        self.id            = sheet.Id
        self.Number        = sheet.SheetNumber
        self.Name          = sheet.Name
        self.Checked       = False
        self.in_sheet_list = _read_appears_in_sheet_list(sheet)


_VIEW_KIND_LABELS = {
    'FloorPlan':       u'Floor Plan',
    'CeilingPlan':     u'Ceiling Plan',
    'Elevation':       u'Elevation',
    'Section':         u'Section',
    'Detail':          u'Detail View',
    'ThreeD':          u'3D View',
    'DraftingView':    u'Drafting View',
    'Legend':          u'Legend',
    'AreaPlan':        u'Area Plan',
    'EngineeringPlan': u'Engineering Plan',
    'Schedule':        u'Schedule',
    'Walkthrough':     u'Walkthrough',
    'Rendering':       u'Rendering',
}


def _view_kind_label(view):
    """Friendly label for a non-sheet view, e.g. "Floor Plan", "3D View"."""
    try:
        name = str(view.ViewType)
    except Exception:
        return u'View'
    return _VIEW_KIND_LABELS.get(name, name)


class LooseViewRow(object):
    """A non-sheet View that is a member of the current print set.

    Bug fix (QA point 1, 2026-09-24): before this class existed, Save
    rebuilt the print set from the sheet checkboxes ALONE, so any loose
    view (floor plan, 3D view...) already in the set silently disappeared
    on the first save. Every such view is now its own checkable row, and
    Save keeps whichever ones stay checked.
    """

    def __init__(self, view):
        self.id      = view.Id
        self.Kind    = _view_kind_label(view)
        self.Name    = view.Name
        self.Label   = u"{0}: {1}".format(self.Kind, self.Name)
        self.Checked = True   # already a member; unchecking drops it on Save


def get_all_sheets():
    return [s for s in FilteredElementCollector(doc).OfClass(ViewSheet)
            if not s.IsPlaceholder]


def get_print_sets():
    return sorted(
        list(FilteredElementCollector(doc).OfClass(ViewSheetSet)),
        key=lambda s: s.Name
    )


def get_set_contents(vss):
    """(sheet_ids, loose_views) -- everything the set has today, split by
    whether each member is a ViewSheet. sheet_ids is a set of ElementId,
    loose_views is a plain list of View so callers can read Name/ViewType
    without a second lookup."""
    sheet_ids = set()
    loose = []
    try:
        for v in vss.Views:
            if isinstance(v, ViewSheet):
                sheet_ids.add(v.Id)
            elif isinstance(v, View):
                loose.append(v)
    except Exception:
        pass
    return sheet_ids, loose


# ---------------------------------------------------------------------------
# Sort helpers
# ---------------------------------------------------------------------------

def _natural_key(s):
    parts = []
    for chunk in re.split(r'(\d+)', s):
        if chunk.isdigit():
            parts.append((0, int(chunk), ''))
        else:
            parts.append((1, 0, chunk.lower()))
    return parts


def build_sort_options():
    options = [
        ('Sheet Number', lambda r: _natural_key(r.Number)),
        ('Sheet Name',   lambda r: r.Name.lower()),
    ]
    sample = next((s for s in FilteredElementCollector(doc).OfClass(ViewSheet)
                   if not s.IsPlaceholder), None)
    if sample is None:
        return options

    seen  = {'Sheet Number', 'Sheet Name'}
    extra = []
    for param in sample.Parameters:
        try:
            if param.StorageType not in (StorageType.String, StorageType.Integer):
                continue
            defn = param.Definition
            name = defn.Name.strip()
            if not name or name in seen:
                continue
            if name.startswith('<') or name.startswith('_'):
                continue
            seen.add(name)

            def make_key_fn(d):
                def key_fn(row):
                    try:
                        sheet = doc.GetElement(row.id)
                        if sheet is None:
                            return ''
                        p = sheet.get_Parameter(d)
                        if p is None:
                            return ''
                        return (p.AsString() or p.AsValueString() or '').lower()
                    except Exception:
                        return ''
                return key_fn

            extra.append((name, make_key_fn(defn)))
        except Exception:
            pass

    extra.sort(key=lambda x: x[0].lower())
    options.extend(extra)
    return options


# ---------------------------------------------------------------------------
# Small dialog helpers (slantisui input dialogs)
# ---------------------------------------------------------------------------

def _ask_name(title, prompt, default=''):
    """Show a /slantis single-line text input dialog. Returns the string or
    None."""
    _body = """
      <StackPanel>
        <TextBlock x:Name="lblPrompt" Foreground="#77736C" FontSize="12"
                   TextWrapping="Wrap" Margin="0,0,0,10"/>
        <TextBox x:Name="txtValue"/>
      </StackPanel>
    """
    _footer = """
      <Grid>
        <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
          <Button x:Name="btnOK"     Content="OK"     Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
          <Button x:Name="btnCancel" Content="Cancel" Style="{StaticResource BtnGhost}"/>
        </StackPanel>
      </Grid>
    """
    win        = ui.parse(title, u"", _body, _footer, width=400, height=300)
    lbl_prompt = win.FindName("lblPrompt")
    txt        = win.FindName("txtValue")
    btn_ok     = win.FindName("btnOK")
    btn_cancel = win.FindName("btnCancel")

    lbl_prompt.Text = prompt
    txt.Text        = default or u""
    result          = [None]

    def on_ok(s, e):
        val = (txt.Text or u'').strip()
        if not val:
            ui.alert(u'Please enter a name.', title='Required')
            return
        result[0] = val
        win.Close()

    def on_cancel(s, e):
        win.Close()

    btn_ok.Click     += on_ok
    btn_cancel.Click += on_cancel
    win.ShowDialog()
    return result[0]


def _ask_new_set(current_name):
    """Dialog for New: name + (when there is something to copy) a choice of
    source. Returns (name, source) with source in ('copy', 'empty'), or
    None if cancelled.

    Feature (QA point 5, 2026-09-24): before this dialog, New silently
    seeded the new set from whatever the code happened to read (the
    alphabetically-first existing set, not what was on screen) and never
    told the user. Now the user picks: a copy of what they are looking at
    right now, unsaved edits included, or an empty set.
    """
    _body = """
      <StackPanel>
        <TextBlock x:Name="lblPrompt" Foreground="#77736C" FontSize="12"
                   TextWrapping="Wrap" Margin="0,0,0,10"/>
        <TextBox x:Name="txtValue" Margin="0,0,0,14"/>
        <StackPanel x:Name="pnlSource">
          <RadioButton x:Name="radCopy" GroupName="grpNewSetSource"
                       IsChecked="True" Margin="0,0,0,8">
            <TextBlock x:Name="lblCopy" TextWrapping="Wrap"/>
          </RadioButton>
          <RadioButton x:Name="radEmpty" GroupName="grpNewSetSource"
                       Content="Empty"/>
        </StackPanel>
      </StackPanel>
    """
    _footer = """
      <Grid>
        <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
          <Button x:Name="btnOK"     Content="Create" Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
          <Button x:Name="btnCancel" Content="Cancel"  Style="{StaticResource BtnGhost}"/>
        </StackPanel>
      </Grid>
    """
    # Bug fix (QA V2 point 2): no fixed height. A fixed 290 cut the "Empty"
    # radio off whenever the "Copy of ..." label ran long; height=None makes
    # slantisui size the window to its content, and the label is a wrapping
    # TextBlock so a long set name grows the window instead of overflowing.
    win        = ui.parse(u'New Print Set', u'', _body, _footer, width=440)
    lbl_prompt = win.FindName('lblPrompt')
    txt        = win.FindName('txtValue')
    pnl_source = win.FindName('pnlSource')
    rad_copy   = win.FindName('radCopy')
    lbl_copy   = win.FindName('lblCopy')
    rad_empty  = win.FindName('radEmpty')
    btn_ok     = win.FindName('btnOK')
    btn_cancel = win.FindName('btnCancel')

    lbl_prompt.Text = u'Name for the new print set:'
    if current_name:
        lbl_copy.Text      = (u'Copy of what you are viewing ("{0}", '
                               u'with your changes)').format(current_name)
        pnl_source.Visibility = Visibility.Visible
    else:
        pnl_source.Visibility = Visibility.Collapsed

    result = [None]

    def on_ok(s, e):
        val = (txt.Text or u'').strip()
        if not val:
            ui.alert(u'Please enter a name.', title='Required')
            return
        if current_name and bool(rad_empty.IsChecked):
            source = u'empty'
        else:
            source = u'copy' if current_name else u'empty'
        result[0] = (val, source)
        win.Close()

    def on_cancel(s, e):
        win.Close()

    btn_ok.Click     += on_ok
    btn_cancel.Click += on_cancel
    win.ShowDialog()
    return result[0]


def _ask_save_discard_cancel(set_name, action=u'closing'):
    """Dialog shown with unsaved changes: Close/X, New and a switch of set
    all use this one, so the user always gets the same three choices.
    Returns 'save', 'discard' or 'cancel'. `action` finishes the sentence
    "Save them before ...".

    Bug fix (QA point 2, 2026-09-24): Close and the window's X used to call
    Window.Close() directly, which lost every unsaved checkbox change with
    no warning.
    """
    _body = """
      <StackPanel>
        <TextBlock x:Name="lblMsg" Foreground="#202022" FontSize="13"
                   TextWrapping="Wrap"/>
      </StackPanel>
    """
    _footer = """
      <Grid>
        <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
          <Button x:Name="btnSave"    Content="Save"    Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
          <Button x:Name="btnDiscard" Content="Discard" Style="{StaticResource BtnGhost}" Margin="0,0,8,0"/>
          <Button x:Name="btnCancel"  Content="Cancel"  Style="{StaticResource BtnGhost}"/>
        </StackPanel>
      </Grid>
    """
    win = ui.parse(u'Unsaved changes', u'', _body, _footer, width=420, height=240)
    win.FindName('lblMsg').Text = (
        u'You have unsaved changes in "{0}". Save them before {1}?'
    ).format(set_name or u'', action)

    result = [u'cancel']

    def pick(val):
        def handler(s, e):
            result[0] = val
            win.Close()
        return handler

    win.FindName('btnSave').Click    += pick(u'save')
    win.FindName('btnDiscard').Click += pick(u'discard')
    win.FindName('btnCancel').Click  += pick(u'cancel')
    win.ShowDialog()
    return result[0]


# ---------------------------------------------------------------------------
# XAML
# ---------------------------------------------------------------------------

# The body goes INSIDE the work-surface card that ui.parse builds. The
# Button/CheckBox/TextBox/ComboBox/DataGrid* styles are INHERITED from the
# slantisui lib (nothing is declared here). The only bespoke bits are the
# ListBoxItem of the print-set panel and the loose-views card, because the
# lib does not style ListBox/ItemsControl (controls specific to this tool).
_BODY = """
  <Grid>
    <Grid.Resources>
      <Style TargetType="ListBoxItem">
        <Setter Property="Foreground" Value="#202022"/>
        <Setter Property="FontSize"   Value="13"/>
        <Setter Property="Template">
          <Setter.Value>
            <ControlTemplate TargetType="ListBoxItem">
              <Border x:Name="bd" Background="Transparent" Padding="10,9"
                      BorderBrush="#F0EEE9" BorderThickness="0,0,0,1">
                <ContentPresenter VerticalAlignment="Center"/>
              </Border>
              <ControlTemplate.Triggers>
                <Trigger Property="IsMouseOver" Value="True">
                  <Setter TargetName="bd" Property="Background" Value="__ROW_HOVER__"/>
                </Trigger>
                <Trigger Property="IsSelected" Value="True">
                  <Setter TargetName="bd" Property="Background" Value="__ROW_SEL__"/>
                </Trigger>
              </ControlTemplate.Triggers>
            </ControlTemplate>
          </Setter.Value>
        </Setter>
      </Style>
    </Grid.Resources>

    <Grid.ColumnDefinitions>
      <ColumnDefinition Width="240"/>
      <ColumnDefinition Width="1"/>
      <ColumnDefinition Width="*"/>
    </Grid.ColumnDefinitions>

    <!-- LEFT: print set list -->
    <Grid Grid.Column="0" Margin="0,0,14,0">
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="*"/>
        <RowDefinition Height="Auto"/>
      </Grid.RowDefinitions>

      <TextBlock Grid.Row="0" Text="PRINT SETS" Foreground="#A6A199"
                 FontSize="10.5" FontWeight="Medium" Margin="2,0,0,6"/>

      <ListBox x:Name="lstSets" Grid.Row="1"
               Background="#FFFFFF" BorderBrush="#ECE9E4" BorderThickness="1"/>

      <StackPanel Grid.Row="2" Orientation="Horizontal" Margin="0,8,0,0">
        <Button x:Name="btnNew"    Content="New"    Style="{StaticResource BtnGhost}" Padding="12,7" Margin="0,0,6,0"/>
        <Button x:Name="btnRename" Content="Rename" Style="{StaticResource BtnGhost}" Padding="12,7" Margin="0,0,6,0"/>
        <Button x:Name="btnDelete" Content="Delete" Style="{StaticResource BtnGhost}" Padding="12,7"/>
      </StackPanel>
    </Grid>

    <!-- SEPARATOR -->
    <Border Grid.Column="1" Background="#ECE9E4"/>

    <!-- RIGHT: sheets -->
    <Grid Grid.Column="2" Margin="14,0,0,0">
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="*"/>
        <RowDefinition Height="Auto"/>
      </Grid.RowDefinitions>

      <!-- Filter row -->
      <Grid Grid.Row="0" Margin="0,0,0,10">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <TextBox x:Name="txtFilter" Grid.Column="0" Margin="0,0,12,0"
                 ToolTip="Type to filter sheets..."/>
        <CheckBox x:Name="chkInList" Grid.Column="1" Style="{StaticResource BrandCheck}"
                  VerticalAlignment="Center" Content="Appear in Sheet List"/>
      </Grid>

      <!-- Sort row -->
      <Grid Grid.Row="1" Margin="0,0,0,10">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="Auto"/>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
          <ColumnDefinition Width="*"/>
        </Grid.ColumnDefinitions>
        <TextBlock Grid.Column="0" Text="Sort:" Foreground="#77736C" FontSize="11.5"
                   VerticalAlignment="Center" Margin="0,0,8,0"/>
        <ComboBox x:Name="cmbSort"  Grid.Column="1" Margin="0,0,12,0"/>
        <TextBlock Grid.Column="2" Text="then by:" Foreground="#77736C" FontSize="11.5"
                   VerticalAlignment="Center" Margin="0,0,8,0"/>
        <ComboBox x:Name="cmbSort2" Grid.Column="3"/>
      </Grid>

      <!-- Check all / Uncheck all + count -->
      <Grid Grid.Row="2" Margin="0,0,0,8">
        <StackPanel HorizontalAlignment="Left" Orientation="Horizontal" VerticalAlignment="Center">
          <Button x:Name="btnAll"  Content="Check all"   Style="{StaticResource BtnGhost}" Margin="0,0,8,0"/>
          <Button x:Name="btnNone" Content="Uncheck all" Style="{StaticResource BtnGhost}"/>
        </StackPanel>
        <TextBlock x:Name="lblCount" HorizontalAlignment="Right" VerticalAlignment="Center"
                   Foreground="#A6A199" FontSize="11.5"/>
      </Grid>

      <!-- Sheet DataGrid (dentro de card blanca redondeada) -->
      <Border Grid.Row="3" CornerRadius="8" Background="#FFFFFF"
              BorderBrush="#ECE9E4" BorderThickness="1" ClipToBounds="True">
        <DataGrid x:Name="grid"
                  AutoGenerateColumns="False"
                  Background="Transparent" BorderThickness="0"
                  CanUserAddRows="False"
                  CanUserDeleteRows="False"
                  CanUserSortColumns="False"
                  SelectionMode="Single"
                  HeadersVisibility="Column">
          <DataGrid.Columns>
            <DataGridTemplateColumn Header="" Width="44">
              <DataGridTemplateColumn.CellTemplate>
                <DataTemplate>
                  <CheckBox Style="{StaticResource BrandCheck}"
                            IsChecked="{Binding Checked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                            HorizontalAlignment="Center"/>
                </DataTemplate>
              </DataGridTemplateColumn.CellTemplate>
            </DataGridTemplateColumn>
            <DataGridTextColumn Header="NUMBER" Width="110"
                                Binding="{Binding Number}" IsReadOnly="True">
              <DataGridTextColumn.ElementStyle>
                <Style TargetType="TextBlock">
                  <Setter Property="FontWeight" Value="Medium"/>
                  <Setter Property="VerticalAlignment" Value="Center"/>
                </Style>
              </DataGridTextColumn.ElementStyle>
            </DataGridTextColumn>
            <DataGridTextColumn Header="SHEET NAME" Width="*"
                                Binding="{Binding Name}"   IsReadOnly="True">
              <DataGridTextColumn.ElementStyle>
                <Style TargetType="TextBlock">
                  <Setter Property="VerticalAlignment" Value="Center"/>
                </Style>
              </DataGridTextColumn.ElementStyle>
            </DataGridTextColumn>
          </DataGrid.Columns>
        </DataGrid>
      </Border>

      <!-- Loose (non-sheet) views already in the set: hidden when there are
           none. Bug fix, QA point 1: these used to be silently dropped by
           Save because only this DataGrid's sheets were ever written back. -->
      <Border x:Name="loosePanel" Grid.Row="4" Margin="0,10,0,0" CornerRadius="8"
              Background="#FFF7EF" BorderBrush="#F0DEC9" BorderThickness="1"
              Padding="12,10" MaxHeight="150" Visibility="Collapsed">
        <Grid>
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
          </Grid.RowDefinitions>
          <TextBlock Grid.Row="0" Text="VIEWS IN THE SET (NOT SHEETS)"
                     Foreground="#A6752E" FontSize="10.5" FontWeight="Medium"
                     Margin="0,0,0,8"/>
          <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
            <ItemsControl x:Name="lstLooseViews">
              <ItemsControl.ItemTemplate>
                <DataTemplate>
                  <CheckBox Style="{StaticResource BrandCheck}" Margin="0,4"
                            IsChecked="{Binding Checked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                            Content="{Binding Label}"/>
                </DataTemplate>
              </ItemsControl.ItemTemplate>
            </ItemsControl>
          </ScrollViewer>
        </Grid>
      </Border>
    </Grid>
  </Grid>
"""

# The two selection tints are the library's own row tokens, injected here rather
# than written as hex: they are derived from ACCENT, so they follow a retone. A
# literal orange in a tool is the one thing a retone cannot reach.
_BODY = (_BODY.replace("__ROW_HOVER__", ui.ROW_HOVER)
              .replace("__ROW_SEL__",   ui.ROW_SEL))

_FOOTER = """
  <Grid>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnSave"  Content="Save changes" Style="{StaticResource BtnPrimary}"
              IsEnabled="False" Margin="0,0,8,0"/>
      <Button x:Name="btnClose" Content="Close" Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""


# ---------------------------------------------------------------------------
# Main form class
# ---------------------------------------------------------------------------

class PrintSetManagerForm(object):

    def __init__(self):
        self._print_sets     = []
        self._all_rows       = []
        self._rows           = []
        self._loose_rows     = []
        self._current_vss    = None
        self._dirty          = False
        self._sort_options   = build_sort_options()
        self._items          = ObservableCollection[SheetRow]()
        self._loose_items    = ObservableCollection[LooseViewRow]()

        self._win = ui.parse(
            "Print Set Manager",
            "Manage sheet membership in print sets",
            _BODY, _FOOTER,
            width=960, height=680,
        )

        # Get named controls
        self.lst_sets     = self._win.FindName("lstSets")
        self.txt_filter   = self._win.FindName("txtFilter")
        self.chk_inlist   = self._win.FindName("chkInList")
        self.cmb_sort     = self._win.FindName("cmbSort")
        self.cmb_sort2    = self._win.FindName("cmbSort2")
        self.grid         = self._win.FindName("grid")
        self.loose_panel  = self._win.FindName("loosePanel")
        self.loose_list   = self._win.FindName("lstLooseViews")
        self.lbl_count    = self._win.FindName("lblCount")
        self.btn_save     = self._win.FindName("btnSave")
        self.btn_all      = self._win.FindName("btnAll")
        self.btn_none     = self._win.FindName("btnNone")
        self.btn_new      = self._win.FindName("btnNew")
        self.btn_rename   = self._win.FindName("btnRename")
        self.btn_delete   = self._win.FindName("btnDelete")
        self.btn_close    = self._win.FindName("btnClose")

        # Populate sort combos
        for name, _ in self._sort_options:
            self.cmb_sort.Items.Add(name)
        if self.cmb_sort.Items.Count > 0:
            self.cmb_sort.SelectedIndex = 0

        self.cmb_sort2.Items.Add(u"(none)")
        for name, _ in self._sort_options:
            self.cmb_sort2.Items.Add(name)
        self.cmb_sort2.SelectedIndex = 1 if self.cmb_sort2.Items.Count > 1 else 0

        # Bind DataGrid / loose-views list
        self.grid.ItemsSource       = self._items
        self.loose_list.ItemsSource = self._loose_items

        # Use ClickEvent (not Checked/Unchecked) so only real user clicks mark
        # dirty. Checked/Unchecked also fire when WPF sets IsChecked via
        # binding during virtualized row render, which would spuriously mark
        # the set as dirty. Same handler for the sheet grid and the loose-
        # views list: both are "a checkbox in this window changed" -> dirty.
        self.grid.AddHandler(
            WpfCheckBox.ClickEvent,
            RoutedEventHandler(self._on_sheet_check_changed),
            True
        )
        self.loose_list.AddHandler(
            WpfCheckBox.ClickEvent,
            RoutedEventHandler(self._on_sheet_check_changed),
            True
        )

        # Wire other events
        self.lst_sets.SelectionChanged    += self._on_set_selected
        self.txt_filter.TextChanged       += self._on_filter_changed
        self.chk_inlist.Checked           += self._on_filter_changed
        self.chk_inlist.Unchecked         += self._on_filter_changed
        self.cmb_sort.SelectionChanged    += self._on_sort_changed
        self.cmb_sort2.SelectionChanged   += self._on_sort_changed
        self.btn_new.Click    += self._on_new_set
        self.btn_rename.Click += self._on_rename_set
        self.btn_delete.Click += self._on_delete_set
        self.btn_all.Click    += self._on_check_all
        self.btn_none.Click   += self._on_uncheck_all
        self.btn_save.Click   += self._on_save
        self.btn_close.Click  += lambda s, e: self._win.Close()
        # Bug fix (QA point 2): Close and the native X both route through
        # Window.Close(), which raises Closing -- one hook covers both.
        self._win.Closing     += self._on_closing

        self._load_print_sets()

    def show(self):
        modeless.show(self._win, TITLE, doc=doc)

    # -----------------------------------------------------------------------
    # Print set list
    # -----------------------------------------------------------------------

    def _load_print_sets(self, select_name=None):
        self._print_sets = get_print_sets()
        self.lst_sets.SelectionChanged -= self._on_set_selected
        self.lst_sets.Items.Clear()
        for ps in self._print_sets:
            self.lst_sets.Items.Add(ps.Name)
        self.lst_sets.SelectionChanged += self._on_set_selected

        if select_name:
            for i, ps in enumerate(self._print_sets):
                if ps.Name == select_name:
                    self.lst_sets.SelectedIndex = i
                    return

        if self._print_sets:
            self.lst_sets.SelectedIndex = 0
        else:
            self._current_vss = None
            self._items.Clear()
            self._rows       = []
            self._all_rows   = []
            self._loose_rows = []
            self._loose_items.Clear()
            self.loose_panel.Visibility = Visibility.Collapsed
            self._update_count()

    def _select_silently(self, vss):
        """Puts the list selection back on `vss` without raising
        _on_set_selected (and so without asking about unsaved changes)."""
        idx = next(
            (i for i, ps in enumerate(self._print_sets) if ps is vss), -1
        )
        if idx >= 0:
            self.lst_sets.SelectionChanged -= self._on_set_selected
            self.lst_sets.SelectedIndex = idx
            self.lst_sets.SelectionChanged += self._on_set_selected

    def _on_set_selected(self, sender, e):
        if self._dirty:
            # Bug fix (QA V2 point 3): same Save / Discard / Cancel dialog as
            # Close and New, instead of a Continue/Cancel confirm.
            old_vss = self._current_vss
            choice  = _ask_save_discard_cancel(
                old_vss.Name if old_vss else u'',
                action=u'switching to another set'
            )
            if choice == u'cancel':
                # Revert selection back to the current set using Dispatcher
                # (the list cannot be changed from inside its own event).
                def revert():
                    self._select_silently(old_vss)
                from System.Windows.Threading import DispatcherPriority
                sender.Dispatcher.BeginInvoke(
                    DispatcherPriority.Background,
                    System.Action(revert)
                )
                return
            if choice == u'save':
                # The list already shows the target, but self._current_vss
                # and the rows are still the OLD set's: save that one first
                # (API call, so through modeless.run), then load the target.
                # Same chain as Close, which closes only after a good save.
                idx = self.lst_sets.SelectedIndex
                if idx < 0 or idx >= len(self._print_sets):
                    return
                target_name = self._print_sets[idx].Name

                def save_then_switch(uiapp):
                    if not self._save_current_set():
                        # Save failed (user already alerted): stay on the
                        # old set with its changes still pending.
                        self._select_silently(old_vss)
                        return
                    # _save_current_set rebuilt self._print_sets and cleared
                    # _dirty, so this selection raises no second prompt.
                    self._load_print_sets(select_name=target_name)

                modeless.run(save_then_switch, doc=doc, title=TITLE)
                return
            # 'discard': fall through and load the target set below.

        idx = self.lst_sets.SelectedIndex
        if idx < 0 or idx >= len(self._print_sets):
            return

        self._current_vss  = self._print_sets[idx]
        self._dirty        = False
        self.btn_save.IsEnabled = False

        def work(uiapp):
            # ViewSheetSet.Views and the sheet collector touch the API: they
            # run inside the ExternalEvent, not in the click.
            self._load_set_contents()

        modeless.run(work, doc=doc, title=TITLE)

    # -----------------------------------------------------------------------
    # Sheet + loose-view loading, sorting, filtering
    # -----------------------------------------------------------------------

    def _load_set_contents(self):
        """Loads both the sheets (DataGrid) and the non-sheet views already
        in the set (the loose-views panel) for self._current_vss."""
        if self._current_vss is None:
            return
        sheet_ids, loose_views = get_set_contents(self._current_vss)

        self._all_rows = []
        for s in get_all_sheets():
            row         = SheetRow(s)
            row.Checked = s.Id in sheet_ids
            self._all_rows.append(row)

        self._loose_rows = [LooseViewRow(v) for v in loose_views]
        self._loose_rows.sort(key=lambda r: (r.Kind, r.Name.lower()))

        self._apply_sort()
        self._apply_filter()
        self._populate_loose()

    def _current_key_fn(self):
        idx1 = self.cmb_sort.SelectedIndex
        fn1  = (self._sort_options[idx1][1]
                if 0 <= idx1 < len(self._sort_options)
                else lambda r: _natural_key(r.Number))

        idx2 = self.cmb_sort2.SelectedIndex
        if idx2 > 0 and (idx2 - 1) < len(self._sort_options):
            fn2 = self._sort_options[idx2 - 1][1]
        else:
            fn2 = None

        if fn2 is None:
            return fn1

        def compound(r):
            return (fn1(r), fn2(r))
        return compound

    def _apply_sort(self):
        key_fn = self._current_key_fn()
        try:
            self._all_rows.sort(key=key_fn)
        except Exception:
            self._all_rows.sort(key=lambda r: r.Number)

    def _apply_filter(self):
        raw          = (self.txt_filter.Text or u'').strip().lower()
        tokens       = [t for t in raw.split() if t]
        only_in_list = bool(self.chk_inlist.IsChecked)

        def _matches(r):
            if only_in_list and not r.in_sheet_list:
                return False
            if tokens:
                haystack = (r.Number + u' ' + r.Name).lower()
                if not all(t in haystack for t in tokens):
                    return False
            return True

        self._rows = [r for r in self._all_rows if _matches(r)]
        self._populate_grid()

    def _populate_grid(self):
        self._items.Clear()
        for row in self._rows:
            self._items.Add(row)
        self._update_count()

    def _populate_loose(self):
        self._loose_items.Clear()
        for row in self._loose_rows:
            self._loose_items.Add(row)
        self.loose_panel.Visibility = (
            Visibility.Visible if self._loose_rows else Visibility.Collapsed
        )
        self._update_count()

    def _update_count(self):
        total   = len(self._all_rows)
        checked = sum(1 for r in self._all_rows if r.Checked)
        if self._loose_rows:
            loose_checked = sum(1 for r in self._loose_rows if r.Checked)
            self.lbl_count.Text = u"{0} / {1} sheets  -  {2} / {3} views".format(
                checked, total, loose_checked, len(self._loose_rows))
        else:
            self.lbl_count.Text = u"{0} / {1} sheets".format(checked, total)

    # -----------------------------------------------------------------------
    # Checkbox change (routed event from DataGrid / loose-views list)
    # -----------------------------------------------------------------------

    def _on_sheet_check_changed(self, sender, e):
        self._dirty = True
        self.btn_save.IsEnabled = True
        self._update_count()

    # -----------------------------------------------------------------------
    # Filter / sort event handlers
    # -----------------------------------------------------------------------

    def _on_filter_changed(self, sender, e):
        self._apply_filter()

    def _on_sort_changed(self, sender, e):
        if self._current_vss is None:
            return

        def work(uiapp):
            # A custom-parameter sort key reads sheet parameters via
            # doc.GetElement: touches the API, runs inside the ExternalEvent.
            self._apply_sort()
            self._apply_filter()

        modeless.run(work, doc=doc, title=TITLE)

    # -----------------------------------------------------------------------
    # Bulk check / uncheck (sheets only -- loose views keep their own check)
    # -----------------------------------------------------------------------

    def _on_check_all(self, sender, e):
        if self._current_vss is None:
            return
        for row in self._rows:
            row.Checked = True
        self._populate_grid()
        self._dirty = True
        self.btn_save.IsEnabled = True

    def _on_uncheck_all(self, sender, e):
        if self._current_vss is None:
            return
        for row in self._rows:
            row.Checked = False
        self._populate_grid()
        self._dirty = True
        self.btn_save.IsEnabled = True

    # -----------------------------------------------------------------------
    # Close with unsaved changes
    # -----------------------------------------------------------------------

    def _on_closing(self, sender, e):
        """Save / Discard / Cancel gate for Close and the native X -- both
        end up calling Window.Close(), which raises this event."""
        if not self._dirty:
            return

        choice = _ask_save_discard_cancel(
            self._current_vss.Name if self._current_vss else u''
        )
        if choice == u'cancel':
            e.Cancel = True
            return
        if choice == u'save':
            # Saving touches the API (Transaction, PrintManager): it has to
            # run through modeless.run, not here in the Closing handler. So
            # this close is cancelled now, and the queued job closes the
            # window itself once the save actually succeeds.
            e.Cancel = True

            def work(uiapp):
                if self._save_current_set():
                    self._win.Close()

            modeless.run(work, doc=doc, title=TITLE)
            return
        # 'discard': nothing was ever written to the model (only the Python-
        # side row state changed), so there is nothing to roll back -- let
        # the close proceed as normal.

    # -----------------------------------------------------------------------
    # Save changes
    # -----------------------------------------------------------------------

    def _on_save(self, sender, e):
        if self._current_vss is None:
            return

        def work(uiapp):
            # Transaction, doc.GetElement and PrintManager touch the API:
            # they run inside the ExternalEvent, not in the click.
            self._save_current_set()

        modeless.run(work, doc=doc, title=TITLE)

    def _save_current_set(self):
        """Writes self._all_rows' and self._loose_rows' checked state into
        self._current_vss.

        Must run inside a valid API context: only called from within a
        work(uiapp) queued through modeless.run. Returns True on success,
        False if it was aborted or failed (the user was already alerted).
        """
        checked_sheet_ids = set(r.id for r in self._all_rows if r.Checked)
        selected_sheets = []
        for sid in checked_sheet_ids:
            el = doc.GetElement(sid)
            if isinstance(el, ViewSheet) and not el.IsPlaceholder:
                selected_sheets.append(el)

        # Bug fix (QA point 1): kept_views is what makes a loose view that
        # stays checked survive the save; before this list existed, Save
        # rebuilt the set from selected_sheets alone and every non-sheet
        # view vanished with no warning.
        kept_views = []
        for r in self._loose_rows:
            if not r.Checked:
                continue
            el = doc.GetElement(r.id)
            if el is not None:
                kept_views.append(el)

        if not selected_sheets and not kept_views:
            ui.alert(u'A print set must contain at least one sheet or view.',
                     title='Empty set')
            return False

        pm            = doc.PrintManager
        pm.PrintRange = PrintRange.Select
        old_name      = self._current_vss.Name
        old_id        = self._current_vss.Id

        new_vs = ViewSet()
        for sh in selected_sheets:
            new_vs.Insert(sh)
        for v in kept_views:
            new_vs.Insert(v)

        expected = len(selected_sheets) + len(kept_views)

        t = Transaction(doc, 'Print Set Manager -- Update set')
        t.Start()
        try:
            pm.ViewSheetSetting.CurrentViewSheetSet = pm.ViewSheetSetting.InSession
            pm.ViewSheetSetting.InSession.Views     = new_vs

            persisted = 0
            for _ in pm.ViewSheetSetting.InSession.Views:
                persisted += 1

            if persisted != expected:
                t.RollBack()
                ui.alert(
                    u'Cannot modify InSession.Views -- assignment rejected by Revit '
                    u'(expected {0} items, found {1} after assignment). '
                    u'This Revit version blocks all known modification paths for '
                    u'ViewSheetSet membership. As a workaround, delete the print '
                    u'set and re-create it through the Revit Print dialog.'.format(
                        expected, persisted),
                    title='Revit API limitation')
                return False

            doc.Delete(old_id)
            pm.ViewSheetSetting.SaveAs(old_name)
            t.Commit()
        except Exception as ex:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
            ui.alert(u'Error updating print set: ' + str(ex), title='Error')
            return False

        self._current_vss = next(
            (s for s in FilteredElementCollector(doc).OfClass(ViewSheetSet)
             if s.Name == old_name), None
        )
        self._dirty = False
        self.btn_save.IsEnabled = False
        self._load_print_sets(select_name=old_name)
        return True

    # -----------------------------------------------------------------------
    # CRUD -- New / Rename / Delete
    # -----------------------------------------------------------------------

    def _create_print_set(self, name, views):
        """Creates a new named print set containing exactly `views` (a list
        of ViewSheet/View elements, possibly empty for the "Empty" choice).
        Must run inside modeless.run: touches Transaction and PrintManager."""
        pm            = doc.PrintManager
        pm.PrintRange = PrintRange.Select

        new_vs = ViewSet()
        for v in views:
            new_vs.Insert(v)

        t = Transaction(doc, 'Print Set Manager -- Create Set')
        t.Start()
        try:
            pm.ViewSheetSetting.CurrentViewSheetSet = pm.ViewSheetSetting.InSession
            pm.ViewSheetSetting.InSession.Views     = new_vs
            pm.ViewSheetSetting.SaveAs(name)
            t.Commit()
            return True
        except Exception as ex:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
            ui.alert(u'Error creating set: ' + str(ex), title='Error')
            return False

    def _on_new_set(self, sender, e):
        # Bug fix (QA V2 point 3): asked ONCE, here, with the same Save /
        # Discard / Cancel dialog as Close. The jump to the new set after
        # Create clears _dirty first (see work below), so it never asks again.
        save_first = False
        if self._dirty:
            choice = _ask_save_discard_cancel(
                self._current_vss.Name if self._current_vss else u'',
                action=u'creating a new set'
            )
            if choice == u'cancel':
                return
            save_first = (choice == u'save')

        current_name = self._current_vss.Name if self._current_vss is not None else None
        picked = _ask_new_set(current_name)
        if picked is None:
            return
        new_name, source = picked

        if any(ps.Name == new_name for ps in self._print_sets):
            ui.alert(u'A print set named "{0}" already exists.'.format(new_name),
                     title='Duplicate name')
            return

        # Snapshot the on-screen checked state NOW, in the click: a pending
        # save (if any) reloads self._all_rows/self._loose_rows from Revit
        # once it runs, and "copy of what you are viewing" has to mean what
        # the user saw when they clicked New, not whatever is left after
        # that reload.
        checked_sheet_ids = set(r.id for r in self._all_rows if r.Checked)
        checked_loose_ids = set(r.id for r in self._loose_rows if r.Checked)

        def work(uiapp):
            # Transaction and PrintManager touch the API: they run inside
            # the ExternalEvent, not in the click. The pending save (if any)
            # runs first; if it fails (user already alerted) New is aborted
            # so the unsaved changes are not lost.
            if save_first and self._current_vss is not None:
                if not self._save_current_set():
                    return

            if source == u'copy':
                src_views = []
                for sid in checked_sheet_ids:
                    el = doc.GetElement(sid)
                    if el is not None:
                        src_views.append(el)
                for vid in checked_loose_ids:
                    el = doc.GetElement(vid)
                    if el is not None:
                        src_views.append(el)
            else:
                src_views = []

            if self._create_print_set(new_name, src_views):
                # The user already answered Save/Discard in the click; clear
                # the flag so selecting the new set does not ask again.
                self._dirty = False
                self.btn_save.IsEnabled = False
                self._load_print_sets(select_name=new_name)

        modeless.run(work, doc=doc, title=TITLE)

    def _on_rename_set(self, sender, e):
        if self._current_vss is None:
            ui.alert(u'Select a print set first.', title='No selection')
            return

        old_name = self._current_vss.Name
        new_name = _ask_name(u'Rename Print Set', u'New name:', default=old_name)
        if not new_name or new_name == old_name:
            return

        if any(ps.Name == new_name for ps in self._print_sets):
            ui.alert(u'A print set named "{}" already exists.'.format(new_name),
                     title='Duplicate name')
            return

        def work(uiapp):
            # Transaction and PrintManager touch the API: they run inside
            # the ExternalEvent, not in the click.

            # Attempt 1: set Name property directly
            t = Transaction(doc, 'Print Set Manager -- Rename Set')
            t.Start()
            try:
                self._current_vss.Name = new_name
                t.Commit()
                self._load_print_sets(select_name=new_name)
                return
            except Exception:
                try:
                    t.RollBack()
                except Exception:
                    pass

            # Attempt 2: SaveAs with new name, delete old
            pm            = doc.PrintManager
            pm.PrintRange = PrintRange.Select
            old_id        = self._current_vss.Id

            t2 = Transaction(doc, 'Print Set Manager -- Rename Set (fallback)')
            t2.Start()
            try:
                pm.ViewSheetSetting.CurrentViewSheetSet = self._current_vss
                pm.ViewSheetSetting.SaveAs(new_name)
                doc.Delete(old_id)
                t2.Commit()
            except Exception as ex2:
                if t2.HasStarted() and not t2.HasEnded():
                    t2.RollBack()
                ui.alert(u'Error renaming: ' + str(ex2), title='Error')
                return

            self._dirty = False
            self._load_print_sets(select_name=new_name)

        modeless.run(work, doc=doc, title=TITLE)

    def _on_delete_set(self, sender, e):
        if self._current_vss is None:
            ui.alert(u'Select a print set first.', title='No selection')
            return

        name = self._current_vss.Name
        if not ui.confirm(
            u'Delete print set "{}"?\nThis cannot be undone.'.format(name),
            title='Confirm delete'
        ):
            return

        def work(uiapp):
            # Transaction and doc.Delete touch the API: they run inside
            # the ExternalEvent, not in the click.
            t = Transaction(doc, 'Print Set Manager -- Delete Set')
            t.Start()
            try:
                doc.Delete(self._current_vss.Id)
                t.Commit()
            except Exception as ex:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
                ui.alert(u'Error deleting: ' + str(ex), title='Error')
                return

            self._current_vss = None
            self._dirty       = False
            self._load_print_sets()

        modeless.run(work, doc=doc, title=TITLE)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def open_window():
    """Show the window, or bring the existing one to front on a second click
    (see lib/modeless.py). No __cleanengine__ on the script.py door: this
    window and every handler live here, in a lib module that survives in
    sys.modules across clicks under rocket mode -- the shape lib/vtm.py
    (View Template Manager) proved working in Revit. __cleanengine__ would
    also reimport modeless.py on every click, which breaks modeless.focus()
    itself (see the module docstring above)."""
    global doc
    if modeless.focus(TITLE):
        return
    doc = revit.doc
    if doc is None:
        ui.alert(u'Open a project document first.', title=TITLE)
        return
    PrintSetManagerForm().show()
