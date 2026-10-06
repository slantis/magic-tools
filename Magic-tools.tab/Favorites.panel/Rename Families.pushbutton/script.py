# -*- coding: utf-8 -*-
__title__ = "Rename\nFamilies"
__author__ = "slantis"
__doc__ = "Cleans up family and type names in bulk, in one window, with seven rules you can stack or use alone, grouped the way you decide them. FIND: remove a piece of text wherever it sits in the name (an office code, a vendor tag), or replace it with another. PREFIX AND SUFFIX: add text at either end, or trim a fixed number of characters off either end for the prefixes no pattern describes. CASE: rewrite the result in Title Case or UPPERCASE. They run in a fixed order -- the trims first, so the sliders count against the name you are reading; the case change before the text you add, so a prefix typed as KDA- is not handed back as Kda-. The tree (Revit category, then family, then its types, each one the toggle for everything under it, with a dash when only part of it is ticked) previews every resulting name live, and the column on the right spells out which rules actually reached each row. Rename never writes straight away: it first opens a review with every change, what will not be renamed and why, and only Confirm touches the model (Back to edit returns to the rules as you left them). Acronyms that must stay uppercase in Title Case (ADA, MEP, RCP...) live in an editable list under %APPDATA%\\MagicTools. Name clashes are flagged in red as you type and those rows are never written. The search box only narrows what you SEE -- the footer always says how many rows are ticked, how many of them the rules rename, and how many of those the search is hiding."

import json
import os
import re

import clr

from pyrevit import revit, DB, script

from System import TimeSpan
from System.Collections.ObjectModel import ObservableCollection
from System.ComponentModel import INotifyPropertyChanged, PropertyChangedEventArgs
from System.Windows import (Thickness, Visibility, TextDecorations, UIElement,
                            RoutedEventHandler, FontWeights)
from System.Windows.Controls import CheckBox as _CheckBox
from System.Windows.Input import MouseButtonEventHandler
from System.Windows.Media import SolidColorBrush, ColorConverter
from System.Windows.Threading import DispatcherTimer

from slantisui import ui
import usage


def _brush(hexstr):
    return SolidColorBrush(ColorConverter.ConvertFromString(hexstr))


# Read from ui, never hardcoded: ui._theme() only rewrites the hexes it finds
# INSIDE the window XAML string, and a brush built here in Python never passes
# through it -- so a literal "#202022" would stay light on a dark host while the
# rest of the row followed the theme. These constants are already resolved.
TXT = _brush(ui.TEXT)
DIM = _brush(ui.TEXT_MUTED)
BAD = _brush(ui.STATUS_BAD)

VIS = Visibility.Visible
GONE = Visibility.Collapsed

INDENT_CAT = Thickness(0)
INDENT_FAM = Thickness(22, 0, 0, 0)
INDENT_TYPE = Thickness(44, 0, 0, 0)

CHEV_OPEN = u"▾"
CHEV_SHUT = u"▸"


def _id_val(eid):
    # ElementId value compat Revit 2022-2026 (IntegerValue removed in 2026)
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def _why(exc):
    """An exception's message as unicode. unicode(exc) itself raises when a plain
    Python exception carries a non-ASCII byte string (a name with an N-tilde or an
    O-slash in it), and it did so from inside an except block, taking the whole
    run down. repr() is ASCII-safe, so it is the fallback."""
    try:
        return unicode(exc)
    except Exception:
        try:
            return u"{}".format(repr(exc))
        except Exception:
            return u"unreadable error"


def _key(name):
    """The form a name is COMPARED in. Revit treats "W1" and "w1" as the same
    name inside a scope, so every clash check, claim and live lookup goes through
    this and never compares raw strings."""
    return name.lower()


def _elem_name(el):
    """ElementType subclasses (FamilySymbol among them) hide Element.Name from
    IronPython and a plain el.Name raises AttributeError -- the silent killer.
    Family is a plain Element and answers either way, so going through the
    descriptor covers both."""
    try:
        return DB.Element.Name.__get__(el)
    except Exception:
        return el.Name


def _set_elem_name(el, new_name):
    """Writing is NOT the broken half. FamilySymbol declares Name write-only --
    the setter is exactly what shadows the readable one it inherits from Element
    (measured) -- so the plain assignment is the right path for both Family and
    FamilySymbol, and it is the only one with precedent here: no other tool
    writes SYMBOL_NAME_PARAM. The parameter stays as a fallback for the case
    where the setter is unavailable."""
    if el is None:
        # Unreachable today: category rows are the only ones without an element
        # and they are kept out of all_rows. Here so that if that ever changes,
        # the run reports a FAILED row instead of throwing out of the loop.
        raise Exception("this row has no element to rename")
    try:
        el.Name = new_name
        return
    except Exception as first:
        # Keep what Revit actually said. Reporting "not writable" for every
        # failure sent people looking for a permissions problem when the real
        # reason was, say, a character the name cannot carry.
        reason = _why(first)
    param = el.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)
    if param is None or param.IsReadOnly:
        raise Exception(reason)
    try:
        param.Set(new_name)
    except Exception:
        raise Exception(reason)


# What Revit refuses in a family or type name. The API raises a generic error
# for these, so the preview has to catch them first, with the real reason.
FORBIDDEN_CHARS = u"\\:{}[]|;<>?`~"
_NAME_CHECK = {}


def name_problem(name):
    """Why Revit would refuse `name`, or u"" when it is fine."""
    if name in _NAME_CHECK:
        return _NAME_CHECK[name]
    bad = []
    for ch in name:
        if ch in FORBIDDEN_CHARS and ch not in bad:
            bad.append(ch)
    if bad:
        reason = u"Revit doesn't accept {} in names".format(
            u", ".join(u"'{}'".format(c) for c in bad))
    else:
        reason = u""
        try:
            # NamingUtils.IsValidName is not in every Revit version: when it is
            # missing (or errors) the list above is all we have.
            if not DB.NamingUtils.IsValidName(name):
                reason = u"Revit doesn't accept this name"
        except Exception:
            pass
    _NAME_CHECK[name] = reason
    return reason


doc = revit.doc

# --------------------------------------------------------------------------
# Acronyms: the dictionary lives OUTSIDE the bundle on purpose. Keeping it in
# the .pushbutton folder would mean a git pull of the extension silently wipes
# whatever the user added.
# Only Title Case reads it; UPPERCASE and "leave as is" never touch it.
# --------------------------------------------------------------------------
ACRONYM_PATH = os.path.join(os.getenv("APPDATA") or "", "MagicTools",
                            "rename_acronyms.json")

DEFAULT_ACRONYMS = [
    "AC", "ACT", "ADA", "AHU", "AV", "CMU", "CW", "DN", "EQ", "FEC", "FF&E",
    "GWB", "HM", "HVAC", "MEP", "OSB", "RCP", "TV", "UL", "VCT", "WC",
]


def load_acronyms():
    """Read the user's acronym list, seeding it with the defaults on first run.

    Never fatal: an unreadable or malformed file falls back to the defaults, so
    a bad edit degrades the Title Case instead of killing the tool.
    """
    try:
        if os.path.exists(ACRONYM_PATH):
            handle = open(ACRONYM_PATH, "r")
            try:
                raw = handle.read()
            finally:
                handle.close()
            if raw.startswith("\xef\xbb\xbf"):      # tolerate a UTF-8 BOM
                raw = raw[3:]
            data = json.loads(raw)
            words = data.get("acronyms", []) if isinstance(data, dict) else data
            found = set()
            for w in words:
                if w:
                    found.add(u"{}".format(w).upper())
            if found:
                return found
    except Exception:
        pass

    try:
        folder = os.path.dirname(ACRONYM_PATH)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        handle = open(ACRONYM_PATH, "w")
        try:
            handle.write(json.dumps({"acronyms": DEFAULT_ACRONYMS}, indent=2))
        finally:
            handle.close()
    except Exception:
        pass
    return set(DEFAULT_ACRONYMS)


# --------------------------------------------------------------------------
# The renaming rule: drop the tag, then (optionally) re-case what is left.
#
# The tag is taken LITERALLY and every occurrence goes, wherever it sits --
# that is the whole point: an office code shows up at the front of one family
# and in the middle of the next. What it deliberately does NOT do is tidy up
# the separator left behind: removing "HMC" from "HMC - Door" leaves " - Door",
# and the live preview is there so you widen the tag to "HMC - " and watch it
# fix itself. Guessing which dash was structural and which was glue is exactly
# the kind of cleverness that would rename a thousand families wrong.
# --------------------------------------------------------------------------
# Where one word ends and the next begins. The separators are kept (the capture
# group), so a name is rebuilt exactly as it came. Parentheses, slashes, dots,
# commas and dashes split too: that is what lets "(ADA)" and "W/" be judged as
# the words ADA and W, instead of as one glued-up token.
WORD_SPLIT = re.compile(u"([-_ ()/.,]+)")

# Short words Title Case leaves lower unless they open the name. "w/" arrives as
# the word "w" and a slash.
CONNECTORS = set([u"and", u"or", u"to", u"of", u"w"])

# Always kept uppercase by Title Case, on top of the user's own list in
# rename_acronyms.json (which an existing install seeded with the first 21 only,
# so these must not depend on that file being re-seeded).
BUILTIN_ACRONYMS = set([
    u"DWV", u"EMT", u"HMC", u"HSS", u"LED", u"MLO", u"NEMA", u"PVC", u"VAV",
])


def strip_tag(name, tag):
    if not tag:
        return name
    # Case-insensitive on purpose: typing "hmc" has to find "HMC". re.escape
    # keeps a tag carrying regex punctuation ("FF&E", "Rev.1") literal. Compiled
    # rather than re.sub(flags=...), which no other tool uses on IronPython
    # 2.7 -- no reason to be the first to find out.
    return re.compile(re.escape(tag), re.IGNORECASE).sub(u"", name)


