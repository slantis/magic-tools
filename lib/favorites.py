# -*- coding: utf-8 -*-
r"""favorites -- what the Favorites bar shows, kept between sessions.

The Favorites panel of Magic Tools holds its tools as hidden buttons
(2026-09-04). What this file stores is which of those buttons are shown:
lib/favbar.py reads it on every boot and on every star click, and the All
Magic Tools window writes it. The tools that are always on the ribbon (All
Magic Tools and the Navigation and Selection stacks) live in their own panel
and are not stored.

TWO THINGS DECIDE THE BAR, since the evening of 2026-09-04 (after the first
live test: presets, to switch quickly between drafting tools and view template
tools):

  * the STARS: up to MAX tools the user picked, one by one, in All Magic Tools;
  * the PRESET on the bar (`bar`): MINE, which is the stars, or the name of a
    group from lib/groups.json (Annotate, Sheets, ...), which puts that
    whole group on the bar instead. The stars are kept while a preset is up,
    so switching back costs one click and loses nothing.

bar_keys() is the one answer favbar needs: the folder names to show right now.

THE KEY IS THE FOLDER NAME of the tool's bundle ("Create Type Filter", not
"Create\nType Filter"). It is what the ribbon button is called
(RibbonItem.Name), so it survives a rebuild, a retitle and a change of
extension hash. `name` beside it is the title at the time of starring, kept
only so the file reads well by eye.

  * the SETS (2026-09-05, afternoon): presets the user saved under a name
    from whatever was on the bar at the time (save the current set, give it a
    name and keep it, so people can really customize their own ribbon). A set
    is a name plus folder names, it behaves on the bar exactly like a group of
    groups.json, and it can be exported to a JSON file and imported on another
    machine, so a team can share one. A user set may not take the name of a
    built-in group or of MINE.

The same file remembers where on the screen each window was last closed, per
title, because that is the user's and not the session's.

WHERE, and why not the obvious place. %APPDATA%\pyRevit\<FILENAME>, which is
per Windows user and per machine -- personal, and it does not travel. Two
places were rejected on evidence, not taste:

  * NOT script.get_config(). pyRevit's userconfig.save_changes() is a silent
    no-op when CONFIG_TYPE lands on 'Admin', which happens when the machine
    has a non-writable %PROGRAMDATA%\pyRevit\pyRevit_config.ini -- and that
    file DOES exist here, read-only by ACL. A favorite that saves on some
    machines and vanishes on others is worse than one that never saves.
  * NOT the extension folder. It is a git clone that gets replaced on deploy.

And the ROOT of %APPDATA%\pyRevit, never the per-version subfolder: pyRevit
prunes that one on boot (coreutils/appdata.py:321-338). It only deletes names
carrying a pid segment, which is also why this filename must not start with
digits followed by an underscore.

FIRST RUN: no file means the DEFAULTS are written, so a fresh install shows
a light bar and not an empty one (a few tools, so each user really picks).
A file that is present but unreadable means NO favorites, not the defaults: a
broken store is started over by starring again, and quietly re-seeding it
would hide that it broke. A version 1 file (the morning of 2026-09-04, stars
only) is read as is and rewritten as version 2 on the next save.

Every read degrades to "no favorites" and every write is best effort: nothing
in this module may keep the ribbon or the window from loading.
"""
import io
import json
import os

VERSION = 2
FILENAME = "_magictools_favorites.json"
MAX = 15
# Folder names, not titles. Until 2026-09-08 a first run starred one tool, the
# most used one at the time; since then the bar starts on STANDARD instead and
# the stars start EMPTY, so "Mine" is really the user's own from the first star
# (the choice was between "bar on Standard, Mine empty" and "Mine seeded as a
# copy of Standard": the first).
DEFAULTS = ()
# STANDARD: the set we put on the ribbon for someone who does not know the
# tools yet (2026-09-08: assume people do not know the tools).
# Chosen by judgement, not by measurement: everyday tools, no setup, nothing
# that deletes or merges. Folder names, like every key here. The doors and the
# pyRevit tools cannot be on it (they are not on the Favorites panel), and
# neither can the sheet stack since 2026-09-08: Next, Parent and Previous
# Sheet are fixed on the ribbon in the Navigation stack, so they left this list.
STANDARD = "standard"
STANDARD_TOOLS = (
    "Find Room",
    "Select Same Type", "Select Same Family",
    "Goodbye Filter",          # Goodbye Filter
    "View Template Manager",                     # replaced an older tool, 2026-09-19
    "Inspect Element Graphics",
    "Align View Titles",
    "Replace In-Place",
    "Print Set Manager",
)
FULL = u"Favorites is full ({0} of {0}). Unstar one to make room.".format(MAX)
# The preset that is the stars themselves. Lower case on purpose, like
# STANDARD, so neither can collide with a group name (groups.json names are
# Title Case).
MINE = "mine"
BUILTIN = (STANDARD, MINE)          # the two fixed chips, in chip order
LABELS = {STANDARD: u"Standard", MINE: u"Mine"}


