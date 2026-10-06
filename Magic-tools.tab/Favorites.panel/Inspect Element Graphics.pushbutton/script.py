# -*- coding: utf-8 -*-
__title__ = "Inspect\nElement Graphics"
__author__ = 'slantis'
__doc__ = "Select (or pick) one element and see everything that affects how it looks in the active view: element override, element hide, category override, category hide, the view filters it matches, and filter visibility off. Applies the precedence Revit uses and marks who wins for each property (color, halftone, transparency, patterns): Element > Filter > Category > Default."
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

import traceback

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from System.Collections.Generic import List as CList
from System.Collections.ObjectModel import ObservableCollection
from System.Windows import Clipboard, Thickness, CornerRadius
from System.Windows.Controls import Border, StackPanel, TextBlock, WrapPanel
from System.Windows.Media import SolidColorBrush, Color as MColor, Brushes
from System.Windows import FontWeights

from Autodesk.Revit.DB import (
    FilteredElementCollector, ElementId,
    OverrideGraphicSettings, ViewDetailLevel,
    GraphicsStyleType, Element, BuiltInParameter,
    ParameterFilterElement, SelectionFilterElement
)
from Autodesk.Revit.UI.Selection import ObjectType

from slantisui import ui
import modeless
from pyrevit import script

def _id_val(eid):
    # ElementId value compat Revit 2022-2026 (IntegerValue removed in 2026)
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


TITLE = u"Inspect Element Graphics"

doc   = __revit__.ActiveUIDocument.Document   # noqa
uidoc = __revit__.ActiveUIDocument            # noqa

INVALID_ID         = ElementId.InvalidElementId
WEIGHT_BY_CATEGORY = -1


# ── /slantis semantic palette (override sources + visibility) ────────────────

def _brush_hex(hex_color):
    h = hex_color.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return SolidColorBrush(MColor.FromRgb(r, g, b))

# Source badges — tinted pills on the light card
BRUSH_ELEM_BG    = _brush_hex("#FAF0DC")
BRUSH_ELEM_FG    = _brush_hex("#9A5400")
BRUSH_ELEM_BD    = _brush_hex("#9A5400")
BRUSH_FILT_BG    = _brush_hex("#E2F1F4")
BRUSH_FILT_FG    = _brush_hex("#0E7180")
BRUSH_FILT_BD    = _brush_hex("#0E7180")
BRUSH_CAT_BG     = _brush_hex("#E8EEF9")
BRUSH_CAT_FG     = _brush_hex("#2F5FB3")
BRUSH_CAT_BD     = _brush_hex("#2F5FB3")

# Visibility states
BRUSH_GOOD_BG    = _brush_hex("#E4F1E7")
BRUSH_GOOD_FG    = _brush_hex("#2A7437")
BRUSH_GOOD_BD    = _brush_hex("#2A7437")
BRUSH_BAD_BG     = _brush_hex("#FBE9E6")
BRUSH_BAD_FG     = _brush_hex("#C0392B")
BRUSH_BAD_BD     = _brush_hex("#C0392B")

# Default / Object Styles
BRUSH_DEF_BG     = Brushes.Transparent
BRUSH_DEF_FG     = _brush_hex("#77736C")

# Value cell
BRUSH_VAL_BG     = _brush_hex("#F0EEE9")
BRUSH_VAL_FG     = _brush_hex("#202022")
BRUSH_MUTED_FG   = _brush_hex("#A6A199")


# ── Property descriptors ──────────────────────────────────────────────────────