def title_word(word, acronyms, first=False, shouting=False):
    """One word of the name, in the order the rules weigh it:

      1. a listed acronym is uppercased (ADA, MEP, PVC...);
      2. anything with a digit is a code (17A, 208V, 480, 2x4) and stays as typed;
      3. a short connector (and, or, to, of, w) goes lower, unless it opens the name;
      4. a word that is ALREADY uppercase, 2 to 5 letters, is read as an acronym
         the user wrote on purpose (LED, VAV) and stays -- except when the whole
         name is uppercase, where every word looks like that and none can be told
         apart: then they all go through the normal case below;
      5. everything else: first letter up, the rest down.
    """
    if not word:
        return word
    if word.upper() in acronyms:
        return word.upper()
    for ch in word:
        if ch.isdigit():
            return word
    if word.lower() in CONNECTORS and not first:
        return word.lower()
    if not shouting and word.isupper() and 2 <= len(word) <= 5:
        return word
    return word[0].upper() + word[1:].lower()


def title_case(text, acronyms):
    """Title Case a name, keeping its separators exactly where they were."""
    letters = text.lower() != text.upper()
    shouting = letters and text == text.upper()
    out = []
    first = True
    for piece in WORD_SPLIT.split(text):
        if not piece or WORD_SPLIT.match(piece):
            out.append(piece)                  # empty or a separator run: as is
        else:
            out.append(title_word(piece, acronyms, first, shouting))
            first = False
    return u"".join(out)


def drop_prefix(name, chars):
    """Cut a fixed number of characters off the front. Blunt by design: it is
    for the prefixes no pattern describes, where you just count and cut."""
    if chars <= 0:
        return name
    return name[chars:]


def drop_suffix(name, chars):
    """The mirror of drop_prefix, off the end."""
    if chars <= 0:
        return name
    if chars >= len(name):
        return u""
    return name[:-chars]


def swap_tag(name, find, repl):
    """strip_tag's sibling: same case-insensitive, regex-safe match, but the
    replacement goes in as a literal. It is handed over as a function so that a
    backreference someone types in the with box (a backslash followed by a
    digit, say) stays text instead of being read as one."""
    if not find:
        return name
    return re.compile(re.escape(find), re.IGNORECASE).sub(lambda m: repl, name)


class Rules(object):
    """What the window is asking for, read once per pass and handed down.

    The three blocks of the window map here: find (remove / replace), the edges
    (add prefix, add suffix, trim start, trim end) and case.
    """

    def __init__(self, remove, find, repl, prefix, suffix,
                 cut_start, cut_end, case):
        self.remove = remove
        self.find = find
        self.repl = repl
        self.prefix = prefix
        self.suffix = suffix
        self.cut_start = cut_start
        self.cut_end = cut_end
        self.case = case

    @property
    def empty(self):
        return not (self.remove or self.find or self.prefix or self.suffix
                    or self.cut_start or self.cut_end or self.case != "none")


def apply_rule(name, r, acronyms):
    """Run every rule the window has set, and report BOTH the resulting name and
    what actually happened to this particular name.

    The order is a decision, not an accident:
      * the two trims run FIRST, so the sliders count against the name you are
        reading in the left column and not some intermediate nobody ever sees;
      * the case change runs BEFORE the replacement, the prefix and the suffix,
        which are the three pieces of text the user types: a prefix entered as
        "KDA-" must not come back as "Kda-", and neither must a replacement
        entered as "kW".

    Each layer is recorded only when it MOVED this name, which is what lets the
    right-hand column tell a row the rule reached from one it did not.
    """
    out = name
    done = []

    if r.cut_start:
        step = drop_prefix(out, r.cut_start)
        if step != out:
            out = step
            done.append(u"cut {} from the start".format(r.cut_start))

    if r.cut_end:
        step = drop_suffix(out, r.cut_end)
        if step != out:
            out = step
            done.append(u"cut {} from the end".format(r.cut_end))

    if r.remove:
        step = strip_tag(out, r.remove)
        if step != out:
            out = step
            done.append(u'"{}" removed'.format(r.remove))

    if r.case == "title":
        step = title_case(out, acronyms)
        if step != out:
            out = step
            done.append(u"Title Case")
    elif r.case == "upper":
        step = out.upper()
        if step != out:
            out = step
            done.append(u"UPPERCASE")

    # Everything below goes in AFTER the case change, on purpose: these three
    # are text the user typed, and they enter exactly as typed. Running the
    # replacement before the case change would hand back "Kw" to someone who
    # asked for "kW", the same way it would turn a "KDA-" prefix into "Kda-".
    if r.find:
        step = swap_tag(out, r.find, r.repl)
        if step != out:
            out = step
            done.append(u'"{}" replaced with "{}"'.format(r.find, r.repl))
    if r.prefix:
        out = r.prefix + out
        done.append(u'prefix "{}"'.format(r.prefix))
    if r.suffix:
        out = out + r.suffix
        done.append(u'suffix "{}"'.format(r.suffix))

    # A name that starts or ends with a space is never what anyone meant (it is
    # what a trim or a removed tag leaves behind), so the edges are cleaned. Only
    # when a rule actually moved this name: an untouched name is left exactly as it is.
    if done:
        step = out.strip()
        if step != out:
            out = step
            done.append(u"edge spaces trimmed")

    return out, done


def resolve_clashes(rows, fixed_names, pre_blocked):
    """Decide which rows the clash check has to block, to a fixed point.

    A row that is blocked does NOT move: it keeps its CURRENT name, and that name
    is still occupied. So blocking one row can free no name at all and can put a
    second row in conflict with the first (W1 -> W is blocked, W1 stays W1, and
    W12 -> W1 now lands on a name that is taken). One pass cannot see that, it
    produced two types called "W1" in the same family. This loops until no new
    row gets blocked; it terminates because the blocked set only ever grows.

    `pre_blocked` is {row: reason} for rows that are stuck on their own (empty or
    invalid name). Returns {row: reason} for every blocked row, those included.
    A row is a "mover" while it is ticked, changed and not blocked; everything
    else sits on its current name. Names are compared case-insensitively, like
    Revit does; a row that only changes its own capitals (ab -> AB) has no rival,
    because it never claims the old spelling and the new one is only its own.
    """
    blocked = dict(pre_blocked)
    while True:
        claims = {}
        for row in rows:
            moving = row.Changed and row.picked and row not in blocked
            final = row.New if moving else row.Current
            claims.setdefault(row.scope, {}).setdefault(_key(final), []).append(
                (row, moving))
        for scope, names in fixed_names.items():
            for name in names:
                claims.setdefault(scope, {}).setdefault(_key(name), []).append(
                    (None, False))

        grew = False
        for row in rows:
            if row in blocked or not (row.Changed and row.picked):
                continue
            rivals = claims[row.scope][_key(row.New)]
            if len(rivals) < 2:
                continue
            if any(r is None for r, _mv in rivals):
                reason = u"name already in use"
            elif any(not mv for _r, mv in rivals):
                # Someone is sitting on that name and is not leaving it.
                reason = u'"{}" is still in use'.format(row.New)
            else:
                reason = u"{} rows want this name".format(len(rivals))
            blocked[row] = reason
            grew = True
        if not grew:
            return blocked


# --------------------------------------------------------------------------
# Row model. One per family and one per type; INotifyPropertyChanged is what
# lets the preview redraw on every keystroke WITHOUT rebinding the grid, so the
# scroll position and the ticks survive while you refine the tag.
# --------------------------------------------------------------------------

# Whether the Types half is on. It lives out here because the family row's
# chevron has to disappear with it: leaving a chevron on a family whose types
# are not being listed offers an expand that opens nothing.
TYPES_ON = {"v": True}

# Set while a bulk tick is running. Answering `Checked` means walking the whole
# subtree, so one row at a time -- each notifying its ancestors, each ancestor
# re-walking its branch -- turns a Select all into quadratic work on a document
# with thousands of types. The bulk goes quiet and repaints once at the end.
_QUIET = {"on": False}


