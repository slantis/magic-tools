# -*- coding: utf-8 -*-
"""Find Room -- window and every handler live HERE, not in the pushbutton's
script.py.

Why: under rocket mode Magic Tools shares ONE engine across clicks and tears
it down the moment a script.py returns, which kills every handler that
script hung off a modeless window -- the window stays painted, no button
responds (diagnosed 2026-09-17). script.py used to survive this with
`__cleanengine__ = True` instead, but a clean engine reimports every lib
module (including this one and lib/modeless.py) on EVERY click, so
`modeless._OPEN` starts empty each time and `modeless.focus()` never finds
the window a previous click opened -- a second click on the ribbon button
opened a second window instead of focusing the first (the same gotcha hit
other tools). A module in lib/ survives in sys.modules for as long as Revit
runs, so hosting the window and its handlers here (the same shape
lib/vtm.py uses for View Template Manager) keeps them alive AND keeps
`modeless._OPEN` populated across clicks, with no `__cleanengine__` needed.

The door (the .pushbutton's script.py) is a three-line stub: `import
findroom` + `findroom.open_window()`.
"""
import re

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')

from pyrevit import revit
from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, BuiltInFailures,
    ElementId
)
from System.Collections.Generic import List as CList
from System.Collections.ObjectModel import ObservableCollection
from System.Windows.Input import Key

from slantisui import ui
import modeless

TITLE = u"Find Room"


# -- Natural sort for room numbers -------------------------------------------
# Revit stores ROOM_NUMBER as text, so a plain string sort reads 1, 10, 11, 2,
# 20, 3. Split into digit / non-digit runs and compare the digit runs as int,
# so "1, 2, 3, 10, 11, 20, 201, E-201" comes out in the order a person expects.
_NUM_RUN = re.compile(r'(\d+)')


def _natural_key(s):
    s = s or u""
    parts = _NUM_RUN.split(s)
    key = []
    for p in parts:
        if p.isdigit():
            key.append((1, int(p)))
        else:
            key.append((0, p.lower()))
    return key


def _redundant_room_ids(doc):
    # Revit flags two Room elements occupying the same enclosed region with a
    # warning; the one Revit keeps out of calculations (Area == 0) is the
    # "redundant" room. BuiltInFailures.RoomFailures.RoomsInSameRegionRooms is
    # the FailureDefinitionId that matches -- confirmed to exist by reflecting
    # RevitAPI.dll (2022-2025 installs), NOT confirmed live against a model with
    # a real redundant room. If the id is missing (older/newer API) or
    # GetWarnings() throws, this degrades to an empty set and every Area == 0
    # room falls back to "open" -- never a wrong label, just a missed one.
    ids = set()
    try:
        target = BuiltInFailures.RoomFailures.RoomsInSameRegionRooms
    except AttributeError:
        return ids
    try:
        warnings = doc.GetWarnings()
    except Exception:
        return ids
    for w in warnings:
        try:
            if w.GetFailureDefinitionId() != target:
                continue
            for eid in w.GetFailingElements():
                ids.add(eid)
        except Exception:
            continue
    return ids


class RoomRow(object):
    def __init__(self, number, name, level, status, eid):
        self.Number = number
        self.Name   = name
        self.Level  = level
        self.Status = status   # "" / "open" / "redundant"
        self._eid   = eid


def _load_all_rooms(doc):
    redundant_ids = _redundant_room_ids(doc)
    all_rooms = (
        FilteredElementCollector(doc)
        .OfCategory(BuiltInCategory.OST_Rooms)
        .WhereElementIsNotElementType()
        .ToElements()
    )
    rows = []
    for room in all_rooms:
        # Not Placed rooms have no Location and no Area: never show these.
        if room.Location is None:
            continue
        np = room.get_Parameter(BuiltInParameter.ROOM_NAME)
        ip = room.get_Parameter(BuiltInParameter.ROOM_NUMBER)
        name   = np.AsString() if np else u""
        number = ip.AsString() if ip else u""
        level  = doc.GetElement(room.LevelId)
        level_name = level.Name if level else u""

        if room.Area > 0:
            status = u""
        elif room.Id in redundant_ids:
            status = u"redundant"
        else:
            status = u"open"   # placed but not enclosed by walls/separation lines

        rows.append(RoomRow(number, name, level_name, status, room.Id))
    rows.sort(key=lambda r: (r.Level, _natural_key(r.Number)))
    return rows