PROPERTIES = [
    ('Halftone',                 'Halftone',                          'bool',   False),
    ('Transparency',             'Transparency',                      'int',    0),
    ('Detail Level',             'DetailLevel',                       'detail', ViewDetailLevel.Undefined),
    ('Projection Line Color',    'ProjectionLineColor',               'color',  None),
    ('Projection Line Weight',   'ProjectionLineWeight',              'weight', -1),
    ('Projection Line Pattern',  'ProjectionLinePatternId',           'id',     None),
    ('Cut Line Color',           'CutLineColor',                      'color',  None),
    ('Cut Line Weight',          'CutLineWeight',                     'weight', -1),
    ('Cut Line Pattern',         'CutLinePatternId',                  'id',     None),
    ('Surface FG Pattern Vis.',  'IsSurfaceForegroundPatternVisible', 'vis',    True),
    ('Surface FG Pattern Color', 'SurfaceForegroundPatternColor',     'color',  None),
    ('Surface FG Pattern',       'SurfaceForegroundPatternId',        'id',     None),
    ('Surface BG Pattern Vis.',  'IsSurfaceBackgroundPatternVisible', 'vis',    True),
    ('Surface BG Pattern Color', 'SurfaceBackgroundPatternColor',     'color',  None),
    ('Surface BG Pattern',       'SurfaceBackgroundPatternId',        'id',     None),
    ('Cut FG Pattern Vis.',      'IsCutForegroundPatternVisible',     'vis',    True),
    ('Cut FG Pattern Color',     'CutForegroundPatternColor',         'color',  None),
    ('Cut FG Pattern',           'CutForegroundPatternId',            'id',     None),
    ('Cut BG Pattern Vis.',      'IsCutBackgroundPatternVisible',     'vis',    True),
    ('Cut BG Pattern Color',     'CutBackgroundPatternColor',         'color',  None),
    ('Cut BG Pattern',           'CutBackgroundPatternId',            'id',     None),
]


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


def _is_prop_overridden(prop, ogs):
    _, attr, vtype, default = prop
    try:
        val = getattr(ogs, attr)
    except Exception:
        return False
    if vtype == 'color':
        return val is not None and val.IsValid
    if vtype == 'id':
        return val is not None and val != INVALID_ID
    if vtype == 'weight':
        return val != WEIGHT_BY_CATEGORY
    if vtype == 'bool':
        return val != default
    if vtype == 'vis':
        return val is False
    if vtype == 'int':
        return val != default
    if vtype == 'detail':
        return val != ViewDetailLevel.Undefined
    return False


def _format_value(prop, val):
    _, _, vtype, _ = prop
    if val is None:
        return '<none>'
    if vtype == 'color':
        if val.IsValid:
            return 'RGB({}, {}, {})'.format(val.Red, val.Green, val.Blue)
        return '<invalid>'
    if vtype == 'id':
        if val == INVALID_ID:
            return '<none>'
        el = doc.GetElement(val)
        if el is None:
            return 'Id {}'.format(_id_val(val))
        try:
            return el.Name
        except Exception:
            return 'Id {}'.format(_id_val(val))
    if vtype == 'weight':
        return 'default' if val == WEIGHT_BY_CATEGORY else str(val)
    if vtype == 'bool':
        return 'ON' if val else 'OFF'
    if vtype == 'vis':
        return 'visible' if val else 'HIDDEN'
    if vtype == 'int':
        return str(val)
    if vtype == 'detail':
        return str(val)
    return repr(val)


def _contrasting_brush(r, g, b):
    L = 0.299 * r + 0.587 * g + 0.114 * b
    return Brushes.Black if L > 140 else Brushes.White


# ── Filter & data collection ──────────────────────────────────────────────────

def _view_filter_ids(view):
    """Filters applied to the view, top of the V/G list first (= highest priority).

    GetFilters() returns a set sorted by ElementId, NOT by priority, so with two
    matching filters that paint the same property the wrong one could win.
    GetOrderedFilters() (Revit 2021+) returns the V/G dialog order; the Revit
    floor here is 2022, but the fallback keeps the tool alive if it is missing.
    """
    try:
        return list(view.GetOrderedFilters())
    except Exception:
        pass
    try:
        return list(view.GetFilters())
    except Exception:
        return []


def _element_category_ids(element):
    """Set of id values for the element's category (and its parent, if any).

    Empty set when the element has no category (model lines of some kinds,
    internal elements): a category-based filter cannot apply to those.
    """
    out = set()
    try:
        cat = element.Category
    except Exception:
        cat = None
    if cat is None:
        return out
    try:
        out.add(_id_val(cat.Id))
    except Exception:
        pass
    try:
        parent = cat.Parent
        if parent is not None:
            out.add(_id_val(parent.Id))
    except Exception:
        pass
    return out