class Row(INotifyPropertyChanged):
    """`scope` is where this name must be unique: families compete
    document-wide, types only inside their own family."""

    def __init__(self, kind, scope, category, current, element, parent=None):
        self._kind = kind
        self._scope = scope
        self._category = category
        self._current = current
        self._element = element
        self._parent = parent
        self._children = []
        self._new = current
        self._checked = True
        self._expanded = False
        self._status = u""
        self._effect = u""       # what the rules did to THIS name, in words
        self._rollup = u""       # and what they are doing to everything under it
        self._blocked = u""      # ...and how much of that is stuck on a clash
        self._active = True      # is its half (Families / Types) ticked on?
        self._parked = False     # sitting on a temp name mid-transaction
        self._held = current     # the name this element holds in the live doc
        self.annotation = False  # set on category rows: tags, symbols, details
        self._pc_handlers = []

    # INotifyPropertyChanged plumbing
    def add_PropertyChanged(self, value):
        self._pc_handlers.append(value)

    def remove_PropertyChanged(self, value):
        if value in self._pc_handlers:
            self._pc_handlers.remove(value)

    def _raise(self, *props):
        if _QUIET["on"]:
            return
        for p in props:
            args = PropertyChangedEventArgs(p)
            for h in list(self._pc_handlers):
                h(self, args)

    # -- plain data -------------------------------------------------------
    @property
    def kind(self):
        return self._kind

    @property
    def scope(self):
        return self._scope

    @property
    def element(self):
        return self._element

    @property
    def children(self):
        return self._children

    @property
    def parent(self):
        return self._parent

    @property
    def is_family(self):
        return self._kind == u"Family"

    @property
    def is_category(self):
        """The root level of the tree. It is not an element: it owns no name to
        rewrite, it is never collected into all_rows, and it exists so a whole
        category can be ticked, untocked and folded away in one gesture."""
        return self._kind == u"Category"

    @property
    def parked(self):
        return self._parked

    @property
    def active(self):
        """Its half of the document (Families / Types) is ticked. An inactive row
        keeps its current name, so it is never counted and never written."""
        return self._active

    # -- bound to the grid ------------------------------------------------
    @property
    def Current(self):
        return self._current

    @property
    def New(self):
        return self._new

    @property
    def Category(self):
        return self._category

    @property
    def Status(self):
        return self._status

    @property
    def Effect(self):
        """Right of the divider: the rules that reached this name, spelled out.
        With seven of them stacked, "what is this one doing to that row" stops
        being answerable by reading the two names and guessing."""
        return self._effect

    @property
    def EffectVis(self):
        return VIS if self._effect else GONE

    @property
    def Rollup(self):
        """What the rules are doing INSIDE this node. A folded category showing
        nothing left you blind until you opened every branch to find out whether
        the run was going to touch anything in there."""
        return self._rollup

    @property
    def RollupVis(self):
        return VIS if self._rollup else GONE

    @property
    def Blocked(self):
        """The same count for what is NOT going to happen underneath. It gets
        its own field instead of borrowing Status: a family's Status is its own
        name clash, and that one cannot be overwritten by news about its
        types."""
        return self._blocked

    @property
    def BlockedVis(self):
        return VIS if self._blocked else GONE

    @property
    def Indent(self):
        if self.is_category:
            return INDENT_CAT
        return INDENT_FAM if self.is_family else INDENT_TYPE

    @property
    def Weight(self):
        return FontWeights.Medium if self.is_category else FontWeights.Normal

    @property
    def Changed(self):
        return self._active and self._new != self._current

    @property
    def NewVis(self):
        return VIS if self.Changed else GONE

    @property
    def StatusVis(self):
        return VIS if self._status else GONE

    @property
    def CurrentBrush(self):
        return DIM if self.Changed else TXT

    @property
    def Strike(self):
        return TextDecorations.Strikethrough if self.Changed else None

    @property
    def NewBrush(self):
        return BAD if self._status else TXT

    @property
    def TickVis(self):
        # A category always shows its box: it is the handle for the whole group,
        # and it is not itself subject to the Families / Types halves. So does a
        # family: with Families switched off it is no longer renamed, but its box
        # is still the handle for the types under it (leaving one family out of
        # a types-only run), shown as a dash when only some are ticked.
        return VIS if (self.is_category or self.is_family or self._active) else GONE

    @property
    def Chevron(self):
        if not self._children:
            return u""
        if self.is_family and not TYPES_ON["v"]:
            return u""
        return CHEV_OPEN if self._expanded else CHEV_SHUT

    @property
    def picked(self):
        """This row's OWN tick, which is the only thing the run writes. Distinct
        from `Checked` below, which is what the box paints: a family whose types
        are half ticked shows a dash, but the family itself is either being
        renamed or it is not, and that answer lives here."""
        return self._checked

    def _parts(self):
        """Every own-tick at or under this row that the user can actually see.
        A row whose half is switched off (Types unticked, say) hides its box, so
        counting it would make a parent show a dash nobody can resolve."""
        parts = []
        if not self.is_category and self._active:
            parts.append(self._checked)
        for kid in self._children:
            parts.extend(kid._parts())
        return parts

    @property
    def Checked(self):
        """Bound to the box: True (all of it), False (none of it) or None, which
        WPF paints as the dash for a partial selection."""
        parts = self._parts()
        if not parts:
            return self._checked
        first = parts[0]
        for p in parts:
            if p != first:
                return None
        return first

    @Checked.setter
    def Checked(self, value):
        # A click on a dash resolves to True: three states to READ, two to set,
        # which is why the boxes are left as IsThreeState="False" -- nobody
        # should be able to click their way INTO a partial selection.
        self._set_checked(bool(value))
        parent = self._parent
        while parent is not None:
            parent._raise("Checked")
            parent = parent._parent

    def _set_checked(self, value):
        """Ticking a node ticks everything under it: the node IS the toggle, and
        a parent disagreeing with its children is the confusion the tree was
        built to remove. The notification goes out even when this row's own tick
        did not move, because its aggregate may have (all -> some, say)."""
        self._checked = value
        for kid in self._children:
            kid._set_checked(value)
        self._raise("Checked")

    # -- recompute --------------------------------------------------------
    def set_new(self, new_name, active, effect=u""):
        """True when something actually moved, so the caller only repaints the
        rows that changed instead of all of them."""
        if (new_name == self._new and active == self._active
                and effect == self._effect):
            return False
        self._new = new_name
        self._active = active
        self._effect = effect
        return True

    def set_status(self, text):
        if text == self._status:
            return False
        self._status = text
        return True

    def set_rollup(self, text, blocked):
        if text == self._rollup and blocked == self._blocked:
            return False
        self._rollup = text
        self._blocked = blocked
        return True

    def redraw(self):
        self._raise("New", "Changed", "NewVis", "CurrentBrush", "Strike",
                    "NewBrush", "Status", "StatusVis", "TickVis", "Effect",
                    "EffectVis", "Rollup", "RollupVis", "Blocked",
                    "BlockedVis")

    @property
    def expanded(self):
        return self._expanded

    def toggle(self):
        if not self._children:
            return
        self._expanded = not self._expanded
        self._raise("Chevron")


def _renameable(fam):
    """In-place families belong to the model, not the library, and non
    user-created ones are Revit's own. Neither is ours to rename in bulk."""
    try:
        if fam.IsInPlace:
            return False
        if fam.IsUserCreated is False:
            return False
    except Exception:
        pass
    return True


def collect():
    """Walk the document ONCE. Everything after this is string work in memory,
    which is what makes a live preview affordable."""
    fam_rows = []
    # Names held by something this run will never rename (in-place families and
    # Revit's own). They still occupy their name in the document, so a proposed
    # name landing on one of them is a real clash.
    fixed = {}
    # Annotation is a property of the CATEGORY, not of the family, so one set of
    # names answers it for the whole tree.
    anno = set()

    families = DB.FilteredElementCollector(doc).OfClass(DB.Family).ToElements()
    for fam in families:
        try:
            fam_name = _elem_name(fam)
        except Exception:
            continue
        if not fam_name:
            continue

        if not _renameable(fam):
            fixed.setdefault(u"@doc", set()).add(_key(fam_name))
            continue

        try:
            category = fam.FamilyCategory.Name if fam.FamilyCategory else u"No Category"
        except Exception:
            category = u"No Category"
        try:
            if (fam.FamilyCategory is not None
                    and fam.FamilyCategory.CategoryType == DB.CategoryType.Annotation):
                anno.add(category)
        except Exception:
            pass  # unreadable: it stays on the model side, where it is visible

        frow = Row(u"Family", u"@doc", category, fam_name, fam)
        fam_rows.append(frow)

        fam_scope = u"fam:{}".format(_id_val(fam.Id))
        try:
            symbol_ids = fam.GetFamilySymbolIds()
        except Exception:
            symbol_ids = []
        for sid in symbol_ids:
            symbol = doc.GetElement(sid)
            if symbol is None:
                continue
            try:
                sym_name = _elem_name(symbol)
            except Exception:
                continue
            if not sym_name:
                continue
            frow.children.append(
                Row(u"Type", fam_scope, category, sym_name, symbol, frow))

    return fam_rows, fixed, anno


def live_names(rows):
    """({scope: {lower-cased name: element id}}, {scopes it could not read}), as
    the document holds the names RIGHT NOW, read fresh for the scopes this run is
    about to write to.

    The names on the rows were read when the window opened. The write loop uses
    this as a last line of defence: whatever the preview said, an element is
    never given a name that another element of the same family (or, for a
    family, of the document) already holds. Revit's UI forbids that duplicate
    but the API accepted it, which is how a family ended up with two "W1".

    A scope that cannot be read completely is handed back, NOT skipped: the rows
    in it are not written, because writing there would be doing it without the net.
    """
    scopes = set(r.scope for r in rows)
    live = {}
    unreadable = set()
    if u"@doc" in scopes:
        names = live.setdefault(u"@doc", {})
        try:
            families = list(
                DB.FilteredElementCollector(doc).OfClass(DB.Family).ToElements())
        except Exception:
            families = []
            unreadable.add(u"@doc")
        for fam in families:
            try:
                names[_key(_elem_name(fam))] = _id_val(fam.Id)
            except Exception:
                unreadable.add(u"@doc")
    done = set()
    for r in rows:
        if r.scope == u"@doc" or r.scope in done or r.parent is None:
            continue
        done.add(r.scope)
        names = live.setdefault(r.scope, {})
        try:
            symbol_ids = r.parent.element.GetFamilySymbolIds()
        except Exception:
            unreadable.add(r.scope)
            continue
        for sid in symbol_ids:
            try:
                names[_key(_elem_name(doc.GetElement(sid)))] = _id_val(sid)
            except Exception:
                unreadable.add(r.scope)
    return live, unreadable


def _live_move(live, row, new_name):
    """Record that this row's element now holds `new_name`."""
    names = live.setdefault(row.scope, {})
    eid = _id_val(row.element.Id)
    if names.get(_key(row._held)) == eid:
        del names[_key(row._held)]
    names[_key(new_name)] = eid
    row._held = new_name