def label(name):
    """What a preset is called on screen: the two fixed ones are Title Case."""
    return LABELS.get(name, name)
GROUPS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "groups.json")
# Old folder name -> new one (2026-09-28, the folders took the name the ribbon
# shows). A star or a set saved before a rename still names the old folder;
# every key read from disk or from an imported set goes through rename() so it
# lands on the button that exists today, and the file is rewritten once.
RENAMES_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "renames.json")

try:
    basestring
except NameError:           # CPython 3, for the offline harness only
    basestring = str

# Read once per session and kept here; every writer keeps it current. The
# window writes and the ribbon sync reads, in the same process.
_STATE = None


def _fresh(favs=None):
    return {"favs": list(favs or []), "bar": STANDARD if PRESETS else MINE,
            "windows": {}, "sets": []}


def path():
    """Absolute path of the store, or None when APPDATA is not set."""
    base = os.environ.get("APPDATA")
    if not base:
        return None
    return os.path.join(base, "pyRevit", FILENAME)


def _read_json(target):
    """Parsed content, or None if unreadable / half-written. Missing is the
    caller's case to tell apart (it seeds), so it is checked before this."""
    try:
        handle = io.open(target, "r", encoding="utf-8")
        try:
            return json.load(handle)
        finally:
            handle.close()
    except Exception:
        return None


# THE EDITION (2026-10-05). An optional lib/edition.json turns this same code
# into the open source edition of the extension: its own store file, its own
# first-run stars, and no presets, so the bar is always the stars. Without the
# file nothing below changes anything.
EDITION_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "edition.json")
_EDITION = _read_json(EDITION_JSON) if os.path.isfile(EDITION_JSON) else None
if not isinstance(_EDITION, dict):
    _EDITION = {}
if isinstance(_EDITION.get("store"), basestring) and _EDITION.get("store"):
    FILENAME = _EDITION["store"]
if isinstance(_EDITION.get("defaults"), list):
    DEFAULTS = tuple(k for k in _EDITION["defaults"] if isinstance(k, basestring))
PRESETS = _EDITION.get("presets", True) is not False


def edition(key, default=None):
    """One value of lib/edition.json, or `default` when the file or key is absent."""
    return _EDITION.get(key, default)


def _write_json(target, obj):
    """Write through a .tmp + rename so a crash cannot leave half a file.

    Binary on purpose: json.dumps gives str under IronPython 2.7 and unicode
    under CPython 3, and encoding it here makes both write the same bytes.
    """
    tmp = target + ".tmp"
    data = json.dumps(obj, indent=1, sort_keys=True)
    if not isinstance(data, bytes):
        data = data.encode("utf-8")
    folder = os.path.dirname(target)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    handle = open(tmp, "wb")
    try:
        handle.write(data)
    finally:
        handle.close()
    if os.path.exists(target):
        os.remove(target)
    os.rename(tmp, target)


def _save():
    try:
        target = path()
        if target and _STATE is not None:
            blob = dict(_STATE)
            blob["v"] = VERSION
            _write_json(target, blob)
    except Exception:
        # The in-memory state stands so the star the user just clicked does not
        # flip back under them; it is the disk copy that was lost.
        pass


_RENAMES = None


def _renames():
    """{old folder: new folder}, every dated batch of renames.json merged. Never raises."""
    global _RENAMES
    if _RENAMES is None:
        _RENAMES = {}
        try:
            blob = _read_json(RENAMES_JSON)
            for batch, table in sorted((blob or {}).items()):
                if isinstance(table, dict):
                    _RENAMES.update(table)
        except Exception:
            _RENAMES = {}
    return _RENAMES


def rename(key):
    """The folder a stored key names today (follows chained renames)."""
    table = _renames()
    for _ in range(8):
        if key not in table:
            break
        key = table[key]
    return key


def _clean_favs(raw):
    seen, favs = set(), []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if isinstance(key, basestring):
            key = rename(key)
        if not key or key in seen:
            continue
        seen.add(key)
        favs.append({"key": key, "name": entry.get("name") or key})
    del favs[MAX:]
    return favs