def filter_matches_element(doc_, f_el, element):
    """Does this view filter apply to this element? True / False / None (unknown).

    ParameterFilterElement: the element's category must be one of the filter's
    categories (GetElementFilter() holds ONLY the rules and says nothing about
    categories), then the rules: GetElementFilter() is None when the filter has
    no rules, which means "every element of those categories".
    SelectionFilterElement: it has no rules, it is a list of element ids.
    None = the filter could not be evaluated (API threw), so the caller can say
    so instead of silently dropping it.
    """
    if isinstance(f_el, SelectionFilterElement):
        try:
            ids = f_el.GetElementIds()
        except Exception:
            return None
        target = _id_val(element.Id)
        for i in ids:
            if _id_val(i) == target:
                return True
        return False
    if isinstance(f_el, ParameterFilterElement):
        elem_cats = _element_category_ids(element)
        if not elem_cats:
            return False
        try:
            filter_cats = f_el.GetCategories()
        except Exception:
            return None
        in_cat = False
        for c in filter_cats:
            if _id_val(c) in elem_cats:
                in_cat = True
                break
        if not in_cat:
            return False
        try:
            ef = f_el.GetElementFilter()
        except Exception:
            return None
        if ef is None:
            return True
        try:
            return bool(ef.PassesFilter(doc_, element.Id))
        except Exception:
            return None
    return None


def get_matching_filters(view, element):
    """Returns (matching, n_unknown); matching is in V/G priority order."""
    out = []
    n_unknown = 0
    for fid in _view_filter_ids(view):
        f_el = doc.GetElement(fid)
        if f_el is None:
            continue
        matches = filter_matches_element(doc, f_el, element)
        if matches is None:
            n_unknown += 1
            continue
        if not matches:
            continue
        try:
            enabled = view.GetIsFilterEnabled(fid)
        except Exception:
            enabled = True
        try:
            visible = view.GetFilterVisibility(fid)
        except Exception:
            visible = True
        try:
            f_ogs = view.GetFilterOverrides(fid)
        except Exception:
            f_ogs = OverrideGraphicSettings()
        out.append({'id': fid, 'name': f_el.Name, 'enabled': enabled,
                    'visible': visible, 'ogs': f_ogs})
    return out, n_unknown


def compute_winner(prop, element_ogs, matching_filters, category_ogs):
    if _is_prop_overridden(prop, element_ogs):
        return (getattr(element_ogs, prop[1]), 'element', 'Element', True)
    for f in matching_filters:
        if not f['enabled']:
            continue
        if _is_prop_overridden(prop, f['ogs']):
            return (getattr(f['ogs'], prop[1]), 'filter', f['name'], True)
    if category_ogs is not None and _is_prop_overridden(prop, category_ogs):
        return (getattr(category_ogs, prop[1]), 'category', 'Category', True)
    return (None, 'default', 'Object Styles', False)


def _get_object_style_value(prop, category):
    """Read the Object Styles (default) value for a property from the Category."""
    label, attr, vtype, default = prop
    if category is None:
        return default
    try:
        if attr == 'ProjectionLineColor' or attr == 'CutLineColor':
            c = category.LineColor
            return c if c is not None and c.IsValid else default
        if attr == 'ProjectionLineWeight':
            w = category.GetLineWeight(GraphicsStyleType.Projection)
            return w if w is not None and w != -1 else default
        if attr == 'ProjectionLinePatternId':
            p = category.GetLinePatternId(GraphicsStyleType.Projection)
            return p if p is not None and p != INVALID_ID else default
        if attr == 'CutLineWeight':
            w = category.GetLineWeight(GraphicsStyleType.Cut)
            return w if w is not None and w != -1 else default
        if attr == 'CutLinePatternId':
            p = category.GetLinePatternId(GraphicsStyleType.Cut)
            return p if p is not None and p != INVALID_ID else default
    except Exception:
        pass
    return default


def _element_header(el):
    cat = '<no category>'
    try:
        if el.Category is not None:
            cat = el.Category.Name
    except Exception:
        pass
    family = ''
    try:
        family = el.Symbol.Family.Name
    except Exception:
        pass
    type_name = ''
    try:
        tid = el.GetTypeId()
        if tid != INVALID_ID:
            type_name = _type_name(doc.GetElement(tid))
    except Exception:
        pass
    name = ''
    try:
        name = el.Name
    except Exception:
        pass
    return cat, family or name, type_name, name