with usage.tool_run(__file__) as run:
    fam_rows, fixed_names, anno_cats = collect()

    if not fam_rows:
        ui.alert("No loadable families found in this document.",
                 title="Rename Families", context=doc.Title)
        script.exit()

    acronyms = load_acronyms() | BUILTIN_ACRONYMS
    all_rows = []
    for f in fam_rows:
        all_rows.append(f)
        all_rows.extend(f.children)

    total_fams = len(fam_rows)
    total_types = len(all_rows) - total_fams

    # The root level of the tree. Categories are NOT part of all_rows on purpose:
    # nothing about them is ever renamed, and keeping them out of that list is what
    # guarantees the write loop can never reach a row with no element behind it.
    _by_cat = {}
    for f in fam_rows:
        _by_cat.setdefault(f.Category, []).append(f)

    cat_rows = []
    for _name in sorted(_by_cat):
        _fams = _by_cat[_name]
        _crow = Row(u"Category", u"@cat", u"",
                    u"{} ({})".format(_name, len(_fams)), None)
        _crow.annotation = _name in anno_cats
        for _f in _fams:
            _f._parent = _crow
            _crow.children.append(_f)
        cat_rows.append(_crow)

    total_anno_fams = sum(len(c.children) for c in cat_rows if c.annotation)
    total_model_fams = total_fams - total_anno_fams

    # --------------------------------------------------------------------------
    # The window. A DataGrid, not a TreeView: it is the one already styled by
    # slantisui, it virtualises (which matters at a few thousand types) and the
    # expand/collapse is ours to drive anyway, since the family row is the toggle.
    # --------------------------------------------------------------------------
    _BODY = """
  <Grid>
    <Grid.ColumnDefinitions>
      <ColumnDefinition Width="330"/>
      <ColumnDefinition Width="*"/>
    </Grid.ColumnDefinitions>

    <!-- The rules live in their own column so the tree can have the whole
         height of the window. Stacked under the header they left the list flat,
         and the list is the part you actually read. -->
    <ScrollViewer Grid.Column="0" VerticalScrollBarVisibility="Auto"
                  HorizontalScrollBarVisibility="Disabled" Margin="0,0,18,0">
      <StackPanel>

        <TextBlock Text="Find" Style="{StaticResource SectionHead}"/>
        <Grid Margin="0,2,0,0">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="84"/>
            <ColumnDefinition Width="*"/>
          </Grid.ColumnDefinitions>
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>

          <TextBlock Grid.Row="0" Grid.Column="0" Text="Remove text" FontSize="13"
                     Foreground="#202022" VerticalAlignment="Center"/>
          <TextBox x:Name="txtTag" Grid.Row="0" Grid.Column="1" Height="30"
                   ToolTip="Every occurrence of this text is removed, wherever it sits in the name. Upper and lower case both match."/>

          <TextBlock Grid.Row="1" Grid.Column="0" Text="Replace" FontSize="13"
                     Foreground="#202022" VerticalAlignment="Center" Margin="0,8,0,0"/>
          <TextBox x:Name="txtFind" Grid.Row="1" Grid.Column="1" Height="30" Margin="0,8,0,0"
                   ToolTip="Text to look for, anywhere in the name. Upper and lower case both match."/>

          <TextBlock Grid.Row="2" Grid.Column="0" Text="with" FontSize="13"
                     Foreground="#77736C" VerticalAlignment="Center" Margin="0,8,0,0"/>
          <TextBox x:Name="txtRepl" Grid.Row="2" Grid.Column="1" Height="30" Margin="0,8,0,0"
                   ToolTip="What goes in its place. Leave it empty and every occurrence is simply dropped. It goes in AFTER the case change, so it is kept exactly as you type it."/>
        </Grid>

        <TextBlock Text="Prefix and suffix" Style="{StaticResource SectionHead}" Margin="0,14,0,0"/>
        <Grid Margin="0,2,0,0">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="84"/>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="Auto"/>
          </Grid.ColumnDefinitions>
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>

          <TextBlock Grid.Row="0" Grid.Column="0" Text="Add prefix" FontSize="13"
                     Foreground="#202022" VerticalAlignment="Center"/>
          <TextBox x:Name="txtPre" Grid.Row="0" Grid.Column="1" Grid.ColumnSpan="2" Height="30"
                   ToolTip="Put in front of the name. It goes in AFTER the case change, so what you type here is kept exactly as you type it."/>

          <TextBlock Grid.Row="1" Grid.Column="0" Text="Add suffix" FontSize="13"
                     Foreground="#202022" VerticalAlignment="Center" Margin="0,8,0,0"/>
          <TextBox x:Name="txtSuf" Grid.Row="1" Grid.Column="1" Grid.ColumnSpan="2" Height="30"
                   Margin="0,8,0,0"
                   ToolTip="Put at the end of the name, after the case change, exactly as you type it."/>

          <TextBlock Grid.Row="2" Grid.Column="0" Text="Trim start" FontSize="13"
                     Foreground="#202022" VerticalAlignment="Center" Margin="0,12,0,0"/>
          <Slider x:Name="sldStart" Grid.Row="2" Grid.Column="1" Minimum="0" Maximum="40"
                  Value="0" TickFrequency="1" IsSnapToTickEnabled="True"
                  VerticalAlignment="Center" Margin="0,12,0,0"
                  ToolTip="Characters cut off the FRONT of the name, counted on the name as you see it in the list. For the prefixes no pattern describes: you count and you cut."/>
          <TextBlock x:Name="lblStart" Grid.Row="2" Grid.Column="2" FontSize="12"
                     Foreground="#77736C" VerticalAlignment="Center"
                     Margin="10,12,0,0" MinWidth="26" TextAlignment="Right"/>

          <TextBlock Grid.Row="3" Grid.Column="0" Text="Trim end" FontSize="13"
                     Foreground="#202022" VerticalAlignment="Center" Margin="0,8,0,0"/>
          <Slider x:Name="sldEnd" Grid.Row="3" Grid.Column="1" Minimum="0" Maximum="40"
                  Value="0" TickFrequency="1" IsSnapToTickEnabled="True"
                  VerticalAlignment="Center" Margin="0,8,0,0"
                  ToolTip="Characters cut off the END of the name, counted on the name as you see it in the list."/>
          <TextBlock x:Name="lblEnd" Grid.Row="3" Grid.Column="2" FontSize="12"
                     Foreground="#77736C" VerticalAlignment="Center"
                     Margin="10,8,0,0" MinWidth="26" TextAlignment="Right"/>
        </Grid>

        <TextBlock Text="Case" Style="{StaticResource SectionHead}" Margin="0,14,0,0"/>
        <StackPanel Margin="0,4,0,0">
          <RadioButton x:Name="rbNone"  Content="Leave as is" GroupName="cs" IsChecked="True"/>
          <RadioButton x:Name="rbTitle" Content="Title Case"  GroupName="cs" Margin="0,7,0,0"/>
          <RadioButton x:Name="rbUpper" Content="UPPERCASE"   GroupName="cs" Margin="0,7,0,0"/>
        </StackPanel>

      </StackPanel>
    </ScrollViewer>

    <Grid Grid.Column="1">
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="*"/>
      </Grid.RowDefinitions>

      <Grid Grid.Row="0" Margin="0,0,0,10">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <TextBox x:Name="txtFilter" Grid.Column="0" Height="30"
                 ToolTip="Type to narrow the list down. This only changes WHAT YOU SEE -- rows it hides keep their tick and are still renamed, and the footer says how many. To leave something out of the run, untick it or untick its category."/>
        <CheckBox x:Name="chkModel" Grid.Column="1" IsChecked="True"
                  VerticalAlignment="Center" Margin="18,0,0,0"
                  ToolTip="The families whose category is a MODEL one: doors, windows, casework, everything that exists in the building. Unticking this UNTICKS them as well as hiding them, so they are left out of the run."/>
        <CheckBox x:Name="chkAnno" Grid.Column="2" IsChecked="True"
                  VerticalAlignment="Center" Margin="16,0,0,0"
                  ToolTip="The families whose category is an ANNOTATION one: tags, symbols, detail items, title blocks. Unticking this UNTICKS them as well as hiding them, so they are left out of the run. Ticking it back ticks the whole half again, the same way ticking a category does."/>
      </Grid>

      <Border Grid.Row="1" CornerRadius="8" Background="#FFFFFF"
              BorderBrush="#ECE9E4" BorderThickness="1" ClipToBounds="True">
        <DataGrid x:Name="grid" AutoGenerateColumns="False" HeadersVisibility="None"
                  SelectionMode="Single" CanUserAddRows="False" CanUserDeleteRows="False"
                  CanUserSortColumns="False" CanUserResizeRows="False"
                  CanUserResizeColumns="False">
          <DataGrid.Columns>
            <DataGridTemplateColumn Width="*">
              <DataGridTemplateColumn.CellTemplate>
                <DataTemplate>
                  <Grid Margin="{Binding Indent}">
                    <Grid.ColumnDefinitions>
                      <ColumnDefinition Width="30"/>
                      <ColumnDefinition Width="18"/>
                      <ColumnDefinition Width="*"/>
                    </Grid.ColumnDefinitions>
                    <CheckBox Grid.Column="0" VerticalAlignment="Top" Margin="0,2,0,0"
                              Visibility="{Binding TickVis}"
                              IsChecked="{Binding Checked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"/>
                    <TextBlock Grid.Column="1" Tag="chev" Text="{Binding Chevron}"
                               FontSize="11" Foreground="#77736C" Cursor="Hand"
                               VerticalAlignment="Top" Margin="0,3,0,0"/>
                    <StackPanel Grid.Column="2" Margin="2,0,8,0">
                      <TextBlock Text="{Binding Current}" FontSize="12.5" TextWrapping="Wrap"
                                 Foreground="{Binding CurrentBrush}"
                                 FontWeight="{Binding Weight}"
                                 TextDecorations="{Binding Strike}"/>
                      <TextBlock Text="{Binding New}" FontSize="13" TextWrapping="Wrap"
                                 Foreground="{Binding NewBrush}" Visibility="{Binding NewVis}"/>
                    </StackPanel>
                  </Grid>
                </DataTemplate>
              </DataGridTemplateColumn.CellTemplate>
            </DataGridTemplateColumn>
            <DataGridTemplateColumn Width="230">
              <DataGridTemplateColumn.CellTemplate>
                <DataTemplate>
                  <Border BorderBrush="#ECE9E4" BorderThickness="1,0,0,0"
                          Padding="10,1,0,0">
                    <StackPanel>
                      <TextBlock Text="{Binding Effect}" FontSize="11" Foreground="#A6A199"
                                 Visibility="{Binding EffectVis}" TextWrapping="Wrap"/>
                      <TextBlock Text="{Binding Status}" FontSize="11" Foreground="#C0392B"
                                 Visibility="{Binding StatusVis}" TextWrapping="Wrap"/>
                      <TextBlock Text="{Binding Rollup}" FontSize="11" Foreground="#77736C"
                                 Visibility="{Binding RollupVis}" TextWrapping="Wrap"/>
                      <TextBlock Text="{Binding Blocked}" FontSize="11" Foreground="#C0392B"
                                 Visibility="{Binding BlockedVis}" TextWrapping="Wrap"/>
                    </StackPanel>
                  </Border>
                </DataTemplate>
              </DataGridTemplateColumn.CellTemplate>
            </DataGridTemplateColumn>
          </DataGrid.Columns>
        </DataGrid>
      </Border>
    </Grid>
  </Grid>
"""

    _FOOTER = """
  <Grid>
    <Grid.ColumnDefinitions>
      <ColumnDefinition Width="*"/>
      <ColumnDefinition Width="Auto"/>
    </Grid.ColumnDefinitions>

    <StackPanel Grid.Column="0" VerticalAlignment="Center" Margin="0,0,16,0">
      <StackPanel Orientation="Horizontal">
        <CheckBox x:Name="chkFam" Content="Families" IsChecked="True"/>
        <TextBlock x:Name="lblFam" FontSize="12" Foreground="#77736C"
                   VerticalAlignment="Center" Margin="7,0,0,0"/>
        <CheckBox x:Name="chkTyp" Content="Types" IsChecked="True" Margin="26,0,0,0"/>
        <TextBlock x:Name="lblTyp" FontSize="12" Foreground="#77736C"
                   VerticalAlignment="Center" Margin="7,0,0,0"/>
      </StackPanel>
      <Grid Margin="0,5,0,0">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="Auto"/>
          <ColumnDefinition Width="*"/>
        </Grid.ColumnDefinitions>
        <TextBlock x:Name="lblCount" Grid.Column="0" Foreground="#A6A199" FontSize="11.5"
                   TextTrimming="CharacterEllipsis"/>
        <TextBlock x:Name="lblWarn" Grid.Column="1" Foreground="#C46A00" FontSize="11.5"
                   Margin="14,0,0,0" TextTrimming="CharacterEllipsis"/>
      </Grid>
    </StackPanel>

    <StackPanel Grid.Column="1" HorizontalAlignment="Right" Orientation="Horizontal"
                VerticalAlignment="Center">
      <Button x:Name="btnAll"    Content="Select all" Style="{StaticResource BtnGhost}" Margin="0,0,8,0"/>
      <Button x:Name="btnOK"     Content="Rename"     Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
      <Button x:Name="btnCancel" Content="Cancel"     Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""

    win = ui.parse(
        "Rename Families",
        "Clean up family and type names in bulk",
        _BODY, _FOOTER, width=1180, height=720,
        context=u"{}  --  acronyms kept uppercase by Title Case are listed in {}"
                .format(doc.Title, ACRONYM_PATH),
    )

    txtTag = win.FindName("txtTag")
    txtFind = win.FindName("txtFind")
    txtRepl = win.FindName("txtRepl")
    txtPre = win.FindName("txtPre")
    txtSuf = win.FindName("txtSuf")
    chkFam = win.FindName("chkFam")
    chkTyp = win.FindName("chkTyp")
    lblFam = win.FindName("lblFam")
    lblTyp = win.FindName("lblTyp")
    sldStart = win.FindName("sldStart")
    sldEnd = win.FindName("sldEnd")
    lblStart = win.FindName("lblStart")
    lblEnd = win.FindName("lblEnd")
    rbNone = win.FindName("rbNone")
    rbTitle = win.FindName("rbTitle")
    rbUpper = win.FindName("rbUpper")
    txtFilter = win.FindName("txtFilter")
    chkModel = win.FindName("chkModel")
    chkAnno = win.FindName("chkAnno")
    chkModel.Content = u"Model ({})".format(total_model_fams)
    chkAnno.Content = u"Annotation ({})".format(total_anno_fams)
    grid = win.FindName("grid")
    lblCount = win.FindName("lblCount")
    lblWarn = win.FindName("lblWarn")
    btnAll = win.FindName("btnAll")
    btnOK = win.FindName("btnOK")
    btnCancel = win.FindName("btnCancel")

    state = {"go": False, "ready": 0, "why": u"no rule", "plan": None}


    def case_mode():
        if rbTitle.IsChecked:
            return "title"
        if rbUpper.IsChecked:
            return "upper"
        return "none"


    def _rollup_text(n_fam, n_typ):
        """"renaming 18 families, 42 types", dropping whichever half is zero."""
        bits = []
        if n_fam:
            bits.append(u"{} famil{}".format(n_fam, u"y" if n_fam == 1 else u"ies"))
        if n_typ:
            bits.append(u"{} type{}".format(n_typ, u"" if n_typ == 1 else u"s"))
        return u"renaming " + u", ".join(bits) if bits else u""


    def _fam_typ(n_fam, n_typ):
        return u"{} famil{} and {} type{}".format(
            n_fam, u"y" if n_fam == 1 else u"ies",
            n_typ, u"" if n_typ == 1 else u"s")


    def _slider_label(chars, _where):
        """Just the number: the column is narrow and the label beside it already
    says which end it is cutting."""
        return u"{}".format(chars) if chars else u"off"


    def recompute():
        """Refresh every proposed name and every clash, then repaint what moved."""
        do_fam = bool(chkFam.IsChecked)
        do_typ = bool(chkTyp.IsChecked)
        TYPES_ON["v"] = do_typ
        rules = Rules(remove=txtTag.Text or u"",
                      find=txtFind.Text or u"",
                      repl=txtRepl.Text or u"",
                      prefix=txtPre.Text or u"",
                      suffix=txtSuf.Text or u"",
                      cut_start=int(sldStart.Value),
                      cut_end=int(sldEnd.Value),
                      case=case_mode())

        lblStart.Text = _slider_label(rules.cut_start, u"front")
        lblEnd.Text = _slider_label(rules.cut_end, u"end")

        dirty = set()
        for row in all_rows:
            active = do_fam if row.is_family else do_typ
            if active:
                new, done = apply_rule(row.Current, rules, acronyms)
                effect = (u"   ·   ".join(done) if done
                          else (u"" if rules.empty else u"not affected"))
            else:
                new, effect = row.Current, u""
            if row.set_new(new, active, effect):
                dirty.add(row)

        # -- clashes, live -------------------------------------------------
        # Reading the NET RESULT of the run (instead of "is that name taken right
        # now") is what correctly lets an A->B / B->A swap through. Rows that end up
        # blocked keep their current name and still occupy it, which resolve_clashes
        # iterates to a fixed point.
        pre = {}
        for row in all_rows:
            if row.Changed:
                if not row.New.strip():
                    pre[row] = u"the name would be empty"
                else:
                    problem = name_problem(row.New)
                    if problem:
                        pre[row] = problem
        why = resolve_clashes(all_rows, fixed_names, pre)

        n_fam = n_typ = sel_fam = sel_typ = tick_fam = tick_typ = 0
        doomed = []
        for row in all_rows:
            status = u""
            if row.Changed:
                status = why.get(row, u"")
                if row.is_family:
                    n_fam += 1
                    if row.picked and not status:
                        sel_fam += 1
                        doomed.append(row)
                else:
                    n_typ += 1
                    if row.picked and not status:
                        sel_typ += 1
                        doomed.append(row)
            if row.active and row.picked:
                if row.is_family:
                    tick_fam += 1
                else:
                    tick_typ += 1
            if row.set_status(status):
                dirty.add(row)

        # Roll the counts up the tree. Everything below a folded node is invisible
        # otherwise, and a category that opens folded is the first thing you see:
        # without this you cannot tell whether the run is about to touch anything in
        # there without opening every branch to check.
        for cat in cat_rows:
            cat_fam = cat_typ = cat_blocked = 0
            for fam in cat.children:
                fam_typ = fam_blocked = 0
                if fam.Changed and fam.picked:
                    if fam.Status:
                        cat_blocked += 1
                    else:
                        cat_fam += 1
                for kid in fam.children:
                    if kid.Changed and kid.picked:
                        if kid.Status:
                            fam_blocked += 1
                        else:
                            fam_typ += 1
                if fam.set_rollup(_rollup_text(0, fam_typ),
                                  u"{} blocked".format(fam_blocked) if fam_blocked
                                  else u""):
                    dirty.add(fam)
                cat_typ += fam_typ
                cat_blocked += fam_blocked
            if cat.set_rollup(_rollup_text(cat_fam, cat_typ),
                              u"{} blocked".format(cat_blocked) if cat_blocked
                              else u""):
                dirty.add(cat)

        for row in dirty:
            row.redraw()

        lblFam.Text = u"{} of {}".format(tick_fam, total_fams) if do_fam else u"off"
        lblTyp.Text = u"{} of {}".format(tick_typ, total_types) if do_typ else u"off"

        # -- the footer ----------------------------------------------------
        # It used to report ONLY "will be renamed", so a loaded list with no rule set
        # yet read as a flat 0 and your own selection was invisible -- the same blind
        # spot that let a search-filtered Select all leave rows ticked off screen and
        # rename them. Now it says what is ticked, what that produces, and how much
        # of it the search box is hiding.
        shown = set()
        tickable = []
        for _cat, fams in filtered_groups():
            for fam, kids, hit in fams:
                shown.add(fam)
                if hit and fam.active:
                    tickable.append(fam)
                for kid in kids:
                    shown.add(kid)
                    if kid.active:
                        tickable.append(kid)
        hidden = sum(1 for row in doomed if row not in shown)

        if rules.empty:
            outcome = u"no rule set yet"
            state["why"] = u"no rule"
        elif sel_fam or sel_typ:
            outcome = u"{} will be renamed".format(_fam_typ(sel_fam, sel_typ))
            state["why"] = u""
        elif n_fam or n_typ:
            outcome = u"every changed row is blocked or unticked: nothing to write"
            state["why"] = u"blocked"
        else:
            outcome = u"the rules change none of them"
            state["why"] = u"unchanged"
        lblCount.Text = outcome
        # No warning glyph: DM Sans has no U+26A0 and the fallback can land on the
        # colour emoji face. The warn colour is the marker.
        # Only the search can hide a row that is still going to be renamed: the
        # half boxes untick what they hide, so it stops being pending.
        narrowed = not (bool(chkModel.IsChecked) and bool(chkAnno.IsChecked))
        lblWarn.Text = (u"{} of those rows {} hidden by the search -- clear it to see"
                        u" them".format(hidden, u"is" if hidden == 1 else u"are")
                        if hidden else u"")

        # The button names the universe it is about to hit, because with something
        # typed in the search box that universe is NOT the whole list.
        all_on = bool(tickable) and all(r.picked for r in tickable)
        if (txtFilter.Text or u"").strip() or narrowed:
            btnAll.Content = u"Clear shown" if all_on else u"Select all shown"
        else:
            btnAll.Content = u"Clear all" if all_on else u"Select all"

        state["ready"] = sel_fam + sel_typ


    def filtered_groups():
        """[(category, [(family, its types, did the family match), ...]), ...] for
    everything the search box lets through, expansion aside.

    ONE place decides what the search hides, so the grid, the Select all button
    and the "hidden by the search" warning in the footer cannot disagree. Them
    disagreeing is what let Select all leave rows ticked outside the search,
    which the run then renamed anyway, since the write always walked all_rows.
    """
        do_typ = bool(chkTyp.IsChecked)
        q = (txtFilter.Text or u"").strip().lower()
        show_model = bool(chkModel.IsChecked)
        show_anno = bool(chkAnno.IsChecked)
        out = []
        for cat in cat_rows:
            # Model / annotation is a second way of narrowing the SAME list, so it
            # rides here with the search instead of becoming an axis of its own.
            if not (show_anno if cat.annotation else show_model):
                continue
            # Matching the category name brings the whole group in, untouched.
            cat_hit = bool(q) and q in cat.Current.lower()
            fams = []
            for fam in cat.children:
                kids = fam.children if do_typ else []
                hit = True
                if q and not cat_hit:
                    hit = q in fam.Current.lower() or q in fam.New.lower()
                    if not hit:
                        # The family did not match, but some of its types did: it is
                        # listed as their header and nothing more. Select all skips
                        # it, because ticking a family cascades to ALL its types --
                        # the ones the search is hiding included.
                        kids = [k for k in kids
                                if q in k.Current.lower() or q in k.New.lower()]
                        if not kids:
                            continue
                fams.append((fam, kids, hit))
            if fams:
                out.append((cat, fams))
        return out


    def visible_rows():
        """The flat list the grid shows. Categories always; families and types as
    their parent is opened -- or straight away while the search box has
    something in it, because leaving a match folded out of sight would be the
    same lie the footer warning exists to prevent."""
        if not chkFam.IsChecked and not chkTyp.IsChecked:
            return []
        searching = bool((txtFilter.Text or u"").strip())
        out = []
        for cat, fams in filtered_groups():
            out.append(cat)
            if not (cat.expanded or searching):
                continue
            for fam, kids, _hit in fams:
                out.append(fam)
                if kids and (fam.expanded or searching):
                    out.extend(kids)
        return out


    def tickable_rows():
        """What Select all is allowed to touch: the rows on screen that are in scope.
    A folded family or type counts -- it is one chevron away, not hidden."""
        out = []
        for _cat, fams in filtered_groups():
            for fam, kids, hit in fams:
                if hit and fam.active:
                    out.append(fam)
                for kid in kids:
                    if kid.active:
                        out.append(kid)
        return out


    # The grid is bound to THIS collection for the life of the window. Handing the
    # DataGrid a new one on every rebind makes it drop its scroll position, which on
    # a tree you fold and unfold all day means one chevron click sends you back to
    # the top of the document.
    _VIS = ObservableCollection[object]()
    grid.ItemsSource = _VIS


    def rebind():
        """Bring the bound collection to what visible_rows() says, IN PLACE.

    Rows never change their relative order (category, then family, then type,
    each sorted once) -- they only appear or disappear -- so walking the wanted
    list once and inserting what is missing is enough. No move, no reset, and
    the ScrollViewer stays where the user left it."""
        wanted = visible_rows()
        keep = set(wanted)
        for idx in range(_VIS.Count - 1, -1, -1):
            if _VIS[idx] not in keep:
                _VIS.RemoveAt(idx)
        for pos, row in enumerate(wanted):
            if pos >= _VIS.Count or _VIS[pos] is not row:
                _VIS.Insert(pos, row)


    def refresh():
        recompute()
        rebind()


    # Typing recomputes a few thousand strings, so it runs on a short debounce: the
    # preview still reads as live, but a fast typist does not queue one full pass
    # per keystroke. Note it does NOT rebind the grid -- INotifyPropertyChanged
    # repaints the rows in place, which is what keeps the scroll position and the
    # ticks where they were while you refine the tag.
    timer = DispatcherTimer()
    timer.Interval = TimeSpan.FromMilliseconds(180)


    def on_tick(s, e):
        timer.Stop()
        # A full refresh, not just a recount: the search box rides this same
        # debounce, and the proposed names it matches against have just moved.
        # Rebinding is cheap now that it updates the collection in place.
        refresh()


    def on_tag_typed(s, e):
        timer.Stop()
        timer.Start()


    def on_scope_changed(s, e):
        # A tick box or a case option changes WHICH rows are listed, so this one
        # goes through the full refresh.
        refresh()
        # Switching Families / Types on or off changes which own-ticks a family or a
        # category adds up (its dash), so repaint the boxes that are on screen.
        for row in visible_rows():
            row._raise("Checked")


    def on_filter(s, e):
        # Same debounce as the tag box: one keystroke re-runs the rule over every
        # name in the document, and a fast typist should not queue one pass each.
        timer.Stop()
        timer.Start()


    def on_grid_click(s, e):
        src = e.OriginalSource
        if getattr(src, "Tag", None) != "chev":
            return
        row = getattr(src, "DataContext", None)
        if isinstance(row, Row):
            row.toggle()
            rebind()


    def on_dbl(s, e):
        row = grid.SelectedItem
        if isinstance(row, Row) and row.children:
            row.toggle()
            rebind()


    def on_row_ticked(s, e):
        # CheckBox.ClickEvent, not Checked/Unchecked: those also fire while WPF
        # realises virtualised rows, which would recount on every scroll. Unticking
        # a row can clear a clash (it stops competing for the name), so this is a
        # recompute, not just a counter bump -- but NOT a rebind, or the grid would
        # jump under the click that caused it.
        recompute()


    # What each half was the last time we looked, so one click cascades its own
    # box and leaves the other one alone.
    _HALF = {False: True, True: True}


    def on_half(s, e):
        """Model / annotation is SCOPE, not a second search box. A branch that
    disappears while its rows stay ticked is exactly the lie that renamed
    families nobody could see, so the box carries its tick down into its
    categories -- the same cascade as clicking each one by hand."""
        for box, anno in ((chkModel, False), (chkAnno, True)):
            want = bool(box.IsChecked)
            if _HALF[anno] == want:
                continue
            _HALF[anno] = want
            _QUIET["on"] = True
            try:
                for cat in cat_rows:
                    if cat.annotation == anno:
                        cat.Checked = want
            finally:
                _QUIET["on"] = False
        for row in visible_rows():
            row._raise("Checked")
        refresh()


    def on_all(s, e):
        rows = tickable_rows()
        turn_on = not (rows and all(r.picked for r in rows))
        _QUIET["on"] = True
        try:
            for row in rows:
                row.Checked = turn_on
        finally:
            _QUIET["on"] = False
        # One repaint for everything the grid can be showing, now that the model has
        # settled. Rows further down are read fresh when scrolling realises them.
        for row in visible_rows():
            row._raise("Checked")
        refresh()


    # --------------------------------------------------------------------------
    # The review: shown BEFORE anything is written. A tick left on from the last run
    # once renamed 48 families of a template with no question asked; the full list
    # of what is about to happen, with what will not, is the question.
    # --------------------------------------------------------------------------
    _REVIEW_BODY = """
  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
    </Grid.RowDefinitions>
    <TextBlock x:Name="lblSummary" Grid.Row="0" TextWrapping="Wrap" FontSize="13"
               Foreground="#202022" Margin="0,0,0,12"/>
    <Border Grid.Row="1" CornerRadius="8" Background="#FFFFFF"
            BorderBrush="#ECE9E4" BorderThickness="1" ClipToBounds="True">
      <DataGrid x:Name="grid" AutoGenerateColumns="False" SelectionMode="Single"
                CanUserAddRows="False" CanUserDeleteRows="False"
                CanUserResizeRows="False" CanUserSortColumns="False">
        <DataGrid.Columns>
          <DataGridTextColumn Header="KIND" Width="70" Binding="{Binding Kind}" IsReadOnly="True"/>
          <DataGridTextColumn Header="CATEGORY" Width="1.3*" Binding="{Binding Category}" IsReadOnly="True"/>
          <DataGridTemplateColumn Header="OLD NAME" Width="2*">
            <DataGridTemplateColumn.CellTemplate>
              <DataTemplate>
                <TextBlock Text="{Binding OldName}" Foreground="{Binding OldBrush}"
                           TextWrapping="Wrap" VerticalAlignment="Center"/>
              </DataTemplate>
            </DataGridTemplateColumn.CellTemplate>
          </DataGridTemplateColumn>
          <DataGridTemplateColumn Header="NEW NAME" Width="2*">
            <DataGridTemplateColumn.CellTemplate>
              <DataTemplate>
                <TextBlock Text="{Binding NewName}" FontWeight="{Binding NewWeight}"
                           TextWrapping="Wrap" VerticalAlignment="Center"/>
              </DataTemplate>
            </DataGridTemplateColumn.CellTemplate>
          </DataGridTemplateColumn>
          <DataGridTemplateColumn Header="RESULT" Width="2*">
            <DataGridTemplateColumn.CellTemplate>
              <DataTemplate>
                <TextBlock Text="{Binding Result}" Foreground="{Binding ResultBrush}"
                           TextWrapping="Wrap" VerticalAlignment="Center"/>
              </DataTemplate>
            </DataGridTemplateColumn.CellTemplate>
          </DataGridTemplateColumn>
        </DataGrid.Columns>
      </DataGrid>
    </Border>
  </Grid>