def _clean_sets(raw):
    """[{"name", "tools"}], names stripped and unique, tools unique strings."""
    sets, seen = [], set()
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        tools = entry.get("tools")
        if not isinstance(name, basestring) or not isinstance(tools, list):
            continue
        name = name.strip()
        if not name or name in seen:
            continue
        keys_ = []
        for key in tools:
            if isinstance(key, basestring) and key:
                key = rename(key)
                if key not in keys_:
                    keys_.append(key)
        seen.add(name)
        sets.append({"name": name, "tools": keys_})
    return sets


def _load(force=False):
    """The whole state, from disk once and then from memory. Never raises."""
    global _STATE
    if _STATE is not None and not force:
        return _STATE
    _STATE = _fresh()
    try:
        target = path()
        if not target:
            return _STATE
        if not os.path.exists(target):
            _STATE = _fresh({"key": k, "name": k} for k in DEFAULTS)
            _save()
            return _STATE
        blob = _read_json(target)
        if not isinstance(blob, dict) or blob.get("v") not in (1, VERSION):
            return _STATE
        state = _fresh(_clean_favs(blob.get("favs")))
        bar = blob.get("bar")
        if PRESETS and isinstance(bar, basestring) and bar:
            state["bar"] = bar
        windows = blob.get("windows")
        if isinstance(windows, dict):
            state["windows"] = windows
        # Same VERSION as before the sets: an older lib reading this file
        # ignores the key and loses nothing, a newer one finds it.
        state["sets"] = _clean_sets(blob.get("sets"))
        _STATE = state
        # A store written before a rename is rewritten once with today's keys.
        old = [e.get("key") for e in blob.get("favs") or [] if isinstance(e, dict)]
        old_sets = [t for e in blob.get("sets") or [] if isinstance(e, dict)
                    for t in (e.get("tools") or [])]
        if any(rename(k) != k for k in old + old_sets if isinstance(k, basestring)):
            _save()
    except Exception:
        _STATE = _fresh()
    return _STATE


# -- the stars -----------------------------------------------------------------

def load(force=False):
    """The starred tools, oldest first, at most MAX. Never raises."""
    return _load(force)["favs"]


def keys():
    """The starred folder names, in starring order."""
    return [e["key"] for e in load()]


def count():
    return len(load())


def is_fav(key):
    return key in keys()


def toggle(key, name=None):
    """Star or unstar one tool. Returns (is_now_starred, message_or_None).

    The message is the one thing the caller has to show: FULL when a star past
    MAX was refused. The file is written immediately rather than on window
    close: it is a few hundred bytes, and a Revit that dies with the window
    open should not lose what was just starred.
    """
    # Re-read before every write: two Revit sessions on one machine share the
    # file, and a stale cache here would overwrite a star the other one saved.
    state = _load(force=True)
    favs = state["favs"]
    kept = [e for e in favs if e["key"] != key]
    if len(kept) < len(favs):
        state["favs"] = kept
        _save()
        return False, None
    if len(favs) >= MAX:
        return False, FULL
    kept.append({"key": key, "name": name or key})
    state["favs"] = kept
    _save()
    return True, None


def clear():
    """Unstar everything at once. Returns how many stars went.

    Requested 2026-09-05, after the first weekend with the bar: a Clear
    favorites button. The preset on the bar is left as it is: clearing the
    stars while a group is up should not also drop the group.
    """
    state = _load(force=True)
    gone = len(state["favs"])
    state["favs"] = []
    _save()
    return gone


# -- the presets ---------------------------------------------------------------

def groups(source=GROUPS_JSON):
    """[(group name, [folder names], has a chip)], every group of lib/groups.json.

    The same file the All Magic Tools window draws its sections from, read here
    too so favbar (which runs at boot, with no window) can put a group on the
    bar.
    A group with "chip": false in the file draws its section in the window but
    offers no preset: one that already has a door of its own on the ribbon
    (decided 2026-09-08: no preset for those, because they always have their
    own window on the ribbon).
    Unreadable means no groups, and then only STANDARD and MINE exist.
    """
    try:
        handle = io.open(source, encoding="utf-8")
        try:
            data = json.load(handle)
        finally:
            handle.close()
        return [(e["group"], list(e["tools"]), bool(e.get("chip", True)))
                for e in data]
    except Exception:
        return []