def collect_data(element, view):
    cat, fam_or_name, type_name, raw_name = _element_header(element)

    cat_id = None
    try:
        if element.Category is not None:
            cat_id = element.Category.Id
    except Exception:
        pass

    try:
        element_ogs = view.GetElementOverrides(element.Id)
    except Exception:
        element_ogs = OverrideGraphicSettings()

    try:
        category_ogs = (view.GetCategoryOverrides(cat_id)
                        if cat_id is not None else None)
    except Exception:
        category_ogs = None

    matching_filters, n_unknown_filters = get_matching_filters(view, element)

    hidden_by_element  = False
    hidden_by_category = False
    hidden_by_filters  = []
    try:
        hidden_by_element = bool(element.IsHidden(view))
    except Exception:
        pass
    if cat_id is not None:
        try:
            hidden_by_category = bool(view.GetCategoryHidden(cat_id))
        except Exception:
            pass
    # A hidden container reports every one of its sub-elements as hidden too,
    # so a curtain panel can answer IsHidden=True without anyone having hidden
    # that panel. Name the host, which is the element the user can act on.
    # Same root cause as the fold in lib/hostecho.py.
    hidden_host = None
    if hidden_by_element:
        try:
            host = element.Host
        except Exception:
            host = None
        if host is not None:
            try:
                if bool(host.IsHidden(view)):
                    try:
                        host_cat = host.Category.Name if host.Category else '<no cat>'
                    except Exception:
                        host_cat = '<no cat>'
                    hidden_host = {'id': _id_val(host.Id), 'category': host_cat}
            except Exception:
                pass

    # Every matching filter counts, in priority order: the old code looked
    # at the first match only. An enabled filter with visibility OFF hides what
    # it matches. Which of several hides wins does not matter for visibility, so
    # the list is kept whole and the top-priority one is named first.
    for f in matching_filters:
        if f['enabled'] and not f['visible']:
            hidden_by_filters.append(f)
    hidden_by_filter = hidden_by_filters[0] if hidden_by_filters else None

    category_obj = None
    try:
        category_obj = element.Category
    except Exception:
        pass

    winners = []
    for p in PROPERTIES:
        val, stype, sname, overridden = compute_winner(
            p, element_ogs, matching_filters, category_ogs)
        if not overridden:
            val = _get_object_style_value(p, category_obj)
        winners.append({'prop': p, 'value': val,
                        'source_type': stype, 'source_name': sname,
                        'overridden': overridden})

    factors = []
    el_props = [p for p in PROPERTIES if _is_prop_overridden(p, element_ogs)]
    if el_props:
        factors.append({'type': 'element', 'name': 'Element override',
                        'subtitle': '', 'props_count': len(el_props),
                        'enabled': True, 'visible': True})
    for f in matching_filters:
        f_props = [p for p in PROPERTIES if _is_prop_overridden(p, f['ogs'])]
        # Every filter that matches is listed, even one with no overrides at
        # all: the user expects to see all the filters that apply to the element.
        subtitle = []
        if not f['enabled']:
            subtitle.append('DISABLED')
        elif not f['visible']:
            subtitle.append('visibility OFF')
        factors.append({'type': 'filter',
                        'name': 'Filter "{}"'.format(f['name']),
                        'subtitle': ' • '.join(subtitle),
                        'props_count': len(f_props),
                        'enabled': f['enabled'], 'visible': f['visible']})
    cat_props = ([p for p in PROPERTIES if _is_prop_overridden(p, category_ogs)]
                 if category_ogs is not None else [])
    if cat_props or hidden_by_category:
        sub = 'category HIDDEN' if hidden_by_category else ''
        factors.append({'type': 'category',
                        'name': 'Category "{}"'.format(cat),
                        'subtitle': sub,
                        'props_count': len(cat_props),
                        'enabled': True, 'visible': not hidden_by_category})

    n_total_filters = len(_view_filter_ids(view))

    tpl_name = ''
    try:
        if view.ViewTemplateId is not None and view.ViewTemplateId != INVALID_ID:
            tpl_el = doc.GetElement(view.ViewTemplateId)
            if tpl_el is not None:
                tpl_name = tpl_el.Name
    except Exception:
        pass

    return {
        'element_id': element.Id, 'category': cat,
        'fam_or_name': fam_or_name, 'type_name': type_name, 'name': raw_name,
        'view_name': view.Name, 'view_type': str(view.ViewType),
        'template': tpl_name,
        'hidden_by_element': hidden_by_element,
        'hidden_by_category': hidden_by_category,
        'hidden_by_filter': hidden_by_filter,
        'hidden_by_filters': hidden_by_filters,
        'hidden_host': hidden_host,
        'is_hidden': (hidden_by_element or hidden_by_category
                      or hidden_by_filter is not None),
        'winners': winners, 'factors': factors,
        'n_total_filters': n_total_filters,
        'n_matching_filters': len(matching_filters),
        'n_unknown_filters': n_unknown_filters,
    }


# ── Text report (for clipboard) ───────────────────────────────────────────────

LINE = '=' * 76