def _counts_text(rows):
    n = len(rows)
    bits = [u"{0} placed room{1}".format(n, u"" if n == 1 else u"s")]
    n_open = sum(1 for r in rows if r.Status == u"open")
    n_red  = sum(1 for r in rows if r.Status == u"redundant")
    if n_open:
        bits.append(u"{0} open".format(n_open))
    if n_red:
        bits.append(u"{0} redundant".format(n_red))
    return u" · ".join(bits)


_BODY = u"""
  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
    </Grid.RowDefinitions>
    <Grid Grid.Row="0" Margin="0,0,0,12">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="Auto"/>
      </Grid.ColumnDefinitions>
      <TextBox x:Name="txtSearch" Grid.Column="0" Height="32" Margin="0,0,8,0"
               ToolTip="Type to search rooms by name, number, or level"/>
      <Button x:Name="btnRefresh" Grid.Column="1" Content="Refresh"
              Style="{StaticResource BtnGhost}"
              ToolTip="Re-read rooms from the model without clearing the search box"/>
    </Grid>
    <DataGrid x:Name="grid" Grid.Row="1" SelectionMode="Extended">
      <DataGrid.Columns>
        <DataGridTemplateColumn Header="" Width="34" CanUserResize="False">
          <DataGridTemplateColumn.CellTemplate>
            <DataTemplate>
              <Grid HorizontalAlignment="Center">
                <Grid.Resources>
                  <Style x:Key="__icoOpen" TargetType="Viewbox">
                    <Setter Property="Visibility" Value="Collapsed"/>
                    <Style.Triggers>
                      <DataTrigger Binding="{Binding Status}" Value="open">
                        <Setter Property="Visibility" Value="Visible"/>
                      </DataTrigger>
                    </Style.Triggers>
                  </Style>
                  <Style x:Key="__icoRedundant" TargetType="Viewbox">
                    <Setter Property="Visibility" Value="Collapsed"/>
                    <Style.Triggers>
                      <DataTrigger Binding="{Binding Status}" Value="redundant">
                        <Setter Property="Visibility" Value="Visible"/>
                      </DataTrigger>
                    </Style.Triggers>
                  </Style>
                </Grid.Resources>
                <Viewbox Style="{StaticResource __icoOpen}" Width="16" Height="16"
                         ToolTip="Room open (Not Enclosed). It is not enclosed by walls or Room Separation Lines: its area cannot be calculated.">
                  <Canvas Width="96" Height="96">
                    <Path Data="M60,18 L18,18 L18,78 L78,78 L78,50"
                          Stroke="__MUTED__" StrokeThickness="6"
                          StrokeStartLineCap="Round" StrokeEndLineCap="Round" StrokeLineJoin="Round"/>
                    <Path Data="M78,36 L78,22 Q78,18 74,18"
                          Stroke="__MUTED__" StrokeThickness="4"
                          StrokeStartLineCap="Round" StrokeEndLineCap="Round"
                          StrokeDashArray="1,8" Opacity="0.55"/>
                    <Line X1="78" Y1="78" X2="78" Y2="50"
                          Stroke="__WARN__" StrokeThickness="7"
                          StrokeStartLineCap="Round" StrokeEndLineCap="Round"/>
                    <Line X1="60" Y1="18" X2="44" Y2="18"
                          Stroke="__WARN__" StrokeThickness="7"
                          StrokeStartLineCap="Round" StrokeEndLineCap="Round"/>
                  </Canvas>
                </Viewbox>
                <Viewbox Style="{StaticResource __icoRedundant}" Width="16" Height="16"
                         ToolTip="Redundant room. Another room occupies the same enclosed space; this one is excluded from area calculations.">
                  <Canvas Width="96" Height="96">
                    <Rectangle Canvas.Left="14" Canvas.Top="14" Width="68" Height="68"
                               RadiusX="4" RadiusY="4" Stroke="__MUTED__" StrokeThickness="6"/>
                    <Path Data="M61,38 A17,17 0 1 0 64,54"
                          Stroke="__WARN__" StrokeThickness="6.5"
                          StrokeStartLineCap="Round" StrokeEndLineCap="Round"/>
                    <Path Data="M63,25 L63,40 L48,40"
                          Stroke="__WARN__" StrokeThickness="6.5"
                          StrokeStartLineCap="Round" StrokeEndLineCap="Round" StrokeLineJoin="Round"/>
                  </Canvas>
                </Viewbox>
              </Grid>
            </DataTemplate>
          </DataGridTemplateColumn.CellTemplate>
        </DataGridTemplateColumn>
        <DataGridTextColumn Header="ROOM #" Width="100" Binding="{Binding Number}"/>
        <DataGridTextColumn Header="NAME"   Width="*"   Binding="{Binding Name}"/>
        <DataGridTextColumn Header="LEVEL"  Width="160" Binding="{Binding Level}"/>
      </DataGrid.Columns>
    </DataGrid>
  </Grid>
"""
_BODY = _BODY.replace(u"__MUTED__", ui.TEXT_MUTED).replace(u"__WARN__", ui.STATUS_WARN)

