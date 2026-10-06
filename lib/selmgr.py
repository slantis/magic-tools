# -*- coding: utf-8 -*-
"""Selection Manager -- window and every handler live HERE, not in the
pushbutton's script.py.

Why: this used to be a "clean engine" tool (`__cleanengine__ = True`, set
the day rocket mode was diagnosed to kill a modeless window's own
handlers). A clean engine reimports every lib/ module from scratch on
every single click -- that is what a clean engine IS -- and that reimport
reset lib/modeless.py's `_OPEN` registry (an empty dict, freshly defined)
on every execution. So `modeless.focus(TITLE)` could never find the
window the FIRST click had opened: a second click always built a second
window instead of bringing the first one to front (QA, 2026-09-24, bug 1;
a risk anticipated when `__cleanengine__` shipped: "the second click may open
a second window"). Find Room had the identical `__cleanengine__` flag and the
identical bug -- copying its focus()/show() call verbatim, which this tool
already did, changes nothing; QA simply tested it with the window already
closed between clicks.

Hosting the window and its handlers in a lib/ module instead survives in
sys.modules for as long as Revit runs (same shape lib/vtm.py uses for View
Template Manager, verified in Revit 2026-09-18), so the door's
`__persistentengine__ = True` WITHOUT `__cleanengine__` is enough: rocket
mode's per-click engine cleanup only tears down what a script.py itself
defined, never a module already sitting in sys.modules. That also means
`modeless.py` stays the SAME module instance across clicks, so its
`_OPEN` registry (and `focus()`) work exactly as designed.

QA improvement 4 (2026-09-24) asked for a tri-state checkbox on each
category header, so a whole category ticks/unticks in one click instead
of one row at a time. Doing that on top of the OLD WPF `GroupStyle` /
`CollectionViewGroup` grouping would need a value converter bound to a
nullable bool -- this codebase has no working precedent for that shape
through `{Binding}` (only ever set by CODE, e.g. `chk.IsChecked = None` in
lib/vgrow.py). So grouping stops being native: the ListBox is now a FLAT
list where a `Header` pseudo-row and its `Row`s are plain items mixed
together (see `_build_display`), and ONE shared DataTemplate shows
whichever half applies via a `Visibility` STRING binding
(`HeaderVis`/`RowVis`) -- the exact binding shape (a plain string
attribute, one-way) that `Display`/`Count` already use successfully in
this same ListBox. The header's tri-state indicator is the same kind of
plain string (`StateGlyph`, one of "[x]" / "[ ]" / "[-]"), never a real
`IsThreeState` CheckBox bound to a nullable bool -- safer given there is
no working precedent for that binding shape under IronPython.

A click on the tri-state glyph or the fold chevron is caught by ONE
handler on the ListBox via `PreviewMouseLeftButtonDown`, walking up the
visual tree from `e.OriginalSource` for a `Tag` of "cat"/"chev" -- the
same walk-up-for-a-Tag idiom `lib/vtm.py`'s `_tag_of`/`_find_up` use for
its own chevrons and badges.

`on_check_changed` (bound to every row CheckBox's Checked/Unchecked, see
`rebuild()`'s own docstring) never calls `rebuild()`: doing that once
caused a real StackOverflow, reproducible offline with as little as one
row (found in review, 2026-09-25) -- reassigning ItemsSource regenerates
every container, which pushes each checked row's Keep into a brand new
CheckBox, which raises Checked again, landing back in the same handler.
It updates the footer count and repaints the ONE header whose tri-state
mark might be stale, in place (`_repaint_header`/`_find_tagged`), instead.
"""

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from System.Windows import UIElement, RoutedEventHandler
from System.Windows.Controls import ListBoxItem
from System.Windows.Controls.Primitives import ToggleButton
from System.Windows.Input import MouseButtonEventHandler
from System.Windows.Media import VisualTreeHelper
from System.Collections.Generic import List

from Autodesk.Revit.DB import Element, ElementId, FamilyInstance

from pyrevit import revit
from slantisui import ui
import modeless

TITLE = u"Selection Manager"


# ---------------------------------------------------------------------------
# Rows: one per (category, family, type) in the selection
# ---------------------------------------------------------------------------