# What the visibility verdict does NOT look at. Shown in the banner and in
# the copied report so "IS VISIBLE" is never read as a promise.
VIS_SCOPE_NOTE = ('Based on element, category and filter visibility only. '
                  'Temporary Hide/Isolate, worksets, phases, design options '
                  'and view range are not checked.')


def _unknown_note(n):
    """Banner/report warning when some filters could not be evaluated, else ''."""
    if n <= 0:
        return u''
    return u'{} filter(s) could not be evaluated, visibility may be incomplete.'.format(n)


def build_text_report(data):
    L = [LINE, 'ELEMENT INSPECTOR — what is affecting this element graphically', LINE, '',
         'ELEMENT',
         '  Id          : {}'.format(_id_val(data['element_id'])),
         '  Category    : {}'.format(data['category']),
         '  Family/Name : {}'.format(data['fam_or_name']),
         '  Type        : {}'.format(data['type_name']),
         '', 'VIEW',
         '  Name        : {}'.format(data['view_name']),
         '  Type        : {}'.format(data['view_type']),
         '  Template    : {}'.format(
             '"{}" applied'.format(data['template']) if data['template'] else '<none>'),
         '',
         LINE, 'VISIBILITY', LINE,
         '  Hidden by element  : {}'.format('YES' if data['hidden_by_element'] else 'no'),
         '  Hidden with host   : {}'.format(
             'YES, its host ({} id {}) is hidden in this view'.format(
                 data['hidden_host']['category'], data['hidden_host']['id'])
             if data['hidden_host'] else 'no'),
         '  Hidden by category : {}'.format('YES' if data['hidden_by_category'] else 'no'),
         '  Hidden by filter   : {}'.format(
             u'YES \u2014 {}'.format(', '.join(
                 'filter "{}"'.format(f['name']) for f in data['hidden_by_filters']))
             if data['hidden_by_filters'] else 'no'),
         '',
         '  => ' + ('ELEMENT IS HIDDEN.' if data['is_hidden'] else 'Element IS VISIBLE.'),
         '  ' + VIS_SCOPE_NOTE]
    if data['n_unknown_filters'] > 0:
        L.append('  ' + _unknown_note(data['n_unknown_filters']))
    L.extend([
         '',
         LINE, 'ALL PROPERTIES — Element > Filter (top) > Category > Object Styles', LINE])
    source_tags = {'element': '[E]', 'filter': '[F]', 'category': '[C]', 'default': '[D]'}
    for w in data['winners']:
        L.append('  {:<26} = {:<22}  <-  {} {}'.format(
            w['prop'][0] + ':',
            _format_value(w['prop'], w['value']),
            source_tags.get(w['source_type'], '[D]'),
            w['source_name']))
    L.extend(['', LINE, 'ACTIVE FACTORS', LINE])
    if not data['factors']:
        L.append('  · No factors override any property.')
    else:
        for f in data['factors']:
            tag = '[E]' if f['type'] == 'element' else ('[F]' if f['type'] == 'filter' else '[C]')
            extra = ' — {}'.format(f['subtitle']) if f['subtitle'] else ''
            if f['props_count'] == 0:
                what = 'no overrides'
            else:
                what = '{} property override{}'.format(
                    f['props_count'], '' if f['props_count'] == 1 else 's')
            L.append('  {} {} ({}){}'.format(tag, f['name'], what, extra))
    non_match = (data['n_total_filters'] - data['n_matching_filters']
                 - data['n_unknown_filters'])
    if non_match > 0:
        L.append('  · {} other filter(s) do not match this element.'.format(non_match))
    if data['n_unknown_filters'] > 0:
        L.append(u'  \u00b7 {} filter(s) could not be evaluated.'.format(
            data['n_unknown_filters']))
    L.extend(['', LINE])
    return '\r\n'.join(L)


# ── WPF row model ─────────────────────────────────────────────────────────────

class WinnerRow(object):
    def __init__(self, prop_label, value, source, value_bg, value_fg,
                 src_bg, src_fg, cell_weight):
        self.Property   = prop_label
        self.Value      = value
        self.Source     = source
        self.ValueBg    = value_bg
        self.ValueFg    = value_fg
        self.SourceBg   = src_bg
        self.SourceFg   = src_fg
        self.CellWeight = cell_weight