"""

    _REVIEW_FOOTER = """
  <Grid>
    <Grid.ColumnDefinitions>
      <ColumnDefinition Width="*"/>
      <ColumnDefinition Width="Auto"/>
    </Grid.ColumnDefinitions>
    <TextBlock x:Name="lblNote" Grid.Column="0" Foreground="#A6A199" FontSize="11.5"
               VerticalAlignment="Center" Margin="0,0,16,0" TextTrimming="CharacterEllipsis"/>
    <StackPanel Grid.Column="1" HorizontalAlignment="Right" Orientation="Horizontal"
                VerticalAlignment="Center">
      <Button x:Name="btnBack" Style="{StaticResource BtnGhost}" Margin="0,0,8,0"/>
      <Button x:Name="btnCsv"  Content="Export CSV" Style="{StaticResource BtnGhost}" Margin="0,0,8,0"/>
      <Button x:Name="btnGo"   Style="{StaticResource BtnPrimary}"/>
    </StackPanel>
  </Grid>
"""

    OK_BRUSH = _brush(ui.STATUS_OK)
    WARN_BRUSH = _brush(ui.STATUS_WARN)


    class ReviewRow(object):
        """One line of the review. Plain attributes would not bind: PascalCase."""

        def __init__(self, row, result, brush):
            self._row = row
            self._result = result
            self._brush = brush

        @property
        def Kind(self):
            return self._row.kind

        @property
        def Category(self):
            return self._row.Category

        @property
        def OldName(self):
            return self._row.Current

        @property
        def OldBrush(self):
            return DIM

        @property
        def NewName(self):
            return self._row.New

        @property
        def NewWeight(self):
            return FontWeights.SemiBold if self._brush is OK_BRUSH else FontWeights.Normal

        @property
        def Result(self):
            return self._result

        @property
        def ResultBrush(self):
            return self._brush


    def checkout_problem(row, ctx):
        """Why Revit would refuse to touch this row for sure: another user has its
    FAMILY checked out. Asked once per family element (a type answers with its
    family's result), never per type. u"" when it is free, not workshared, or
    Revit would not say (an unanswered question is not a prediction of failure)."""
        fam = row if row.is_family else row.parent
        if fam is None or fam.element is None or ctx["off"] or not doc.IsWorkshared:
            return u""
        if fam in ctx["seen"]:
            return ctx["seen"][fam]
        answer = u""
        try:
            owner = clr.Reference[str]()
            status = DB.WorksharingUtils.GetCheckoutStatus(doc, fam.element.Id, owner)
            if status == DB.CheckoutStatus.OwnedByOtherUser:
                answer = u"checked out by {}".format(owner.Value or u"another user")
        except Exception:
            ctx["off"] = True
        ctx["seen"][fam] = answer
        return answer


    def current_plan():
        """(rows to write, families first; rows ticked and changed but blocked; rows
    that are bound to fail, as [(row, why)]). The last group is NOT in the first:
    a row we know will fail is not sent to Revit."""
        todo = [r for r in all_rows if r.Changed and r.picked and not r.Status]
        held = [r for r in all_rows if r.Changed and r.picked and r.Status]
        ordered = ([r for r in todo if r.is_family] +
                   [r for r in todo if not r.is_family])
        ctx = {"off": False, "seen": {}}
        writable = []
        failing = []
        for r in ordered:
            why = checkout_problem(r, ctx)
            if why:
                failing.append((r, why))
            else:
                writable.append(r)
        return writable, held, failing


    def _plural(n, one, many):
        return u"{} {}".format(n, one if n == 1 else many)


    def _confirm_label(n_fam, n_typ):
        bits = []
        if n_fam:
            bits.append(_plural(n_fam, u"family", u"families"))
        if n_typ:
            bits.append(_plural(n_typ, u"type", u"types"))
        return u"Confirm: rename " + u" and ".join(bits)


    def _csv_field(value):
        text = u"" if value is None else u"{}".format(value)
        for bad in (u",", u'"', u"\n", u"\r"):
            if bad in text:
                return u'"' + text.replace(u'"', u'""') + u'"'
        return text


    def review_changes():
        """Show what is about to be written. True only on Confirm."""
        ordered, held, doomed = current_plan()
        # What the user is shown is exactly what gets written: the apply step reads
        # this, it does not plan again.
        state["plan"] = (ordered, held, doomed)
        rows = []
        n_fam = sum(1 for r in ordered if r.is_family)
        n_typ = len(ordered) - n_fam
        n_fail = len(doomed)
        for r in ordered:
            rows.append(ReviewRow(r, u"Will rename", OK_BRUSH))
        for r in held:
            rows.append(ReviewRow(r, u"Will not rename - {}".format(r.Status),
                                  WARN_BRUSH))
        for r, why in doomed:
            rows.append(ReviewRow(r, u"Will fail - {}".format(why), BAD))

        if ordered:
            parts = [u"You are about to rename {} and {}.".format(
                _plural(n_fam, u"family", u"families"),
                _plural(n_typ, u"type", u"types"))]
        else:
            parts = [u"Nothing can be renamed."]
        if held:
            parts.append(u"{} will not be renamed (name clash or invalid name)."
                         .format(len(held)))
        if n_fail:
            parts.append(u"{} will fail and are left out.".format(n_fail))

        rv = ui.parse("Rename Families", "Review before renaming",
                      _REVIEW_BODY, _REVIEW_FOOTER, width=1040, height=660,
                      context=doc.Title)
        try:
            rv.Owner = win
        except Exception:
            pass
        rv.FindName("lblSummary").Text = u"  ".join(parts)
        lblNote = rv.FindName("lblNote")
        lblNote.Text = u"Nothing has been written yet."
        btnBack = rv.FindName("btnBack")
        btnGo = rv.FindName("btnGo")
        btnBack.Content = u"< Back to edit"
        if ordered:
            btnGo.Content = _confirm_label(n_fam, n_typ)
        else:
            # Nothing left to write: the button must not say "Confirm: rename " and
            # stop mid-sentence, nor be clickable.
            btnGo.Content = u"Nothing to confirm"
            btnGo.IsEnabled = False

        items = ObservableCollection[object]()
        for item in rows:
            items.Add(item)
        rv.FindName("grid").ItemsSource = items

        answer = {"go": False}

        def on_go(sender, args):
            answer["go"] = True
            rv.Close()

        def on_csv(sender, args):
            # Microsoft.Win32, not WinForms: the netcore engine of Revit 2025+ does
            # not carry System.Windows.Forms.
            from Microsoft.Win32 import SaveFileDialog
            dlg = SaveFileDialog()
            dlg.Filter = "CSV file (*.csv)|*.csv|All files (*.*)|*.*"
            dlg.DefaultExt = ".csv"
            dlg.AddExtension = True
            dlg.FileName = "Rename Families - review"
            if not dlg.ShowDialog():
                return
            lines = [u",".join(_csv_field(h) for h in
                               (u"Kind", u"Category", u"Old name", u"New name",
                                u"Result"))]
            for it in rows:
                lines.append(u",".join(_csv_field(v) for v in
                                       (it.Kind, it.Category, it.OldName,
                                        it.NewName, it.Result)))
            # BOM + CRLF so Excel opens it with the accents and the rows right.
            blob = (unichr(0xFEFF) + u"\r\n".join(lines) + u"\r\n").encode("utf-8")
            handle = open(dlg.FileName, "wb")
            try:
                handle.write(blob)
            finally:
                handle.close()
            lblNote.Text = u"Exported {} rows to {}".format(
                len(rows), os.path.basename(dlg.FileName))

        btnGo.Click += on_go
        btnBack.Click += lambda sender, args: rv.Close()
        rv.FindName("btnCsv").Click += on_csv
        rv.ShowDialog()
        return answer["go"]


    NOTHING_TO_RENAME = {
        u"no rule": u"No rule is set. Fill in at least one on the left: text to "
                    u"remove or replace, a prefix or suffix, a trim, or a case change.",
        u"blocked": u"Every name the rules change is blocked (the red notes say why) "
                    u"or unticked, so there is nothing to write.",
        u"unchanged": u"The rules do not change any of the names that are ticked.",
    }


    def on_ok(s, e):
        # The rules may have been typed less than the debounce ago: settle them
        # first, or the review below would show names from the previous keystroke.
        timer.Stop()
        refresh()
        if not state["ready"]:
            # Silence here read as a dead button. Say why there is nothing to do.
            ui.alert(NOTHING_TO_RENAME.get(state["why"], NOTHING_TO_RENAME[u"unchanged"]),
                     title="Nothing to rename", context=doc.Title)
            return
        # Nothing is written from here. The review comes first, on top of this
        # window, so "Back to edit" is just closing it: the rules are still in place.
        if review_changes():
            state["go"] = True
            win.Close()


    timer.Tick += on_tick
    for _box in (txtTag, txtFind, txtRepl, txtPre, txtSuf):
        _box.TextChanged += on_tag_typed
    # Dragging a slider fires one event per tick crossed, so they ride the same
    # debounce as typing rather than recomputing the whole document per pixel.
    sldStart.ValueChanged += on_tag_typed
    sldEnd.ValueChanged += on_tag_typed
    txtFilter.TextChanged += on_filter
    chkFam.Click += on_scope_changed
    chkTyp.Click += on_scope_changed
    chkModel.Click += on_half
    chkAnno.Click += on_half
    for rb in (rbNone, rbTitle, rbUpper):
        rb.Click += on_scope_changed
    grid.AddHandler(UIElement.MouseLeftButtonUpEvent,
                    MouseButtonEventHandler(on_grid_click), True)
    grid.AddHandler(_CheckBox.ClickEvent, RoutedEventHandler(on_row_ticked), True)
    grid.MouseDoubleClick += on_dbl
    btnAll.Click += on_all
    btnOK.Click += on_ok
    btnCancel.Click += lambda s, e: win.Close()

    refresh()
    txtTag.Focus()
    win.ShowDialog()

    if not state["go"]:
        script.exit()

    # --------------------------------------------------------------------------
    # Apply. Families first, then types, so the report reads top-down. Each row is
    # guarded on its own: one rejection from the API must not take the whole run
    # down with it.
    #
    # The parking pass is what makes an A->B / B->A swap survive. The clash check
    # works on the net result and rightly calls a swap legal, but the writes happen
    # one at a time against the LIVE document: renaming A to B while B is still
    # called B trips Revit's duplicate-name check. So every row SITTING ON a name
    # another row wants is first parked on a throwaway name. Parking the rows that
    # WANT a taken name instead is not enough: it fixes a two-row swap but still
    # fails a chain (A->B, B->C, C->Z), where the blocker has to move out of the
    # way regardless of who asked for it.
    # --------------------------------------------------------------------------
    ordered, blocked, doomed = state["plan"]

    renamed = 0
    failed = 0
    for row, why in doomed:
        # Known to fail, so it was never sent to Revit.
        row.set_status(u"FAILED - {} (not attempted)".format(why))
        failed += 1

    # Not `with revit.Transaction(...)`: its __exit__ swallows a commit that Revit
    # refuses, and the rows had already been marked "Renamed" by then. Here the
    # commit status is read, because a rolled-back run (a family another user has
    # checked out, a failure Revit cannot resolve) changed NOTHING in the model.
    txn = DB.Transaction(doc, "Rename Families")
    rolled_back = u""
    try:
        txn.Start()
        live, unreadable = live_names(ordered)
        todo = []
        for row in ordered:
            if row.scope in unreadable:
                row.set_status(u"FAILED - could not read the existing names here, "
                               u"left untouched")
                failed += 1
            else:
                todo.append(row)

        # Names are keyed lower-cased (Revit ignores case): {scope: {key: [rows that
        # want it]}}, and {scope: {keys held right now}}.
        claimed = {}
        for row in todo:
            claimed.setdefault(row.scope, {}).setdefault(_key(row.New), []).append(row)
        taken_now = {}
        for row in all_rows:
            taken_now.setdefault(row.scope, set()).add(_key(row.Current))
        for scope, names in fixed_names.items():
            taken_now.setdefault(scope, set()).update(_key(n) for n in names)

        for i, row in enumerate(todo):
            # Park only when ANOTHER row wants the name this one sits on. A row that
            # just changes its own capitals (ab -> AB) wants its own name: no parking.
            if not [r for r in claimed.get(row.scope, {}).get(_key(row.Current), [])
                    if r is not row]:
                continue
            park = u"{}_MTTMP{}".format(row.Current, i)
            while (_key(park) in taken_now.get(row.scope, set())
                   or _key(park) in live.get(row.scope, {})):
                park += u"X"
            try:
                _set_elem_name(row.element, park)
                row._parked = True
                _live_move(live, row, park)
            except Exception as exc:
                row.set_status(u"FAILED - {}".format(_why(exc)))
                failed += 1

        for row in todo:
            if row.Status.startswith(u"FAILED"):
                continue
            try:
                # Safety net: the preview already blocks clashes, but it works on the
                # names read at the start. Never write a name another element holds.
                holder = live.get(row.scope, {}).get(_key(row.New))
                if holder is not None and holder != _id_val(row.element.Id):
                    raise Exception(u"another {} already has the name \"{}\""
                                    .format(u"family" if row.is_family
                                            else u"type in this family", row.New))
                _set_elem_name(row.element, row.New)
                _live_move(live, row, row.New)
                row.set_status(u"Renamed")
                renamed += 1
            except Exception as exc:
                failed += 1
                reason = _why(exc)
                if not row.parked:
                    row.set_status(u"FAILED - {}".format(reason))
                    continue
                # This one is sitting on a throwaway name right now. Leaving it
                # there would plant junk in the model, so put the original back --
                # unless something else has taken that name in the meantime (then
                # restoring would make the very duplicate this tool exists to
                # prevent), or the restore itself fails: either way, say which name
                # it is actually stuck on.
                temp = row._held
                holder = live.get(row.scope, {}).get(_key(row.Current))
                if holder is not None and holder != _id_val(row.element.Id):
                    row.set_status(u"FAILED - STILL ON TEMP NAME - {} (its original "
                                   u"name is taken now, rename by hand) - {}"
                                   .format(temp, reason))
                    continue
                try:
                    _set_elem_name(row.element, row.Current)
                    _live_move(live, row, row.Current)
                    row.set_status(u"FAILED (original name restored) - {}"
                                   .format(reason))
                except Exception:
                    row.set_status(u"FAILED - STILL ON TEMP NAME - {} (rename by "
                                   u"hand) - {}".format(temp, reason))

        result = txn.Commit()
        if result != DB.TransactionStatus.Committed:
            rolled_back = u"Revit did not commit the transaction ({})".format(result)
    except Exception as exc:
        run.error()
        rolled_back = u"the run was aborted ({})".format(_why(exc))
        try:
            if txn.HasStarted() and not txn.HasEnded():
                txn.RollBack()
        except Exception:
            pass

    if rolled_back:
        # The rollback undid EVERYTHING, parking included: a row that said "STILL ON
        # TEMP NAME" or "original name restored" refers to a state that no longer
        # exists. Only the rows that were never written to keep their own status
        # ("could not read"; the "(not attempted)" ones are not in `ordered`).
        for row in ordered:
            if not row.Status.startswith(u"FAILED - could not read"):
                row.set_status(u"NOT APPLIED - {}".format(rolled_back))
        renamed = 0
        failed = len(ordered) + len(doomed)

    # --------------------------------------------------------------------------
    # Report (Export CSV comes free with show_table)
    # --------------------------------------------------------------------------
    # Rows left on a throwaway name (only possible when the run did commit): the
    # model has junk names the user must fix by hand, so the headline says so.
    stuck = sum(1 for r in ordered if u"STILL ON TEMP NAME" in r.Status)
    stuck_note = u"   |   {} stuck on temp name".format(stuck) if stuck else u""

    report = [[r.kind, r.Category, r.Current, r.New, r.Status]
              for r in ordered + blocked + [d[0] for d in doomed]]

    bits = []
    if int(sldStart.Value):
        bits.append(u"Cut {} from the start".format(int(sldStart.Value)))
    if int(sldEnd.Value):
        bits.append(u"Cut {} from the end".format(int(sldEnd.Value)))
    if txtTag.Text:
        bits.append(u"Removed '{}'".format(txtTag.Text))
    if case_mode() == "title":
        bits.append(u"Title Case (acronyms: {})".format(ACRONYM_PATH))
    elif case_mode() == "upper":
        bits.append(u"UPPERCASE")
    if txtFind.Text:
        bits.append(u"Replaced '{}' with '{}'".format(txtFind.Text, txtRepl.Text))
    if txtPre.Text:
        bits.append(u"Prefix '{}'".format(txtPre.Text))
    if txtSuf.Text:
        bits.append(u"Suffix '{}'".format(txtSuf.Text))

    ui.show_table(
        report,
        [("Kind", 70), ("Category", "1.4*"), ("Old name", "2*"),
         ("New name", "2*"), ("Status", "1.4*")],
        title="Rename Families",
        subtitle=(u"Nothing was renamed: {}".format(rolled_back) if rolled_back
                  else u"{} renamed   |   {} skipped (clash)   |   {} failed{}"
                       .format(renamed, len(blocked), failed, stuck_note)),
        summary=u"   |   ".join(bits) if bits else u"No rule applied",
        width=1000, height=620, context=doc.Title,
    )