def _name(element):
    """Element.Name is ambiguous on ElementType under IronPython."""
    try:
        return Element.Name.__get__(element) or u""
    except Exception:
        try:
            return element.Name or u""
        except Exception:
            return u""


def _describe(doc, element):
    """(category, family, type) of one element, always three strings."""
    cat = u"(no category)"
    try:
        if element.Category is not None:
            cat = element.Category.Name
    except Exception:
        pass
    fam, typ = u"", u""
    try:
        if isinstance(element, FamilyInstance):
            fam = _name(element.Symbol.Family)
            typ = _name(element.Symbol)
        else:
            tid = element.GetTypeId()
            if tid is not None and tid != ElementId.InvalidElementId:
                etype = doc.GetElement(tid)
                if etype is not None:
                    fam = etype.FamilyName or u""
                    typ = _name(etype)
    except Exception:
        pass
    if not fam and not typ:
        typ = _name(element) or u"(no type)"
    return cat, fam, typ


class Row(object):
    """Plain attributes, not properties: the ListBox binds to them the way
    other tools' rows are bound, the pattern known to work under
    IronPython. finish() fills the derived ones once the ids are in,
    including the fields the shared DataTemplate needs to show the ROW
    half and hide the HEADER half (see Header below)."""

    def __init__(self, cat, fam, typ):
        self.Category = cat
        self.Family = fam
        self.Type = typ
        self.ids = []
        self.Keep = True
        self.Display = u""
        self.Count = u""
        self.search_text = u""
        # Shared with Header through the one DataTemplate -- a header never
        # reads these, but the binding still evaluates them either way.
        self.HeaderVis = u"Collapsed"
        self.RowVis = u"Visible"
        self.StateGlyph = u""
        self.Chevron = u""
        self.Name = u""

    def finish(self):
        if self.Family and self.Type and self.Family != self.Type:
            self.Display = u"{0} : {1}".format(self.Family, self.Type)
        else:
            self.Display = self.Type or self.Family
        self.Count = u"{0}".format(len(self.ids))
        self.search_text = (self.Category + u" " + self.Family + u" "
                            + self.Type).lower()
        self.Name = self.Category
        return self


def _tri_glyph(tri):
    if tri is True:
        return u"[x]"
    if tri is False:
        return u"[ ]"
    return u"[-]"


_CHEV_OPEN = u"▾"   # small triangle down, expanded
_CHEV_SHUT = u"▸"   # small triangle right, collapsed


class Header(object):
    """One category's fold/tri-state header row, mixed into the same flat
    ItemsSource as Row objects (see _build_display). `.rows` is only the
    rows CURRENTLY VISIBLE under this header, i.e. post search filter --
    that is the scope both the header's own checkbox and All/None/Invert
    act on."""

    def __init__(self, category, rows, expanded):
        self.Category = category
        self.rows = rows
        self.Name = category
        self.Count = u"{0}".format(sum(len(r.ids) for r in rows))
        self.StateGlyph = _tri_glyph(self.tri_state())
        self.Chevron = _CHEV_OPEN if expanded else _CHEV_SHUT
        # Shared with Row through the one DataTemplate.
        self.HeaderVis = u"Visible"
        self.RowVis = u"Collapsed"
        self.Display = u""
        self.Keep = False

    def tri_state(self):
        vals = set(r.Keep for r in self.rows)
        if len(vals) == 1:
            return next(iter(vals))
        return None


def build_rows(doc, uidoc):
    """Rows for the selection of `uidoc`, sorted by category, family, type."""
    buckets = {}
    for eid in uidoc.Selection.GetElementIds():
        element = doc.GetElement(eid)
        if element is None:
            continue
        key = _describe(doc, element)
        row = buckets.get(key)
        if row is None:
            row = buckets[key] = Row(*key)
        row.ids.append(eid)
    rows = [row.finish() for row in buckets.values()]
    rows.sort(key=lambda r: (r.Category.lower(), r.Family.lower(),
                             r.Type.lower()))
    return rows