def build_winner_rows(data):
    rows = []
    for w in data['winners']:
        vtype = w['prop'][2]
        v_str = _format_value(w['prop'], w['value'])
        is_default = w['source_type'] == 'default'

        # Value cell coloring
        if is_default:
            if vtype == 'color' and w['value'] is not None and w['value'].IsValid:
                r, g, b = w['value'].Red, w['value'].Green, w['value'].Blue
                vbg = SolidColorBrush(MColor.FromRgb(r, g, b))
                vfg = _contrasting_brush(r, g, b)
            else:
                vbg, vfg = BRUSH_DEF_BG, BRUSH_DEF_FG
        elif vtype == 'color' and w['value'] is not None and w['value'].IsValid:
            r, g, b = w['value'].Red, w['value'].Green, w['value'].Blue
            vbg = SolidColorBrush(MColor.FromRgb(r, g, b))
            vfg = _contrasting_brush(r, g, b)
        elif vtype == 'vis' and w['value'] is False:
            vbg, vfg = BRUSH_BAD_BG, BRUSH_BAD_FG
        else:
            vbg, vfg = BRUSH_VAL_BG, BRUSH_VAL_FG

        # Source cell coloring
        if w['source_type'] == 'element':
            src = '[E]  {}'.format(w['source_name'])
            sbg, sfg = BRUSH_ELEM_BG, BRUSH_ELEM_FG
        elif w['source_type'] == 'filter':
            src = '[F]  Filter "{}"'.format(w['source_name'])
            sbg, sfg = BRUSH_FILT_BG, BRUSH_FILT_FG
        elif w['source_type'] == 'category':
            src = '[C]  {} override'.format(w['source_name'])
            sbg, sfg = BRUSH_CAT_BG, BRUSH_CAT_FG
        else:
            src = 'Object Styles'
            sbg, sfg = BRUSH_DEF_BG, BRUSH_DEF_FG

        # DEFAULT rows read as plain text (weight 400 in the closed mockup);
        # only actual overrides carry the SemiBold pill weight.
        wt = FontWeights.Normal if is_default else FontWeights.SemiBold
        rows.append(WinnerRow(w['prop'][0], v_str, src, vbg, vfg, sbg, sfg, wt))
    return rows


def build_factor_card(f):
    """Return a Border with the factor info, /slantis-styled."""
    if f['type'] == 'element':
        bg, fg, bd, tag = BRUSH_ELEM_BG, BRUSH_ELEM_FG, BRUSH_ELEM_BD, 'E'
    elif f['type'] == 'filter':
        bg, fg, bd, tag = BRUSH_FILT_BG, BRUSH_FILT_FG, BRUSH_FILT_BD, 'F'
    else:
        bg, fg, bd, tag = BRUSH_CAT_BG, BRUSH_CAT_FG, BRUSH_CAT_BD, 'C'

    card = Border()
    card.Background      = bg
    card.BorderBrush     = bd
    card.BorderThickness = Thickness(1)
    card.CornerRadius    = CornerRadius(5)
    card.Padding         = Thickness(14, 10, 14, 10)
    card.Margin          = Thickness(0, 0, 8, 8)
    card.MinWidth        = 220

    sp = StackPanel()
    head = TextBlock()
    head.Text       = '[{}]  {}'.format(tag, f['name'])
    head.Foreground = fg
    head.FontWeight = FontWeights.SemiBold
    head.FontSize   = 12
    sp.Children.Add(head)

    if f['props_count'] == 0:
        sub_text = 'no overrides'
    else:
        sub_text = '{} property override{}'.format(
            f['props_count'], '' if f['props_count'] == 1 else 's')
    if f['subtitle']:
        sub_text += '   •   ' + f['subtitle']
    sub = TextBlock()
    sub.Text       = sub_text
    sub.Foreground = fg
    sub.FontSize   = 11
    sub.Margin     = Thickness(0, 3, 0, 0)
    sp.Children.Add(sub)

    card.Child = sp
    return card


# ── Body XAML ─────────────────────────────────────────────────────────────────

