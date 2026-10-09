# -*- coding: utf-8 -*-
__title__ = "Create\nType Filter"
__author__ = 'slantis'
__doc__ = "Creates a new Parameter Filter from the Type Name of the selected elements and applies it with graphic overrides, all in one window: name, targets (the active view and every view template, with a note on where each one actually sends the filter) and the overrides editor side by side. Ticks nothing by default -- pick your targets, set the overrides, Create and apply."

import clr

clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from Autodesk.Revit.DB import (
    FilteredElementCollector,
    ParameterFilterElement,
    ParameterFilterUtilities,
    ElementParameterFilter,
    ElementFilter,
    FilterRule,
    FilterCategoryRule,
    LogicalAndFilter,
    LogicalOrFilter,
    FilterStringRule,
    ParameterValueProvider,
    FilterStringEquals,
    BuiltInParameter,
    Transaction,
    ElementId,
    View,
    ViewType,
    OverrideGraphicSettings
)

from pyrevit import revit
from System.Collections.Generic import List, HashSet
from System.Collections.ObjectModel import ObservableCollection
from System.ComponentModel import INotifyPropertyChanged, PropertyChangedEventArgs
from System.Windows import Visibility, RoutedEventHandler
from System.Windows.Media import SolidColorBrush, ColorConverter
from System.Windows.Controls import CheckBox as _CheckBox

from slantisui import ui
import vgrow
import usage