def _build_display(rows, query, expand_state):
    """Flat list mixing Header and Row items: the single source both the
    ListBox and the header/footer actions read from. Rows come in already
    sorted by category (build_rows), so a category change in the loop is
    exactly where one bucket ends and the next begins. A category with no
    row matching the filter is skipped entirely -- same as the old native
    grouping used to hide an empty group. `expand_state` is a plain dict,
    category name -> bool; a missing key means expanded (the default, so a
    category nobody folded yet still opens showing everything)."""
    query = (query or u"").strip().lower()
    display = []
    bucket = []
    current_cat = None

    def flush():
        if not bucket:
            return
        expanded = expand_state.get(current_cat, True)
        display.append(Header(current_cat, list(bucket), expanded))
        if expanded:
            display.extend(bucket)

    for row in rows:
        if query and query not in row.search_text:
            continue
        if row.Category != current_cat:
            flush()
            bucket = []
            current_cat = row.Category
        bucket.append(row)
    flush()
    return display


# ---------------------------------------------------------------------------
# XAML
# ---------------------------------------------------------------------------

_BODY = u"""
  <Grid>
    <Grid.Resources>
      <DataTemplate x:Key="RowTemplate">
        <Grid>
          <Border Visibility="{Binding HeaderVis}" Background="Transparent"
                  Padding="2,14,2,4">
            <DockPanel LastChildFill="True">
              <TextBlock DockPanel.Dock="Right" Text="{Binding Count}"
                         Foreground="#A6A199" FontSize="11"
                         VerticalAlignment="Center"/>
              <Border Tag="chev" Cursor="Hand" Padding="0,0,8,0">
                <TextBlock Text="{Binding Chevron}" Foreground="#77736C"
                           FontSize="11" VerticalAlignment="Center"/>
              </Border>
              <Border Tag="cat" Cursor="Hand" Padding="0,0,8,0">
                <TextBlock Tag="stateTxt" Text="{Binding StateGlyph}"
                           Foreground="#77736C" FontSize="12" Width="22"
                           TextAlignment="Center" VerticalAlignment="Center"/>
              </Border>
              <TextBlock Text="{Binding Name}" Foreground="#A6A199"
                         FontSize="10.5" FontWeight="Medium"
                         VerticalAlignment="Center"/>
            </DockPanel>
          </Border>

          <Border Visibility="{Binding RowVis}" Background="Transparent"
                  Padding="10,7" Margin="0,1,0,0">
            <DockPanel LastChildFill="True">
              <CheckBox Style="{StaticResource BrandCheck}"
                        IsChecked="{Binding Keep, Mode=TwoWay}"
                        VerticalAlignment="Center" Margin="22,0,10,0"/>
              <TextBlock DockPanel.Dock="Right" Text="{Binding Count}"
                         Foreground="#A6A199" FontSize="12" Margin="10,0,0,0"
                         VerticalAlignment="Center"/>
              <TextBlock Text="{Binding Display}" Foreground="#202022"
                         FontSize="13" VerticalAlignment="Center"
                         TextTrimming="CharacterEllipsis"/>
            </DockPanel>
          </Border>
        </Grid>
      </DataTemplate>

      <Style TargetType="ListBoxItem">
        <Setter Property="Padding" Value="0"/>
        <Setter Property="Background" Value="Transparent"/>
        <Setter Property="Template">
          <Setter.Value>
            <ControlTemplate TargetType="ListBoxItem">
              <Border x:Name="itemBorder" Background="{TemplateBinding Background}"
                      CornerRadius="6">
                <ContentPresenter/>
              </Border>
              <ControlTemplate.Triggers>
                <Trigger Property="IsMouseOver" Value="True">
                  <Setter TargetName="itemBorder" Property="Background" Value="__ROW_HOVER__"/>
                </Trigger>
              </ControlTemplate.Triggers>
            </ControlTemplate>
          </Setter.Value>
        </Setter>
      </Style>
    </Grid.Resources>

    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
    </Grid.RowDefinitions>

    <Grid Grid.Row="0" Margin="0,0,0,12">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="Auto"/>
        <ColumnDefinition Width="Auto"/>
      </Grid.ColumnDefinitions>
      <TextBox x:Name="txtFilter" Grid.Column="0" Height="34" Margin="0,0,8,0"
               ToolTip="Type to filter by category, family or type"/>
      <Button x:Name="btnExpandAll" Grid.Column="1" Content="__CHEV_OPEN__ all"
              Style="{StaticResource BtnGhost}" Margin="0,0,6,0"
              ToolTip="Expand every category"/>
      <Button x:Name="btnCollapseAll" Grid.Column="2" Content="__CHEV_SHUT__ all"
              Style="{StaticResource BtnGhost}"
              ToolTip="Collapse every category"/>
    </Grid>

    <Border Grid.Row="1" CornerRadius="8" Background="#FFFFFF"
            BorderBrush="#ECE9E4" BorderThickness="1" ClipToBounds="True">
      <ListBox x:Name="lstRows"
               ItemTemplate="{StaticResource RowTemplate}"
               ScrollViewer.VerticalScrollBarVisibility="Auto"
               SelectionMode="Single"
               Background="Transparent" BorderThickness="0" Padding="8"/>
    </Border>
  </Grid>
"""
# The row hover tint is the library's own token, injected here rather than
# written as hex: it follows a retone of the accent.
_BODY = _BODY.replace(u"__ROW_HOVER__", ui.ROW_HOVER)
_BODY = _BODY.replace(u"__CHEV_OPEN__", _CHEV_OPEN)
_BODY = _BODY.replace(u"__CHEV_SHUT__", _CHEV_SHUT)