_FOOTER = u"""
  <Grid>
    <TextBlock x:Name="lblCount" VerticalAlignment="Center" Foreground="#77736C"/>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnSelect" Content="Select &amp; Zoom"
              Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
      <Button x:Name="btnClose"  Content="Close"
              Style="{StaticResource BtnGhost}"/>
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

    all_rows = _load_all_rooms(doc)
    if len(all_rows) == 0:
        ui.alert(u"No placed rooms found in this model.", title=TITLE)
        return

    win = ui.parse(TITLE, _counts_text(all_rows),
                   _BODY, _FOOTER, width=680, height=560)

    txtSearch  = win.FindName("txtSearch")
    grid       = win.FindName("grid")
    lblCount   = win.FindName("lblCount")
    btnSelect  = win.FindName("btnSelect")
    btnClose   = win.FindName("btnClose")
    btnRefresh = win.FindName("btnRefresh")
    subtitle   = win.FindName("__slui_subtitle__")

    def _to_col(items):
        c = ObservableCollection[object]()
        for it in items:
            c.Add(it)
        return c

    state = {'all': all_rows, 'filtered': all_rows}

    def _apply_filter():
        q = txtSearch.Text.strip().lower()
        if not q:
            state['filtered'] = state['all']
        else:
            tokens = q.split()
            def match(r):
                h = u' '.join([r.Number, r.Name, r.Level]).lower()
                return all(t in h for t in tokens)
            state['filtered'] = [r for r in state['all'] if match(r)]
        grid.ItemsSource = _to_col(state['filtered'])
        n = len(state['filtered'])
        n_total = len(state['all'])
        lblCount.Text = u"{0} of {1} rooms".format(n, n_total)
        if subtitle is not None:
            subtitle.Text = _counts_text(state['all'])

    _apply_filter()

    def on_search(s, e):
        _apply_filter()

    def on_refresh(s, e):
        # Re-reads the model but keeps whatever the user already typed in the
        # search box. No DocumentChanged listener yet -- this is the manual,
        # explicit version; the button exists precisely because rooms created,
        # deleted, or renamed while the window is open are not picked up on
        # their own.
        # _load_all_rooms() collects, reads parameters and calls
        # doc.GetWarnings() -- all Revit API, illegal straight from a WPF
        # Click handler on a modeless window (see lib/modeless.py). Route it
        # through the ExternalEvent, same as on_select below.
        def work(uiapp):
            state['all'] = _load_all_rooms(doc)
            _apply_filter()

        modeless.run(work, doc=doc, title=TITLE)

    def on_select(s, e):
        sel = list(grid.SelectedItems)
        if not sel:
            query = txtSearch.Text.strip()
            if query:
                # A search narrowed the list: Enter/Select & Zoom on the
                # matches is a reasonable shortcut even with no row checked.
                sel = state['filtered']
        if not sel:
            # Empty search + nothing checked used to fall back to "everything
            # in the model" -- refuse instead of guessing.
            ui.alert(u"Select a room first.", title=TITLE,
                     context=u"Type in the search box or select one or more "
                             u"rows in the list, then press Enter or Select "
                             u"& Zoom.")
            return
        ids = CList[ElementId]([r._eid for r in sel])

        def work(uiapp):
            # Selection and zoom touch the API: they run inside the
            # ExternalEvent, with the UIDocument Revit hands us.
            uidoc = uiapp.ActiveUIDocument
            uidoc.Selection.SetElementIds(ids)
            uidoc.ShowElements(ids)

        modeless.run(work, doc=doc, title=TITLE)

    def on_key(s, e):
        if e.Key == Key.Return:
            on_select(s, e)

    txtSearch.TextChanged    += on_search
    txtSearch.PreviewKeyDown += on_key
    btnRefresh.Click         += on_refresh
    btnSelect.Click          += on_select
    btnClose.Click           += lambda s, e: win.Close()

    modeless.show(win, TITLE, doc=doc)
    txtSearch.Focus()