with usage.tool_run(__file__) as run:

    doc = revit.doc
    uidoc = revit.uidoc


    # =========================================================
    # GET SELECTED ELEMENTS
    # =========================================================

    def _id_val(eid):
        # ElementId value compat Revit 2022-2026 (IntegerValue removed in 2026)
        try:
            return eid.Value
        except AttributeError:
            return eid.IntegerValue


    selection_ids = uidoc.Selection.GetElementIds()

    if not selection_ids:
        ui.alert("Please select one or more elements before running this tool.", title="Create Type Filter")
        raise SystemExit

    elements = [doc.GetElement(eid) for eid in selection_ids]


    # =========================================================
    # SPLIT SELECTION: what can actually be filtered (QA 2026-09-24, bug 2)
    # =========================================================
    # ParameterFilterElement only ever applies to a fixed set of categories --
    # title blocks are never in it -- and even a filterable category needs an
    # element that actually HAS a type (a Room's GetTypeId() is always
    # invalid). Both used to surface only at "Create and apply", as a bare
    # "...Parameter name: elementFilter" exception with nothing created.
    # Sorting it out before the window opens lets the tool proceed with
    # whatever IS filterable and say plainly what got left out and why --
    # same as Revit's own Filters dialog only ever offering filterable
    # categories in its category picker.

    try:
        _ALL_FILTERABLE_CATS = set(
            _id_val(cid) for cid in ParameterFilterUtilities.GetAllFilterableCategories())
    except Exception:
        _ALL_FILTERABLE_CATS = None  # could not ask Revit -- do not silently trust everything

    included = []                # (element, category, type_element, type_name)
    excluded_no_type = {}         # category name -> count (filterable category, element has no type)
    excluded_uncategorized = 0
    excluded_not_filterable = {}  # category name -> count (Revit never filters this category)

    for el in elements:
        cat = el.Category
        if not cat:
            excluded_uncategorized += 1
            continue

        cat_name = cat.Name
        if _ALL_FILTERABLE_CATS is not None and _id_val(cat.Id) not in _ALL_FILTERABLE_CATS:
            excluded_not_filterable[cat_name] = excluded_not_filterable.get(cat_name, 0) + 1
            continue

        type_id = el.GetTypeId()
        type_el = doc.GetElement(type_id) if type_id and type_id != ElementId.InvalidElementId else None
        tname = None
        if type_el:
            p = type_el.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
            tname = p.AsString() if p else None

        if not tname:
            excluded_no_type[cat_name] = excluded_no_type.get(cat_name, 0) + 1
            continue

        included.append((el, cat, type_el, tname))

    if not included:
        ui.alert(
            u"Nothing here can be filtered.\n\n"
            u"The selection has no filterable types: rooms, title blocks and a "
            u"few other categories are never part of a Parameter Filter, and an "
            u"element with no type (like a Room) has nothing to match on. "
            u"Select doors, walls, windows or other typed elements and try "
            u"again.",
            title="Create Type Filter", width=520)
        raise SystemExit


    # =========================================================
    # COLLECT CATEGORIES + TYPE NAMES FROM WHAT'S FILTERABLE
    # =========================================================

    category_ids = set()
    category_names = set()
    type_names = set()
    _cat_id_by_val = {}

    for _el, _cat, _type_el, _tname in included:
        category_ids.add(_cat.Id)
        _cat_id_by_val[_id_val(_cat.Id)] = _cat.Id
        try:
            category_names.add(_cat.Name)
        except Exception:
            pass
        type_names.add(_tname)

    cat_ids = List[ElementId]()
    for cid in category_ids:
        cat_ids.Add(cid)


    # =========================================================
    # PROPOSE A FILTER NAME + THE "DETECTED" BLURB
    # =========================================================
    # Feedback, 2026-09-19: cut the number of steps if possible, tools that show
    # everything at once are preferred -- the name field starts filled instead of
    # empty, and the picker/briefing/dialog hops collapse into one window.
    # Duplicate-name validation still happens at Apply, same as before; this is
    # only a starting guess.

    _sorted_types = sorted(type_names)
    if len(_sorted_types) == 1:
        proposed_name = _sorted_types[0]
    else:
        proposed_name = u"{} +{}".format(_sorted_types[0], len(_sorted_types) - 1)

    detected_text = u"{} type{} in {}: {}".format(
        len(_sorted_types), u"" if len(_sorted_types) == 1 else u"s",
        u", ".join(sorted(category_names)), u", ".join(_sorted_types)
    )

    def _singular_category(name):
        # Revit category names are plural ("Rooms", "Assemblies"); with a count of
        # 1 they should read "1 Room", "1 Assembly". Names that do not end in "s"
        # (Casework) and the "ss" ones (Mass) are left as they are.
        if name.endswith(u"ies") and len(name) > 3:
            return name[:-3] + u"y"
        if name.endswith(u"s") and not name.endswith(u"ss"):
            return name[:-1]
        return name


    def _count_category(n, name):
        return u"{} {}".format(n, _singular_category(name) if n == 1 else name)


    _excluded_notes = []
    for _cat_name, _n in sorted(excluded_no_type.items()):
        _excluded_notes.append(
            u"⚠ Left out: {} (no type)".format(_count_category(_n, _cat_name)))
    for _cat_name, _n in sorted(excluded_not_filterable.items()):
        _excluded_notes.append(
            u"⚠ Left out: {} (Revit does not filter this category)"
            .format(_count_category(_n, _cat_name)))
    if excluded_uncategorized:
        _excluded_notes.append(
            u"⚠ Left out: {} element{} with no category"
            .format(excluded_uncategorized, u"" if excluded_uncategorized == 1 else u"s"))
    # The "Left out" lines go in their own warning-coloured block (lblLeftOut),
    # not appended to the grey DETECTED text, so they are not missed.
    left_out_text = u"\n".join(_excluded_notes)

    existing_names = set(
        f.Name for f in FilteredElementCollector(doc).OfClass(ParameterFilterElement)
    )


    # =========================================================
    # BUILD (FAMILY + TYPE) RULES (OR) -- pure, no model writes
    # =========================================================
    # QA 2026-09-24 (bug 1): the old version tested category and type name as
    # two INDEPENDENT axes -- category in {Doors, Walls} AND name in
    # {MTT-A, MTT-B} -- so a door named MTT-B or a wall named MTT-A matched
    # too, and any type name repeated across families (a common one:
    # "Default") crossed the same way. The fix pairs them: only
    # (this family, this type name) OR (that family, that type name) --
    # never the cross product.
    #
    # "Family" is BuiltInParameter.SYMBOL_FAMILY_NAME_PARAM -- what the Type
    # Properties dialog's "Family" field reads, for loaded AND system
    # families alike (a WallType answers "Basic Wall" there). Whether it is
    # actually filterable for the categories in THIS selection is asked of
    # Revit itself (ParameterFilterUtilities.GetFilterableParametersInCommon)
    # instead of assumed, per the gotcha this design flagged: SetElementFilter
    # throws if a parameter is not filterable for every category involved.
    # When it is not filterable in common (not verified live for every
    # category combination), the pair falls back to (this element's category,
    # this type name) -- still safe against the cross-category bug, though a
    # type name shared by two different families WITHIN one category could
    # still slip through in that fallback; unverified.

    def _string_rule(provider, evaluator, value):
        # FilterStringRule(provider, evaluator, value) -- the 3-arg ctor -- was
        # measured against the 2026 RevitAPI.xml/DLL to exist already in Revit
        # 2022 (the suspicion of a 2023+-only ctor was not it). Kept as a safety
        # net, not the fix: if some future/older Revit ever lacks it, this falls
        # back to the 4-arg ctor with the trailing caseSensitive bool instead of
        # throwing unguarded.
        try:
            return FilterStringRule(provider, evaluator, value)
        except Exception:
            return FilterStringRule(provider, evaluator, value, True)


    _TYPE_NAME_PID = ElementId(BuiltInParameter.SYMBOL_NAME_PARAM)
    _FAMILY_NAME_PID = ElementId(BuiltInParameter.SYMBOL_FAMILY_NAME_PARAM)

    try:
        _common_filterable = set(
            _id_val(pid) for pid in
            ParameterFilterUtilities.GetFilterableParametersInCommon(doc, cat_ids))
        _use_family_pairing = _id_val(_FAMILY_NAME_PID) in _common_filterable
    except Exception:
        _use_family_pairing = False

    _type_provider = ParameterValueProvider(_TYPE_NAME_PID)
    _family_provider = ParameterValueProvider(_FAMILY_NAME_PID) if _use_family_pairing else None

    pairs = set()   # (category_id_value, family_name or None, type_name)
    for _el, _cat, _type_el, _tname in included:
        fam_name = None
        if _use_family_pairing:
            try:
                fam_name = _type_el.FamilyName
            except Exception:
                fam_name = None
        pairs.add((_id_val(_cat.Id), fam_name, _tname))

    # Code review, 2026-09-25 (measured against RevitAPI.xml +
    # ParameterFilterElement.ElementFilterIsAcceptableForParameterFilterElement):
    # an ElementCategoryFilter is NOT accepted inside the filter a
    # ParameterFilterElement carries -- only ElementParameterFilter, or logical
    # combinations of ElementParameterFilters, pass. The documented way to say
    # "this ONE category AND this value" is a SINGLE ElementParameterFilter
    # built from an ordered 2-rule list -- a FilterCategoryRule for exactly one
    # category, then the value rule -- and that shape is only valid when its
    # parent is a LogicalOrFilter (i.e. more than one pair in play). For the
    # single-pair case there is no parent LogicalOrFilter, and cat_ids (passed
    # separately to ParameterFilterElement.Create) already scopes the whole
    # filter to that one category, so the category rule would be both invalid
    # there and redundant -- the value rule alone is enough.
    _single_pair = len(pairs) == 1

    per_pair_filters = []
    for _cat_id_val, _fam_name, _tname in pairs:
        name_rule = _string_rule(_type_provider, FilterStringEquals(), _tname)
        if _fam_name is not None:
            fam_rule = _string_rule(_family_provider, FilterStringEquals(), _fam_name)
            per_pair_filters.append(LogicalAndFilter(
                ElementParameterFilter(fam_rule), ElementParameterFilter(name_rule)))
        elif _single_pair:
            per_pair_filters.append(ElementParameterFilter(name_rule))
        else:
            cat_rule_list = List[FilterRule]()
            cat_rule_list.Add(FilterCategoryRule(List[ElementId]([_cat_id_by_val[_cat_id_val]])))
            cat_rule_list.Add(name_rule)
            per_pair_filters.append(ElementParameterFilter(cat_rule_list))

    if len(per_pair_filters) == 1:
        element_filter = per_pair_filters[0]
    else:
        ef_list = List[ElementFilter]()
        for f in per_pair_filters:
            ef_list.Add(f)
        element_filter = LogicalOrFilter(ef_list)


    # =========================================================
    # COLLECT VIEW TEMPLATES (exclude unsupported types)
    # =========================================================

    UNSUPPORTED_VIEW_TYPES = {
        ViewType.Schedule,
        ViewType.DraftingView,
        ViewType.Legend,
        ViewType.ProjectBrowser,
        ViewType.SystemBrowser,
        ViewType.Undefined,
    }

    templates = [
        v for v in FilteredElementCollector(doc).OfClass(View)
        if v.IsTemplate and v.ViewType not in UNSUPPORTED_VIEW_TYPES
    ]
    template_map = {v.Name: v for v in templates}

    # If active view has a template, bubble it to top
    _active_vt_id = doc.ActiveView.ViewTemplateId
    _active_template = None
    _current_name = None
    if _active_vt_id != ElementId.InvalidElementId:
        _active_template = doc.GetElement(_active_vt_id)
        if _active_template and _active_template.Name in template_map:
            _current_name = _active_template.Name

    _sorted_template_names = sorted(template_map.keys())
    if _current_name:
        _sorted_template_names.remove(_current_name)
        _sorted_template_names.insert(0, _current_name)

    _active_view_available = bool(
        doc.ActiveView and doc.ActiveView.ViewType not in UNSUPPORTED_VIEW_TYPES)


    # =========================================================
    # WHERE THE ACTIVE VIEW WOULD REALLY PUT THE FILTER
    # =========================================================
    # Ticking the active view is the natural move, but where the filter LANDS is
    # decided by that view's template:
    #   - no template, or a template that does not control Filters -> the filter
    #     sits on the VIEW. It paints there and nowhere else, and it never shows
    #     up in any template's filter list.
    #   - a template that DOES control Filters -> the view cannot hold filters of
    #     its own, so the only place the filter can go is the template, and from
    #     there it paints every view using it.
    # Both were silent before 2026-09-19 ("the filter paints but the template
    # does not list it"). Reported as a one-line Note on the row now, instead of
    # a ui.confirm() briefing the user had to read through a whole extra stop.
    # (`_id_val` is defined once, up in the GET SELECTED ELEMENTS section.)

    _FILTERS_PARAM = _id_val(ElementId(BuiltInParameter.VIS_GRAPHICS_FILTERS))


    def _controls_filters(template):
        """Whether this view template drives the Filters of the views using it.

Returns True, False, or None when Revit would not answer. None must
never be read as False: "not controlled" is exactly what sends the
filter to the view, so a silent guess there rebuilds this bug from the
other side.
"""
        if template is None:
            return False
        try:
            loose = set(_id_val(pid) for pid
                        in template.GetNonControlledTemplateParameterIds())
        except Exception:
            return None
        return _FILTERS_PARAM not in loose


    _ACTIVE_VIEW_CONTROL = _controls_filters(_active_template)
    _ACTIVE_VIEW_GOVERNED = _ACTIVE_VIEW_CONTROL is True

    if not _active_view_available:
        _active_note = u""
        _active_effective_target = None
    elif _active_template is None:
        _active_note = u"No template: paints only in this view"
        _active_effective_target = doc.ActiveView
    elif _ACTIVE_VIEW_GOVERNED:
        _active_note = (
            u"Goes to template \"{}\" (it controls this view's Filters)"
            .format(_active_template.Name))
        _active_effective_target = _active_template
    elif _ACTIVE_VIEW_CONTROL is None:
        _active_note = (
            u"Template \"{}\": Revit would not confirm it controls Filters, "
            u"so this is sent to the VIEW".format(_active_template.Name))
        _active_effective_target = doc.ActiveView
    else:
        _active_note = (
            u"Paints only in this view; template \"{}\" will not list it"
            .format(_active_template.Name))
        _active_effective_target = doc.ActiveView


    # =========================================================
    # TARGET ROWS -- one DataGrid, active view first, then templates
    # =========================================================

    class TargetRow(INotifyPropertyChanged):
        """One row of the "Apply to" grid: a checkbox, a name and an optional
Note (only the active view row carries one -- see `_active_note` above).
`target` is the element that will actually get the filter: for the
active view row it may already be its template (`_active_effective_
target`), never the raw `doc.ActiveView` when the template governs it.
"""

        def __init__(self, target, name, note):
            self.target = target
            self.Name = name
            self.Note = note
            self.NoteVis = Visibility.Visible if note else Visibility.Collapsed
            self._checked = False
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
        def Checked(self):
            return self._checked

        @Checked.setter
        def Checked(self, value):
            self._checked = bool(value)
            self._raise("Checked")


    all_rows = []
    if _active_view_available:
        all_rows.append(TargetRow(
            _active_effective_target,
            u"★ Active View: {}".format(doc.ActiveView.Name),
            _active_note))
    for _name in _sorted_template_names:
        # When the active view's template governs its Filters, the "Active View"
        # row above already IS that template (reviewer, 2026-09-19): listing it
        # again would offer two ticks for one destination.
        if _ACTIVE_VIEW_GOVERNED and _active_view_available and _name == _current_name:
            continue
        all_rows.append(TargetRow(template_map[_name], _name, u""))


    # =========================================================
    # THE WINDOW -- name + targets + overrides editor, one stop
    # =========================================================
    # Used to be five stops (ask_for_string, pick_list, confirm briefing, the
    # overrides dialog, a closing alert). Feedback, 2026-09-19: tools that show
    # everything at once are preferred -- collapsed to two:
    # this window, then the summary alert. The overrides editor is lib/vgrow.py's
    # `editor_xaml()`/`attach()` pasted straight into this body,
    # instead of opening `show_edit_dialog` as a second modal on top of this one.

    _BODY_XAML = u"""
  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
    </Grid.RowDefinitions>

    <!-- TOP: filter name + what was detected in the selection -->
    <Grid Grid.Row="0" Margin="0,0,0,12">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="280"/>
        <ColumnDefinition Width="20"/>
        <ColumnDefinition Width="*"/>
      </Grid.ColumnDefinitions>
      <StackPanel Grid.Column="0">
        <TextBlock Text="FILTER NAME" Style="{StaticResource SectionHead}"/>
        <TextBox x:Name="txtName" Height="32"/>
      </StackPanel>
      <StackPanel Grid.Column="2" VerticalAlignment="Bottom">
        <TextBlock Text="DETECTED" Style="{StaticResource SectionHead}"/>
        <TextBlock x:Name="lblDetected" TextWrapping="Wrap" FontSize="11.5"
                   Foreground="#77736C"/>
        <TextBlock x:Name="lblLeftOut" TextWrapping="Wrap" FontSize="11.5"
                   FontWeight="SemiBold" Margin="0,6,0,0"
                   Visibility="Collapsed"/>
      </StackPanel>
    </Grid>

    <!-- MAIN: targets (left) + overrides editor (right) -->
    <Grid Grid.Row="1">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="360" MinWidth="260"/>
        <ColumnDefinition Width="14"/>
        <ColumnDefinition Width="*" MinWidth="420"/>
      </Grid.ColumnDefinitions>
      <GridSplitter Grid.Column="1" Width="4" HorizontalAlignment="Center"
                    VerticalAlignment="Stretch" Background="#ECE9E4"
                    ResizeBehavior="PreviousAndNext" ResizeDirection="Columns"
                    Cursor="SizeWE" Focusable="False"/>

      <!-- LEFT: targets -->
      <Grid Grid.Column="0">
        <Grid.RowDefinitions>
          <RowDefinition Height="Auto"/>
          <RowDefinition Height="Auto"/>
          <RowDefinition Height="*"/>
        </Grid.RowDefinitions>
        <TextBlock Grid.Row="0" Text="APPLY TO" Style="{StaticResource SectionHead}"/>
        <TextBox x:Name="txtTargetFilter" Grid.Row="1" Height="30" Margin="0,0,0,8"
                 ToolTip="Type to filter view templates"/>
        <Border Grid.Row="2" CornerRadius="8" Background="#FFFFFF"
                BorderBrush="#ECE9E4" BorderThickness="1" ClipToBounds="True">
          <DataGrid x:Name="gridTargets" AutoGenerateColumns="False" HeadersVisibility="None"
                    SelectionMode="Single" CanUserAddRows="False" CanUserDeleteRows="False"
                    CanUserSortColumns="False" CanUserResizeRows="False"
                    CanUserResizeColumns="False">
            <DataGrid.Columns>
              <!-- 44, not 36: the cell style pads 12 px each side (see
                   vtm.py's own note on its checkbox+chevron column), and a
                   36-wide column left the 15 px box with no room, half a
                   checkbox, noticed the moment this window first opened
                   in Revit. 44 is the same width ui.pick_list's own
                   checkbox-only column (_PICK_CHECKCOL) already uses. -->
              <DataGridTemplateColumn Width="44">
                <DataGridTemplateColumn.CellTemplate>
                  <DataTemplate>
                    <CheckBox Style="{StaticResource BrandCheck}" IsThreeState="False"
                              IsChecked="{Binding Checked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                              HorizontalAlignment="Center" VerticalAlignment="Center"/>
                  </DataTemplate>
                </DataGridTemplateColumn.CellTemplate>
              </DataGridTemplateColumn>
              <DataGridTemplateColumn Width="*">
                <DataGridTemplateColumn.CellTemplate>
                  <DataTemplate>
                    <StackPanel Margin="0,5">
                      <TextBlock Text="{Binding Name}" FontSize="12.5" Foreground="#202022"
                                 TextTrimming="CharacterEllipsis"/>
                      <TextBlock Text="{Binding Note}" FontSize="10.5" Foreground="#A6A199"
                                 TextWrapping="Wrap" Margin="0,2,0,0"
                                 Visibility="{Binding NoteVis}"/>
                    </StackPanel>
                  </DataTemplate>
                </DataGridTemplateColumn.CellTemplate>
              </DataGridTemplateColumn>
            </DataGrid.Columns>
          </DataGrid>
        </Border>
      </Grid>

      <!-- RIGHT: overrides editor, lib/vgrow.py -->
      <Grid Grid.Column="2">
        <Grid.RowDefinitions>
          <RowDefinition Height="Auto"/>
          <RowDefinition Height="*"/>
        </Grid.RowDefinitions>
        <TextBlock Grid.Row="0" Text="OVERRIDES" Style="{StaticResource SectionHead}"/>
        <Grid Grid.Row="1">
          <!-- EDITOR -->
        </Grid>
      </Grid>
    </Grid>
  </Grid>
"""

    _FOOTER_XAML = u"""
  <Grid>
    <TextBlock x:Name="lblCount" VerticalAlignment="Center" Foreground="#A6A199" FontSize="11.5"/>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnApply" Content="Create and apply" Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
      <Button x:Name="btnCancel" Content="Cancel" Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""

    _body = _BODY_XAML.replace(u"<!-- EDITOR -->", vgrow.editor_xaml(extra_visibility=True))

    win = ui.parse(
        u"Create Type Filter",
        u"Name it, pick where it lands, set the overrides",
        _body, _FOOTER_XAML,
        width=980, height=720,
        context=(
            u"Name the filter, tick the views or view templates it should "
            u"apply to, and set the overrides it paints with. Ticking a view "
            u"template applies the filter to every view using that template; "
            u"ticking the active view only affects that one view, unless its "
            u"template already controls Filters, in which case the tick goes "
            u"to the template instead (see the Note on that row)."
        )
    )

    txt_name        = win.FindName("txtName")
    lbl_detected    = win.FindName("lblDetected")
    lbl_left_out    = win.FindName("lblLeftOut")
    txt_tgt_filter  = win.FindName("txtTargetFilter")
    grid_targets    = win.FindName("gridTargets")
    lbl_count       = win.FindName("lblCount")
    btn_apply       = win.FindName("btnApply")
    btn_cancel      = win.FindName("btnCancel")

    txt_name.Text = proposed_name
    txt_name.SelectAll()
    lbl_detected.Text = detected_text
    if left_out_text:
        # Warning token from the lib, never a hex literal (resolved per theme).
        lbl_left_out.Foreground = SolidColorBrush(
            ColorConverter.ConvertFromString(ui.STATUS_WARN))
        lbl_left_out.Text = left_out_text
        lbl_left_out.Visibility = Visibility.Visible

    # The filter does not exist yet, so the editor starts from a blank OGS.
    _blank = OverrideGraphicSettings()
    editor = vgrow.attach(win, prefill_ogs=_blank, extra_visibility=True, doc=doc)


    def _update_count():
        n = sum(1 for r in all_rows if r.Checked)
        lbl_count.Text = u"{} target{}".format(n, u"" if n == 1 else u"s")


    def _rebind_targets():
        q = (txt_tgt_filter.Text or u"").strip().lower()
        shown = [r for r in all_rows if not q or q in r.Name.lower()]
        col = ObservableCollection[object]()
        for r in shown:
            col.Add(r)
        grid_targets.ItemsSource = col
        _update_count()


    def on_check(s, e):
        _update_count()


    def on_target_filter_typed(s, e):
        _rebind_targets()


    def on_cancel(s, e):
        win.Close()


    result = {"ok": False, "summary": u""}


    def on_apply(s, e):
        name = (txt_name.Text or u"").strip()
        if not name:
            ui.alert(u"Enter a name for the filter.", title="Create Type Filter")
            return
        if name in existing_names:
            ui.alert(u"A filter with this name already exists.", title="Create Type Filter")
            return

        ticked = [r for r in all_rows if r.Checked]
        if not ticked:
            ui.alert(u"Tick at least one view or view template to apply the filter to.",
                     title="Create Type Filter")
            return

        res = editor.build()
        if res is None:
            return

        targets = []
        seen = set()
        for r in ticked:
            tid = _id_val(r.target.Id)
            if tid in seen:
                continue
            seen.add(tid)
            targets.append(r.target)

        # Code review, 2026-09-25: ask Revit itself whether this exact
        # element_filter is one ParameterFilterElement.Create/SetElementFilter
        # would accept for these categories, instead of finding out from a bare
        # exception after the Transaction already started.
        try:
            _filter_acceptable = ParameterFilterElement.ElementFilterIsAcceptableForParameterFilterElement(
                doc, HashSet[ElementId](cat_ids), element_filter)
        except Exception:
            _filter_acceptable = None  # could not ask Revit -- do not block on an unverifiable check

        if _filter_acceptable is False:
            ui.alert(
                u"Revit would reject this filter's rule for the categories "
                u"involved (ElementFilterIsAcceptableForParameterFilterElement "
                u"returned False). Nothing was created.",
                title="Create Type Filter", width=560)
            return

        # ONE transaction for filter creation + apply: cancelling the window
        # before this point writes nothing, and a target Revit refuses no
        # longer leaves an orphaned filter behind (fixed 2026-09-19 by
        # folding the two transactions the old flow used into this one).
        with Transaction(doc, "Create Type Filter") as t:
            t.Start()
            try:
                new_filter = ParameterFilterElement.Create(
                    doc, name, cat_ids, element_filter)

                for vt in targets:
                    if new_filter.Id not in vt.GetFilters():
                        vt.AddFilter(new_filter.Id)

                    if res.visibility is not None:
                        vt.SetFilterVisibility(new_filter.Id, res.visibility)

                    # apply_intents resolves set/clear/untouched from
                    # the editor's tri-state result against this target's
                    # actual overrides (the filter was just created, so this is
                    # normally blank, but reading it instead of assuming blank
                    # survives a target that Revit pre-populated on its own).
                    actual = vt.GetFilterOverrides(new_filter.Id)
                    vt.SetFilterOverrides(new_filter.Id, vgrow.apply_intents(actual, res.intents))

                t.Commit()
            except Exception as ex:
                run.error()
                t.RollBack()
                ui.alert(
                    u"Could not create the filter. Nothing was created or applied.\n\n{}"
                    .format(ex),
                    title="Create Type Filter", width=560
                )
                return

        # Name every target for what it is. A view and a template paint the same
        # way from the element's side, so this summary is the only place the
        # difference can be told -- saying "template(s)" for a view is what made
        # the 2026-09-19 report read as a bug in the filter rather than a choice
        # in the picker.
        summary_lines = [u"Filter '{}' created.".format(name), u"", u"Applied to:"]
        for _t in targets:
            if _t.IsTemplate:
                summary_lines.append(u"    View template:  {}".format(_t.Name))
            else:
                summary_lines.append(
                    u"    View:  {}    (paints only in this view)".format(_t.Name))

        result["ok"] = True
        result["summary"] = u"\n".join(summary_lines)
        win.Close()


    grid_targets.AddHandler(_CheckBox.ClickEvent, RoutedEventHandler(on_check), True)
    txt_tgt_filter.TextChanged += on_target_filter_typed
    btn_apply.Click  += on_apply
    btn_cancel.Click += on_cancel

    _rebind_targets()
    win.ShowDialog()

    if not result["ok"]:
        raise SystemExit

    ui.alert(result["summary"], title="Create Type Filter", width=560)
