# -*- coding: utf-8 -*-
"""Convert an imported DWG into native Revit detail lines and text notes,
using ONLY line styles and text types that already exist in the project.

This tool NEVER creates a filled region.

Curves
- Every curve is created exactly as the import hands it over: an arc stays an
  arc, a circle stays a circle, a spline stays a spline. When the CAD sits at a
  different height than the view, the curve is MOVED onto the view plane
  (a translation keeps the curve type); it is only ever broken into segments
  as a last resort.
- A whole circle comes in as ONE element you can select and give a radius,
  not as two half arcs. It is only ever halved if this build of Revit
  refuses to take it whole.
- Polylines whose vertices lie on a circle are rebuilt as real arcs, so
  linework that arrives faceted comes back curved.
- Curves are matched to existing Line Styles by LINE PATTERN first
  (continuous / dashed / center...), then weight, then name. The name of the
  CAD layer also counts as a hint (walls and concrete lean to a wide line,
  text and dimensions to a thin one). Only styles meant for drawing are ever
  offered: <Fabric Sheets>, <Invisible lines>, <Beyond>, boundaries and the
  like are not.

Hatches
- The tool ASKS what to do with them: convert them to linework, or leave
  them out so you can place your own Revit filled regions over the empty
  areas. Either way no filled region is ever created.
- Revit's API cannot tell a hatch's pattern line from any other line, so
  leaving hatches out is done from the DXF, which carries every HATCH with
  its boundary paths, islands included. Only a line on the hatch's own
  layer whose middle sits inside that boundary is removed, so the outline
  of the hatched area always survives.
- SOLID hatches reach the API as planar solids and meshes. Revit can only
  hold those as filled regions, so the fill itself is not converted either
  way; when "Convert hatches to linework" is on and a DXF is at hand, the
  outline of each one is drawn as detail lines.
- Revit often hands over a hatch EMPTY: the area and its boundary, but none
  of the stripes of its pattern. With "Convert hatches to linework" and a DXF
  at hand, those stripes are drawn as detail lines from the DXF itself (it
  carries the pattern, already clipped to the boundary), on the line style
  of the hatch's layer.

Text
- TEXT, MTEXT, ATTRIB and ATTDEF from a matching ASCII DXF become editable
  Revit TextNotes - including the text inside blocks, which is reached by
  expanding INSERT entities against the BLOCKS section.
- Notes are sized to match the CAD text on paper. The options window lists
  every CAD text size with the closest existing text type already chosen for
  it, says how far out that match is, and lets you send any size to a type of
  your own or to a NEW type made at the CAD size, whose name you can edit
  (proposed after the size and font, marked "(CAD)"). When the closest type
  is more than about 25 % off the CAD size, the NEW type is what comes
  preselected for that size, visibly, with its name; every other size starts
  on its closest existing type.
- DXF coordinates are in the drawing's own units, so the reader works out how
  many feet one DXF unit is ($INSUNITS, checked against the drawing extents
  and the import's real bounding box) before placing anything. Without that
  step text lands hundreds of times too far out and Revit refuses it.

Leaders
- A CAD LEADER or MULTILEADER becomes a real Revit leader with a real
  arrowhead, hanging off the note it points from, rather than a handful of
  detail lines and a filled triangle. The DXF names the annotation a classic
  leader belongs to, so that link is used first, and otherwise the leader
  goes to the nearest note within reach of it.
- A multileader carries its own text, which nothing else in the DXF reader
  would ever see, so it arrives as a note of its own with its leader.
- The lines and the filled arrowhead that drew the CAD arrow are removed, so
  the arrow is not there twice.

Nothing degenerate can stop the conversion: fragments below Revit's minimum
line length are dropped up front, loops are welded so they cannot come out
discontinuous, anything Revit still refuses is resolved while saving instead
of a modal error, and text and lines commit separately in one undo step.

Never creates new line styles, fill patterns or filled regions. The only
thing it can add to the project is a text type, built on one the project
already has, and only after you have answered its question.

Where it runs
- In any 2D view of a project (drafting, detail, plan, section,
  elevation) AND inside a Detail Item family: the geometry API is the
  same, only the factory that makes the detail curve differs, and so does
  where the line styles live (the family's own subcategories, or the
  project's Lines category). This is the fusion of CAD to Lines (family
  side) and Clean Explode CAD (project side) into one tool.
- The DXF reader lives in lib/cadtext.py: pure Python, testable outside
  Revit.
"""

__title__ = "Clean\nExplode CAD"
__author__ = "slantis"
__doc__ = "Explode an imported DWG into native detail lines on existing " \
          "line styles, editable text notes and real Revit leaders. Works " \
          "in any 2D project view and inside Detail Item families. Keeps " \
          "arcs curved and circles whole, lets you leave hatches out, " \
          "never creates a filled region or a line style, and drops " \
          "degenerate geometry instead of failing."

import math
import os
import re
from collections import defaultdict

import clr
# The WPF assemblies must be referenced BEFORE anything under System.Windows
# is imported: IronPython only sees the namespace once the CLR assembly that
# carries it is loaded (MT-P40).
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
from System.Windows import (
    Clipboard, CornerRadius, GridLength, GridUnitType, TextWrapping,
    TextTrimming, Thickness, VerticalAlignment, Visibility)
from System.Windows.Controls import (
    Border, ColumnDefinition, ComboBox, ComboBoxItem, Grid, RadioButton,
    StackPanel, TextBlock, TextBox)
from System.Windows.Media import BrushConverter

from pyrevit import revit, script
from slantisui import ui
import usage

_BRUSH_CONVERTER = BrushConverter()


def _brush(hex_value):
    """A SolidColorBrush from a hex string, for controls built in code
    rather than XAML (the two options grids below): read from ui.* tokens
    so the light-theme palette never gets typed twice."""
    return _BRUSH_CONVERTER.ConvertFromString(hex_value)


def _id_val(eid):
    # ElementId value compat Revit 2022-2026 (IntegerValue removed in 2026)
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue

# The ASCII DXF reader: text, leaders and hatches, in pure Python.
from cadtext import (
    UNIT_CANDIDATES, _IDENTITY_FRAME, _expand,
    attach_leaders, collect_hatches, collect_leaders, count_hatches,
    dimension_keys, entities_extent, header_extents, header_unit_scale,
    leader_text_records, read_dxf, robust_range, robust_span)

from Autodesk.Revit.DB import (
    BuiltInCategory, GraphicsStyle, GraphicsStyleType, Options,
    ViewDetailLevel, GeometryInstance, GeometryElement, Curve, Line, Arc,
    PolyLine, Transform, ViewType, ElementId, LinePatternElement,
    Solid, Mesh, FilteredElementCollector, XYZ, TextNote,
    TextNoteOptions, TextNoteType, ElementTypeGroup, BuiltInParameter,
    HorizontalTextAlignment, ModelPathUtils, TextNoteLeaderTypes,
)
from Autodesk.Revit.DB import ImportInstance, DocumentValidation
from Autodesk.Revit.DB import (
    Transaction as DBTransaction, TransactionGroup, TransactionStatus,
    IFailuresPreprocessor, FailureProcessingResult, FailureSeverity,
    FailureResolutionType,
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException
from System.Collections.Generic import List

logger = script.get_logger()

ERROR_LOG = os.path.join(os.getenv("APPDATA") or "", "pyRevit",
                         "_magictools_errors.log")


def _diag_log(text):
    """Append a diagnostic to the Magic Tools error log, and say where.

    pyRevit's logger is not a reliable channel: LoggerWrapper._emit returns
    without a word when its runtime service does not resolve, and the Log
    call itself sits inside an except: pass. Anything that has to survive
    the run goes to a file instead.
    """
    import datetime
    try:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(ERROR_LOG, "ab") as log_file:
            log_file.write((u"\n=== Clean Explode CAD  {}\n{}\n"
                            .format(stamp, text)).encode("utf-8"))
        return ERROR_LOG
    except Exception:
        return None

doc = revit.doc
uidoc = revit.uidoc
active_view = doc.ActiveView

# A Detail Item family and a project view are both fair game. The geometry
# API is the same; what differs is the factory that makes a detail curve
# (FamilyCreate inside a family, Create in a project) and where the line
# styles live (the family's own subcategories, or the Lines category).
IS_FAMILY = doc.IsFamilyDocument

VALID_VIEW_TYPES = (
    ViewType.DraftingView,
    ViewType.Detail,
    ViewType.FloorPlan,
    ViewType.CeilingPlan,
    ViewType.Section,
    ViewType.Elevation,
    ViewType.AreaPlan,
)

BLACKLIST_KEYWORDS = (
    "sketch", "room separation", "area boundary", "axis of rotation",
    "insulation batting", "path of travel", "space separation",
    # Line styles Revit keeps for something other than drawing a detail.
    # <Fabric Sheets> is the one that used to win for ordinary layers: with
    # nothing to tell the continuous styles apart, the first one in the list
    # took them all (94 of the 148 lines of a 1:10 detail).
    "fabric", "reinforcement", "rebar", "invisible", "beyond", "boundary",
    "separation", "stair path",
)

# Of Revit's own line styles (their names are written between < >, which a
# style made by somebody can never contain) only these are for drawing.
DRAWABLE_BUILTINS = (
    "thin line", "medium line", "wide line", "centerline", "center line",
    "hidden", "overhead", "demolished", "dash", "dot", "phantom", "divide",
)
# The same three weights by category, so a Revit in another language still
# offers them: the names are translated, the categories are not.
DRAWABLE_CATEGORY_NAMES = ("OST_ThinLines", "OST_MediumLines",
                           "OST_WideLines", "OST_HiddenLines")

# What the name of a CAD layer says about how heavy its line should be. A
# word has to START a piece of the name (A-MURO, WALL-HATCH, a-anno-dims) so
# that CONTEXT does not read as TEXT.
WIDE_LAYER_HINTS = ("mur", "wall", "hormig", "conc")
THIN_LAYER_HINTS = ("texto", "text", "anot", "anno", "cota", "dim")

PATTERN_FAMILIES = (
    ("center", ("center", "centre", "cent", "axis")),
    ("dashdot", ("dashdot", "dash dot", "dash_dot", "phantom", "divide")),
    ("dot", ("dot", "hidden2",)),
    ("dash", ("dash", "hidden", "hid", "dashed")),
)

SOLID_FALLBACK_ORDER = ["thin lines", "<thin lines>",
                        "medium lines", "<medium lines>",
                        "lines", "<lines>"]

# Revit rejects any curve shorter than ShortCurveTolerance (1/32", about
# 0.8 mm). Imported geometry is NOT filtered and CAD drawings are full of
# hairline fragments below it, so they are dropped before anything is made.
SHORT_CURVE_TOLERANCE = doc.Application.ShortCurveTolerance
MIN_CURVE_LENGTH = SHORT_CURVE_TOLERANCE * 1.05

# Rebuild real arcs out of polylines whose vertices sit on a circle. Set to
# False to keep faceted linework exactly as the import delivers it.
REBUILD_ARCS_FROM_POLYLINES = True
ARC_MIN_POINTS = 4               # 3 points always fit a circle - 4 is proof
# A facet of a tessellated curve turns gently; a corner turns hard. 32 deg
# keeps a 12-sided circle (30 deg a vertex) while leaving an octagon (45)
# and a hexagon (60) as the polygons they are.
ARC_MAX_TURN = math.radians(32)
ARC_MAX_POINTS = 400             # most facets one rebuilt arc may swallow
LINE_MAX_POINTS = 400            # most facets one rebuilt line may swallow
# How far a rebuilt curve may stray from the facets it replaces, as a share
# of one facet's own length. Raise it to simplify harder, lower it to stay
# closer to the imported linework.
SIMPLIFY_RATIO = 0.05

# How far the imported linework of a leader may sit from the leader's own
# path and still be recognised as part of it.
LEADER_LINE_TOLERANCE = 0.25

# How far from the arrow tip a small filled shape counts as its arrowhead.
ARROWHEAD_REACH = 0.6

# How far an existing text type's size may sit from what the CAD text needs
# before the tool offers to make one that matches (0.12 = 12 %).
TEXT_SIZE_TOLERANCE = 0.12

# How far the CLOSEST existing text type may sit from a CAD text size before
# the options window stops proposing it and proposes making one at the CAD
# size instead (1.25 = the type is more than 25 % off, bigger or smaller). In
# a project with big types, the 2.5, 3.5 and 5 mm of a 1:10 detail all fell
# on the same 1/4" type; a type that far off is not a match, it is a guess.
TEXT_TYPE_CREATE_RATIO = 1.25

# The smallest text this tool will ever build a type for, ON PAPER: 1/64"
# (about 0.4 mm), which is the floor Revit's own text size field offers.
# Below this the duplicate fails and the CAD is almost certainly not at the
# scale the drawing assumes, so the tool warns about the scale and falls
# back to the closest existing type instead of offering to make one. It is
# a policy line as much as a Revit limit: if Revit turns out to accept
# something smaller, text that small is still not worth a type of its own.
MIN_TEXT_SIZE = 1.0 / 64.0 / 12.0

# A sheet of details drawn at different scales has several text heights.
# At most this many new text types are ever offered.

# How well a real unit scale has to place the text before the tool stops
# looking (below this, it falls back to fitting the drawing onto the import).
LAST_RESORT_THRESHOLD = 0.6

# Elements are created in committed batches: a batch Revit refuses is split
# and retried instead of the whole conversion being lost.
BATCH_SIZE = 2000
MAX_SPLIT_DEPTH = 8


# ----------------------------------------------------------------------------
# Selection helpers
# ----------------------------------------------------------------------------
class ImportInstanceFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, ImportInstance)

    def AllowReference(self, reference, position):
        return False


def get_import_instance():
    for el in revit.get_selection().elements:
        if isinstance(el, ImportInstance):
            return el
    try:
        ref = uidoc.Selection.PickObject(
            ObjectType.Element, ImportInstanceFilter(),
            "Select an imported DWG instance to explode cleanly")
        return doc.GetElement(ref.ElementId)
    except OperationCanceledException:
        return None


# ----------------------------------------------------------------------------
# Failure handling - a bad curve must never roll back the whole conversion
# ----------------------------------------------------------------------------
class FailureSwallower(IFailuresPreprocessor):
    """Resolve every failure Revit posts while committing.

    Errors like "Line is too short" are modal and cannot be ignored, so Revit
    stops on a dialog whose only real answer is Cancel - which throws away
    everything the transaction created. Deleting the few offending elements
    here keeps all the good geometry and no dialog is ever shown.
    """

    def __init__(self):
        self.deleted_ids = set()
        self.descriptions = defaultdict(int)
        # The ids this batch created, refreshed by create_in_batches right
        # before each commit. A failure that names anything else is
        # somebody's model, never ours to delete: that batch rolls back
        # and is split, so the one curve that upset Revit is dropped alone
        # and the existing element is left exactly as it was.
        self.own_ids = set()
        self.foreign = 0        # batches rolled back for that reason

    def _only_ours(self, failure):
        """True when every element the failure names was made by this
        batch. A failure that names nothing is not ours either: there is
        nothing to delete, so it must roll back."""
        try:
            keys = [eid.ToString() for eid in failure.GetFailingElementIds()]
        except Exception:
            return False
        return bool(keys) and all(key in self.own_ids for key in keys)

    @property
    def rejected(self):
        return len(self.deleted_ids)

    def _remember(self, failure):
        try:
            for element_id in failure.GetFailingElementIds():
                self.deleted_ids.add(element_id.ToString())
        except Exception:
            pass

    def PreprocessFailures(self, accessor):
        try:
            failures = list(accessor.GetFailureMessages())
        except Exception:
            return FailureProcessingResult.Continue
        if not failures:
            return FailureProcessingResult.Continue

        handled = False
        unresolved = []
        for failure in failures:
            try:
                self.descriptions[failure.GetDescriptionText()] += 1
            except Exception:
                pass
            try:
                severity = failure.GetSeverity()
            except Exception:
                severity = None

            if severity == FailureSeverity.Warning:
                try:
                    accessor.DeleteWarning(failure)
                    handled = True
                except Exception:
                    pass
                continue

            # An error. Deleting is only ever an answer for elements this
            # batch made itself; anything else rolls the batch back so the
            # split isolates the culprit without touching the model.
            if not self._only_ours(failure):
                self.foreign += 1
                return FailureProcessingResult.ProceedWithRollBack

            try:
                if failure.HasResolutionOfType(
                        FailureResolutionType.DeleteElements):
                    failure.SetCurrentResolutionType(
                        FailureResolutionType.DeleteElements)
                    self._remember(failure)
                    accessor.ResolveFailure(failure)
                    handled = True
                    continue
            except Exception:
                pass
            unresolved.append(failure)

        if unresolved:
            ids = List[ElementId]()
            seen = set()
            for failure in unresolved:
                try:
                    for element_id in failure.GetFailingElementIds():
                        key = element_id.ToString()
                        if key in seen:
                            continue
                        seen.add(key)
                        self.deleted_ids.add(key)
                        ids.Add(element_id)
                except Exception:
                    pass
            if ids.Count:
                try:
                    accessor.DeleteElements(ids)
                    handled = True
                except Exception as ex:
                    logger.debug("Could not delete failing elements: %s", ex)

        if handled:
            return FailureProcessingResult.ProceedWithCommit
        return FailureProcessingResult.Continue


def start_transaction(name, swallower):
    """Start a transaction that resolves its own failures instead of asking."""
    transaction = DBTransaction(doc, name)
    transaction.Start()
    swallower.own_ids = set()      # nothing made yet in this transaction
    options = transaction.GetFailureHandlingOptions()
    options.SetFailuresPreprocessor(swallower)
    options.SetClearAfterRollback(True)
    options.SetForcedModalHandling(True)
    transaction.SetFailureHandlingOptions(options)
    return transaction


def close_transaction(transaction):
    """Commit, and report whether the work actually survived."""
    try:
        return transaction.Commit() == TransactionStatus.Committed
    except Exception as ex:
        logger.debug("Commit failed: %s", ex)
        try:
            if transaction.HasStarted():
                transaction.RollBack()
        except Exception:
            pass
        return False


def create_in_batches(name, items, action, swallower, made, depth=0,
                      errors=None):
    """Run `action` over `items` in committed batches.

    `action` returns a list of (type_name, ElementId) for whatever it made.
    A batch Revit refuses to commit is split in half and retried, so one
    impossible item can never take the rest of the drawing down with it.
    Returns the number of items that produced nothing.
    """
    if not items:
        return 0

    if depth == 0:
        failed = 0
        for start in range(0, len(items), BATCH_SIZE):
            failed += create_in_batches(
                name, items[start:start + BATCH_SIZE], action, swallower,
                made, 1, errors)
        return failed

    transaction = start_transaction(name, swallower)
    batch_made = []
    failed = 0
    for item in items:
        try:
            result = action(item)
        except Exception as ex:
            logger.debug("%s: item skipped (%s)", name, ex)
            if errors is not None:
                errors[str(ex).strip().splitlines()[0][:150]] += 1
            failed += 1
            continue
        if result:
            batch_made.extend(result)
        else:
            failed += 1

    # What the swallower may delete if Revit complains at commit: exactly
    # what this batch made, nothing that was already in the model.
    swallower.own_ids = set(eid.ToString() for _, eid in batch_made)
    if close_transaction(transaction):
        made.extend(batch_made)
        return failed

    logger.debug("%s: batch of %d rolled back, splitting", name, len(items))
    if len(items) == 1 or depth >= MAX_SPLIT_DEPTH:
        return len(items)
    half = len(items) // 2
    return (create_in_batches(name, items[:half], action, swallower,
                              made, depth + 1, errors)
            + create_in_batches(name, items[half:], action, swallower,
                                made, depth + 1, errors))


def count_survivors(records):
    """Count only the elements still in the model once everything committed."""
    counts = defaultdict(int)
    total = 0
    for type_name, element_id in records:
        try:
            if doc.GetElement(element_id) is not None:
                counts[type_name] += 1
                total += 1
        except Exception:
            pass
    return total, counts


def element_alive(element_id):
    """True when the element is still in the model."""
    try:
        return doc.GetElement(element_id) is not None
    except Exception:
        return False


# ----------------------------------------------------------------------------
# Pattern / weight introspection
# ----------------------------------------------------------------------------
SOLID_PATTERN_ID = LinePatternElement.GetSolidPatternId()


def category_pattern_info(category):
    try:
        pat_id = category.GetLinePatternId(GraphicsStyleType.Projection)
    except Exception:
        return True, ""
    if pat_id is None or pat_id == ElementId.InvalidElementId \
            or pat_id == SOLID_PATTERN_ID:
        return True, "solid"
    pat_el = doc.GetElement(pat_id)
    name = pat_el.Name.lower() if pat_el is not None else ""
    return False, name


def category_weight(category):
    try:
        w = category.GetLineWeight(GraphicsStyleType.Projection)
        return int(w) if w is not None else None
    except Exception:
        return None


def pattern_family(pattern_name):
    if not pattern_name or pattern_name == "solid":
        return "solid"
    for family, keywords in PATTERN_FAMILIES:
        for kw in keywords:
            if kw in pattern_name:
                return family
    return "otherdashed"


# ----------------------------------------------------------------------------
# Existing line styles (NEVER creates any)
# ----------------------------------------------------------------------------
def line_style_parent():
    """The category whose subcategories are the line styles this document
    offers: the family's own category inside a family (Detail Items >
    Medium Lines...), the Lines category in a project. A family whose
    category has no subcategories falls back to Lines."""
    if IS_FAMILY:
        try:
            family_category = doc.OwnerFamily.FamilyCategory
            if family_category is not None \
                    and family_category.SubCategories.Size > 0:
                return family_category
        except Exception as ex:
            logger.debug("Family category not readable: %s", ex)
    return doc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines)


def new_detail_curve(view, curve):
    """The one call that differs between a family and a project."""
    if IS_FAMILY:
        return doc.FamilyCreate.NewDetailCurve(view, curve)
    return doc.Create.NewDetailCurve(view, curve)


def _drawable_category_ids():
    """The ids of the line-weight categories, whatever language Revit speaks.
    Names this Revit does not know are skipped, never an error."""
    ids = set()
    for name in DRAWABLE_CATEGORY_NAMES:
        category = getattr(BuiltInCategory, name, None)
        if category is not None:
            try:
                ids.add(int(category))
            except Exception:
                pass
    return ids