_FOOTER = u"""
  <Grid>
    <TextBlock x:Name="lblCount" VerticalAlignment="Center" Foreground="#A6A199" FontSize="11.5"/>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnRefresh" Content="Refresh" Style="{StaticResource BtnGhost}"   Margin="0,0,8,0" ToolTip="Re-read what is selected in Revit now"/>
      <Button x:Name="btnAll"     Content="All"     Style="{StaticResource BtnGhost}"   Margin="0,0,8,0" ToolTip="Check every row the filter shows"/>
      <Button x:Name="btnNone"    Content="None"    Style="{StaticResource BtnGhost}"   Margin="0,0,8,0" ToolTip="Uncheck every row the filter shows"/>
      <Button x:Name="btnInvert"  Content="Invert"  Style="{StaticResource BtnGhost}"   Margin="0,0,8,0" ToolTip="Invert every row the filter shows"/>
      <Button x:Name="btnOK"      Content="Apply"   Style="{StaticResource BtnPrimary}" Margin="0,0,8,0" ToolTip="Select only the checked rows"/>
      <Button x:Name="btnCancel"  Content="Close"   Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""


# ---------------------------------------------------------------------------
# Visual-tree helper -- same walk-up-for-a-Tag idiom as lib/vtm.py's
# _tag_of/_find_up, used to catch a click landing inside a shared DataTemplate.
# ---------------------------------------------------------------------------

def _tag_hit(src):
    """Walk up from `src` (a click's OriginalSource) for a Border tagged
    "chev" or "cat"; stop at the ListBoxItem, whatever tag it carries (a
    click elsewhere in the row is not ours). Returns (tag, DataContext) or
    (None, None)."""
    node = src
    while node is not None:
        tag = getattr(node, "Tag", None)
        if tag in (u"chev", u"cat"):
            return tag, getattr(node, "DataContext", None)
        if isinstance(node, ListBoxItem):
            return None, None
        try:
            node = VisualTreeHelper.GetParent(node)
        except Exception:
            return None, None
    return None, None


def _find_tagged(root, tag):
    """First visual DESCENDANT of `root` whose Tag == `tag`, or None. Same
    family as `_tag_hit` (walk the visual tree by Tag), the other
    direction: down from a container instead of up from a click."""
    if root is None:
        return None
    if getattr(root, "Tag", None) == tag:
        return root
    try:
        count = VisualTreeHelper.GetChildrenCount(root)
    except Exception:
        return None
    for i in range(count):
        found = _find_tagged(VisualTreeHelper.GetChild(root, i), tag)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

def open_window():
    """Show the window, or bring the live one to front (the shape every
    modeless Magic Tools window follows, see lib/modeless.py and
    lib/vtm.py's own open_window).

    Reads `revit.doc`/`revit.uidoc` directly: safe here because this is
    the very first thing the door's click does, before any Transaction --
    it is ONLY unsafe to read `revit.doc` deep inside a function AFTER a
    Transaction already committed in this same run, when the builtin
    `__revit__` has gone stale (it goes to None after the first Transaction
    commits on the shared engine).
    """
    if modeless.focus(TITLE):
        return

    doc = revit.doc
    uidoc = revit.uidoc
    if doc is None:
        ui.alert(u"Open a project document first.", title=TITLE)
        return

    # The first read happens in the door's click, a valid API context;
    # every later one goes through modeless.run.
    rows = build_rows(doc, uidoc)

    win = ui.parse(
        TITLE,
        u"Carve the selection down to the type: uncheck what goes, Apply",
        _BODY, _FOOTER,
        width=520, height=620,
        context=u"Unchecked rows stay here as a pool: check them again and Apply "
                u"to bring them back. Double click a row to keep only that one "
                u"within its category. The [x]/[ ]/[-] mark by a category name "
                u"ticks or clears the whole category; the arrow next to it folds "
                u"the category away.",
    )

    txt_filter = win.FindName("txtFilter")
    lst_rows = win.FindName("lstRows")
    lbl_count = win.FindName("lblCount")
    btn_expand_all = win.FindName("btnExpandAll")
    btn_collapse_all = win.FindName("btnCollapseAll")
    btn_refresh = win.FindName("btnRefresh")
    btn_all = win.FindName("btnAll")
    btn_none = win.FindName("btnNone")
    btn_invert = win.FindName("btnInvert")
    btn_ok = win.FindName("btnOK")
    btn_cancel = win.FindName("btnCancel")

    # `expand`: category name -> bool, missing key means expanded (default).
    state = {'rows': rows, 'expand': {}}

    def update_count():
        all_rows = state['rows']
        kept = sum(len(r.ids) for r in all_rows if r.Keep)
        total = sum(len(r.ids) for r in all_rows)
        if not all_rows:
            lbl_count.Text = u"Nothing selected in Revit. Select something and Refresh."
        else:
            lbl_count.Text = u"{0} of {1} elements kept, {2} rows".format(
                kept, total, len(all_rows))

    def query_text():
        return (txt_filter.Text or u'').strip().lower()

    def visible_rows():
        """Rows the filter box currently shows: with an empty box that is
        every row, with something typed only the rows that match -- the
        exact scope All/None/Invert (and each header's own checkbox) act
        on (QA bug 2: they used to act on the whole selection even with a
        filter hiding most of it)."""
        query = query_text()
        if not query:
            return list(state['rows'])
        return [r for r in state['rows'] if query in r.search_text]

    def rebuild():
        """Single choke point: recompute the flat Header+Row list from
        state['rows'] + the filter text + the fold state, and rebind. Every
        handler below ends in this, so the header tri-state/chevron and the
        footer count are never stale."""
        display = _build_display(state['rows'], txt_filter.Text, state['expand'])
        lst_rows.ItemsSource = display
        update_count()

    def on_filter_changed(sender, e):
        rebuild()

    def _repaint_header(category):
        """Recompute one header's tri-state and push the new glyph onto
        its live TextBlock, without touching ItemsSource. Used after a
        single row's Keep flips (see on_check_changed for why rebuild()
        cannot be that path's ending)."""
        for item in lst_rows.Items:
            if isinstance(item, Header) and item.Category == category:
                item.StateGlyph = _tri_glyph(item.tri_state())
                container = lst_rows.ItemContainerGenerator.ContainerFromItem(item)
                if container is not None:
                    txt = _find_tagged(container, u"stateTxt")
                    if txt is not None:
                        txt.Text = item.StateGlyph
                # Virtualized out of view: the object itself now carries the
                # right glyph, so whenever it scrolls back in (or the next
                # rebuild() runs) the binding reads the fresh value.
                return

    def on_check_changed(sender, e):
        # The checkbox binding already wrote Keep. Do NOT call rebuild()
        # here: reassigning ItemsSource regenerates every container, which
        # pushes each checked row's Keep into a BRAND NEW CheckBox via the
        # binding, which raises Checked again, landing right back here --
        # unbounded recursion, a real StackOverflow with as few as one row
        # (found in review; reproduced offline, never with zero
        # rows -- no CheckBox, no event to loop on). Only two things can be
        # stale after ONE row's Keep flips: the footer count and the
        # tri-state mark of that row's own category header. Update both in
        # place; a full rebuild() here would also reset the scroll position
        # on every single click.
        update_count()
        row = getattr(e.OriginalSource, "DataContext", None)
        if isinstance(row, Row):
            _repaint_header(row.Category)

    def on_all(sender, e):
        for r in visible_rows():
            r.Keep = True
        rebuild()

    def on_none(sender, e):
        for r in visible_rows():
            r.Keep = False
        rebuild()

    def on_invert(sender, e):
        for r in visible_rows():
            r.Keep = not r.Keep
        rebuild()

    def on_double_click(sender, e):
        """Keep only the row under the mouse, among the rows of ITS OWN
        category: the fast way to isolate one type without losing the rest
        of the selection (QA improvement 3: it used to uncheck every other
        category too, not just the sibling rows of the one clicked)."""
        row = lst_rows.SelectedItem
        if not isinstance(row, Row):
            return
        for r in state['rows']:
            if r.Category == row.Category:
                r.Keep = (r is row)
        rebuild()

    def on_list_mouse_down(sender, e):
        tag, header = _tag_hit(e.OriginalSource)
        if tag is None or not isinstance(header, Header):
            return
        e.Handled = True
        if tag == u"chev":
            state['expand'][header.Category] = not state['expand'].get(
                header.Category, True)
        else:  # "cat" -- the tri-state glyph: click resolves to fully
            # checked unless it is already fully checked, then it resolves
            # to fully unchecked (never lands on "mixed") -- only touching
            # the rows the filter currently shows under that header.
            newval = not (header.tri_state() is True)
            for r in header.rows:
                r.Keep = newval
        rebuild()

    def on_expand_all(sender, e):
        for r in state['rows']:
            state['expand'][r.Category] = True
        rebuild()

    def on_collapse_all(sender, e):
        for r in state['rows']:
            state['expand'][r.Category] = False
        rebuild()

    def on_refresh(sender, e):
        def work(uiapp):
            # Reading the selection and the elements is API work: inside
            # the ExternalEvent, with the UIDocument Revit hands us.
            state['rows'] = build_rows(doc, uiapp.ActiveUIDocument)
            rebuild()
        modeless.run(work, doc=doc, title=TITLE)

    def on_ok(sender, e):
        keep = []
        for r in state['rows']:
            if r.Keep:
                keep.extend(r.ids)
        ids = List[ElementId](keep)

        def work(uiapp):
            # The window stays open: the pool of rows is kept, so an
            # unchecked type can be checked again and applied back.
            uiapp.ActiveUIDocument.Selection.SetElementIds(ids)
            update_count()

        modeless.run(work, doc=doc, title=TITLE)

    def on_cancel(sender, e):
        win.Close()

    txt_filter.TextChanged += on_filter_changed
    lst_rows.MouseDoubleClick += on_double_click
    # Checked/Unchecked bubble from a row's CheckBox up to the list: one
    # handler on the list keeps the footer count and that row's header
    # mark live, in place (see on_check_changed for why it never rebuilds).
    lst_rows.AddHandler(ToggleButton.CheckedEvent, RoutedEventHandler(on_check_changed))
    lst_rows.AddHandler(ToggleButton.UncheckedEvent, RoutedEventHandler(on_check_changed))
    lst_rows.AddHandler(UIElement.PreviewMouseLeftButtonDownEvent,
                        MouseButtonEventHandler(on_list_mouse_down), True)
    btn_expand_all.Click += on_expand_all
    btn_collapse_all.Click += on_collapse_all
    btn_refresh.Click += on_refresh
    btn_all.Click += on_all
    btn_none.Click += on_none
    btn_invert.Click += on_invert
    btn_ok.Click += on_ok
    btn_cancel.Click += on_cancel

    rebuild()
    modeless.show(win, TITLE, doc=doc)
    txt_filter.Focus()