_BODY = """
  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>

    <Border x:Name="visBadge" Grid.Row="0"
            CornerRadius="5" Padding="16,12" Margin="0,0,0,14">
      <StackPanel>
        <TextBlock x:Name="lblVis" FontWeight="SemiBold" FontSize="13" TextWrapping="Wrap"/>
        <TextBlock x:Name="lblVisNote" FontSize="11" Margin="0,4,0,0"
                   TextWrapping="Wrap" Opacity="0.8"/>
      </StackPanel>
    </Border>

    <TextBlock Grid.Row="1" Margin="0,0,0,8"
               Text="ALL PROPERTIES  &#x2022;  Element &gt; Filter &gt; Category &gt; Object Styles"
               Foreground="#A6A199" FontSize="11" FontWeight="SemiBold"/>

    <DataGrid x:Name="grid" Grid.Row="2" SelectionMode="Single"
              CanUserSortColumns="False" HeadersVisibility="Column">
      <DataGrid.Columns>
        <DataGridTextColumn Header="PROPERTY" Width="220"
                            Binding="{Binding Property}"/>
        <DataGridTemplateColumn Header="VALUE" Width="260">
          <DataGridTemplateColumn.CellTemplate>
            <DataTemplate>
              <Border Background="{Binding ValueBg}" Padding="10,4" CornerRadius="4"
                      Margin="2,3" HorizontalAlignment="Left">
                <TextBlock Text="{Binding Value}" Foreground="{Binding ValueFg}"
                           FontWeight="{Binding CellWeight}" VerticalAlignment="Center"/>
              </Border>
            </DataTemplate>
          </DataGridTemplateColumn.CellTemplate>
        </DataGridTemplateColumn>
        <DataGridTemplateColumn Header="WINNER" Width="*">
          <DataGridTemplateColumn.CellTemplate>
            <DataTemplate>
              <Border Background="{Binding SourceBg}" Padding="10,4" CornerRadius="4"
                      Margin="2,3" HorizontalAlignment="Left">
                <TextBlock Text="{Binding Source}" Foreground="{Binding SourceFg}"
                           FontWeight="{Binding CellWeight}" VerticalAlignment="Center"/>
              </Border>
            </DataTemplate>
          </DataGridTemplateColumn.CellTemplate>
        </DataGridTemplateColumn>
      </DataGrid.Columns>
    </DataGrid>

    <TextBlock Grid.Row="3" Margin="0,14,0,8"
               Text="ACTIVE FACTORS"
               Foreground="#A6A199" FontSize="11" FontWeight="SemiBold"/>

    <Border Grid.Row="4" MinHeight="80" MaxHeight="160">
      <ScrollViewer VerticalScrollBarVisibility="Auto"
                    HorizontalScrollBarVisibility="Disabled">
        <WrapPanel x:Name="factorsPanel" Orientation="Horizontal"/>
      </ScrollViewer>
    </Border>
  </Grid>
"""