def is_drafting_style(subcat, lname, drawable_ids):
    """True for a line style that is meant for drawing a detail."""
    if any(kw in lname for kw in BLACKLIST_KEYWORDS):
        return False
    if not lname.startswith("<"):
        return True                 # a style somebody made, or a family's own
    try:
        if int(_id_val(subcat.Id)) in drawable_ids:
            return True
    except Exception:
        pass
    return any(kw in lname for kw in DRAWABLE_BUILTINS)


def collect_line_styles(strict=True):
    """The line styles to choose from. `strict` offers only those meant for
    drawing; a document where that leaves nothing (a Revit whose own styles
    are named in another language and carry no recognised category) is read
    again without it, which keeps only the old blacklist."""
    styles = []
    lines_cat = line_style_parent()
    if lines_cat is None:
        return styles
    drawable_ids = _drawable_category_ids()
    for subcat in lines_cat.SubCategories:
        name = subcat.Name
        lname = name.lower()
        if strict:
            if not is_drafting_style(subcat, lname, drawable_ids):
                continue
        elif any(kw in lname for kw in BLACKLIST_KEYWORDS):
            continue
        gs = subcat.GetGraphicsStyle(GraphicsStyleType.Projection)
        if gs is None:
            continue
        is_solid, pat_name = category_pattern_info(subcat)
        styles.append({
            "name": name,
            "lname": lname,
            "style": gs,
            "solid": is_solid,
            "pattern": pat_name,
            "family": pattern_family(pat_name if not is_solid else "solid"),
            "weight": category_weight(subcat),
        })
    if strict and not styles:
        return collect_line_styles(strict=False)
    return styles


def layer_weight_hint(layer_lname):
    """"wide", "thin" or None: what the name of a CAD layer suggests."""
    if not layer_lname:
        return None
    words = [word for word in re.split(r"[^a-z]+", layer_lname) if word]
    for hints, answer in ((THIN_LAYER_HINTS, "thin"),
                          (WIDE_LAYER_HINTS, "wide")):
        if any(word.startswith(hint) for word in words for hint in hints):
            return answer
    return None


def get_cad_layer_info(geo_obj):
    name, is_solid, pat_name, weight = None, True, "solid", None
    try:
        gs_id = geo_obj.GraphicsStyleId
        if gs_id and gs_id != ElementId.InvalidElementId:
            gs = doc.GetElement(gs_id)
            if isinstance(gs, GraphicsStyle):
                cat = gs.GraphicsStyleCategory
                if cat is not None:
                    name = cat.Name
                    is_solid, pat_name = category_pattern_info(cat)
                    weight = category_weight(cat)
    except Exception:
        pass
    return name, is_solid, pat_name, weight


def build_style_matcher(styles):
    cache = {}
    solid_styles = [s for s in styles if s["solid"]]
    dashed_styles = [s for s in styles if not s["solid"]]

    def default_solid():
        by_name = dict((s["lname"], s) for s in solid_styles)
        for fb in SOLID_FALLBACK_ORDER:
            if fb in by_name:
                return by_name[fb]
        return solid_styles[0] if solid_styles else (
            styles[0] if styles else None)

    def default_dashed():
        for s in dashed_styles:
            if s["family"] == "dash":
                return s
        return dashed_styles[0] if dashed_styles else default_solid()

    def score(candidate, family, weight, layer_lname):
        pts = 0
        hint = layer_weight_hint(layer_lname)
        if hint == "wide":
            if "wide" in candidate["lname"]:
                pts += 70
            elif "medium" in candidate["lname"]:
                pts += 35
        elif hint == "thin" and "thin" in candidate["lname"]:
            pts += 70
        # When nothing else tells two styles apart, an ordinary layer takes
        # the plain thin line rather than whichever style sorts first.
        if "thin" in candidate["lname"]:
            pts += 3
        elif "medium" in candidate["lname"]:
            pts += 2
        if candidate["family"] == family:
            pts += 100
        if weight is not None and candidate["weight"] is not None:
            pts += max(0, 50 - 10 * abs(candidate["weight"] - weight))
        elif weight is None:
            if "thin" in candidate["lname"]:
                pts += 15
            elif "medium" in candidate["lname"]:
                pts += 10
        if layer_lname:
            if layer_lname == candidate["lname"]:
                pts += 40
            elif layer_lname in candidate["lname"] \
                    or candidate["lname"] in layer_lname:
                pts += 20
        return pts

    def match(layer_name, is_solid, pattern_name, weight):
        key = (layer_name, is_solid, pattern_name, weight)
        if key in cache:
            return cache[key]
        pool = solid_styles if is_solid else dashed_styles
        if pool:
            family = pattern_family(pattern_name)
            lname = (layer_name or "").lower()
            result = max(pool, key=lambda s: score(s, family, weight, lname))
        else:
            result = default_solid() if is_solid else default_dashed()
        if result is None:
            result = default_solid()
        cache[key] = result
        return result

    return match


# ----------------------------------------------------------------------------
# Naming and sizing helpers
# ----------------------------------------------------------------------------
def Element_name(el):
    """Element.Name accessor that works in IronPython."""
    from Autodesk.Revit.DB import Element
    return Element.Name.__get__(el)


def type_name(element):
    """An element type's name, never raising."""
    try:
        return Element_name(element)
    except Exception:
        pass
    try:
        return element.Name
    except Exception:
        return "Unnamed"


def note_type_name(note):
    """Type name of a text note, never raising - a naming problem must never
    make the tool report a note it really did create as failed."""
    try:
        return type_name(note.TextNoteType)
    except Exception:
        return "Text"


# mm or inches for every size this tool writes: the question windows, the
# names of the text types it makes, the report. None follows the document's
# own length units, which is where it starts; the toggle in either question
# window overrides it for the rest of the run.
UNIT_MODE = [None]


def use_metric():
    return document_is_metric() if UNIT_MODE[0] is None else UNIT_MODE[0]


def document_is_metric():
    try:
        from Autodesk.Revit.DB import SpecTypeId, UnitTypeId
        unit = doc.GetUnits().GetFormatOptions(SpecTypeId.Length) \
            .GetUnitTypeId()
        return (unit == UnitTypeId.Millimeters
                or unit == UnitTypeId.Centimeters
                or unit == UnitTypeId.Meters)
    except Exception:
        return False


def format_size(value):
    """A length in feet written the way a text type is usually named."""
    if value is None:
        return "?"
    if use_metric():
        millimetres = value * 304.8
        # One decimal turns everything under Revit's floor into "0.0 mm",
        # which hides the very number that says the scale is wrong.
        if millimetres < 0.1:
            return "{:.3f} mm".format(millimetres)
        return "{:.1f} mm".format(millimetres)
    inches = value * 12.0
    sixtyfourths = int(round(inches * 64.0))
    if sixtyfourths <= 0:
        return '{:.4f}"'.format(inches)
    whole, remainder = divmod(sixtyfourths, 64)
    if remainder == 0:
        return '{}"'.format(whole)
    numerator, denominator = remainder, 64
    while numerator % 2 == 0 and denominator % 2 == 0:
        numerator //= 2
        denominator //= 2
    if whole:
        return '{} {}/{}"'.format(whole, numerator, denominator)
    return '{}/{}"'.format(numerator, denominator)


def _size_bucket_key(size):
    """The bucket a paper size falls into: sizes within about 5% of each
    other share one key. Exposed (not just inlined in group_sizes below) so
    a type override picked in the options window for one group still
    matches later, even once the final tally of notes for that size shifts
    a group's own average slightly (leader-carried notes added after the
    window read, for one)."""
    return int(round(math.log(size) * 20.0))


def group_sizes(sizes, limit=None):
    """[(size, how many notes use it)], most used first.

    Sizes within about 5 % of each other count as one size, so a single
    drawing height does not look like a dozen. Details drawn at different
    scales DO give genuinely different sizes - that is what this is for.
    """
    buckets = defaultdict(list)
    for size in sizes:
        if size and size > 0:
            buckets[_size_bucket_key(size)].append(size)
    groups = [(sum(group) / len(group), len(group))
              for group in buckets.values()]
    groups.sort(key=lambda item: (-item[1], -item[0]))
    return groups[:limit] if limit else groups


def dominant_size(sizes):
    """(the size most of the text uses, how many distinct sizes there are)."""
    groups = group_sizes(sizes)
    if not groups:
        return None, 0
    return groups[0][0], len(groups)


def taken_type_names(of_class=None):
    """Every type name already in the document, in one pass."""
    return set(type_name(element) for element
               in FilteredElementCollector(doc).OfClass(
                   of_class or TextNoteType))


def unique_type_name(wanted, of_class=None, taken=None):
    """`wanted`, or the next free variation of it.

    `taken` lets a caller naming several types at once read the document
    once instead of once per name.
    """
    if taken is None:
        taken = taken_type_names(of_class)
    if wanted not in taken:
        return wanted
    index = 2
    while "{} {}".format(wanted, index) in taken:
        index += 1
    return "{} {}".format(wanted, index)


def proposed_type_name(size, base_type, reserved=None, taken=None):
    """Name for a new text type, following how this project names them.

    `reserved` holds names already promised to other new types in this run,
    which do not exist yet and so would not be seen as taken; `taken` is the
    document's own names, read once by the caller.
    """
    font = ""
    try:
        parameter = base_type.get_Parameter(BuiltInParameter.TEXT_FONT)
        if parameter is not None:
            font = parameter.AsString() or ""
    except Exception:
        pass
    label = "{} {}".format(format_size(size), font).strip()
    name = unique_type_name("{} (CAD)".format(label), taken=taken)
    index = 2
    while reserved and name in reserved:
        name = unique_type_name("{} (CAD) {}".format(label, index),
                                taken=taken)
        index += 1
    return name


class ConversionError(Exception):
    """Something the user can act on, raised by the tool itself.

    NOT Autodesk.Revit.Exceptions.InvalidOperationException: that class has
    no public constructor, so raising it from IronPython throws "Cannot
    create instances of InvalidOperationException because it has no public
    constructors" and the real reason never reaches anybody.
    """


def set_text_size(text_type, wanted):
    """Set the text size, or the largest size Revit will accept under it.

    Revit caps how big a text type may be, and CAD text can ask for more.
    Returns the size that stuck, or None if none did.
    """
    parameter = text_type.get_Parameter(BuiltInParameter.TEXT_SIZE)
    if parameter is None or parameter.IsReadOnly:
        return None

    def attempt(value):
        try:
            return bool(parameter.Set(value))
        except Exception:
            return False

    if attempt(wanted):
        return wanted
    low, high, best = 0.0, wanted, None
    for _ in range(14):
        middle = (low + high) * 0.5
        if attempt(middle):
            best, low = middle, middle
        else:
            high = middle
    return best


def create_cad_text_type(base_type, size, name):
    """Duplicate an existing text type at the size the CAD text needs.

    This is the ONE thing the tool can add to the project, and only after
    you have said yes to the question it asks first.
    Returns (new type, size actually applied).
    """
    new_type = base_type.Duplicate(name)
    if not type_has_arrowhead(new_type):
        arrowhead = any_arrowhead_id()
        if arrowhead is not None:
            try:
                new_type.get_Parameter(
                    BuiltInParameter.LEADER_ARROWHEAD).Set(arrowhead)
            except Exception as ex:
                logger.debug("Arrowhead could not be set: %s", ex)
    applied = set_text_size(new_type, size)
    if applied is None:
        try:
            doc.Delete(new_type.Id)
        except Exception:
            pass
        raise ConversionError(
            "Revit would not accept a text size of {}.".format(
                format_size(size)))
    return new_type, applied





# ----------------------------------------------------------------------------
# Curve validation and rebuilding
# ----------------------------------------------------------------------------
def curve_length(curve):
    """Length of a curve, or None when it cannot be measured.

    A whole circle has no ends, so Revit refuses to measure it; its length
    is its circumference, which is what decides whether it is big enough to
    draw at all.
    """
    try:
        return curve.Length
    except Exception:
        pass
    try:
        if not curve.IsBound and curve.IsCyclic:
            return 2.0 * math.pi * curve.Radius
    except Exception:
        pass
    try:
        return curve.ApproximateLength
    except Exception:
        return None


def curve_is_long_enough(curve):
    """False for the hairline fragments Revit reports as 'Line is too short'."""
    if curve is None:
        return False
    length = curve_length(curve)
    if length is None:
        return True  # unmeasurable: leave the judgement to Revit
    return length > MIN_CURVE_LENGTH


def bind_curve(curve):
    """A whole circle stays whole.

    Revit takes a full circle or ellipse as ONE detail curve, so it is
    handed over exactly as the CAD has it. split_curve() below is only
    reached if Revit refuses it.
    """
    return [curve]


def is_whole_circle(curve):
    """True for a curve that closes on itself: a circle or a full ellipse.

    Revit hands a circle over either with no bounds at all or bounded
    across its whole period, depending on where it came from, so both
    have to count.
    """
    try:
        if not curve.IsCyclic:
            return False
    except Exception:
        return False
    try:
        if not curve.IsBound:
            return True
    except Exception:
        return False
    try:
        span = curve.GetEndParameter(1) - curve.GetEndParameter(0)
        return abs(abs(span) - curve.Period) < 1e-9
    except Exception:
        return False


def split_curve(curve):
    """Two halves of a whole circle or ellipse, for when Revit will not
    take it in one piece. Ask is_whole_circle() before calling this."""
    halves = []
    for start, end in ((0.0, math.pi), (math.pi, 2.0 * math.pi)):
        try:
            half = curve.Clone()
            half.MakeBound(start, end)
        except Exception:
            return []
        halves.append(half)
    return halves


def full_circle(centre, radius, normal, through):
    """One whole circle as a single curve, or None when it cannot be made."""
    try:
        x_axis = (through - centre).Normalize()
        y_axis = normal.CrossProduct(x_axis).Normalize()
        circle = Arc.Create(centre, radius, 0.0, 2.0 * math.pi,
                            x_axis, y_axis)
    except Exception:
        return None
    return circle if curve_is_long_enough(circle) else None


def _distance_to_plane(point, origin, normal):
    return normal.DotProduct(point - origin)


def plane_offset(curve, view):
    """Signed distance from a curve to the view plane, or None when the
    curve is not parallel to it (and so cannot simply be moved onto it)."""
    origin = view.Origin
    normal = view.ViewDirection
    if is_whole_circle(curve):
        # A whole circle has no ends to walk along, so Revit will not
        # tessellate it. Its own plane says everything - and without this
        # a circle sitting at another height could never be moved at all.
        try:
            centre, own_normal = curve.Center, curve.Normal
        except Exception:
            return None
        try:
            if abs(abs(own_normal.DotProduct(normal)) - 1.0) > 1e-9:
                return None
        except Exception:
            return None
        return _distance_to_plane(centre, origin, normal)
    try:
        points = curve.Tessellate()
    except Exception:
        return None
    if not points:
        return None
    first = _distance_to_plane(points[0], origin, normal)
    for point in points[1:]:
        if abs(_distance_to_plane(point, origin, normal) - first) > 1e-7:
            return None
    return first


def move_to_view_plane(curve, view):
    """Translate a curve onto the view plane, keeping its type intact.

    This is what stops arcs, circles and splines being chopped into
    segments when the CAD sits at a different height than the view.
    """
    offset = plane_offset(curve, view)
    if offset is None:
        return None
    if abs(offset) < 1e-12:
        return curve
    try:
        shift = Transform.CreateTranslation(
            view.ViewDirection.Multiply(-offset))
        return curve.CreateTransformed(shift)
    except Exception:
        return None


def make_projector(view):
    origin = view.Origin
    normal = view.ViewDirection

    def project(pt):
        d = normal.DotProduct(pt - origin)
        return pt - normal.Multiply(d)
    return project


def weld_points(points):
    """Drop points closer together than Revit's minimum line length.

    Welding - rather than skipping the short segments - is what keeps a
    chain of points continuous. Skipping one leaves a gap, and Revit then
    answers "This curve will make the loop discontinuous".
    """
    clean = []
    for point in points:
        if not clean or point.DistanceTo(clean[-1]) >= MIN_CURVE_LENGTH:
            clean.append(point)
    return clean


def flatten_curve(curve, view):
    """Last resort: project a curve onto the view plane, keeping its shape
    where possible. Only used once Revit has refused both the curve as it
    stands and the same curve moved onto the view plane."""
    project = make_projector(view)
    try:
        if isinstance(curve, Line):
            start = project(curve.GetEndPoint(0))
            end = project(curve.GetEndPoint(1))
            if start.DistanceTo(end) <= MIN_CURVE_LENGTH:
                return []
            return [Line.CreateBound(start, end)]

        if isinstance(curve, Arc) and curve.IsBound \
                and not is_whole_circle(curve):
            start = project(curve.GetEndPoint(0))
            end = project(curve.GetEndPoint(1))
            middle = project(curve.Evaluate(0.5, True))
            arc = Arc.Create(start, end, middle)
            return [arc] if curve_is_long_enough(arc) else []

        points = weld_points([project(p) for p in curve.Tessellate()])
        segments = []
        for index in range(len(points) - 1):
            try:
                segments.append(
                    Line.CreateBound(points[index], points[index + 1]))
            except Exception:
                pass
        return segments
    except Exception:
        return []


# --- polyline -> arcs and long lines ----------------------------------------
def _turn_angle(a, b, c):
    """How sharply the line turns at b, in radians, or None."""
    u, v = b - a, c - b
    lu, lv = u.GetLength(), v.GetLength()
    if lu < 1e-12 or lv < 1e-12:
        return None
    cosine = max(-1.0, min(1.0, u.DotProduct(v) / (lu * lv)))
    return math.acos(cosine)


def _circumcircle(a, b, c):
    """(centre, radius) of the circle through three points, or None.

    Worked out directly rather than through Arc.Create, because the fitter
    tries this hundreds of times per curve.
    """
    ab, ac = b - a, c - a
    cross = ab.CrossProduct(ac)
    norm = cross.GetLength()
    if norm < 1e-12:
        return None                                   # the points are in line
    centre_offset = (cross.CrossProduct(ab).Multiply(ac.GetLength() ** 2)
                     + ac.CrossProduct(cross).Multiply(ab.GetLength() ** 2)
                     ).Multiply(1.0 / (2.0 * norm * norm))
    return a + centre_offset, centre_offset.GetLength()