def presets(source=GROUPS_JSON):
    """[(group name, [folder names])], the groups that are offered as chips."""
    return [(name, tools) for name, tools, chip in groups(source) if chip]


def bar():
    """STANDARD, MINE, or the name of the group or set that is on the bar."""
    return _load()["bar"]


def set_bar(name):
    """Put STANDARD, the stars (MINE), one group or one user set on the bar.

    Unknown name -> MINE. A user set shadows a group of the same name, which
    cannot happen through save_set (it refuses those names) but can through a
    groups.json that gains a group after the set was saved.
    """
    state = _load(force=True)
    if not PRESETS or (name not in BUILTIN and name not in dict(user_sets())
                       and name not in dict(presets())):
        name = MINE
    state["bar"] = name
    _save()
    return name


def bar_keys():
    """The folder names the bar shows right now: the active preset's tools.

    A group that is on the bar but no longer in groups.json (a rename between
    two deploys), or a set that was deleted from another window, falls back
    to the stars, so the bar never goes empty for it.
    """
    active = bar()
    if active == STANDARD:
        return list(STANDARD_TOOLS)
    if active != MINE:
        for name, tools in user_sets() + presets():
            if name == active:
                return list(tools)
    return keys()


# -- user sets ---------------------------------------------------------------

EXPORT_KEY = "magic_tools_sets"
NAME_MAX = 40


def user_sets():
    """[(set name, [folder names])], in the order they were saved."""
    return [(s["name"], list(s["tools"])) for s in _load()["sets"]]


def _reserved(name):
    # Every group name, chip or not: a user set with a group's name would read
    # as that group in the window's headers.
    return (name in BUILTIN or name.lower() in BUILTIN
            or name in [g for g, _, _ in groups()])


def save_set(name, tools):
    """Save (or overwrite) one set. Returns (clean name, None) or (None, why).

    The tools come from the caller, which passes bar_keys(): "current set" is
    whatever is on the ribbon right now, stars or a group, so a user can also
    take a built-in group as the start of their own.
    """
    name = (name or u"").strip()[:NAME_MAX]
    if not name:
        return None, u"A set needs a name."
    if _reserved(name):
        return None, (u"\u201c%s\u201d is a built-in group; pick another name."
                      % name)
    tools = _clean_sets([{"name": name, "tools": list(tools or [])}])[0]["tools"]
    if not tools:
        return None, u"Nothing on the ribbon to save."
    state = _load(force=True)
    kept = [s for s in state["sets"] if s["name"] != name]
    kept.append({"name": name, "tools": list(tools)})
    state["sets"] = kept
    _save()
    return name, None


def delete_set(name):
    """Drop one set. The bar goes back to the stars if it was showing it."""
    state = _load(force=True)
    before = len(state["sets"])
    state["sets"] = [s for s in state["sets"] if s["name"] != name]
    if state["bar"] == name:
        state["bar"] = MINE
    _save()
    return len(state["sets"]) < before


def export_sets(target):
    """Write every user set to `target` (JSON). Returns how many. Raises."""
    sets = _load(force=True)["sets"]
    _write_json(target, {EXPORT_KEY: VERSION, "sets": sets})
    return len(sets)


def import_sets(source):
    """Merge the sets of an exported file. Returns (added, updated, skipped).

    Same name -> the incoming tools replace ours (that is what sharing a
    newer version of a set means). A name that is a built-in group here is
    skipped and counted. Raises ValueError on a file with no sets in it.
    """
    blob = _read_json(source)
    raw = blob.get("sets") if isinstance(blob, dict) else blob
    incoming = _clean_sets(raw)
    if not incoming:
        raise ValueError("no sets in that file")
    state = _load(force=True)
    by_name = dict((s["name"], s) for s in state["sets"])
    added = updated = skipped = 0
    for entry in incoming:
        if _reserved(entry["name"]):
            skipped += 1
        elif entry["name"] in by_name:
            by_name[entry["name"]]["tools"] = entry["tools"]
            updated += 1
        else:
            state["sets"].append(entry)
            by_name[entry["name"]] = entry
            added += 1
    _save()
    return added, updated, skipped


# -- the window ----------------------------------------------------------------

def window_place(title):
    """(left, top) the window called `title` was last closed at, or None."""
    place = _load()["windows"].get(title)
    try:
        return float(place["left"]), float(place["top"])
    except Exception:
        return None


def set_window_place(title, left, top):
    state = _load(force=True)
    state["windows"][title] = {"left": int(round(left)), "top": int(round(top))}
    _save()