_FOOTER = """
  <Grid>
    <StackPanel HorizontalAlignment="Left" Orientation="Horizontal">
      <Button x:Name="btnPick"   Content="Pick another&#x2026;"  Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
      <Button x:Name="btnSelect" Content="Select in view" Style="{StaticResource BtnGhost}"   Margin="0,0,8,0"/>
      <Button x:Name="btnCopy"   Content="Copy report"    Style="{StaticResource BtnGhost}"/>
    </StackPanel>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnClose"  Content="Close" Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""


# ── Form runner ───────────────────────────────────────────────────────────────

def show_inspector(element, view):
    """Display the inspector."""
    data = collect_data(element, view)

    title_text = data['fam_or_name'] or data['name'] or 'Element'
    if data['type_name']:
        title_text = '{}  —  {}'.format(title_text, data['type_name'])

    tpl_part = ('   •   Template: "{}"'.format(data['template'])
                if data['template'] else '')
    subtitle = '{}   •   Id {}   •   View "{}"{}'.format(
        data['category'], _id_val(data['element_id']),
        data['view_name'], tpl_part)

    win = ui.parse(title_text, subtitle, _BODY, _FOOTER, width=980, height=720)

    visBadge      = win.FindName("visBadge")
    lblVis        = win.FindName("lblVis")
    lblVisNote    = win.FindName("lblVisNote")
    grid          = win.FindName("grid")
    factorsPanel  = win.FindName("factorsPanel")
    btnPick       = win.FindName("btnPick")
    btnSelect     = win.FindName("btnSelect")
    btnCopy       = win.FindName("btnCopy")
    btnClose      = win.FindName("btnClose")

    # Visibility badge
    if data['is_hidden']:
        reasons = []
        if data['hidden_by_element']:
            if data['hidden_host']:
                reasons.append(
                    'hidden with its host ({} id {}), not on its own'.format(
                        data['hidden_host']['category'],
                        data['hidden_host']['id']))
            else:
                reasons.append('hidden by Element')
        if data['hidden_by_category']:
            reasons.append('hidden by Category')
        if data['hidden_by_filters']:
            reasons.append('hidden by {}'.format(', '.join(
                'Filter "{}"'.format(f['name']) for f in data['hidden_by_filters'])))
        lblVis.Text           = 'Element is HIDDEN in this view  —  ' + ', '.join(reasons)
        visBadge.Background   = BRUSH_BAD_BG
        visBadge.BorderBrush  = BRUSH_BAD_BD
        visBadge.BorderThickness = Thickness(1)
        lblVis.Foreground     = BRUSH_BAD_FG
    else:
        lblVis.Text           = 'Element IS VISIBLE in this view'
        visBadge.Background   = BRUSH_GOOD_BG
        visBadge.BorderBrush  = BRUSH_GOOD_BD
        visBadge.BorderThickness = Thickness(1)
        lblVis.Foreground = BRUSH_GOOD_FG

    lblVisNote.Text       = VIS_SCOPE_NOTE
    if data['n_unknown_filters'] > 0:
        lblVisNote.Text = (VIS_SCOPE_NOTE + u'\n'
                           + _unknown_note(data['n_unknown_filters']))
    lblVisNote.Foreground = lblVis.Foreground

    # Winners grid
    rows = build_winner_rows(data)
    col = ObservableCollection[object]()
    for r in rows:
        col.Add(r)
    grid.ItemsSource = col

    # Factor cards
    if not data['factors']:
        none_lbl = TextBlock()
        none_lbl.Text       = 'No factors override any property of this element.'
        none_lbl.Foreground = BRUSH_MUTED_FG
        none_lbl.Margin     = Thickness(4, 4, 0, 0)
        factorsPanel.Children.Add(none_lbl)
    else:
        for f in data['factors']:
            factorsPanel.Children.Add(build_factor_card(f))

    non_match = (data['n_total_filters'] - data['n_matching_filters']
                 - data['n_unknown_filters'])
    notes = []
    if non_match > 0:
        notes.append(u'\u00b7 {} other filter(s) applied to view do not match.'.format(non_match))
    if data['n_unknown_filters'] > 0:
        notes.append(u'\u00b7 {} filter(s) could not be evaluated.'.format(
            data['n_unknown_filters']))
    for txt in notes:
        note = TextBlock()
        note.Text       = txt
        note.Foreground = BRUSH_MUTED_FG
        note.FontSize   = 11
        note.Margin     = Thickness(4, 8, 0, 0)
        factorsPanel.Children.Add(note)

    def on_copy(s, e):
        try:
            Clipboard.SetText(build_text_report(data))
        except Exception:
            pass

    def on_select(s, e):
        def work(uiapp):
            uidoc = uiapp.ActiveUIDocument
            try:
                uidoc.Selection.SetElementIds(CList[ElementId]([element.Id]))
                uidoc.ShowElements(CList[ElementId]([element.Id]))
            except Exception:
                pass
        modeless.run(work, doc=doc, title=TITLE)

    def on_pick(s, e):
        def work(uiapp):
            uidoc = uiapp.ActiveUIDocument
            win.Hide()
            try:
                ref = uidoc.Selection.PickObject(
                    ObjectType.Element, 'Pick another element to inspect')
            except Exception:
                win.Show()
                return
            if ref is None:
                win.Show()
                return
            new_el = doc.GetElement(ref.ElementId)
            if new_el is None:
                win.Show()
                return
            win.Close()
            show_inspector(new_el, doc.ActiveView)
        modeless.run(work, doc=doc, title=TITLE)

    btnCopy.Click   += on_copy
    btnSelect.Click += on_select
    btnPick.Click   += on_pick
    btnClose.Click  += lambda s, e: win.Close()

    modeless.show(win, TITLE, doc=doc)


def get_target_element():
    try:
        ids = list(uidoc.Selection.GetElementIds())
    except Exception:
        ids = []
    if ids:
        el = doc.GetElement(ids[0])
        if el is not None:
            return el
    try:
        ref = uidoc.Selection.PickObject(
            ObjectType.Element, 'Pick an element to inspect')
    except Exception:
        return None
    if ref is None:
        return None
    return doc.GetElement(ref.ElementId)


try:
    if modeless.focus(TITLE):
        script.exit()
    if uidoc is None:
        ui.alert('No active document.', title='Inspect Element Graphics')
    elif doc.ActiveView is None:
        ui.alert('No active view.', title='Inspect Element Graphics')
    else:
        target = get_target_element()
        if target is not None:
            show_inspector(target, doc.ActiveView)
except Exception:
    ui.alert('Inspect Element error:\n\n' + traceback.format_exc(),
             title='Inspect Element -- Error')