def fit_tolerance(points):
    """How far a rebuilt curve may sit from the polyline it replaces.

    Tied to the size of the facets themselves: a tessellation is already an
    approximation of the real curve, so a rebuilt arc that stays well inside
    one facet's own error is closer to the true shape than the facets were.
    """
    lengths = [points[index].DistanceTo(points[index + 1])
               for index in range(len(points) - 1)]
    lengths = [length for length in lengths if length > 0]
    if not lengths:
        return MIN_CURVE_LENGTH
    lengths.sort()
    median = lengths[len(lengths) // 2]
    return max(MIN_CURVE_LENGTH * 0.25, median * SIMPLIFY_RATIO)


def _off_segment(point, start, end):
    """Distance from a point to the segment start-end."""
    direction = end - start
    length = direction.GetLength()
    if length < 1e-12:
        return point.DistanceTo(start)
    along = (point - start).DotProduct(direction) / (length * length)
    along = max(0.0, min(1.0, along))
    return point.DistanceTo(start + direction.Multiply(along))


def _first_off_circle(points, start, end, centre, radius, tolerance):
    """The first index between start and end that is not on the circle."""
    for index in range(start + 1, end):
        if abs(points[index].DistanceTo(centre) - radius) > tolerance:
            return index
    return None


def _furthest_arc(points, start, tolerance, cap=ARC_MAX_POINTS):
    """How far one arc can reach from `start` before it stops fitting.

    Grows a circle point by point, then checks the whole span once and
    pulls back to the last point that really sits on it.
    """
    limit = min(len(points) - 1, start + cap)
    end = start + 2
    while end <= limit:
        # a polygon is not a faceted curve, however round it looks
        angle = _turn_angle(points[end - 2], points[end - 1], points[end])
        if angle is None or angle > ARC_MAX_TURN:
            break
        circle = _circumcircle(points[start], points[(start + end) // 2],
                               points[end])
        if circle is None:
            break
        centre, radius = circle
        if abs(points[end].DistanceTo(centre) - radius) > tolerance:
            break
        end += 1
    end -= 1

    while end - start >= 2:
        circle = _circumcircle(points[start], points[(start + end) // 2],
                               points[end])
        if circle is None:
            end -= 1
            continue
        centre, radius = circle
        bad = _first_off_circle(points, start, end, centre, radius, tolerance)
        if bad is None:
            return end
        end = bad - 1
    return start


def _furthest_line(points, start, tolerance, cap=LINE_MAX_POINTS):
    """How far one straight line can reach from `start`.

    This is what collapses a run of nearly-collinear facets into the single
    line it was drawn as.
    """
    limit = min(len(points) - 1, start + cap)
    end = start + 1
    best = end
    while end <= limit:
        off = 0.0
        for index in range(start + 1, end):
            off = max(off, _off_segment(points[index], points[start],
                                        points[end]))
            if off > tolerance:
                break
        if off > tolerance:
            break
        best = end
        end += 1
    return best


def _arc_through(points, tolerance=None):
    """An Arc through the first/middle/last point, but only if EVERY point
    really sits on it. Returns None when the run is not circular."""
    circle = _circumcircle(points[0], points[len(points) // 2], points[-1])
    if circle is None:
        return None
    centre, radius = circle
    if tolerance is None:
        tolerance = max(1e-5, radius * 1e-4)
    for point in points:
        if abs(point.DistanceTo(centre) - radius) > tolerance:
            return None
    try:
        arc = Arc.Create(points[0], points[-1], points[len(points) // 2])
    except Exception:
        return None
    return arc if curve_is_long_enough(arc) else None


def simplify_points(points, census=None):
    """Rebuild a run of points as the fewest arcs and lines that still
    describe it, within a tolerance taken from the facets themselves.

    A curve that arrived as two hundred tiny segments comes back as a
    handful of arcs; a straight edge broken into pieces comes back as one
    line; a corner or a genuine polygon is left exactly as it was.
    """
    tolerance = fit_tolerance(points)
    made = []
    index = 0
    count = len(points)
    while index < count - 1:
        arc_end = _furthest_arc(points, index, tolerance)
        line_end = _furthest_line(points, index, tolerance)

        if arc_end - index >= ARC_MIN_POINTS - 1 and arc_end >= line_end:
            arc = _arc_through(points[index:arc_end + 1], tolerance)
            if arc is not None:
                made.append(arc)
                if census is not None:
                    census["rebuilt_arcs"] += 1
                index = arc_end
                continue

        try:
            made.append(Line.CreateBound(points[index], points[line_end]))
        except Exception:
            pass
        index = line_end
    return made


def _closed_circle(points):
    """The single circle a closed faceted polyline describes.

    Returns one whole circle, so a faceted ring comes back as one element
    you can select and give a radius - not as two half arcs. Only if Revit
    cannot build the whole circle does it fall back to two halves.
    """
    ring = points[:-1] if points[0].DistanceTo(points[-1]) < 1e-9 else points
    count = len(ring)
    if count < 8:
        return None
    # A regular polygon's corners sit exactly on a circle too, so the shape
    # alone proves nothing: an octagon is only a circle if it turns gently.
    for index in range(count):
        angle = _turn_angle(ring[index - 2], ring[index - 1], ring[index])
        if angle is None or angle > ARC_MAX_TURN:
            return None
    a, b, c = ring[0], ring[count // 3], ring[2 * count // 3]
    try:
        probe = Arc.Create(a, c, b)
        center, radius = probe.Center, probe.Radius
    except Exception:
        return None
    tolerance = max(1e-5, radius * 1e-4)
    for point in ring:
        if abs(point.DistanceTo(center) - radius) > tolerance:
            return None
    whole = full_circle(center, radius, probe.Normal, a)
    if whole is not None:
        return [whole]
    try:
        first = Arc.Create(a, c, b)
        second = Arc.Create(c, a, ring[(5 * count) // 6])
    except Exception:
        return None
    if not curve_is_long_enough(first) or not curve_is_long_enough(second):
        return None
    return [first, second]


def curves_from_points(points, census=None):
    """Turn a polyline's points into curves, rebuilding arcs where the
    vertices demonstrably lie on a circle."""
    clean = weld_points(points)
    if len(clean) < 2:
        return []

    def bump(key, amount=1):
        if census is not None:
            census[key] += amount

    def lines(subset):
        made = []
        for index in range(len(subset) - 1):
            try:
                made.append(Line.CreateBound(subset[index], subset[index + 1]))
            except Exception:
                pass
        return made

    if not REBUILD_ARCS_FROM_POLYLINES or len(clean) < ARC_MIN_POINTS:
        return lines(clean)

    closed = clean[0].DistanceTo(clean[-1]) < MIN_CURVE_LENGTH
    if closed:
        circle = _closed_circle(clean)
        if circle:
            bump("rebuilt_arcs", len(circle))
            if len(circle) == 1:
                bump("rebuilt_circles")
            return circle

    made = simplify_points(clean, census)
    if not made:
        return lines(clean)
    bump("facets_replaced", max(0, (len(clean) - 1) - len(made)))
    return made


def _point_key(point):
    return (round(point.X, 6), round(point.Y, 6), round(point.Z, 6))


def chain_lines(curves):
    """Walk lines that meet end to end and return the runs they draw.

    Imported CAD often arrives as one separate Line per facet rather than as
    a polyline, so a faceted circle is a dozen unrelated lines. Chaining
    them is what lets the arc rebuilder see the circle at all. A vertex
    where more than two lines meet is a junction, and ends a run.

    Returns [(points, [line indices used]), ...].
    """
    ends = defaultdict(list)
    usable = []
    for index, curve in enumerate(curves):
        try:
            start, finish = curve.GetEndPoint(0), curve.GetEndPoint(1)
        except Exception:
            continue
        if _point_key(start) == _point_key(finish):
            continue
        usable.append(index)
        ends[_point_key(start)].append(index)
        ends[_point_key(finish)].append(index)

    def step(point, index):
        """The one other line meeting `index` at `point`, if there is one."""
        joined = ends.get(_point_key(point))
        if not joined or len(joined) != 2:
            return None
        other = joined[0] if joined[1] == index else joined[1]
        return None if other == index else other

    def far_end(index, point):
        start = curves[index].GetEndPoint(0)
        if _point_key(start) == _point_key(point):
            return curves[index].GetEndPoint(1)
        return start

    used = set()
    runs = []
    for index in usable:
        if index in used:
            continue
        used.add(index)
        start = curves[index].GetEndPoint(0)
        finish = curves[index].GetEndPoint(1)
        members = [index]

        forward = [start, finish]
        current, tip = index, finish
        while True:
            nxt = step(tip, current)
            if nxt is None or nxt in used:
                break
            used.add(nxt)
            members.append(nxt)
            tip = far_end(nxt, tip)
            forward.append(tip)
            current = nxt

        backward = []
        current, tip = index, start
        while True:
            nxt = step(tip, current)
            if nxt is None or nxt in used:
                break
            used.add(nxt)
            members.append(nxt)
            tip = far_end(nxt, tip)
            backward.append(tip)
            current = nxt

        points = list(reversed(backward)) + forward
        if len(points) >= ARC_MIN_POINTS:
            runs.append((points, members))
    return runs


def rebuild_arcs_from_lines(entries, census):
    """Turn runs of separate straight lines back into arcs where their
    vertices sit on a circle. Anything that is not clearly a faceted arc is
    handed back exactly as it came in, untouched.

    entries: [(curve, layer, solid, pattern, weight), ...]
    """
    if not REBUILD_ARCS_FROM_POLYLINES or len(entries) < ARC_MIN_POINTS:
        return entries

    grouped = defaultdict(list)
    for position, entry in enumerate(entries):
        if isinstance(entry[0], Line):
            grouped[entry[1:]].append((position, entry[0]))

    dropped = set()
    rebuilt = []
    for style_key, members in grouped.items():
        if len(members) < ARC_MIN_POINTS:
            continue
        positions = [item[0] for item in members]
        curves = [item[1] for item in members]
        try:
            runs = chain_lines(curves)
        except Exception as ex:
            logger.debug("Line chaining skipped: %s", ex)
            continue
        for points, indices in runs:
            try:
                pieces = curves_from_points(points, census)
            except Exception as ex:
                logger.debug("Arc rebuild skipped: %s", ex)
                continue
            has_arc = any(isinstance(piece, Arc) for piece in pieces)
            if not has_arc and len(pieces) * 2 > len(indices):
                continue        # not curved and barely simpler: leave it
            for index in indices:
                dropped.add(positions[index])
            for piece in pieces:
                rebuilt.append((piece,) + style_key)

    if not rebuilt:
        return entries
    kept = [entry for position, entry in enumerate(entries)
            if position not in dropped]
    return kept + rebuilt


# ----------------------------------------------------------------------------
# CAD leaders -> real Revit leaders
# ----------------------------------------------------------------------------
def any_arrowhead_id():
    """An arrowhead style the project already has, or None.

    A Revit leader only draws an arrow if its text type names one, so a new
    CAD text type is given the same arrowhead the project already uses.
    """
    for text_type in FilteredElementCollector(doc).OfClass(TextNoteType):
        try:
            parameter = text_type.get_Parameter(
                BuiltInParameter.LEADER_ARROWHEAD)
            if parameter is None:
                continue
            arrowhead = parameter.AsElementId()
            if arrowhead and arrowhead != ElementId.InvalidElementId:
                return arrowhead
        except Exception:
            continue
    return None


def type_has_arrowhead(text_type):
    try:
        parameter = text_type.get_Parameter(BuiltInParameter.LEADER_ARROWHEAD)
        if parameter is None:
            return False
        arrowhead = parameter.AsElementId()
        return bool(arrowhead) and arrowhead != ElementId.InvalidElementId
    except Exception:
        return False


def add_note_leaders(note, record, place, view, census=None):
    """Hang the CAD leaders off a text note as real Revit leaders."""
    leaders = record.get("leaders") or []
    if not leaders:
        return 0
    project = make_projector(view)
    try:
        origin = note.Coord
    except Exception:
        origin = None

    made = 0

    def lost():
        # The CAD lines that drew this arrow are already gone by the time the
        # leader is made (they are dropped before the notes are created), so
        # an arrow that fails here is lost with the CAD: it is counted, and
        # the delete confirmation says so.
        if census is not None:
            census["leaders_failed"] += 1

    for leader in leaders:
        points = leader["points"]
        try:
            tip = project(place(points[0][0], points[0][1], 0.0))
        except Exception:
            lost()
            continue
        side = TextNoteLeaderTypes.TNLT_STRAIGHT_R
        if origin is not None:
            try:
                if (tip - origin).DotProduct(view.RightDirection) < 0:
                    side = TextNoteLeaderTypes.TNLT_STRAIGHT_L
            except Exception:
                pass
        try:
            handle = note.AddLeader(side)
        except Exception as ex:
            logger.debug("Leader refused: %s", ex)
            lost()
            continue
        try:
            handle.End = tip
        except Exception as ex:
            logger.debug("Leader tip refused: %s", ex)
            lost()
            continue
        if len(points) >= 3:
            try:
                handle.Elbow = project(place(points[1][0], points[1][1], 0.0))
            except Exception:
                pass
        leader["used"] = True
        made += 1
        if census is not None:
            census["leaders"] += 1
    return made


def leader_paths(records, place, view):
    """The model-space path of every leader that became a Revit leader."""
    project = make_projector(view)
    paths = []
    for record in records:
        for leader in record.get("leaders") or []:
            points = []
            for point in leader["points"]:
                try:
                    points.append(project(place(point[0], point[1], 0.0)))
                except Exception:
                    points = []
                    break
            if len(points) >= 2:
                paths.append(points)
    return paths


def _near_path(point, path, tolerance):
    for index in range(len(path) - 1):
        if _off_segment(point, path[index], path[index + 1]) <= tolerance:
            return True
    return False


def drop_leader_lines(curves, paths):
    """Remove the imported lines that drew a leader Revit now draws itself."""
    if not paths or not curves:
        return curves, 0
    prepared = []
    for path in paths:
        length = sum(path[i].DistanceTo(path[i + 1])
                     for i in range(len(path) - 1))
        tolerance = max(MIN_CURVE_LENGTH * 2.0,
                        length * LEADER_LINE_TOLERANCE * 0.1)
        xs = [point.X for point in path]
        ys = [point.Y for point in path]
        prepared.append((path, tolerance,
                         (min(xs) - tolerance, min(ys) - tolerance,
                          max(xs) + tolerance, max(ys) + tolerance)))

    kept, dropped = [], 0
    for entry in curves:
        try:
            start = entry[0].GetEndPoint(0)
            end = entry[0].GetEndPoint(1)
        except Exception:
            kept.append(entry)
            continue
        remove = False
        for path, tolerance, box in prepared:
            if not (box[0] <= start.X <= box[2] and box[1] <= start.Y <= box[3]
                    and box[0] <= end.X <= box[2]
                    and box[1] <= end.Y <= box[3]):
                continue
            if _near_path(start, path, tolerance) \
                    and _near_path(end, path, tolerance):
                remove = True
                break
        if remove:
            dropped += 1
        else:
            kept.append(entry)
    return kept, dropped


def fill_centre(shape, transform):
    """Roughly where a solid or mesh sits, in model coordinates."""
    try:
        if isinstance(shape, Solid):
            return transform.OfPoint(shape.ComputeCentroid())
    except Exception:
        pass
    try:
        total = [0.0, 0.0, 0.0]
        count = 0
        for index in range(shape.NumTriangles):
            triangle = shape.get_Triangle(index)
            for corner in range(3):
                point = transform.OfPoint(triangle.get_Vertex(corner))
                total[0] += point.X
                total[1] += point.Y
                total[2] += point.Z
                count += 1
        if count:
            return XYZ(total[0] / count, total[1] / count, total[2] / count)
    except Exception:
        pass
    return None


def drop_arrowhead_fills(fills, paths):
    """Remove the filled triangle a CAD leader used for its arrowhead."""
    if not paths or not fills:
        return fills, 0
    tips = []
    for path in paths:
        reach = path[0].DistanceTo(path[1]) * ARROWHEAD_REACH
        tips.append((path[0], max(reach, MIN_CURVE_LENGTH * 4.0)))

    kept, dropped = [], 0
    for shape, transform, layer in fills:
        centre = fill_centre(shape, transform)
        if centre is None:
            kept.append((shape, transform, layer))
            continue
        if any(centre.DistanceTo(tip) <= reach for tip, reach in tips):
            dropped += 1
        else:
            kept.append((shape, transform, layer))
    return kept, dropped


# ----------------------------------------------------------------------------
# Geometry extraction
# ----------------------------------------------------------------------------
def extract_geometry(import_instance):
    """Walk the import geometry and return:
       curves  : [(curve, layer, is_solid, pattern, weight), ...]
       fills   : [(Solid_or_Mesh, accumulated_transform, layer), ...]
       census  : what the import actually contained, for the report
    """
    opts = Options()
    opts.DetailLevel = ViewDetailLevel.Fine
    geo_elem = import_instance.get_Geometry(opts)

    curves, fills = [], []
    census = defaultdict(int)
    if geo_elem is None:
        return curves, fills, census

    def walk(geometry, transform):
        for obj in geometry:
            if isinstance(obj, GeometryInstance):
                sym = obj.GetSymbolGeometry()
                if sym is not None:
                    walk(sym, transform.Multiply(obj.Transform))
            elif isinstance(obj, GeometryElement):
                walk(obj, transform)
            elif isinstance(obj, Curve):
                layer, solid, pat, w = get_cad_layer_info(obj)
                try:
                    census[type(obj).__name__] += 1
                except Exception:
                    census["Curve"] += 1
                try:
                    curves.append((obj.CreateTransformed(transform),
                                   layer, solid, pat, w))
                except Exception:
                    census["skipped"] += 1
            elif isinstance(obj, PolyLine):
                layer, solid, pat, w = get_cad_layer_info(obj)
                pts = [transform.OfPoint(p) for p in obj.GetCoordinates()]
                census["PolyLine"] += 1
                census["polyline_points"] += len(pts)
                for piece in curves_from_points(pts, census):
                    curves.append((piece, layer, solid, pat, w))
            elif isinstance(obj, Solid):
                if obj.Faces.Size > 0:
                    layer, _, _, _ = get_cad_layer_info(obj)
                    fills.append((obj, transform, layer))
                    census["Solid"] += 1
            elif isinstance(obj, Mesh):
                layer, _, _, _ = get_cad_layer_info(obj)
                fills.append((obj, transform, layer))
                census["Mesh"] += 1
            else:
                census["other"] += 1

    walk(geo_elem, Transform.Identity)
    curves = rebuild_arcs_from_lines(curves, census)
    return curves, fills, census


# ----------------------------------------------------------------------------
# CAD hatches: draw them as lines, or leave them out
# ----------------------------------------------------------------------------
# Revit's API cannot tell a hatch's pattern line from any other line - it
# hands both over as plain geometry. The DXF does know: every HATCH carries
# its boundary. Mapping those boundaries into the model is what makes
# "leave the hatches out" possible at all.
#
# Everything here is built so that being unsure means keeping the linework.
# A line is only thrown away when it is on the hatch's own layer, lies
# wholly within its boundary, and the DXF is demonstrably sitting on top of
# the import.

# How close to a boundary a line has to sit before it counts as the outline
# of the hatched area rather than part of its fill, as a share of the
# hatch's own size.
HATCH_EDGE_TOLERANCE = 0.002


def make_view_uv(view):
    """Flatten a model point onto the view's own plane.

    A plan view lies in world X/Y, but a section or an elevation does not,
    and comparing world X/Y there would collapse every hatch onto a line.
    """
    try:
        right, up = view.RightDirection, view.UpDirection
        right.DotProduct(up)
    except Exception:
        return lambda point: (point.X, point.Y)

    def to_uv(point):
        try:
            return (right.DotProduct(point), up.DotProduct(point))
        except Exception:
            return (point.X, point.Y)
    return to_uv


def hatch_areas(hatches, place, to_uv):
    """Hatch boundaries mapped into the view, with a box to test against.

    Solid hatches are left out: Revit imports them as a filled shape rather
    than as lines, so there is no linework of theirs to remove - and using
    one as a boundary would only endanger whatever is drawn over it.
    """
    areas = []
    for hatch in hatches:
        if hatch.get("solid"):
            continue
        rings = []
        for ring in hatch.get("rings") or []:
            points = []
            for x, y in ring:
                try:
                    points.append(to_uv(place(x, y, 0.0)))
                except Exception:
                    points = []
                    break
            if len(points) >= 3:
                rings.append(points)
        if not rings:
            continue
        us = [p[0] for ring in rings for p in ring]
        vs = [p[1] for ring in rings for p in ring]
        box = (min(us), min(vs), max(us), max(vs))
        diagonal = math.hypot(box[2] - box[0], box[3] - box[1])
        if diagonal <= MIN_CURVE_LENGTH:
            continue
        areas.append({
            "hatch": hatch, "rings": rings, "box": box,
            "layer": (hatch.get("layer") or "").strip().lower(),
            "tolerance": max(MIN_CURVE_LENGTH * 2.0,
                             diagonal * HATCH_EDGE_TOLERANCE)})
    return areas


def _inside_rings(u, v, rings):
    """Even/odd test across every ring, so islands read as outside."""
    inside = False
    for ring in rings:
        previous = ring[-1]
        for point in ring:
            if (point[1] > v) != (previous[1] > v):
                span = previous[1] - point[1]
                if span and u < (previous[0] - point[0]) * (v - point[1]) \
                        / span + point[0]:
                    inside = not inside
            previous = point
    return inside


def _on_rings(u, v, rings, tolerance):
    """True when the point sits on a boundary rather than inside it."""
    for ring in rings:
        previous = ring[-1]
        for point in ring:
            du, dv = point[0] - previous[0], point[1] - previous[1]
            span = du * du + dv * dv
            if span <= 0.0:
                along = 0.0
            else:
                along = ((u - previous[0]) * du + (v - previous[1]) * dv) \
                    / span
                along = max(0.0, min(1.0, along))
            if math.hypot(u - (previous[0] + du * along),
                          v - (previous[1] + dv * along)) <= tolerance:
                return True
            previous = point
    return False


def curve_probes(curve):
    """The middle of a curve and its two ends, or None.

    A hatch's fill lines are clipped to the boundary, so their ends sit ON
    it and only the middle proves the line is inside the hatched area. The
    ends still matter: a line that merely CROSSES the hatch has its ends
    outside, and must not be taken for part of the fill.
    """
    middle = None
    try:
        middle = curve.Evaluate(0.5, True)
    except Exception:
        try:
            points = curve.Tessellate()
            middle = points[len(points) // 2] if points else None
        except Exception:
            middle = None
    if middle is None:
        return None
    try:
        return middle, curve.GetEndPoint(0), curve.GetEndPoint(1)
    except Exception:
        return middle, middle, middle


def _is_hatch_fill(probes, areas, to_uv):
    """True only for a line that lies wholly inside one hatched area."""
    middle, start, end = probes
    try:
        mu, mv = to_uv(middle)
    except Exception:
        return False
    for area in areas:
        low_u, low_v, high_u, high_v = area["box"]
        if not (low_u <= mu <= high_u and low_v <= mv <= high_v):
            continue
        rings, tolerance = area["rings"], area["tolerance"]
        if _on_rings(mu, mv, rings, tolerance):
            return False              # this is the outline: keep it
        if not _inside_rings(mu, mv, rings):
            continue
        for tip in (start, end):
            try:
                tu, tv = to_uv(tip)
            except Exception:
                return False
            if not (_inside_rings(tu, tv, rings)
                    or _on_rings(tu, tv, rings, tolerance)):
                return False          # it only crosses the hatch: keep it
        return True
    return False


def drop_hatch_linework(curves, areas, to_uv):
    """Remove the lines Revit drew for a CAD hatch's pattern.

    Only a curve on the hatch's OWN layer, lying wholly inside that hatch
    and not along its boundary, is taken out - so the outline of the
    hatched area survives, a line that crosses it survives, and nothing on
    another layer is touched.
    """
    if not areas or not curves:
        return curves, 0
    by_layer = defaultdict(list)
    for area in areas:
        by_layer[area["layer"]].append(area)

    kept, dropped = [], 0
    for entry in curves:
        pool = by_layer.get((entry[1] or "").strip().lower())
        if not pool:
            kept.append(entry)
            continue
        probes = curve_probes(entry[0])
        if probes is not None and _is_hatch_fill(probes, pool, to_uv):
            dropped += 1
        else:
            kept.append(entry)
    return kept, dropped


# How much of a hatch's pattern has to be there, as a share of the stripes the
# DXF says it has, before the pattern counts as having arrived.
HATCH_STRIPE_SHARE = 0.5


def hatches_with_stripes(curves, areas, to_uv, minimum=2):
    """The indexes (into `areas`) of the hatches whose pattern lines Revit
    DID hand over: curves on the hatch's own layer lying wholly inside it and
    not along its boundary, at least `minimum` of them and, when the DXF
    says how many stripes the pattern has (`segments`), at least
    HATCH_STRIPE_SHARE of those. A single stray line is not a pattern, and
    neither are the few wall joints that happen to sit on the hatch's layer
    inside a big hatched area."""
    by_layer = defaultdict(list)
    for index, area in enumerate(areas):
        by_layer[area["layer"]].append((index, area))
    found = defaultdict(int)
    for entry in curves:
        pool = by_layer.get((entry[1] or "").strip().lower())
        if not pool:
            continue
        probes = curve_probes(entry[0])
        if probes is None:
            continue
        for index, area in pool:
            if _is_hatch_fill(probes, [area], to_uv):
                found[index] += 1
                break
    arrived = set()
    for index, count in found.items():
        stripes = len((areas[index].get("hatch") or {}).get("segments") or [])
        needed = minimum
        if stripes:
            needed = max(minimum, int(math.ceil(stripes * HATCH_STRIPE_SHARE)))
        if count >= needed:
            arrived.add(index)
    return arrived


# ----------------------------------------------------------------------------
# CAD text -> Revit TextNotes
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# The questions: one /slantis window, answered before anything is touched
# ----------------------------------------------------------------------------
_OPTIONS_BODY = u"""
  <ScrollViewer VerticalScrollBarVisibility="Auto">
  <StackPanel Margin="0,0,10,0">
    <TextBlock x:Name="lblWhat" TextWrapping="Wrap" FontSize="12.5"
               Foreground="#77736C" Margin="0,0,0,18"/>

    <StackPanel x:Name="secLineStyles">
      <TextBlock Text="LINE STYLES" Style="{StaticResource SectionHead}"/>
      <TextBlock TextWrapping="Wrap" FontSize="11.5" Foreground="#A6A199"
                 Margin="0,2,0,8"
                 Text="Each CAD layer takes the line style below, matched by line pattern first, then weight, then name. Pick another one if it is wrong."/>
      <Border x:Name="bdrLineStyles" CornerRadius="8" BorderBrush="#ECE9E4"
              BorderThickness="1" MaxHeight="220" Margin="0,0,0,18">
        <ScrollViewer VerticalScrollBarVisibility="Auto">
          <StackPanel x:Name="pnlLineStyles" Margin="8"/>
        </ScrollViewer>
      </Border>
    </StackPanel>

    <TextBlock Text="TEXT AND LEADERS" Style="{StaticResource SectionHead}"/>
    <RadioButton x:Name="rbGeometry" GroupName="text" IsChecked="True"
                 Content="Geometry only"/>
    <TextBlock TextWrapping="Wrap" FontSize="11.5" Foreground="#A6A199"
               Margin="26,2,0,8"
               Text="Lines and curves come in as detail lines on the line styles this document already has. CAD text and dimension values are left behind, so keep the original if you need them."/>
    <RadioButton x:Name="rbText" GroupName="text"
                 Content="Bring in text and leaders from a DXF of this drawing"/>
    <TextBlock TextWrapping="Wrap" FontSize="11.5" Foreground="#A6A199"
               Margin="26,2,0,10"
               Text="Text becomes editable text notes and CAD arrows become real leaders with arrowheads. Revit cannot read either from a DWG, so you will be asked for an ASCII DXF of the same drawing (AutoCAD: Save As, AutoCAD ASCII DXF)."/>
    <StackPanel x:Name="pnlUnits" Orientation="Horizontal"
                Margin="26,0,0,18" Visibility="Collapsed">
      <TextBlock Text="Text sizes in" FontSize="11.5" Foreground="#A6A199"
                 Margin="0,0,10,0" VerticalAlignment="Center"/>
      <RadioButton x:Name="rbMm" GroupName="units" Content="mm"
                   Margin="0,0,14,0"/>
      <RadioButton x:Name="rbInch" GroupName="units" Content="inches"/>
    </StackPanel>

    <StackPanel x:Name="secTextSizes" Margin="26,0,0,0" Visibility="Collapsed">
      <Grid>
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <TextBlock Grid.Column="0" Text="TEXT SIZES"
                   Style="{StaticResource SectionHead}"/>
        <Button x:Name="btnAutoSizes" Grid.Column="1"
                Content="Automatic match" Style="{StaticResource BtnGhost}"
                Padding="12,4" FontSize="11.5"
                VerticalAlignment="Center"/>
      </Grid>
      <TextBlock TextWrapping="Wrap" FontSize="11.5" Foreground="#A6A199"
                 Margin="0,2,0,6"
                 Text="Each CAD text size takes the closest existing text type, or a new type made at the CAD size when the closest one is more than 25% off. Pick another one if it is wrong."/>
      <Border CornerRadius="8" BorderBrush="#ECE9E4" BorderThickness="1"
              Padding="10,8" Margin="0,0,0,10">
        <TextBlock TextWrapping="Wrap" FontSize="11.5" Foreground="#77736C"
                   Text="These sizes come from the scale the CAD was drawn at. If the DWG is not scaled the way this view assumes, every size below is wrong and so is anything picked for it. Checking that the CAD is correctly scaled before exploding is worth the minute it takes."/>
      </Border>
      <TextBlock x:Name="lblSizeStatus" TextWrapping="Wrap" FontSize="11.5"
                 Foreground="#A6A199" Margin="0,0,0,10" Visibility="Collapsed"/>
      <Border x:Name="bdrTextSizes" CornerRadius="8" BorderBrush="#ECE9E4"
              BorderThickness="1" MaxHeight="180" Margin="0,0,0,18"
              Visibility="Collapsed">
        <ScrollViewer VerticalScrollBarVisibility="Auto">
          <StackPanel x:Name="pnlTextSizes" Margin="8"/>
        </ScrollViewer>
      </Border>
    </StackPanel>

    <TextBlock Text="HATCHES" Style="{StaticResource SectionHead}"/>
    <RadioButton x:Name="rbHatchLines" GroupName="hatch" IsChecked="True"
                 Content="Convert hatches to linework"/>
    <TextBlock TextWrapping="Wrap" FontSize="11.5" Foreground="#A6A199"
               Margin="26,2,0,8"
               Text="A concrete or diagonal pattern arrives as many small detail lines. When Revit does not hand over the lines of a pattern, they are drawn from the DXF of the drawing if there is one, and a solid fill comes in as its outline."/>
    <RadioButton x:Name="rbHatchOut" GroupName="hatch"
                 Content="Leave hatches out (needs the DXF)"/>
    <TextBlock TextWrapping="Wrap" FontSize="11.5" Foreground="#A6A199"
               Margin="26,2,0,18"
               Text="Hatched areas come in empty with their outline kept, ready for your own filled regions. Solid fills are never converted either way."/>

    <Border x:Name="bdrDxf" CornerRadius="8" BorderBrush="#ECE9E4"
            BorderThickness="1" Padding="10,8" Margin="26,0,0,18"
            Visibility="Collapsed">
      <Grid>
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <StackPanel Grid.Column="0" VerticalAlignment="Center"
                    Margin="0,0,10,0">
          <TextBlock Text="DXF FILE" FontSize="10.5" Foreground="#A6A199"
                     Margin="0,0,0,2"/>
          <TextBlock x:Name="lblDxfFile" FontSize="12.5" Foreground="#202022"
                     TextTrimming="CharacterEllipsis"/>
        </StackPanel>
        <Button x:Name="btnDxf" Grid.Column="1" Content="Select file"
                Style="{StaticResource BtnGhost}" Padding="14,5"
                FontSize="12" VerticalAlignment="Center"/>
      </Grid>
    </Border>

    <TextBlock Text="THE ORIGINAL IMPORT AFTERWARDS" Style="{StaticResource SectionHead}"/>
    <RadioButton x:Name="rbDelete" GroupName="after" IsChecked="True"
                 Content="Delete it"/>
    <RadioButton x:Name="rbHide" GroupName="after" Margin="0,5,0,0"
                 Content="Hide it in this view"/>
    <RadioButton x:Name="rbKeep" GroupName="after" Margin="0,5,0,0"
                 Content="Keep it as it is"/>
  </StackPanel>
  </ScrollViewer>
"""

_OPTIONS_FOOTER = u"""
  <Grid>
    <TextBlock x:Name="lblFoot" VerticalAlignment="Center" FontSize="11.5"
               Foreground="#A6A199" TextWrapping="Wrap" MaxWidth="330"
               HorizontalAlignment="Left"
               Text="Never creates line styles, fill patterns or filled regions."/>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnGo" Content="Explode" Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
      <Button x:Name="btnCancel" Content="Cancel" Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""


def describe_import(census, fills):
    """One sentence on what the import holds, for the options window."""
    groups = (("Line", "line", "lines"), ("Arc", "arc", "arcs"),
              ("Ellipse", "ellipse", "ellipses"),
              ("NurbSpline", "spline", "splines"),
              ("HermiteSpline", "spline", "splines"),
              ("PolyLine", "polyline", "polylines"))
    counts, order = {}, []
    for key, one, many in groups:
        n = census.get(key, 0)
        if not n:
            continue
        if (one, many) not in counts:
            counts[(one, many)] = 0
            order.append((one, many))
        counts[(one, many)] += n
    bits = []
    for one, many in order:
        n = counts[(one, many)]
        bit = "{:,} {}".format(n, one if n == 1 else many)
        if many == "polylines" and census.get("polyline_points"):
            bit += " ({:,} points)".format(census["polyline_points"])
        bits.append(bit)
    if fills:
        n = len(fills)
        bits.append("{:,} solid {}".format(n, "fill" if n == 1 else "fills"))
    if not bits:
        return ("Revit hands over no 2D geometry for this import; "
                "text may still come from the DXF.")
    if len(bits) == 1:
        listing = bits[0]
    else:
        listing = ", ".join(bits[:-1]) + " and " + bits[-1]
    return "This import holds {}.".format(listing)


# ----------------------------------------------------------------------------
# The two curation grids inside the options window: which line style a CAD
# layer gets, and which text type a CAD text size gets. Both start out
# preselected with exactly what the automatic matchers already choose, so a
# user who never touches either grid gets today's result untouched - the
# grids only ever narrow what match_style()/closest() would have picked on
# their own, they never bypass them.
# ----------------------------------------------------------------------------
GRID_COMBO_WIDTH = 190     # the chooser column of both option grids


def summarize_layer_styles(curves, match_style):
    """One row per CAD layer that carries a curve: how many curves it has
    and the line style match_style() proposes for it, busiest layer first.

    A curve with no layer (no GraphicsStyle at all) is left out: there is
    no CAD name to show or choose between for it, and it still goes through
    match_style() untouched when the geometry is actually created.
    """
    order = []
    by_layer = {}
    for _curve, layer, solid, pattern, weight in curves:
        if not layer:
            continue
        if layer not in by_layer:
            order.append(layer)
            by_layer[layer] = {"layer": layer, "count": 0, "solid": solid,
                               "pattern": pattern, "weight": weight}
        by_layer[layer]["count"] += 1
    rows = []
    for name in order:
        row = by_layer[name]
        row["style"] = match_style(row["layer"], row["solid"],
                                   row["pattern"], row["weight"])
        rows.append(row)
    rows.sort(key=lambda r: -r["count"])
    return rows


def _combo_item(combo, text, tag, selected):
    item = ComboBoxItem()
    item.Content = text
    item.Tag = tag
    combo.Items.Add(item)
    if selected:
        combo.SelectedItem = item
    return item


def _grid_row(caption, detail=None):
    """One line of an options grid: the label on the left, and an empty cell
    on the right for the chooser. A card with stacked radios per row was the
    first shape; with four layers it already pushed the rest of the window
    below the fold, which is the thing this redesign exists to fix.

    Returns (row, cell): add the ComboBox to `cell`.
    """
    row = Grid()
    row.Margin = Thickness(0, 0, 0, 5)
    left = ColumnDefinition()
    left.Width = GridLength(1, GridUnitType.Star)
    right = ColumnDefinition()
    right.Width = GridLength(GRID_COMBO_WIDTH)
    row.ColumnDefinitions.Add(left)
    row.ColumnDefinitions.Add(right)

    left_stack = StackPanel()
    left_stack.VerticalAlignment = VerticalAlignment.Center
    left_stack.Margin = Thickness(0, 0, 10, 0)
    label = TextBlock()
    label.Text = caption
    label.FontSize = 12
    label.TextTrimming = TextTrimming.CharacterEllipsis
    label.Foreground = _brush(ui.TEXT)
    left_stack.Children.Add(label)
    if detail:
        note = TextBlock()
        note.Text = detail
        note.FontSize = 10.5
        note.TextTrimming = TextTrimming.CharacterEllipsis
        note.Foreground = _brush(ui.TEXT_MUTED)
        left_stack.Children.Add(note)
    Grid.SetColumn(left_stack, 0)
    row.Children.Add(left_stack)

    cell = StackPanel()
    Grid.SetColumn(cell, 1)
    row.Children.Add(cell)
    return row, cell


def build_layer_style_grid(host_panel, layer_rows, styles):
    """Fill host_panel with one row per CAD layer. Returns a function that
    reads back {layer name: chosen style}, defaulting to the proposal for
    every row the user never touched - so reading it without opening the
    grid at all reproduces match_style()'s own answer, layer by layer."""
    host_panel.Children.Clear()
    getters = []
    for row in layer_rows:
        proposed = row["style"]
        caption = u"{}  \u00b7  {} curve{}".format(
            row["layer"], row["count"], u"" if row["count"] == 1 else u"s")
        line, cell = _grid_row(caption)

        combo = ComboBox()
        for style in styles:
            _combo_item(combo, style["name"], style, style is proposed)
        cell.Children.Add(combo)

        def getter(combo=combo, fallback=proposed):
            item = combo.SelectedItem
            return item.Tag if item is not None else fallback

        host_panel.Children.Add(line)
        getters.append((row["layer"], getter))

    def read():
        return dict((layer, getter()) for layer, getter in getters)
    return read


def load_text_size_rows(import_instance, curves, closest_text_type,
                       known_path=None, path_resolved=False):
    """Read the DXF text (asking for the file here, exactly once, so main()
    never has to ask again) and group it by paper size the same way main()
    itself does before it asks about new text types.

    Returns (rows, path, status): `rows` is [] when there is nothing to
    group, `path` is the DXF path this resolved (None if none was picked),
    and `status` is the human sentence for why, for when rows come back
    empty.
    """
    (records, leaders, hatches, header, extent, path, status
     ) = load_cad_text(import_instance, known_path=known_path,
                       path_resolved=path_resolved)
    if not records:
        return [], path, status

    transform = import_transform(import_instance)
    model_box = geometry_bounds(curves, import_instance)
    sample = list(records)
    for leader in (leaders or [])[:400]:
        for point in leader["points"][:2]:
            sample.append({"insertion_point": (point[0], point[1], 0.0)})
    for hatch in (hatches or [])[:200]:
        for ring in hatch["rings"][:2]:
            for point in ring[:4]:
                sample.append({"insertion_point": (point[0], point[1], 0.0)})
    _place, height_scale, _label, _confidence, _verified = \
        build_text_placement(sample, header, extent, model_box, transform)

    paper_sizes = []
    for record in records:
        if not (record.get("text") or "").strip():
            continue
        model_height = text_height_in_model(record, height_scale)
        if model_height:
            paper_sizes.append(model_height / max(active_view.Scale, 1))

    rows = []
    for size, count in group_sizes(paper_sizes):
        base_type, base_size = closest_text_type(size)
        # How far the nearest existing type is, and whether Revit can even
        # hold this size: both used to live in a follow-up dialog, and both
        # belong on the row they are talking about.
        off_by = (None if not base_size
                  else max(base_size, size) / min(base_size, size))
        rows.append({"size": size, "count": count,
                     "bucket": _size_bucket_key(size), "type": base_type,
                     "base_size": base_size, "off_by": off_by,
                     "below_floor": size < MIN_TEXT_SIZE})
    return rows, path, None


def parse_size(label):
    """The length format_size() would have written as `label`, in feet.

    Only used to ask whether the pretty label still tells the truth, so an
    unparseable one answers "no" rather than raising.
    """
    try:
        text = label.strip()
        if text.endswith("mm"):
            return float(text[:-2].strip()) / 304.8
        text = text.rstrip('"').strip()
        whole, fraction = 0.0, 0.0
        if " " in text:
            first, text = text.split(" ", 1)
            whole = float(first)
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            fraction = float(numerator) / float(denominator)
        else:
            whole = float(text)
        return (whole + fraction) / 12.0
    except Exception:
        return None


def size_row_labels(size_rows):
    """One label per row, and no two alike.

    format_size() rounds to 64ths of an inch, and the grouping cuts at about
    5%, so near Revit's floor a single label covers a 3x range: four rows all
    reading 1/64" with no way to tell them apart. Where that happens the
    label carries the plain decimal too.
    """
    labels = [format_size(row["size"]) for row in size_rows]
    seen = {}
    for label in labels:
        seen[label] = seen.get(label, 0) + 1
    out = []
    for row, label in zip(size_rows, labels):
        # Two reasons the pretty form is not the truth: another row reads the
        # same, or the rounding moved the number by more than 1%.
        exact = (u"{:.3f} mm".format(row["size"] * 304.8) if use_metric()
                 else u'{:.4f}"'.format(row["size"] * 12.0))
        rounded = parse_size(label)
        honest = (rounded is not None and row["size"] > 0
                  and abs(rounded - row["size"]) / row["size"] <= 0.01)
        if seen[label] > 1 or not honest:
            label = u"{}  ({})".format(label, exact)
        out.append(label)
    return out


def _is_create(choice):
    """A "Create ..." pick from a size row, as opposed to an existing type."""
    return (isinstance(choice, tuple) and len(choice) == 5
            and choice[0] == "create")


def build_size_type_grid(host_panel, size_rows, text_types, choices,
                        name_boxes):
    """Fill host_panel with one row per text size group. `choices` is a
    dict the caller keeps across rebuilds ({bucket key: chosen type}) -
    the mm/inches toggle rebuilds this grid to relabel the sizes, and this
    is what makes a row's choice survive that. `name_boxes` is the same
    idea for the editable name of a type to create: the caller keeps it and
    reads it at the end, so a name typed before a unit flick is not lost. Seeded here with the
    proposal for every row not already in it, so a row nobody touches
    keeps proposing what closest() already picked."""
    host_panel.Children.Clear()
    labels = size_row_labels(size_rows)
    taken = taken_type_names()
    reserved = set()
    for row, label in zip(size_rows, labels):
        bucket = row["bucket"]
        proposed = row["type"]
        # Making a type below Revit's floor always fails, so it is not
        # offered: the only thing it buys is a rolled back transaction.
        create_choice = None
        if proposed is not None and not row["below_floor"]:
            name = proposed_type_name(row["size"], proposed, reserved, taken)
            reserved.add(name)
            create_choice = ("create", bucket, row["size"], proposed, name)
        # The nearest existing type is the proposal, unless it is so far off
        # that it is not a match at all: then the proposal is a new type at
        # the CAD size, plainly shown on its row with its name, and one pick
        # away from the closest existing type. It is only ever made when the
        # user presses Explode with it chosen.
        far = bool(row["off_by"]) and row["off_by"] > TEXT_TYPE_CREATE_RATIO
        if bucket not in choices:
            choices[bucket] = (create_choice
                               if create_choice is not None and far
                               else proposed)
        current = choices[bucket]
        caption = u"{}  \u00b7  {} note{}".format(
            label, row["count"], u"" if row["count"] == 1 else u"s")
        # What the second dialog used to say, said on the row it is about.
        detail = None
        if row["below_floor"]:
            detail = u"smaller than {}, the least Revit can hold".format(
                format_size(MIN_TEXT_SIZE))
        elif row["off_by"] and row["off_by"] > 1.0 + TEXT_SIZE_TOLERANCE:
            detail = u"nearest existing type is {:.1f}x out".format(
                row["off_by"])
            if far and create_choice is not None:
                detail += u", so a new type is proposed"
        line, inner = _grid_row(caption, detail)

        def is_current(text_type, current=current):
            if _is_create(current):
                return False        # "Create a new type" is the answer here
            if text_type is None or current is None:
                return text_type is current
            return _id_val(text_type.Id) == _id_val(current.Id)

        combo = ComboBox()
        name_box = None
        if create_choice is not None:
            _combo_item(combo,
                        u"Create a new type at {}".format(
                            format_size(row["size"])),
                        create_choice, _is_create(current))
            name_box = TextBox()
            name_box.Text = create_choice[4]
            name_box.FontSize = 11.5
            name_box.Margin = Thickness(0, 4, 0, 0)
            # The name only takes room while "Create" is the answer.
            name_box.Visibility = (Visibility.Visible if _is_create(current)
                                   else Visibility.Collapsed)
            name_boxes[bucket] = name_box
        for text_type, type_size in text_types:
            # The type's own size next to its name: the row says what the CAD
            # is, this says what it would become, and the two are compared by
            # eye instead of trusted.
            _combo_item(combo,
                        u"{}  \u00b7  {}".format(
                            type_name(text_type),
                            format_size(type_size) if type_size else u"?"),
                        text_type, is_current(text_type))

        def remember(sender, args, bucket=bucket, combo=combo,
                     name_box=name_box):
            item = combo.SelectedItem
            if item is None:
                return
            choices[bucket] = item.Tag
            if name_box is not None:
                # The name only matters while "Create" is the answer, so it
                # only takes room then.
                name_box.Visibility = (Visibility.Visible
                                       if _is_create(item.Tag)
                                       else Visibility.Collapsed)
        combo.SelectionChanged += remember
        inner.Children.Add(combo)
        if name_box is not None:
            inner.Children.Add(name_box)

        host_panel.Children.Add(line)


def ask_options(import_instance, census, fills, curves, styles, match_style):
    """Everything the tool needs to know, asked once. Returns a dict with
    `text`, `ignore_hatches`, `cleanup` ("delete" / "hide" / "keep"),
    `layer_styles` ({CAD layer: chosen line style}), `type_overrides`
    ({size bucket: chosen text type}), `dxf_path` and `dxf_path_resolved`
    (so a DXF read in here for the grid is never read - or prompted for -
    a second time), or None when the window is closed or cancelled.
    """
    where = "family" if IS_FAMILY else "view"
    subtitle = u"{}  \u2192  {} {}".format(
        Element_name(import_instance) or "CAD import", where, active_view.Name)
    win = ui.parse("Clean Explode CAD", subtitle, _OPTIONS_BODY,
                   _OPTIONS_FOOTER, width=600, height=720)
    win.FindName("lblWhat").Text = describe_import(census, fills)
    win.FindName("rbMm").IsChecked = use_metric()
    win.FindName("rbInch").IsChecked = not use_metric()
    state = {"result": None, "dxf_loaded": False, "dxf_path": None,
             "size_rows": None, "type_choices": {}, "name_boxes": {}}

    def checked(name):
        return win.FindName(name).IsChecked == True

    # ---- line styles grid: the data is already sitting there -------------
    layer_rows = summarize_layer_styles(curves, match_style)
    read_layer_styles = None
    if layer_rows:
        try:
            read_layer_styles = build_layer_style_grid(
                win.FindName("pnlLineStyles"), layer_rows, styles)
        except Exception as ex:
            logger.debug("Line style grid failed to build: %s", ex)
            win.FindName("secLineStyles").Visibility = Visibility.Collapsed
    else:
        win.FindName("secLineStyles").Visibility = Visibility.Collapsed

    # ---- text sizes grid: needs the DXF, so it waits for the checkbox ----
    _match_preview, closest_text_type_preview, text_type_catalog = \
        build_text_type_matcher(active_view)
    sec_sizes = win.FindName("secTextSizes")
    bdr_sizes = win.FindName("bdrTextSizes")
    lbl_size_status = win.FindName("lblSizeStatus")
    pnl_sizes = win.FindName("pnlTextSizes")

    def render_size_grid():
        rows = state["size_rows"]
        if not rows:
            bdr_sizes.Visibility = Visibility.Collapsed
            return
        try:
            build_size_type_grid(pnl_sizes, rows, text_type_catalog,
                                 state["type_choices"],
                                 state["name_boxes"])
            bdr_sizes.Visibility = Visibility.Visible
        except Exception as ex:
            logger.debug("Text size grid failed to build: %s", ex)
            bdr_sizes.Visibility = Visibility.Collapsed

    bdr_dxf = win.FindName("bdrDxf")
    lbl_dxf = win.FindName("lblDxfFile")
    btn_dxf = win.FindName("btnDxf")

    def load_dxf(forced_path=None):
        """Read the DXF: once on its own, and again whenever the user picks
        another file. `forced_path` skips the asking, because the button has
        already done it."""
        try:
            rows, path, status = load_text_size_rows(
                import_instance, curves, closest_text_type_preview,
                known_path=forced_path, path_resolved=forced_path is not None)
        except Exception as ex:
            logger.debug("Text size preview failed: %s", ex)
            rows, path, status = [], forced_path, None
        state["dxf_loaded"] = True
        state["dxf_path"] = path
        state["size_rows"] = rows
        # The size buckets belong to the file that produced them, so a
        # different file starts the choices over. Cleared in place: the grid
        # rows built earlier hold a reference to this very dict.
        state["type_choices"].clear()
        if rows:
            lbl_size_status.Visibility = Visibility.Collapsed
        elif status:
            lbl_size_status.Text = status
            lbl_size_status.Visibility = Visibility.Visible
        else:
            lbl_size_status.Visibility = Visibility.Collapsed
        refresh_dxf_row()

    def ensure_dxf_loaded():
        if state["dxf_loaded"]:
            return
        load_dxf()

    def refresh_dxf_row(*_args):
        # Two separate answers need the DXF, so the row follows whether ANY
        # of them is on, not which one turned it on.
        if not (checked("rbText") or checked("rbHatchOut")):
            bdr_dxf.Visibility = Visibility.Collapsed
            return
        bdr_dxf.Visibility = Visibility.Visible
        path = state["dxf_path"]
        if path:
            lbl_dxf.Text = os.path.basename(path)
            lbl_dxf.Foreground = _brush(ui.TEXT)
            btn_dxf.Content = "Change"
        else:
            lbl_dxf.Text = u"No file selected"
            lbl_dxf.Foreground = _brush(ui.TEXT_MUTED)
            btn_dxf.Content = "Select file"

    def on_pick_dxf(sender, args):
        # Cancelling the picker used to be a dead end: the DXF question was
        # marked answered and nothing offered to ask it again.
        path = pick_dxf_file(state["dxf_path"])
        if not path:
            return
        load_dxf(path)
        if checked("rbText"):
            render_size_grid()
        else:
            # A DXF the user went and picked is a promise of text: show_units
            # (fired by this) draws the sizes grid for it.
            win.FindName("rbText").IsChecked = True

    btn_dxf.Click += on_pick_dxf

    # Nothing in the geometry-only path ever prints a text size, so the
    # unit row and the sizes grid only appear once the text answer asks
    # for one - and that is also the one moment the DXF gets read.
    units_row = win.FindName("pnlUnits")

    def show_units(sender, args):
        show_text = checked("rbText")
        units_row.Visibility = (Visibility.Visible if show_text
                                else Visibility.Collapsed)
        sec_sizes.Visibility = (Visibility.Visible if show_text
                                else Visibility.Collapsed)
        if show_text:
            ensure_dxf_loaded()
            render_size_grid()
        refresh_dxf_row()
    win.FindName("rbText").Checked += show_units
    win.FindName("rbGeometry").Checked += show_units

    # "Leave hatches out" also needs the DXF (for the hatch boundaries),
    # even when text itself was never asked for - same as main() does
    # today. This never shows the sizes grid on its own.
    def on_hatch_out(sender, args):
        ensure_dxf_loaded()
        refresh_dxf_row()
    win.FindName("rbHatchOut").Checked += on_hatch_out
    win.FindName("rbHatchLines").Checked += refresh_dxf_row

    def relabel_sizes(sender, args):
        # format_size() reads UNIT_MODE[0]; on_go() sets it too, but only
        # at the very end, so a toggle here has to set it live for the
        # grid's own labels to follow along as the user flips it.
        UNIT_MODE[0] = checked("rbMm")
        if state["size_rows"]:
            render_size_grid()
    win.FindName("rbMm").Checked += relabel_sizes
    win.FindName("rbInch").Checked += relabel_sizes

    def automatic_sizes(sender, args):
        # Emptied in place, not rebound: build_size_type_grid() reseeds every
        # row from its own proposal, and the rows already built hold a
        # reference to this very dict.
        state["type_choices"].clear()
        state["name_boxes"].clear()
        render_size_grid()
    win.FindName("btnAutoSizes").Click += automatic_sizes

    def typed_name(bucket, choice):
        """What the name box says now, or the name that was proposed."""
        box = state["name_boxes"].get(bucket)
        try:
            typed = (box.Text or u"").strip() if box is not None else u""
        except Exception:
            typed = u""
        return typed or choice[4]

    def on_go(sender, args):
        UNIT_MODE[0] = checked("rbMm")
        if checked("rbDelete"):
            cleanup = "delete"
        elif checked("rbHide"):
            cleanup = "hide"
        else:
            cleanup = "keep"
        state["result"] = {
            "text": checked("rbText"),
            "ignore_hatches": checked("rbHatchOut"),
            "cleanup": cleanup,
            "layer_styles": read_layer_styles() if read_layer_styles else {},
            "type_overrides": dict(
                (bucket, choice)
                for bucket, choice in state["type_choices"].items()
                if choice is not None and not _is_create(choice)),
            "new_types": dict(
                (bucket, (choice[2], choice[3], typed_name(bucket, choice)))
                for bucket, choice in state["type_choices"].items()
                if _is_create(choice)),
            "dxf_path": state["dxf_path"],
            "dxf_path_resolved": state["dxf_loaded"],
        }
        win.Close()

    win.FindName("btnGo").Click += on_go
    win.FindName("btnCancel").Click += lambda sender, args: win.Close()

    # A DXF linked to the import is a promise of text. With "Delete it"
    # selected, opening on "Geometry only" would throw all of that text away
    # for anybody who presses Explode without reading, so the text answer
    # starts on whenever there is a DXF to read it from.
    preset = linked_dxf_path(import_instance)
    if preset and os.path.isfile(preset):
        try:
            win.FindName("rbText").IsChecked = True
        except Exception as ex:
            logger.debug("Text answer could not be preset: %s", ex)

    win.ShowDialog()
    return state["result"]


def linked_dxf_path(import_instance):
    """Return a linked ASCII DXF path when Revit exposes one."""
    try:
        if not import_instance.IsExternalFileReference():
            return None
        external_ref = import_instance.GetExternalFileReference()
        if external_ref is None:
            return None
        path = ModelPathUtils.ConvertModelPathToUserVisiblePath(
            external_ref.GetPath())
        if path and os.path.splitext(path)[1].lower() == ".dxf":
            return path
    except Exception:
        pass
    return None


def pick_dxf_file(current=None):
    """Open the file dialog for the DXF and return what was picked, or None.

    Always asks, unlike select_dxf_source(), which takes a DXF that Revit
    already has linked without bothering the user: this one is what the
    "Change" button in the options window calls, and there the point IS to
    ask again.
    """
    from Microsoft.Win32 import OpenFileDialog
    dialog = OpenFileDialog()
    dialog.Title = "Select the ASCII DXF of this drawing (text and hatches)"
    dialog.Filter = "AutoCAD ASCII DXF (*.dxf)|*.dxf|All files (*.*)|*.*"
    dialog.Multiselect = False
    if current and os.path.isfile(current):
        dialog.InitialDirectory = os.path.dirname(current)
        dialog.FileName = os.path.basename(current)
    result = dialog.ShowDialog()
    if result is None or not bool(result):
        return None
    return dialog.FileName


def select_dxf_source(import_instance):
    """Find the DXF that contains text for the selected CAD instance."""
    path = linked_dxf_path(import_instance)
    if path and os.path.isfile(path):
        return path
    return pick_dxf_file()


# What the last DXF read found beyond what the text reader hands over. A
# dimension's value only becomes a note when the reader knows how to take it
# out of the dimension; the dimensions the reader sees (cadtext's own
# dimension_keys, the criteria the text reading uses) are what the tool checks
# the notes against, one dimension at a time, so a value that never arrived is
# reported instead of silently lost.
DXF_FACTS = {"dimension_keys": [], "hatches": 0}


def load_cad_text(import_instance, known_path=None, path_resolved=False):
    """Read the DXF and return (records, leaders, hatches, header, extent,
    source, status).

    `path_resolved` is True once the options window already asked for the
    DXF (even when the answer was "no file was picked"), so this never
    prompts a second time: `known_path` is used exactly as it stands,
    including None. With `path_resolved` False (the default) this behaves
    exactly as before: it does the asking itself.

    hatches is None when the DXF could never be read at all, and a list
    (possibly empty) once it has been, so the caller can tell "there are no
    hatches in the drawing" from "there was no drawing to look in".
    """
    DXF_FACTS["dimension_keys"] = []
    DXF_FACTS["hatches"] = 0
    if path_resolved:
        path = known_path
    else:
        try:
            path = select_dxf_source(import_instance)
        except Exception as ex:
            return [], [], None, {}, None, None, ("The DXF was not read: {}"
                                                .format(ex))

    if not path:
        return [], [], None, {}, None, None, (
            "No DXF was selected, so the CAD text was not converted and "
            "hatches could not be told apart from the rest of the linework.")

    try:
        with open(path, "rb") as source_file:
            data = source_file.read()
        if data[:18] == b"AutoCAD Binary DXF":
            return [], [], None, {}, None, path, (
                "That DXF is a BINARY DXF, which cannot be read as text. "
                "Save it again from AutoCAD as ASCII DXF and run the tool "
                "once more.")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", "replace")
        header, entities, blocks = read_dxf(text)
        try:
            DXF_FACTS["dimension_keys"] = dimension_keys(entities, blocks)
        except Exception as ex:
            logger.debug("Dimensions could not be counted: %s", ex)
        try:
            DXF_FACTS["hatches"] = count_hatches(entities, blocks)
        except Exception as ex:
            logger.debug("Hatches could not be counted: %s", ex)
        extent = entities_extent(entities)
        records = []
        _expand(entities, blocks, _IDENTITY_FRAME, 0, records)
        try:
            leaders = collect_leaders(entities, blocks)
        except Exception as ex:
            logger.debug("Leaders could not be read: %s", ex)
            leaders = []
        try:
            hatches = collect_hatches(entities, blocks)
        except Exception as ex:
            logger.debug("Hatches could not be read: %s", ex)
            hatches = []
    except Exception as ex:
        return [], [], None, {}, None, path, ("The DXF was not read: {}"
                                            .format(ex))

    if not records:
        return [], leaders, hatches, header, extent, path, (
            "No TEXT, MTEXT or ATTRIB entities were found in that DXF. "
            "If the drawing's text lives in an xref, bind it before "
            "exporting the DXF.")
    return records, leaders, hatches, header, extent, path, None


def import_transform(import_instance):
    """Return the transform from CAD source coordinates to model coordinates."""
    try:
        return import_instance.GetTotalTransform()
    except Exception:
        try:
            opts = Options()
            opts.DetailLevel = ViewDetailLevel.Fine
            geometry = import_instance.get_Geometry(opts)
            if geometry is not None:
                for obj in geometry:
                    if isinstance(obj, GeometryInstance):
                        return obj.Transform
        except Exception:
            pass
    return Transform.Identity


def geometry_bounds(curves, import_instance):
    """(minX, minY, maxX, maxY) of the geometry that was really imported.

    Measured from the converted curves themselves rather than trusting
    Element.get_BoundingBox, which is not always available for an import.
    This box is what the text placement is checked against.
    """
    xs, ys = [], []
    step = max(1, len(curves) // 8000) if curves else 1
    for index in range(0, len(curves), step):
        curve = curves[index][0]
        try:
            points = (curve.GetEndPoint(0), curve.GetEndPoint(1))
        except Exception:
            try:
                points = curve.Tessellate()
            except Exception:
                continue
        for point in points:
            xs.append(point.X)
            ys.append(point.Y)

    range_x = robust_range(xs)
    range_y = robust_range(ys)
    if range_x is not None and range_y is not None:
        return (range_x[0], range_y[0], range_x[1], range_y[1])

    low_x = None
    if low_x is None:
        try:
            bbox = import_instance.get_BoundingBox(None)
        except Exception:
            bbox = None
        if bbox is None:
            return None
        return (bbox.Min.X, bbox.Min.Y, bbox.Max.X, bbox.Max.Y)


# What $INSUNITS can declare, as (feet per drawing unit, name), so the report
# can say which unit the drawing was read in.
UNIT_NAMES = (
    (1.0 / 12.0, "inches"), (1.0, "feet"), (5280.0, "miles"),
    (1.0 / 304.8, "millimetres"), (1.0 / 30.48, "centimetres"),
    (1.0 / 0.3048, "metres"), (1000.0 / 0.3048, "kilometres"),
    (1.0e-6 / 12.0, "microinches"), (0.001 / 12.0, "mils"),
    (3.0, "yards"), (1.0e-6 / 0.3048, "microns"),
    (0.1 / 0.3048, "decimetres"), (10.0 / 0.3048, "decametres"),
    (100.0 / 0.3048, "hectometres"),
)


def unit_name(scale):
    """The unit a feet-per-drawing-unit scale stands for, in words."""
    for value, name in UNIT_NAMES:
        if abs(value - scale) <= value * 1e-6:
            return name
    return "{:g} feet per drawing unit".format(scale)


def build_text_placement(records, header, dxf_extent, model_box, transform):
    """Work out how a DXF coordinate becomes a model coordinate.

    DXF coordinates are in the drawing's own units (usually millimetres or
    inches) while Revit works in feet, so without this step text is placed
    hundreds of times too far out - normally past Revit's limits, where it
    cannot be created at all and every note is silently lost.

    Each candidate is PREDICTED (from $INSUNITS, and from the drawing's own
    size against the size of the geometry Revit really imported) and then
    VERIFIED by counting how much of the text lands on the drawing. If no
    unit scale on top of the import's transform works, the drawing is
    matched onto the imported geometry directly, so the text still lands.

    Returns (place, height_scale, label, confidence, verified). Verified
    means a real drawing scale was chosen AND the drawing's own size was
    there to check it against. Without that check a scale ten times too
    small still scores a perfect 100%, because everything it places lands
    somewhere inside the drawing - convincing, and plainly wrong. Nothing
    is ever deleted on an unverified placement.
    """
    def scaled_by_transform(scale):
        def place(x, y, z):
            return transform.OfPoint(XYZ(x * scale, y * scale, z * scale))
        return place

    try:
        placement_scale = transform.OfVector(XYZ(1.0, 0.0, 0.0)).GetLength()
    except Exception:
        placement_scale = 1.0

    candidates = []       # (place, height_scale, label, is_last_resort)
    seen_scales = []

    def offer(scale, label):
        label = label.replace("meters", "metres")
        if not scale or scale <= 0:
            return
        for existing in seen_scales:
            if abs(existing - scale) < existing * 1e-6:
                return
        seen_scales.append(scale)
        candidates.append((scaled_by_transform(scale),
                           scale * placement_scale, label, False))

    # 1. what the file says
    header_scale = header_unit_scale(header)
    if header_scale:
        offer(header_scale,
              "{} per the DXF header".format(unit_name(header_scale)))

    # 2. what the drawing's own size says against the imported geometry
    ratio = None
    extent = dxf_extent or header_extents(header)
    if extent and model_box:
        low, high = extent
        dxf_size = max(high[0] - low[0], high[1] - low[1])
        model_size = max(model_box[2] - model_box[0],
                         model_box[3] - model_box[1])
        if dxf_size > 0 and model_size > 0:
            ratio = model_size / dxf_size
            best = min(UNIT_CANDIDATES,
                       key=lambda item: abs(math.log(item[0] / ratio)))
            if abs(math.log(best[0] / ratio)) < 0.15:       # within ~15 %
                offer(best[0], "{} (from the drawing size)".format(best[1]))

    # 3. the usual suspects
    for scale, label in UNIT_CANDIDATES:
        offer(scale, label)

    # 4. last resort: lay the drawing straight onto the imported geometry,
    #    which does not depend on the import's transform being what we think
    if extent and model_box and ratio:
        low, high = extent
        dxf_cx = (low[0] + high[0]) * 0.5
        dxf_cy = (low[1] + high[1]) * 0.5
        model_cx = (model_box[0] + model_box[2]) * 0.5
        model_cy = (model_box[1] + model_box[3]) * 0.5

        def fitted(x, y, z, r=ratio, dcx=dxf_cx, dcy=dxf_cy,
                   mcx=model_cx, mcy=model_cy):
            return XYZ((x - dcx) * r + mcx, (y - dcy) * r + mcy, 0.0)

        candidates.append((fitted, ratio,
                           "matched onto the imported geometry", True))

    if not candidates:
        return (scaled_by_transform(1.0), placement_scale, "feet", 0.0, False)

    if not model_box or not records:
        place, height_scale, label, _ = candidates[0]
        return place, height_scale, label, 0.0, False

    pad_x = max((model_box[2] - model_box[0]) * 0.25, 1.0)
    pad_y = max((model_box[3] - model_box[1]) * 0.25, 1.0)
    model_span = max(model_box[2] - model_box[0], model_box[3] - model_box[1])
    step = max(1, len(records) // 400)
    sample = records[::step]

    # How much of the drawing the text covers, read off the DXF itself.
    # Checking only "does it land inside the drawing" is not enough: a scale
    # that is far TOO SMALL collapses every note into one corner, which is
    # still inside, and would score a perfect 100 %. This is the measure
    # that tells those apart.
    expected_coverage = None
    if extent:
        # The header writes x/y/z, the entities x/y, so index rather than
        # unpack.
        low, high = extent
        drawing_span = max(high[0] - low[0], high[1] - low[1])
        text_span = max(
            robust_span([record["insertion_point"][0]
                         for record in sample]) or 0.0,
            robust_span([record["insertion_point"][1]
                         for record in sample]) or 0.0)
        if drawing_span > 0 and text_span > 0:
            expected_coverage = text_span / drawing_span

    def score(place):
        """Lands on the drawing (0-1) and spreads across it as it should."""
        placed = []
        for record in sample:
            x, y, z = record["insertion_point"]
            try:
                placed.append(place(x, y, z))
            except Exception:
                continue
        if not placed:
            return 0.0
        inside = 0
        for point in placed:
            if model_box[0] - pad_x <= point.X <= model_box[2] + pad_x \
                    and model_box[1] - pad_y <= point.Y <= model_box[3] + pad_y:
                inside += 1
        landed = float(inside) / len(placed)
        if expected_coverage is None or model_span <= 0:
            return landed
        span = max(robust_span([point.X for point in placed]) or 0.0,
                   robust_span([point.Y for point in placed]) or 0.0)
        if span <= 0:
            return landed
        coverage = span / model_span
        fit = (min(coverage, expected_coverage)
               / max(coverage, expected_coverage))
        return landed * fit

    def rank(only_last_resort):
        ranked = [(score(candidate[0]), -index, index)
                  for index, candidate in enumerate(candidates)
                  if candidate[3] == only_last_resort]
        ranked.sort(reverse=True)
        return ranked[0] if ranked else None

    # A real unit scale on top of the import's own transform is always
    # preferred. Laying the drawing onto the geometry can match anything,
    # including a scale a few per cent off, so it only gets a say when no
    # real unit works.
    best = rank(False)
    if best is None or best[0] < LAST_RESORT_THRESHOLD:
        fallback = rank(True)
        if fallback is not None and (best is None or fallback[0] > best[0]):
            best = fallback
    rate, _, index = best
    place, height_scale, label, last_resort = candidates[index]
    verified = not last_resort and expected_coverage is not None
    return place, height_scale, label, rate, verified


def text_position(record, view, place):
    x, y, z = record["insertion_point"]
    return make_projector(view)(place(x, y, z))


def text_rotation(record, transform, view):
    radians = math.radians(record["rotation"])
    direction = transform.OfVector(
        XYZ(math.cos(radians), math.sin(radians), 0.0))
    direction = direction - view.ViewDirection.Multiply(
        direction.DotProduct(view.ViewDirection))
    if direction.GetLength() < 1e-9:
        return 0.0
    direction = direction.Normalize()
    return math.atan2(direction.DotProduct(view.UpDirection),
                      direction.DotProduct(view.RightDirection))


def text_height_in_model(record, height_scale):
    height = record.get("height", 0.0)
    if height <= 0.0:
        return None
    return abs(height) * height_scale


def build_text_type_matcher(view, overrides=None):
    """Match CAD text height to the closest existing Revit text type.

    `overrides` is {_size_bucket_key(paper size): TextNoteType}, read from
    the TEXT SIZES grid of the options window. A group the user re-pointed
    at a specific type there wins over the automatic nearest match, every
    time the size is asked for again - including from inside the "what
    should the new text types be" flow below, since that also goes through
    closest(). Passing nothing (or {}) reproduces today's plain matching.
    """
    overrides = overrides or {}
    text_types = []
    for text_type in FilteredElementCollector(doc).OfClass(TextNoteType):
        try:
            parameter = text_type.get_Parameter(BuiltInParameter.TEXT_SIZE)
            size = parameter.AsDouble() if parameter is not None else None
            if size is not None:
                text_types.append((text_type, size))
        except Exception:
            pass

    default_id = doc.GetDefaultElementTypeId(ElementTypeGroup.TextNoteType)
    default_type = doc.GetElement(default_id)
    if default_type is not None and not text_types:
        text_types.append((default_type, None))

    sized_types = [item for item in text_types if item[1] is not None]

    def _own_size(text_type):
        try:
            parameter = text_type.get_Parameter(BuiltInParameter.TEXT_SIZE)
            return parameter.AsDouble() if parameter is not None else None
        except Exception:
            return None

    def closest(paper_size):
        """(type, its size) nearest to `paper_size`, both in feet."""
        chosen = overrides.get(_size_bucket_key(paper_size)) \
            if paper_size else None
        if chosen is not None:
            return chosen, _own_size(chosen)
        if not sized_types:
            return default_type, None
        return min(sized_types, key=lambda item: abs(item[1] - paper_size))

    def match(model_height):
        if not text_types:
            return default_type
        if model_height is None or model_height <= 0.0:
            return default_type
        return closest(model_height / max(view.Scale, 1))[0]

    # The catalog is ONLY for the options window's TEXT SIZES grid (radio /
    # combo candidates): sorting a COPY for display never touches the
    # `sized_types` list closest() itself closes over, so it cannot change
    # which type wins a tie there.
    catalog = sorted(sized_types or text_types,
                     key=lambda item: (item[1] is None, item[1] or 0.0))
    return match, closest, catalog


def create_text_note(record, transform, match_text_type, place, height_scale):
    text = record.get("text", "")
    if not text or not text.strip():
        return None
    note_type = match_text_type(text_height_in_model(record, height_scale))
    if note_type is None:
        raise ConversionError("No Revit TextNoteType is available.")
    options = TextNoteOptions(note_type.Id)
    options.HorizontalAlignment = HorizontalTextAlignment.Left
    options.Rotation = text_rotation(record, transform, active_view)
    text = text.replace("\n", "\r")
    return TextNote.Create(doc, active_view.Id,
                           text_position(record, active_view, place),
                           text, options)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# The summary window
# ----------------------------------------------------------------------------
# Built with ui.parse() and a body of its own rather than ui.show_report(),
# whose body is a single TextBox with TextWrapping="NoWrap": every sentence
# ran off the right edge and the whole run read as one wall of text. Here the
# numbers are cards, the two tallies are panels, and the sentences are notes
# carrying a state dot, warnings first. The plain text is still built, for the
# Copy button and as the fallback if this window ever refuses to parse.
#
# No hex of the accent family is written here: they are injected from the ui
# tokens at build time, because a hex typed into a tool's body goes stale the
# day ACCENT moves. The neutrals are the canonical handoff values, which
# ui.build() runs through its theme table.

NOTE_RANK = {"warn": 0, "ok": 1, "info": 2}

# What the report says each CAD entity turned into. Read off the code, not
# assumed: extract_geometry() sends every Curve subclass to the curve list and
# Solid/Mesh to `fills`, bind_curve() hands a curve to Revit untouched, and
# curves_from_points() expands a PolyLine into lines plus the arcs it can
# prove. `fills` is never converted, because the tool creates no filled region.
BECAME = {
    "Line": "detail lines",
    "Arc": "detail arcs, kept as arcs (a whole circle stays one element)",
    "Ellipse": "detail ellipses, kept whole",
    "NurbSpline": "detail splines, kept as splines",
    "HermiteSpline": "detail splines, kept as splines",
    "PolyLine": "detail lines, with arcs rebuilt where the points sit on "
                "a circle",
    "Curve": "detail curves",
    "Solid": "not converted: Revit can only hold a solid fill as a filled "
             "region, and this tool never creates one",
    "Mesh": "not converted: Revit can only hold a solid fill as a filled "
            "region, and this tool never creates one",
    "other": "not converted: geometry Revit does not hand over as curves",
}


def xml_escape(text):
    """Escape a value for use inside a XAML attribute."""
    return (u"{}".format(text).replace(u"&", u"&amp;").replace(u"<", u"&lt;")
            .replace(u">", u"&gt;").replace(u'"', u"&quot;"))


def one_line(text):
    """Flatten a multi-line sentence so it can live in a wrapping TextBlock."""
    return u" ".join(u"{}".format(text).replace(u"\n   \u2022 ", u" \u00b7 ")
                     .replace(u"\n", u" ").split())


_KPI_CARD = u"""
      <Border Background="#FAF8F5" BorderBrush="#ECE9E4" BorderThickness="1"
              CornerRadius="10" Padding="15,11" Margin="__MARGIN__">
        <StackPanel>
          <TextBlock Text="__VALUE__" FontSize="__SIZE__" FontWeight="Bold"
                     Foreground="#202022" TextTrimming="CharacterEllipsis"/>
          <TextBlock Text="__LABEL__" FontSize="11" Foreground="#77736C"
                     TextWrapping="Wrap" Margin="0,3,0,0"/>
        </StackPanel>
      </Border>
"""


def kpi_card(value, label, last=False, size="27"):
    return (_KPI_CARD.replace("__VALUE__", xml_escape(value))
            .replace("__LABEL__", xml_escape(label))
            .replace("__SIZE__", size)
            .replace("__MARGIN__", "0,0,0,0" if last else "0,0,10,0"))


_BAR_ROW = u"""
          <StackPanel Margin="0,12,0,0">
            <Grid>
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="Auto"/>
              </Grid.ColumnDefinitions>
              <TextBlock Grid.Column="0" Text="__NAME__" FontSize="12.5"
                         Foreground="#202022" TextTrimming="CharacterEllipsis"/>
              <TextBlock Grid.Column="1" Text="__COUNT__" FontSize="12.5"
                         FontWeight="Medium" Foreground="#77736C"
                         Margin="10,0,0,0"/>
            </Grid>
            <TextBlock Text="__CAPTION__" FontSize="11"
                       Foreground="#A6A199" TextWrapping="Wrap"
                       Margin="0,2,0,0" Visibility="__CAP_VIS__"/>
            <Grid Height="4" Margin="0,6,0,0">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="__FILL__*"/>
                <ColumnDefinition Width="__REST__*"/>
              </Grid.ColumnDefinitions>
              <Border Grid.Column="0" CornerRadius="2" Background="__ACCENT__"/>
              <Border Grid.Column="1" CornerRadius="2" Background="#F0EEE9"
                      Margin="2,0,0,0"/>
            </Grid>
          </StackPanel>
"""


def bar_row(name, count, total, caption=""):
    rest = max(total - count, 0)
    return (_BAR_ROW.replace("__NAME__", xml_escape(name))
            .replace("__COUNT__", xml_escape("{:,}".format(count)))
            .replace("__CAPTION__", xml_escape(caption))
            .replace("__CAP_VIS__", "Visible" if caption else "Collapsed")
            .replace("__FILL__", str(count)).replace("__REST__", str(rest)))


def layer_caption(layers):
    """"from A-GLAZ, A-WALL and 3 more layers" - the honest answer to why a
    given Revit style was the one that got used."""
    if not layers:
        return ""
    ranked = sorted(layers.items(), key=lambda kv: -kv[1])
    shown = [name for name, _ in ranked[:3]]
    rest = len(ranked) - len(shown)
    listing = ", ".join(shown)
    if rest:
        listing += " and {} more layer{}".format(rest, "" if rest == 1 else "s")
    return u"from {}".format(listing)


# "Arc -> detail arcs", on ONE line, not the destination stacked under the
# entity: it is how the pairing reads (2026-09-22). The arrow and the
# destination are muted so the CAD entity stays the thing the eye lands on.
_COUNT_ROW = u"""
          <Grid Margin="0,12,0,0">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="*"/>
              <ColumnDefinition Width="Auto"/>
            </Grid.ColumnDefinitions>
            <TextBlock Grid.Column="0" FontSize="12.5" TextWrapping="Wrap">
              <Run Text="__NAME__" Foreground="#202022"/><Run
                   Text="__ARROW__" Foreground="#A6A199"/><Run
                   Text="__BECAME__" Foreground="#A6A199"/>
            </TextBlock>
            <TextBlock Grid.Column="1" Text="__COUNT__" FontSize="12.5"
                       FontWeight="Medium" Foreground="#202022"
                       VerticalAlignment="Top" Margin="10,0,0,0"/>
          </Grid>
"""


def count_row(name, count, caption=""):
    return (_COUNT_ROW.replace("__NAME__", xml_escape(name))
            .replace("__ARROW__", u"  \u2192  " if caption else u"")
            .replace("__BECAME__", xml_escape(caption))
            .replace("__COUNT__", xml_escape("{:,}".format(count))))


_PANEL = u"""
      <Border Grid.Column="__COL__" Background="#FAF8F5" BorderBrush="#ECE9E4"
              BorderThickness="1" CornerRadius="10" Padding="16,13,16,16">
        <StackPanel>
          <TextBlock Style="{StaticResource SectionHead}" Text="__HEAD__"/>
__ROWS__
          <TextBlock Text="__FOOT__" FontSize="11" Foreground="#A6A199"
                     TextWrapping="Wrap" Margin="0,14,0,0"
                     Visibility="__FOOT_VIS__"/>
        </StackPanel>
      </Border>
"""


def panel(head, rows, column, foot=""):
    return (_PANEL.replace("__HEAD__", xml_escape(head))
            .replace("__ROWS__", u"".join(rows))
            .replace("__COL__", str(column))
            .replace("__FOOT__", xml_escape(foot))
            .replace("__FOOT_VIS__", "Visible" if foot else "Collapsed"))


_NOTE_ROW = u"""
          <Grid Margin="0,11,0,0">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="Auto"/>
              <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>
            <Border Grid.Column="0" Width="7" Height="7" CornerRadius="4"
                    Background="__DOT__" VerticalAlignment="Top"
                    Margin="0,6,11,0"/>
            <TextBlock Grid.Column="1" Text="__TEXT__" FontSize="12.5"
                       Foreground="#202022" TextWrapping="Wrap"/>
          </Grid>
"""


def note_row(level, text):
    dot = {"warn": "__WARN__", "ok": "__OK__"}.get(level, "__MUTED__")
    return (_NOTE_ROW.replace("__TEXT__", xml_escape(one_line(text)))
            .replace("__DOT__", dot))


_SUMMARY_BODY = u"""
  <Grid>
    <ScrollViewer VerticalScrollBarVisibility="Auto"
                  HorizontalScrollBarVisibility="Disabled">
      <StackPanel Margin="0,0,6,0">
        <UniformGrid Columns="__KPI_COLS__" Margin="0,0,0,13">
__KPIS__
        </UniformGrid>
        <Grid Margin="0,0,0,13">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="__GAP__"/>
            <ColumnDefinition Width="__RIGHT__"/>
          </Grid.ColumnDefinitions>
__PANELS__
        </Grid>
        <StackPanel Visibility="__NOTES_VIS__">
          <TextBlock Style="{StaticResource SectionHead}" Text="__NOTES_HEAD__"
                     Margin="0,0,0,2"/>
          <Border Background="#FAF8F5" BorderBrush="#ECE9E4" BorderThickness="1"
                  CornerRadius="10" Padding="16,5,16,16">
            <StackPanel>
__NOTES__
            </StackPanel>
          </Border>
        </StackPanel>
      </StackPanel>
    </ScrollViewer>
  </Grid>
"""

_SUMMARY_FOOTER = u"""
  <Grid>
    <TextBlock x:Name="lblSummary" VerticalAlignment="Center" FontSize="13"
               Foreground="#77736C"/>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnCopy" Content="Copy report"
              Style="{StaticResource BtnGhost}" Margin="0,0,8,0"/>
      <Button x:Name="btnOK" Content="Done" Style="{StaticResource BtnPrimary}"/>
    </StackPanel>
  </Grid>
"""


def show_summary(subtitle, summary, kpis, styles_used, style_layers, contents,
                 polyline_points, notes, plain_text):
    """The /slantis summary window. Falls back to the plain report on any
    failure, so a cosmetic problem can never cost the user the result."""
    try:
        cards = []
        for index, (value, label) in enumerate(kpis):
            numeric = u"{}".format(value).replace(u",", u"").isdigit()
            cards.append(kpi_card(value, label, last=(index == len(kpis) - 1),
                                  size="27" if numeric else "19"))

        panels = []
        if styles_used:
            ranked = sorted(styles_used.items(), key=lambda kv: -kv[1])
            top = ranked[0][1] or 1
            panels.append(panel(
                "LINE STYLES USED",
                [bar_row(name, count, top,
                         layer_caption(style_layers.get(name)))
                 for name, count in ranked], 0,
                foot="Each CAD layer took the existing style that matched it "
                     "best: line pattern first, then weight, then name. "
                     "No style was created."))
        if contents:
            panels.append(panel(
                "WHAT CAME IN, AND WHAT IT BECAME",
                [count_row(name, count, BECAME.get(name, ""))
                 for name, count in contents],
                2 if panels else 0,
                foot=("{:,} polyline points".format(polyline_points)
                      if polyline_points else "")))

        ordered = sorted(notes, key=lambda item: NOTE_RANK.get(item[0], 3))
        body = (_SUMMARY_BODY
                .replace("__KPIS__", u"".join(cards))
                .replace("__KPI_COLS__", str(len(cards) or 1))
                .replace("__PANELS__", u"".join(panels))
                .replace("__GAP__", "16" if len(panels) > 1 else "0")
                .replace("__RIGHT__", "*" if len(panels) > 1 else "0")
                .replace("__NOTES__",
                         u"".join(note_row(lvl, txt) for lvl, txt in ordered))
                .replace("__NOTES_HEAD__",
                         "WORTH A LOOK" if ordered
                         and ordered[0][0] == "warn" else "NOTES")
                .replace("__NOTES_VIS__", "Visible" if ordered else "Collapsed")
                .replace("__ACCENT__", ui.ACCENT)
                .replace("__WARN__", ui.STATUS_WARN)
                .replace("__OK__", ui.STATUS_OK)
                .replace("__MUTED__", ui.TEXT_MUTED))

        win = ui.parse(u"Clean Explode CAD", subtitle, body, _SUMMARY_FOOTER,
                       width=780, height=740)
        win.FindName("lblSummary").Text = summary
        win.FindName("btnOK").Click += lambda s, e: win.Close()

        def copy_report(sender, args):
            try:
                Clipboard.SetText(plain_text)
                sender.Content = "Copied"
            except Exception as ex:
                logger.debug("Report could not be copied: %s", ex)

        win.FindName("btnCopy").Click += copy_report
        win.ShowDialog()
    except Exception as ex:
        # To the FILE, not only to the logger: the plain report and the Copy
        # button produce the same text, so without a trace on disk there is no
        # telling a fallback from a happy run that got copied.
        import traceback
        logger.error("Summary window failed, falling back to plain text: %s",
                     ex)
        _diag_log("Summary window failed, fell back to the plain report.\n"
                  + traceback.format_exc())
        ui.show_report(plain_text, title="Clean Explode CAD",
                       subtitle=subtitle, summary=summary,
                       width=680, height=560)


# Looking for CAD lines that already run along a solid fill's outline costs
# (solid fills x imported lines) comparisons. Past this many the check is
# skipped and the outline is drawn regardless: a doubled line is a smaller
# evil than a tool that stops to think.
HATCH_DEDUPE_LIMIT = 3000000

# Most hatch pattern lines drawn from the DXF in one run. Each hatch is also
# capped by the reader; this is the ceiling for all of them together, so a
# drawing full of dense patterns cannot bury Revit under lines.
HATCH_LINE_BUDGET = 15000


def _off_segment_uv(point, start, end):
    """Distance from a (u, v) point to the segment start-end, in the plane."""
    du, dv = end[0] - start[0], end[1] - start[1]
    span = du * du + dv * dv
    if span <= 0.0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    along = ((point[0] - start[0]) * du + (point[1] - start[1]) * dv) / span
    along = max(0.0, min(1.0, along))
    return math.hypot(point[0] - (start[0] + du * along),
                      point[1] - (start[1] + dv * along))


def hatch_linework(drawable, solids, place, view, existing, style_for):
    """Detail lines for the hatches Revit did not hand over as lines.

    `drawable`  hatches whose pattern stripes Revit gave nothing of; their
                `segments` (DXF coordinates, already clipped to the boundary)
                are drawn as they stand. A hatch whose stripes hit the
                reader's cap, or that would take the run past
                HATCH_LINE_BUDGET pattern lines, is not drawn AT ALL: half a
                hatch is false, and a pattern in the wrong scale is what
                would otherwise freeze Revit under tens of thousands of
                lines. It is counted in "too_dense".
    `solids`    solid hatches: their outline (the rings) is drawn, except an
                edge a CAD line already runs along. The fill itself cannot
                be a line, and this tool never creates a filled region.
    `place`     the DXF -> model placement the text uses, so a line lands
                exactly where the hatch rings do.
    `existing`  the imported curves, to avoid doubling an outline.
    `style_for` layer name -> (line style, layer name), the same mapping the
                imported lines of that layer get.

    Returns ([(curve, style, layer)], {"hatches", "lines", "too_dense",
    "solids"}).
    """
    project = make_projector(view)
    made = []
    stats = {"hatches": 0, "lines": 0, "too_dense": 0, "solids": 0}
    remaining = HATCH_LINE_BUDGET

    def model(point):
        return project(place(point[0], point[1], 0.0))

    def line(a, b):
        try:
            start, end = model(a), model(b)
            if start.DistanceTo(end) <= MIN_CURVE_LENGTH:
                return None
            return Line.CreateBound(start, end)
        except Exception:
            return None

    for hatch in drawable:
        segments = hatch.get("segments") or []
        if not segments:
            continue
        if hatch.get("segments_capped") or len(segments) > remaining:
            stats["too_dense"] += 1
            continue
        style, layer = style_for(hatch.get("layer"))
        pieces = []
        for segment in segments:
            curve = line(segment[0], segment[1])
            if curve is not None:
                pieces.append((curve, style, layer))
        if pieces:
            made.extend(pieces)
            remaining -= len(pieces)
            stats["hatches"] += 1
            stats["lines"] += len(pieces)

    if not solids:
        return made, stats

    to_uv = make_view_uv(view)
    flat = []
    if len(solids) * len(existing) <= HATCH_DEDUPE_LIMIT:
        for entry in existing:
            curve = entry[0]
            try:
                if not isinstance(curve, Line):
                    continue
                a = to_uv(curve.GetEndPoint(0))
                b = to_uv(curve.GetEndPoint(1))
            except Exception:
                continue
            flat.append((a, b, min(a[0], b[0]), min(a[1], b[1]),
                         max(a[0], b[0]), max(a[1], b[1])))

    for hatch in solids:
        style, layer = style_for(hatch.get("layer"))
        edges = []
        for ring in hatch.get("rings") or []:
            if len(ring) < 3:
                continue
            points = list(ring)
            if points[0] != points[-1]:
                points.append(points[0])
            for index in range(len(points) - 1):
                edges.append((points[index], points[index + 1]))
        if not edges:
            continue
        try:
            uvs = [(to_uv(model(a)), to_uv(model(b))) for a, b in edges]
        except Exception:
            continue
        xs = [p[0] for pair in uvs for p in pair]
        ys = [p[1] for pair in uvs for p in pair]
        diagonal = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        tolerance = max(MIN_CURVE_LENGTH * 2.0,
                        diagonal * HATCH_EDGE_TOLERANCE)
        near = [item for item in flat
                if item[2] <= max(xs) + tolerance
                and item[4] >= min(xs) - tolerance
                and item[3] <= max(ys) + tolerance
                and item[5] >= min(ys) - tolerance]
        for (a, b), (ua, ub) in zip(edges, uvs):
            middle = ((ua[0] + ub[0]) * 0.5, (ua[1] + ub[1]) * 0.5)
            if any(_off_segment_uv(ua, la, lb) <= tolerance
                   and _off_segment_uv(ub, la, lb) <= tolerance
                   and _off_segment_uv(middle, la, lb) <= tolerance
                   for la, lb, _x0, _y0, _x1, _y1 in near):
                continue                    # the CAD already draws this edge
            curve = line(a, b)
            if curve is not None:
                made.append((curve, style, layer))
        stats["solids"] += 1
    return made, stats


def _count_of(count, one, many):
    return u"{:,} {}".format(count, one if count == 1 else many)


def unconverted_items(counts):
    """What the CAD still holds that did not come across, one sentence each,
    in words a user can weigh. `counts` holds plain integers; anything at zero
    is left out, and an empty list means nothing would be lost."""
    items = []
    n = counts.get("lines", 0)
    if n:
        items.append(u"{} could not be converted".format(
            _count_of(n, u"line", u"lines")))
    if counts.get("unchecked"):
        items.append(u"Text, dimension values and hatch patterns could not "
                     u"be checked because no DXF was read")
    n = counts.get("dimensions", 0)
    if n:
        items.append(u"{} (the numbers on the CAD dimensions)".format(
            _count_of(n, u"dimension value", u"dimension values")))
    n = counts.get("texts_left", 0)
    if n:
        items.append(u"{} left behind because you chose Geometry only"
                     .format(_count_of(n, u"text", u"texts")))
    n = counts.get("texts_lost", 0)
    if n:
        items.append(u"{} that could not be created".format(
            _count_of(n, u"text", u"texts")))
    n = counts.get("leaders", 0)
    if n:
        items.append(u"{} that could not be rebuilt as Revit leaders, and "
                     u"the lines that drew them are already gone".format(
                         _count_of(n, u"CAD arrow", u"CAD arrows")))
    n = counts.get("hatches", 0)
    if n:
        items.append(u"{} whose pattern could not be drawn".format(
            _count_of(n, u"hatch", u"hatches")))
    n = counts.get("hatches_dense", 0)
    if n:
        items.append(u"{} that could not be drawn, because their pattern is "
                     u"too dense (usually a pattern in the wrong scale)"
                     .format(_count_of(n, u"hatch", u"hatches")))
    n = counts.get("solid_fills", 0)
    if n:
        items.append(u"{} (Revit can only hold a solid fill as a filled "
                     u"region, so at most its outline comes across)"
                     .format(_count_of(n, u"solid fill", u"solid fills")))
    return items


_KEEP_BODY = u"""
  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>
    <TextBlock Grid.Row="0" x:Name="lblIntro" TextWrapping="Wrap"
               FontSize="13" Foreground="__TEXT__" Margin="0,0,0,12"/>
    <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto"
                  MaxHeight="240">
      <StackPanel Margin="0,0,6,0">
__ROWS__
      </StackPanel>
    </ScrollViewer>
    <TextBlock Grid.Row="2" x:Name="lblAsk" TextWrapping="Wrap" FontSize="13"
               Foreground="__DIM__" Margin="0,16,0,0"/>
  </Grid>
"""

_KEEP_FOOTER = u"""
  <Grid>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnKeep" Content="Keep the CAD" IsDefault="True"
              Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
      <Button x:Name="btnDelete" Content="Delete anyway"
              Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""


def confirm_delete_anyway(items):
    """Before the CAD is deleted: say what would be lost with it, and let the
    user keep it. True only when they press "Delete anyway" - closing the
    window, Escape and Enter all keep the CAD."""
    try:
        rows = u"".join(
            _NOTE_ROW.replace("__TEXT__", xml_escape(item))
            .replace("__DOT__", ui.STATUS_WARN)
            for item in items)
        body = (_KEEP_BODY.replace("__ROWS__", rows)
                .replace("__TEXT__", ui.TEXT)
                .replace("__DIM__", ui.TEXT_DIM))
        win = ui.parse(u"Clean Explode CAD", u"Before the CAD is deleted",
                       body, _KEEP_FOOTER, width=540,
                       context=u"The CAD is only deleted without a question "
                               u"when nothing is lost. Everything this tool "
                               u"does, the deletion included, is a single "
                               u"undo step (Ctrl+Z).")
        win.FindName("lblIntro").Text = (
            u"Not everything in this CAD could be converted. Deleting it "
            u"would lose:")
        win.FindName("lblAsk").Text = (
            u"Keep the CAD to check what is missing, or delete it anyway.")
        state = {"delete": False}

        def on_delete(sender, args):
            state["delete"] = True
            win.Close()

        win.FindName("btnDelete").Click += on_delete
        win.FindName("btnKeep").Click += lambda sender, args: win.Close()
        win.ShowDialog()
        return state["delete"]
    except Exception:
        import traceback
        _diag_log("The delete confirmation failed, the CAD is kept.\n"
                  + traceback.format_exc())
        return False


def _roll_back(transaction):
    try:
        if transaction.HasStarted():
            transaction.RollBack()
    except Exception:
        pass


def _import_facts(import_id):
    """What Revit says about the import right now, for the error log: when a
    delete is refused, this is what tells a pinned CAD from a missing one."""
    facts = []
    try:
        element = doc.GetElement(import_id)
    except Exception as ex:
        return "GetElement failed: {}".format(ex)
    if element is None:
        return "GetElement returned None (the element is not in the model)"
    for label, read in (
            ("type", lambda: type(element).__name__),
            ("valid", lambda: element.IsValidObject),
            ("pinned", lambda: element.Pinned),
            ("owner view", lambda: element.OwnerViewId.ToString()),
            ("group", lambda: element.GroupId.ToString()),
            ("can delete",
             lambda: DocumentValidation.CanDeleteElement(doc, import_id))):
        try:
            facts.append("{}={}".format(label, read()))
        except Exception as ex:
            facts.append("{}=?({})".format(label, ex))
    return ", ".join(facts)


def remove_original_import(import_id, cleanup, swallower):
    """Delete or hide the original CAD import, found again from its id.

    The element is looked up fresh, never read off the object that was
    picked at the start: by the time this runs the document has been through
    dozens of transactions. Returns (outcome, message): outcome is "done"
    when the delete or the hide took effect, "gone" when the CAD was already
    not in the model, "failed" otherwise; message is None when it worked, or
    (level, sentence) in words a user can act on. Revit's own message
    ("ElementId cannot be deleted. Parameter name: elementId") says nothing
    about what to do, so it goes to the error log, with the state of the
    element, and not to the report.
    """
    deleting = cleanup == "delete"
    verb = "removed from the model" if deleting else "hidden in this view"
    by_hand = ("It is still in the view: select it and press Delete to "
               "remove it yourself." if deleting else
               "Hide it by hand if it is in the way.")
    try:
        element = doc.GetElement(import_id)
    except Exception:
        element = None
    if element is None:
        return "gone", ("info", "The original CAD was no longer in the "
                        "model, so there was nothing to {}.".format(
                            "delete" if deleting else "hide"))

    transaction = start_transaction(
        "Clean Explode CAD - {} import".format(cleanup.capitalize()),
        swallower)
    problem = None
    try:
        if deleting:
            # A pinned CAD is the usual reason Revit refuses to delete one,
            # and it is about to be deleted anyway.
            try:
                if element.Pinned:
                    element.Pinned = False
            except Exception:
                pass
            allowed = True
            try:
                allowed = bool(
                    DocumentValidation.CanDeleteElement(doc, import_id))
            except Exception:
                allowed = True          # unknown: let Delete have its say
            if allowed:
                doc.Delete(import_id)
            else:
                problem = "DocumentValidation.CanDeleteElement is False"
        else:
            active_view.HideElements(List[ElementId]([import_id]))
    except Exception as ex:
        problem = "{}".format(ex)

    if problem:
        _roll_back(transaction)
        _diag_log("Original import not {}: {}\n{}".format(
            "deleted" if deleting else "hidden", problem,
            _import_facts(import_id)))
        return "failed", ("warn", "The original CAD could not be {}. {}"
                          .format(verb, by_hand))
    if not close_transaction(transaction):
        return "failed", ("warn", "The original CAD could not be {}: Revit "
                          "would not accept the change. {}".format(
                              verb, by_hand))
    if deleting:
        try:
            still_there = doc.GetElement(import_id) is not None
        except Exception:
            still_there = False
        if still_there:
            _diag_log("Delete accepted but the import is still in the model: "
                      + _import_facts(import_id))
            return "failed", ("warn", "The original CAD could not be {}. {}"
                              .format(verb, by_hand))
    return "done", None


def main():
    if active_view is None or active_view.ViewType not in VALID_VIEW_TYPES:
        ui.alert(
            "Detail lines can only be created in 2D views\n"
            "(Drafting, Detail, Plan, Section, Elevation).\n\n"
            "Current view: {} ({})".format(
                active_view.Name if active_view is not None else "-",
                active_view.ViewType if active_view is not None else "-"),
            title="Clean Explode CAD")
        script.exit()

    import_instance = get_import_instance()
    if import_instance is None:
        script.exit()

    # Read the name NOW, while the element is certainly alive. With the
    # "delete the import" option the summary used to ask a deleted element
    # for its name and the whole run died on the last line, after every
    # curve had already been converted (2026-09-22).
    try:
        import_label = Element_name(import_instance) or "CAD import"
    except Exception:
        import_label = "CAD import"
    # The id too, now: from here on the import is always found again from
    # it (see remove_original_import), never read off the picked object.
    import_id = import_instance.Id

    styles = collect_line_styles()
    if not styles:
        ui.alert("No line styles found in this {}.\n"
                 "The tool never creates new styles, so it cannot run."
                 .format("family" if IS_FAMILY else "project"),
                 title="Clean Explode CAD")
        script.exit()
    match_style = build_style_matcher(styles)

    # Read the import before asking anything, so the window can say what it
    # holds and the user decides with that in front of them.
    curves, fills, census = extract_geometry(import_instance)

    answers = ask_options(import_instance, census, fills, curves, styles,
                          match_style)
    if answers is None:
        script.exit()
    import_text = answers["text"]
    ignore_hatches = answers["ignore_hatches"]
    cleanup = answers["cleanup"]

    # The DXF is read whenever it is needed: for the text, for leaving the
    # hatches out, or for both. The options window already read it (and
    # already asked for the path) the moment either checkbox needed it, so
    # this reuses that instead of asking a second time.
    geometry_only = ("Text and leaders were not imported - you chose "
                     "geometry only.")
    wants_dxf = import_text or ignore_hatches
    # Even on "Geometry only", a DXF that is already linked or chosen is read
    # (never asked for): it is how the tool knows what the CAD holds that
    # would be lost with it, before deleting it.
    spare_dxf = None
    if not wants_dxf:
        spare_dxf = answers.get("dxf_path")
        if not spare_dxf:
            linked = linked_dxf_path(import_instance)
            if linked and os.path.isfile(linked):
                spare_dxf = linked
    skipped_text = []           # DXF texts left behind by "Geometry only"
    if wants_dxf or spare_dxf:
        (text_records, cad_leaders, cad_hatches, dxf_header, dxf_extent,
         text_source, dxf_status) = load_cad_text(
            import_instance,
            known_path=(answers.get("dxf_path") if wants_dxf
                        else spare_dxf),
            path_resolved=(answers.get("dxf_path_resolved", False)
                           if wants_dxf else True))
        text_status = dxf_status if import_text else geometry_only
        if not import_text:
            skipped_text = [record for record in text_records
                            if (record.get("text") or "").strip()]
            text_records, cad_leaders = [], []
    else:
        text_records, cad_leaders, cad_hatches, dxf_header = [], [], [], {}
        dxf_extent = text_source = dxf_status = None
        text_status = geometry_only
    dxf_dimension_keys = set(DXF_FACTS.get("dimension_keys") or []) \
        if (wants_dxf or spare_dxf) else set()
    dxf_unreadable = cad_hatches is None
    cad_hatches = cad_hatches or []
    # True only when a DXF was actually read: it is the only way to know what
    # the CAD holds besides its lines.
    dxf_was_read = bool(wants_dxf or spare_dxf) and not dxf_unreadable
    # HATCH entities the DXF shows that the reader could not turn into a
    # hatch (a boundary it cannot represent faithfully). Revit may or may not
    # have handed over their stripes, and there is no boundary to check them
    # against, so they cannot be told apart from lost ones.
    hatches_unread = 0
    if dxf_was_read and not ignore_hatches:
        hatches_unread = max(0, DXF_FACTS.get("hatches", 0) - len(cad_hatches))
    if not curves and not fills and not text_records:
        ui.alert("No usable 2D geometry was found in the selected import.",
                 title="Clean Explode CAD")
        script.exit()

    text_transform = import_transform(import_instance)
    match_text_type, closest_text_type, _text_type_catalog = \
        build_text_type_matcher(active_view,
                               overrides=answers.get("type_overrides"))
    model_box = geometry_bounds(curves, import_instance)
    # Leader tips are as good as text for checking the placement, and a
    # drawing can have many leaders and little text.
    placement_sample = list(text_records)
    for leader in cad_leaders[:400]:
        for point in leader["points"][:2]:
            placement_sample.append(
                {"insertion_point": (point[0], point[1], 0.0)})
    for hatch in cad_hatches[:200]:
        for ring in hatch["rings"][:2]:
            for point in ring[:4]:
                placement_sample.append(
                    {"insertion_point": (point[0], point[1], 0.0)})
    (place_text, text_height_scale, unit_label, unit_confidence,
     placement_verified) = build_text_placement(
        placement_sample, dxf_header, dxf_extent, model_box, text_transform)

    # ---- leaders: tie each arrow to the note it points from -------------
    leaders_attached, orphan_leaders = attach_leaders(text_records,
                                                      cad_leaders)
    carried = leader_text_records(cad_leaders)
    if carried:
        text_records = list(text_records) + carried
        leaders_attached += len(carried)
    dropped_leader_lines = dropped_arrowheads = 0
    if leaders_attached:
        paths = leader_paths(text_records, place_text, active_view)
        curves, dropped_leader_lines = drop_leader_lines(curves, paths)
        fills, dropped_arrowheads = drop_arrowhead_fills(fills, paths)

    # ---- hatches: leave them out when that is what was asked for --------
    hatch_shapes = []
    dropped_hatch_lines = 0
    hatch_placement_poor = False
    solid_hatches = len([h for h in cad_hatches if h.get("solid")])
    if ignore_hatches and cad_hatches:
        # A hatch boundary decides what gets thrown away, so it is only
        # trusted on a real drawing scale that the import corroborates.
        # The last-resort placement lays the DXF over the geometry and can
        # match anything, including an alignment that is plainly wrong, so
        # linework is never removed on the strength of it.
        if not placement_verified \
                or unit_confidence < LAST_RESORT_THRESHOLD:
            hatch_placement_poor = True
        else:
            try:
                to_uv = make_view_uv(active_view)
                hatch_shapes = hatch_areas(cad_hatches, place_text, to_uv)
                curves, dropped_hatch_lines = drop_hatch_linework(
                    curves, hatch_shapes, to_uv)
            except Exception as ex:
                logger.error("Hatches could not be left out: %s", ex)

    # ---- hatches kept as linework: did Revit hand over their pattern? ----
    # With a DXF in hand the tool can tell the hatches whose stripes arrived
    # from the ones that came in empty, which is what it has to know before
    # the CAD is thrown away.
    hatch_audit = {"unchecked": 0, "bare": 0, "unplaced": 0, "drawable": []}
    if not ignore_hatches and cad_hatches:
        patterned = [h for h in cad_hatches if not h.get("solid")]
        if patterned:
            if not placement_verified \
                    or unit_confidence < LAST_RESORT_THRESHOLD:
                hatch_audit["unchecked"] = len(patterned)
            else:
                try:
                    to_uv = make_view_uv(active_view)
                    areas = hatch_areas(patterned, place_text, to_uv)
                    # hatch_areas drops a hatch that is tiny or cannot be
                    # placed: nothing checks it and nothing draws it.
                    hatch_audit["unplaced"] = len(patterned) - len(areas)
                    striped = hatches_with_stripes(curves, areas, to_uv)
                    for index, area in enumerate(areas):
                        if index in striped:
                            continue
                        if area["hatch"].get("segments"):
                            hatch_audit["drawable"].append(area["hatch"])
                        else:
                            hatch_audit["bare"] += 1
                except Exception as ex:
                    logger.error("Hatches could not be checked: %s", ex)
                    hatch_audit.update({"unchecked": len(patterned),
                                        "unplaced": 0, "bare": 0,
                                        "drawable": []})

    # ---- prepare: only what Revit cannot hold is filtered out ------------
    prepared_curves = []
    style_layers = defaultdict(lambda: defaultdict(int))
    too_short = 0
    # The options window's LINE STYLES grid, resolved: a layer nobody
    # touched there still comes back holding exactly what match_style()
    # itself would have proposed for it (see summarize_layer_styles /
    # build_layer_style_grid), so this only ever narrows the automatic
    # match, never bypasses it.
    layer_style_overrides = answers.get("layer_styles") or {}
    for curve, layer, solid, pattern, weight in curves:
        style = layer_style_overrides.get(layer) if layer else None
        if style is None:
            style = match_style(layer, solid, pattern, weight)
        if style is not None and layer:
            style_layers[style["name"]][layer] += 1
        pieces = bind_curve(curve)
        if not pieces:
            census["skipped"] += 1
            continue
        for piece in pieces:
            if curve_is_long_enough(piece):
                prepared_curves.append((piece, style))
            else:
                too_short += 1

    # ---- hatches Revit handed over empty: draw them from the DXF ---------
    # Only on a drawing scale the import corroborates, the same trust the
    # leave-them-out path asks for: lines drawn on a guessed placement would
    # land in the wrong place, and are worse than none.
    hatch_work = {"hatches": 0, "lines": 0, "too_dense": 0, "solids": 0}
    if not ignore_hatches and cad_hatches and placement_verified \
            and unit_confidence >= LAST_RESORT_THRESHOLD:
        layer_facts = {}
        for _curve, layer, solid, pattern, weight in curves:
            if layer:
                layer_facts.setdefault(layer.strip().lower(),
                                       (layer, solid, pattern, weight))

        def hatch_style(layer_name):
            """The line style the imported lines of this hatch's layer get,
            so a pattern drawn from the DXF looks like the rest of it."""
            name, solid, pattern, weight = layer_facts.get(
                (layer_name or "").strip().lower(),
                (layer_name, True, "solid", None))
            style = layer_style_overrides.get(name) if name else None
            if style is None:
                style = match_style(name, solid, pattern, weight)
            return style, name

        try:
            made_lines, hatch_work = hatch_linework(
                hatch_audit["drawable"],
                [h for h in cad_hatches if h.get("solid")],
                place_text, active_view, curves, hatch_style)
            for curve, style, layer in made_lines:
                prepared_curves.append((curve, style))
                if style is not None and layer:
                    style_layers[style["name"]][layer] += 1
        except Exception as ex:
            logger.error("Hatches could not be drawn from the DXF: %s", ex)

    prepared_text = [record for record in text_records
                     if (record.get("text") or "").strip()]

    # ---- text size: ask before adding anything to the project ------------
    planned_types = []          # [(bucket, size, base type, name)] to make
    forced_by_bucket = {}       # {size bucket: the type made for it}
    text_size_note = None
    created_types = []          # [(name, size applied)] for the report

    paper_sizes = []
    for record in prepared_text:
        model_height = text_height_in_model(record, text_height_scale)
        if model_height:
            paper_sizes.append(model_height / max(active_view.Scale, 1))

    # Only for the scale warning in the report now: what to do about each
    # size was settled in the options window.
    below_floor = [(size, count) for size, count in group_sizes(paper_sizes)
                   if size < MIN_TEXT_SIZE]

    def warn_about_scale():
        """The scale warning, in the units in force when it is asked for."""
        if not below_floor:
            return None
        return (
            u"\u26a0 {} of the notes work out at {} or less on paper in "
            "this view (scale 1:{}), under the {} that is the smallest "
            "text Revit can hold. Text that small almost always means "
            "the CAD is not at the scale the drawing assumes: check the "
            "DWG's units and the scale it was drawn at before trusting "
            "these notes. They are placed with the closest existing "
            "text type.".format(
                sum(count for _, count in below_floor),
                format_size(max(size for size, _ in below_floor)),
                active_view.Scale, format_size(MIN_TEXT_SIZE)))

    for bucket, spec in (answers.get("new_types") or {}).items():
        size, base_type, name = spec
        planned_types.append((bucket, size, base_type, name))

    moved_to_plane = [0]
    # Curves that lost their type: a CAD not parallel to the view cannot be
    # slid onto it, and the API has no "project keeping the curve type", so
    # the only way left is walking the curve and joining straight segments.
    # Counted to be said out loud: an ellipse coming back as thirty little
    # lines is a surprise otherwise, and it means the DWG is not flat.
    faceted_curves = [0]
    halved = [0]        # circles Revit would not take whole

    def text_type_for(model_height):
        """The type this note's size group was given in the options window.

        By bucket, not by nearest size: the window asked the question per
        group, so the answer is looked up the same way. The old nearest-size
        tie-break is what let a newly created type quietly outrank a type
        the user had picked by hand.
        """
        if forced_by_bucket and model_height:
            paper = model_height / max(active_view.Scale, 1)
            made = forced_by_bucket.get(_size_bucket_key(paper))
            if made is not None:
                return made
        return match_text_type(model_height)

    dim_note_keys = {}          # note id -> the CAD dimension it carries

    def dimension_of(record):
        """Which dimension a record is the value of."""
        return record.get("dimension") or ("record", id(record))

    def make_text(record):
        note = create_text_note(record, text_transform, text_type_for,
                                place_text, text_height_scale)
        if note is None:
            return []
        if record.get("source") == "DIMENSION":
            dim_note_keys[note.Id.ToString()] = dimension_of(record)
        if record.get("leaders"):
            try:
                add_note_leaders(note, record, place_text, active_view, census)
            except Exception as ex:
                logger.debug("Leaders failed on a note: %s", ex)
                census["leaders_failed"] += len(
                    [one for one in record["leaders"] if not one.get("used")])
            try:
                if not type_has_arrowhead(note.TextNoteType):
                    census["leaders_without_arrowhead"] += 1
            except Exception as ex:
                logger.debug("Arrowhead check failed on a note: %s", ex)
        return [(note_type_name(note), note.Id)]

    def make_line(item):
        curve, style = item

        def create(crv):
            detail = new_detail_curve(active_view, crv)
            if style is not None:
                try:
                    detail.LineStyle = style["style"]
                except Exception:
                    pass
            return (style["name"] if style else "Unknown", detail.Id)

        def place(one, allow_facets=True):
            """Get one curve into the view: as it stands, else moved onto
            the view plane, else - only when nothing else is left - broken
            into straight segments."""
            try:
                # The curve exactly as the DWG has it.
                return [create(one)]
            except Exception as ex:
                logger.debug("Curve refused as-is (%s)", ex)

            # Refused: almost always because the CAD sits at a different
            # height than the view. Moving it keeps arcs, circles and
            # splines whole.
            moved = move_to_view_plane(one, active_view)
            if moved is not None and curve_is_long_enough(moved):
                try:
                    done = [create(moved)]
                    moved_to_plane[0] += 1
                    return done
                except Exception as ex:
                    logger.debug("Moved curve failed: %s", ex)

            if not allow_facets:
                return []

            # Genuinely not parallel to the view: project it, segmenting
            # only what cannot be projected any other way.
            done = []
            for piece in flatten_curve(one, active_view):
                if not curve_is_long_enough(piece):
                    continue
                try:
                    done.append(create(piece))
                except Exception as ex:
                    logger.debug("Curve failed: %s", ex)
            if done and not isinstance(one, Line):
                # A straight line flattens to a straight line: nothing lost,
                # nothing to report. Only curves are worth counting here.
                faceted_curves[0] += 1
            return done

        whole = is_whole_circle(curve)
        made = place(curve, not whole)
        if made or not whole:
            return made

        # A whole circle this build of Revit will not take in one piece:
        # halve it rather than facet it. Each half gets the same treatment,
        # so a circle at another height still comes through as two arcs
        # instead of thirty little lines.
        halves = split_curve(curve)
        if len(halves) >= 2:
            complete = True
            for half in halves:
                done = place(half) if curve_is_long_enough(half) else []
                if not done:
                    complete = False
                made.extend(done)
            if made:
                if complete:
                    halved[0] += 1
                return made

        return place(curve)      # nothing else worked: facets it is

    # ---- create: two independent phases, one undo step -------------------
    swallower = FailureSwallower()
    text_made, line_made = [], []

    phases = (
        ("Clean Explode CAD - Text Notes", prepared_text, make_text, text_made),
        ("Clean Explode CAD - Detail Lines", prepared_curves, make_line,
         line_made),
    )
    phase_failures = {}
    phase_errors = defaultdict(lambda: defaultdict(int))

    type_failures = []          # [(name, what Revit said)]
    cleanup_result = None       # None, or (level, sentence) for the report
    # What really happened to the CAD, for the report: the answer picked in
    # the options window says what was ASKED, not what Revit did.
    import_gone = False         # no longer in the model
    import_hidden = False       # hidden in this view
    kept_by_choice = False      # the user pressed "Keep the CAD"

    group = TransactionGroup(doc, "Clean Explode CAD")
    group.Start()
    try:
        taken_names = taken_type_names()
        for bucket, size, base_type, name in planned_types:
            # A name typed by hand can collide with one the project already
            # has; Duplicate() would just throw. Step aside instead.
            name = unique_type_name(name, taken=taken_names)
            taken_names.add(name)
            transaction = start_transaction(
                "Clean Explode CAD - Text Type", swallower)
            made = None
            try:
                made = create_cad_text_type(base_type, size, name)
                if made:
                    swallower.own_ids = set([made[0].Id.ToString()])
            except Exception as ex:
                logger.error("New text type failed (%s at %s): %s",
                             name, format_size(size), ex)
                type_failures.append((name, str(ex)))
            if close_transaction(transaction) and made:
                new_type, applied = made
                forced_by_bucket[bucket] = new_type
                created_types.append((type_name(new_type), size, applied))

        if type_failures:
            # One line for the lot: six identical errors in the output
            # window say no more than one does.
            reasons = sorted(set(reason for _, reason in type_failures))
            text_size_note = (
                "{} of the {} new text types could not be made, so those "
                "notes took the closest existing type instead.\nRevit "
                "said: {}".format(len(type_failures), len(planned_types),
                                  "; ".join(reasons)))

        # No progress bar here on purpose: pyRevit's bar re-anchors on
        # HOST_APP.uiapp at every tick, and that goes None after the first
        # commit (a known Revit API gotcha: it fails after the first Transaction).
        # The batches commit as they go, so nothing is lost if Revit is
        # slow; the report at the end says what happened.
        for name, items, action, made in phases:
            try:
                phase_failures[name] = create_in_batches(
                    name, items, action, swallower, made,
                    errors=phase_errors[name])
            except Exception as ex:
                logger.error("%s failed: %s", name, ex)
                phase_errors[name][str(ex)[:150]] += 1
                phase_failures[name] = len(items)

        # ---- the original import: what the options window said ------------
        # Inside the same transaction group as the conversion, so the whole
        # run - new lines, notes AND the removal of the CAD - is one Ctrl+Z.
        # Own DBTransaction on the captured `doc`: the pyrevit wrapper reads
        # revit.doc, which is None from the first commit on (see gotchas).
        lines_now = count_survivors(line_made)[0]
        notes_now = count_survivors(text_made)[0]
        if cleanup in ("delete", "hide") and (lines_now or notes_now):
            try:
                leftovers = []
                if cleanup == "delete":
                    # Deleting is the one answer that loses whatever did not
                    # come across, so it is the one that asks first.
                    dim_records = [
                        r for r in list(text_records) + list(skipped_text)
                        if r.get("source") == "DIMENSION"]
                    dims_attempted = len(
                        [r for r in prepared_text
                         if r.get("source") == "DIMENSION"])
                    # Notes that carry a dimension value, and the distinct
                    # dimensions they cover: a dimension whose block has two
                    # texts is one dimension, not two.
                    dims_made = len(
                        [1 for _name, eid in text_made
                         if eid.ToString() in dim_note_keys])
                    made_dimensions = set(
                        dim_note_keys[eid.ToString()]
                        for _name, eid in text_made
                        if eid.ToString() in dim_note_keys
                        and element_alive(eid))
                    all_dimensions = set(dxf_dimension_keys) | set(
                        dimension_of(r) for r in dim_records)
                    # Curves Revit refused (a batch it would not commit, a
                    # curve it could not take, an element it deleted on
                    # commit) are gone with the CAD. Fragments too short to
                    # draw (too_short) are not a loss: they are noise.
                    lines_unconverted = (
                        phase_failures.get(
                            "Clean Explode CAD - Detail Lines", 0)
                        + census.get("skipped", 0) + swallower.rejected
                        + census.get("other", 0))
                    if prepared_curves and not lines_now:
                        # Notes came across but not one line did: all of
                        # them are still only in the CAD.
                        lines_unconverted = max(lines_unconverted,
                                                len(prepared_curves))
                    leftovers = unconverted_items({
                        "lines": lines_unconverted,
                        "leaders": census.get("leaders_failed", 0),
                        # No DXF read: the text, the dimension values and the
                        # hatch patterns of the CAD were never looked at, so
                        # nothing can say they came across.
                        "unchecked": not dxf_was_read,
                        "dimensions": len(all_dimensions - made_dimensions),
                        "texts_left": len(
                            [r for r in skipped_text
                             if r.get("source") != "DIMENSION"]),
                        "texts_lost": max(
                            0, (len(prepared_text) - dims_attempted)
                            - (notes_now - dims_made)),
                        "hatches": (hatch_audit["bare"]
                                    + hatch_audit["unchecked"]
                                    + hatch_audit["unplaced"]
                                    + hatches_unread
                                    + len(hatch_audit["drawable"])
                                    - hatch_work["hatches"]
                                    - hatch_work["too_dense"]),
                        "hatches_dense": hatch_work["too_dense"],
                        # Without a DXF the only count is what Revit itself
                        # hands over as solid fills (hatches and arrowheads).
                        "solid_fills": (len(fills) if not dxf_was_read
                                        else 0 if ignore_hatches
                                        else solid_hatches),
                    })
                if leftovers and not confirm_delete_anyway(leftovers):
                    kept_by_choice = True
                    cleanup_result = (
                        "warn", u"The original CAD was kept, because it "
                        u"still holds what did not come across: {}."
                        .format(u"; ".join(leftovers)))
                else:
                    outcome, cleanup_result = remove_original_import(
                        import_id, cleanup, swallower)
                    import_gone = (outcome == "gone"
                                   or (outcome == "done"
                                       and cleanup == "delete"))
                    import_hidden = outcome == "done" and cleanup == "hide"
                    if leftovers and cleanup_result is None:
                        cleanup_result = (
                            "warn", u"The original CAD was deleted at your "
                            u"word, with what did not come across: {}."
                            .format(u"; ".join(leftovers)))
            except Exception:
                import traceback
                _diag_log("Cleaning up the original import failed.\n"
                          + traceback.format_exc())
                cleanup_result = (
                    "warn", "The original CAD could not be {}. Select it and "
                    "press Delete to remove it yourself.".format(
                        "removed from the model" if cleanup == "delete"
                        else "hidden in this view"))
    finally:
        try:
            if group.GetStatus() == TransactionStatus.Started:
                group.Assimilate()
        except Exception as ex:
            logger.debug("Could not assimilate transaction group: %s", ex)

    if import_gone:
        # Trust the model, not the flag: the group is closed now.
        try:
            if doc.GetElement(import_id) is not None:
                import_gone = False
                cleanup_result = (
                    "warn", "The original CAD is still in the model: select "
                    "it and press Delete to remove it yourself.")
        except Exception:
            pass

    text_failed = phase_failures.get("Clean Explode CAD - Text Notes", 0)
    failed = phase_failures.get("Clean Explode CAD - Detail Lines", 0)

    created, styles_used = count_survivors(line_made)
    text_created, text_types_used = count_survivors(text_made)

    if created == 0 and text_created == 0:
        ui.alert(
            "Nothing could be created in this view."
            + ("\n\nThe import holds {} solid fills. Revit can "
               "only hold a solid fill as a filled region, and this tool "
               "never creates those."
               .format(len(fills)) if fills else ""),
            title="Clean Explode CAD")
        script.exit()

    # ---- summary ---------------------------------------------------------
    # The same report in two shapes: `notes` drives the /slantis summary
    # window (one state dot per sentence, warnings first), `parts` keeps the
    # plain text for the Copy button and for the fallback window. say()
    # writes both at once, so the two can never drift apart.
    notes = []
    parts = ["Successfully converted {} lines across {} existing "
             "styles.".format(created, len(styles_used))]

    def say(text, level="info"):
        parts.append(text)
        notes.append((level, text))

    if styles_used:
        parts.append("\n".join(
            "   • {}: {} lines".format(n, c)
            for n, c in sorted(styles_used.items(), key=lambda kv: -kv[1])))
    if text_created:
        headline = ("Created {} editable Revit text notes from {}\n"
                    "(read as {}, {:.0f}% of it landing on the drawing):"
                    .format(text_created,
                            os.path.basename(text_source or "the DXF"),
                            unit_label, unit_confidence * 100.0))
        type_lines = "\n".join(
            "   • {}: {}".format(n, c)
            for n, c in sorted(text_types_used.items(),
                               key=lambda kv: -kv[1]))
        parts.append(headline)
        parts.append(type_lines)
        notes.append(("ok", headline + "\n" + type_lines))
    elif text_status:
        say(text_status)
    elif prepared_text:
        reasons = phase_errors.get("Clean Explode CAD - Text Notes") or {}
        detail = "\n".join(
            "   • {} ({}x)".format(message, count)
            for message, count in sorted(reasons.items(),
                                         key=lambda kv: -kv[1])[:3])
        say("Found {} text entities in {} but none could be placed.\n"
            "Units read as {} ({:.0f}% of the text landing on the drawing)."
            .format(len(prepared_text),
                    os.path.basename(text_source or "the DXF"), unit_label,
                    unit_confidence * 100.0)
            + ("\nRevit said:\n" + detail if detail else ""), "warn")
    if created_types:
        lines = []
        for name, wanted, applied in created_types:
            if applied and wanted and applied < wanted * 0.999:
                lines.append('   • "{}" at {} - Revit would not accept '
                             'the {} the CAD asks for'.format(
                                 name, format_size(applied),
                                 format_size(wanted)))
            else:
                lines.append('   • "{}" at {}'.format(
                    name, format_size(applied or wanted)))
        say("New text type{} created{}:\n{}".format(
            "" if len(created_types) == 1 else "s",
            ", each note taking the one you picked for its size",
            "\n".join(lines)), "ok")
    scale_warning = warn_about_scale()
    if scale_warning:
        say(scale_warning, "warn")
    if text_size_note:
        say(text_size_note)
    if text_failed and text_created:
        say("Text entities that could not be created: {}."
            .format(text_failed), "warn")
    if ignore_hatches:
        if dropped_hatch_lines:
            say("Left the hatches out: {} lines that drew {} hatched areas "
                "were removed. The outline of each area was kept, so you can "
                "place your own filled regions over them."
                .format(dropped_hatch_lines, len(hatch_shapes)), "ok")
        elif hatch_shapes:
            say("Found {} hatches in the DXF, but none of the imported "
                "linework fell inside them, so nothing was removed. That "
                "happens when a hatch was exploded in CAD, or when its lines "
                "sit on a different layer than the hatch itself."
                .format(len(hatch_shapes)))
        elif hatch_placement_poor:
            say("{} hatches were read from the DXF, but it could not be "
                "matched to the import closely enough to say where they "
                "are{}. Nothing was removed, because guessing would have "
                "taken real linework with it. Exporting the DXF from the "
                "same drawing, unclipped and with the same layers on, is "
                "what makes this work."
                .format(len(cad_hatches),
                        "" if not placement_verified
                        else " (only {:.0f}% of it landed on the drawing)"
                        .format(unit_confidence * 100.0)), "warn")
        elif dxf_unreadable:
            say("The hatches could not be left out because the DXF was not "
                "read{}".format(": " + dxf_status if dxf_status else "."),
                "warn")
        elif cad_hatches and solid_hatches >= len(cad_hatches):
            say("The {} hatches in the DXF are all solid fills. Revit "
                "imports those as a filled shape rather than as lines, so "
                "there was no hatch linework to leave out."
                .format(len(cad_hatches)))
        elif cad_hatches:
            say("{} hatches were read from the DXF but could not be located "
                "on the drawing, so none were left out."
                .format(len(cad_hatches)), "warn")
        else:
            say("No HATCH entities were found in the DXF, so there was "
                "nothing to leave out. A hatch that was exploded in CAD is "
                "ordinary linework and cannot be told apart from it.")
    if not ignore_hatches and cad_hatches:
        if hatch_work["lines"]:
            say("Drew {:,} detail lines for the pattern of {} hatch{}: Revit "
                "did not hand over their stripes, so they come from the DXF, "
                "on the line style of each hatch's layer.".format(
                    hatch_work["lines"], hatch_work["hatches"],
                    "" if hatch_work["hatches"] == 1 else "es"), "ok")
        if hatch_work["too_dense"]:
            say("{} hatches could not be drawn: their pattern is too dense "
                "(usually a pattern in the wrong scale) or would have taken "
                "the run past {:,} pattern lines. A half-drawn hatch would "
                "be false, so none of their lines were made."
                .format(hatch_work["too_dense"], HATCH_LINE_BUDGET), "warn")
        if hatch_work["solids"]:
            say("{} solid fill{} kept as outline. Revit can only hold a "
                "solid fill as a filled region, and this tool never creates "
                "one.".format(hatch_work["solids"],
                              "" if hatch_work["solids"] == 1 else "s"))
        if hatch_audit["unchecked"]:
            say("{} hatches could not be matched to the import closely "
                "enough to tell whether their pattern arrived or to draw "
                "it. Nothing was drawn for them, because lines placed on a "
                "guess would land in the wrong place."
                .format(hatch_audit["unchecked"]), "warn")
        if hatch_audit["bare"]:
            say("{} hatches came in without pattern lines and the DXF "
                "carries none to draw either.".format(hatch_audit["bare"]),
                "warn")
        if hatch_audit["unplaced"]:
            say("{} hatches were too small or could not be placed on the "
                "drawing, so their pattern was neither checked nor drawn."
                .format(hatch_audit["unplaced"]), "warn")
    if hatches_unread:
        say("{} hatches in the DXF could not be read (their outline is a "
            "shape the reader cannot follow), so their pattern was neither "
            "checked nor drawn.".format(hatches_unread), "warn")
    if fills:
        # The solids Revit hands over are solid HATCHES and the filled tip of
        # an arrow or a dimension. Only the DXF can tell which, so the
        # sentence says "hatch" only when there is one to say it about.
        dxf_read_ok = dxf_was_read
        if dxf_read_ok and not solid_hatches:
            say("{} filled arrowhead{} (solid fills) {} not converted: Revit "
                "can only hold a solid fill as a filled region, and this "
                "tool never creates one. The outline of each arrow still "
                "comes in as linework.".format(
                    len(fills), "" if len(fills) == 1 else "s",
                    "was" if len(fills) == 1 else "were"))
        else:
            say("{} solid fill{} {} not converted{}. Revit can only hold a "
                "solid fill as a filled region, and this tool never creates "
                "one; any outline the CAD drew still comes in as linework."
                .format(len(fills), "" if len(fills) == 1 else "s",
                        "was" if len(fills) == 1 else "were",
                        " (solid hatches and filled arrowheads)"
                        if dxf_read_ok else ""), "warn")
    if halved[0]:
        say("{} circles had to be split into two halves - this "
            "build of Revit would not take them as one element."
            .format(halved[0]), "warn")
    if census.get("rebuilt_circles"):
        say("Rebuilt {} faceted rings as whole circles."
            .format(census["rebuilt_circles"]), "ok")
    if census.get("rebuilt_arcs"):
        say("Rebuilt {} real arcs out of faceted linework{}."
            .format(census["rebuilt_arcs"],
                    ", replacing {} straight segments".format(
                        census["facets_replaced"])
                    if census.get("facets_replaced") else ""), "ok")
    elif census.get("facets_replaced"):
        say("Merged {} redundant straight segments."
            .format(census["facets_replaced"]), "ok")
    if moved_to_plane[0]:
        say("Moved {} curves onto the view plane (the CAD sits at a "
            "different height); their shape was kept."
            .format(moved_to_plane[0]))
    if faceted_curves[0]:
        say("{} {} not parallel to the view, so {} come through as "
            "straight segments instead of real curves: the DWG has "
            "geometry off the drawing plane. Check the CAD if the "
            "shape matters."
            .format(faceted_curves[0],
                    "curve was" if faceted_curves[0] == 1
                    else "curves were",
                    "it had to" if faceted_curves[0] == 1
                    else "they had to"), "warn")
    if census.get("leaders"):
        say("Rebuilt {} CAD arrows as real Revit leaders on the text they "
            "point from{}{}.".format(
                census["leaders"],
                ", removing {} lines that drew them".format(
                    dropped_leader_lines) if dropped_leader_lines else "",
                " and {} filled arrowheads".format(dropped_arrowheads)
                if dropped_arrowheads else ""), "ok")
    if census.get("leaders_failed"):
        say("{} CAD arrows could not be rebuilt as Revit leaders, and the "
            "lines that drew them were removed.".format(
                census["leaders_failed"]), "warn")
    if census.get("leaders_without_arrowhead"):
        say("{} of those notes use a text type with no leader arrowhead set, "
            "so their leaders draw without an arrow. Set Leader Arrowhead in "
            "that type's properties to fix it."
            .format(census["leaders_without_arrowhead"]), "warn")
    if orphan_leaders:
        say("{} CAD arrows had no text to hang from and were left "
            "as linework.".format(len(orphan_leaders)), "warn")
    if too_short:
        say("Skipped {} CAD fragments shorter than Revit's minimum "
            "line length ({:.4f}\")."
            .format(too_short, MIN_CURVE_LENGTH * 12.0), "warn")
    if swallower.foreign:
        say("{} batches were rolled back and retried because Revit's "
            "complaint involved elements that were already in the model; "
            "those were left untouched and only the new curve at fault was "
            "dropped.".format(swallower.foreign), "warn")
    if cleanup_result:
        say(cleanup_result[1], cleanup_result[0])
    if swallower.rejected:
        say("Revit rejected {} more elements while saving; they were "
            "removed and everything else was kept."
            .format(swallower.rejected), "warn")
    if failed or census.get("skipped"):
        say("Curves that could not be converted: {}."
            .format(failed + census.get("skipped", 0)), "warn")

    # Built with an explicit loop instead of a generator handed to join().
    # On 2026-09-22 this one statement died with "Sequence contains no
    # elements" (a .NET exception surfacing as SystemError) AFTER the whole
    # import had already been converted, so a line of the summary took a
    # finished run down with it. The same census values replayed outside
    # Revit do not reproduce it, so the cause is still open: the loop keeps
    # a summary from ever being fatal again, and dumps the census, with the
    # type of every key and value, the moment a piece refuses.
    DERIVED_COUNTS = ("rebuilt_arcs", "rebuilt_circles", "skipped",
                      "polyline_points", "facets_replaced", "leaders",
                      "leaders_without_arrowhead", "leaders_failed")
    geometry_bits = []
    contents = []
    for key in sorted(census):
        if key in DERIVED_COUNTS:
            continue
        try:
            count = census[key]
            if count:
                geometry_bits.append("{0} {1}".format(count, key))
                contents.append((key, count))
        except Exception as ex:
            logger.error(
                "Census entry refused: key=%r (%s) -- %s",
                key, type(key).__name__, ex)
    try:
        geometry_report = ", ".join(geometry_bits)
    except Exception as ex:
        geometry_report = ""
        logger.error("Geometry report could not be assembled: %s", ex)
    if not geometry_report and census:
        # Nothing came out of a non-empty census. That is the shape the
        # 2026-09-22 failure had, and it is the one case where the census
        # contents are worth keeping, so they go to the log file (the logger
        # can drop them) with the type of every key and value.
        try:
            dump = "; ".join(
                ["%r (%s) = %r (%s)" % (k, type(k).__name__,
                                        census[k], type(census[k]).__name__)
                 for k in sorted(census)])
        except Exception as ex:
            dump = "census could not be dumped: %s" % (ex,)
        _diag_log("Census produced no geometry line. Census was: " + dump)
        logger.info("Census produced no geometry line. Census was: %s", dump)
    contents.sort(key=lambda kv: -kv[1])
    if geometry_report:
        parts.append("What the import contained: {}{}.".format(
            geometry_report,
            " ({} polyline points)".format(census["polyline_points"])
            if census.get("polyline_points") else ""))
    if census.get("other"):
        say("Pattern hatches (ANSI31...) arrive as loose lines with "
            "no boundary, so they convert as linework.")

    # What the CAD ended up as, from what happened and not from what was
    # asked: a refused delete or a "Keep the CAD" must not read "deleted".
    # The subject first, what was done to it second: a big "Deleted" or
    # "Hidden" reads as an alarm when it is really just the cleanup the user
    # asked for in the options window (2026-09-22).
    if import_gone:
        after, fate = "import deleted", "deleted after converting"
    elif import_hidden:
        after, fate = "import hidden", "hidden in this view"
    elif kept_by_choice:
        after, fate = "import kept", "kept, as you chose"
    elif cleanup == "delete":
        after, fate = "import not deleted", "could not be deleted"
    elif cleanup == "hide":
        after, fate = "import not hidden", "could not be hidden"
    else:
        after, fate = "import kept", "left in the view"
    kpis = [("{:,}".format(created), "detail lines created"),
            ("{:,}".format(len(styles_used)), "existing line styles used")]
    if text_created or prepared_text:
        kpis.append(("{:,}".format(text_created), "text notes created"))
    kpis.append(("Source CAD", fate))
    show_summary(
        subtitle=u"{}  →  {}".format(import_label, active_view.Name),
        summary=u"{:,} curves · {:,} text notes · {}".format(
            created, text_created, after),
        kpis=kpis, styles_used=styles_used, style_layers=style_layers,
        contents=contents,
        polyline_points=census.get("polyline_points", 0),
        notes=notes, plain_text="\n\n".join(parts))

    logger.info("Geometry: %s | Unit scale: %s (%s) | Rejected: %s",
                dict(census), text_height_scale, unit_label,
                dict(swallower.descriptions))


with usage.tool_run(__file__) as run:
    if __name__ == "__main__":
        try:
            main()
        except Exception as err:
            run.error()
            import traceback
            # A message like "Sequence contains no elements" comes straight out of
            # a .NET call and never says which one, so the traceback is the only
            # way to find it. It is NOT left to the output window: pyRevit's
            # logger drops a message without a word when its runtime service does
            # not resolve (LoggerWrapper._emit returns early on a None service,
            # and the Log call itself sits inside an except: pass). So the
            # traceback goes on screen and to disk, where it cannot get lost.
            trace = traceback.format_exc()
            logger.error("Clean Explode CAD failed: %s\n%s", err, trace)
            log_path = _diag_log(trace)
            try:
                ui.show_report(
                    trace, title="Clean Explode CAD",
                    subtitle=u"The tool failed: {}".format(err),
                    summary=(u"Anything already converted was kept in the view."
                             + (u"  \u00b7  Also saved to {}".format(log_path)
                                if log_path else u"")),
                    width=760, height=560)
            except Exception:
                ui.alert("The tool failed with an error:\n\n{}\n\n"
                         "Anything already converted was kept in the view.\n\n"
                         "{}".format(err, trace),
                         title="Clean Explode CAD")
