# -*- coding: utf-8 -*-
"""Read text, leaders and hatches out of an ASCII DXF.

Pure Python, no CAD and no Revit dependency: this is the half of Clean
Explode CAD that can be exercised outside Revit. Revit's API hands an
imported DWG over as bare geometry, with no text, no leader semantics and
no way to tell a hatch's lines from any other line; the DXF of the same
drawing carries all three, so the tool reads it here and maps the result
onto the import in the model (see the tool's script.py).

Everything is returned as plain dicts and tuples in the drawing's own
units; header_unit_scale() and the placement search in the tool decide how
many feet one drawing unit is.
"""

import itertools
import math
import re

# How far from a note a leader may land, in multiples of the text height,
# before it is treated as belonging to something else. Lives here because
# attach_leaders() is the one that measures it; the tool imports it back.
LEADER_REACH = 8.0



_TEXT_TYPES = ("TEXT", "MTEXT", "ATTRIB", "ATTDEF")

# $INSUNITS -> feet per drawing unit (0 and anything unlisted is unitless)
INSUNITS_TO_FEET = {
    1: 1.0 / 12.0,                  # inches
    2: 1.0,                         # feet
    3: 5280.0,                      # miles
    4: 1.0 / 304.8,                 # millimeters
    5: 1.0 / 30.48,                 # centimeters
    6: 1.0 / 0.3048,                # meters
    7: 1000.0 / 0.3048,             # kilometers
    8: 1.0e-6 / 12.0,               # microinches
    9: 0.001 / 12.0,                # mils
    10: 3.0,                        # yards
    13: 1.0e-6 / 0.3048,            # microns
    14: 0.1 / 0.3048,               # decimeters
    15: 10.0 / 0.3048,              # decameters
    16: 100.0 / 0.3048,             # hectometers
}

# Candidate unit scales tried when the header does not settle it, as
# (feet per drawing unit, label).
UNIT_CANDIDATES = (
    (1.0, "feet"),
    (1.0 / 12.0, "inches"),
    (1.0 / 304.8, "millimeters"),
    (1.0 / 30.48, "centimeters"),
    (1.0 / 0.3048, "meters"),
)


class _Frame(object):
    """Where a block's contents end up: scale, rotation and offset.

    Text inside a block is written in the block's own coordinates, so it only
    lands correctly once the INSERT that places the block has been applied.
    """

    def __init__(self, offset=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0),
                 rotation=0.0, base=(0.0, 0.0, 0.0), parent=None):
        self.offset = offset
        self.scale = scale
        self.rotation = rotation
        self.base = base
        self.parent = parent

    def apply(self, point):
        x = (point[0] - self.base[0]) * self.scale[0]
        y = (point[1] - self.base[1]) * self.scale[1]
        z = (point[2] - self.base[2]) * self.scale[2]
        angle = math.radians(self.rotation)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        placed = (self.offset[0] + x * cos_a - y * sin_a,
                  self.offset[1] + x * sin_a + y * cos_a,
                  self.offset[2] + z)
        return self.parent.apply(placed) if self.parent else placed

    @property
    def text_scale(self):
        own = abs(self.scale[1]) or 1.0
        return own * (self.parent.text_scale if self.parent else 1.0)

    @property
    def text_rotation(self):
        return self.rotation + (self.parent.text_rotation if self.parent else 0)


_IDENTITY_FRAME = _Frame()


def _dxf_pairs(dxf_text):
    """Group-code/value pairs, tolerating stray or unpaired lines.

    A single odd line used to make the whole read fail, which silently cost
    every text note in the drawing.
    """
    lines = dxf_text.splitlines()
    pairs = []
    index = 0
    total = len(lines)
    while index + 1 < total:
        try:
            code = int(lines[index].strip())
        except ValueError:
            index += 1          # stray line: resync on the next one
            continue
        pairs.append((code, lines[index + 1]))
        index += 2
    return pairs


def _group(groups, code, default=None):
    for group_code, value in groups:
        if group_code == code:
            return value
    return default


def _number(groups, code, default=0.0):
    value = _group(groups, code)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(groups, code, default=0):
    value = _group(groups, code)
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _entity_point(groups, base_code):
    return (_number(groups, base_code),
            _number(groups, base_code + 10),
            _number(groups, base_code + 20))


try:
    _unichr = unichr            # IronPython 2.7
except NameError:               # CPython 3
    _unichr = chr

# \U+00B0: a character written by its code point, in MTEXT and in old TEXT.
_UNICODE_ESCAPE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")

# AutoCAD's own character codes, used in plain TEXT as well as MTEXT. The
# diameter sign is the capital O with a stroke (U+00D8), as AutoCAD draws it.
SPECIAL_CODES = (("%%d", u"\u00b0"), ("%%D", u"\u00b0"),
                 ("%%p", u"\u00b1"), ("%%P", u"\u00b1"),
                 ("%%c", u"\u00d8"), ("%%C", u"\u00d8"),
                 ("%%%", "%"))

# Codes that carry an argument up to a ';' - font, colour, height, width,
# tracking, obliquing, alignment and paragraph settings.
_MTEXT_ARGUMENT_CODES = "fFcCHWTQAMpP"
# Codes that just switch something on or off and carry nothing.
_MTEXT_TOGGLE_CODES = "LlOoKkNX"


def decode_mtext(text):
    """Plain text out of an MTEXT string.

    MTEXT carries its formatting inline - {\\fArial|b0|i0|c0|p18;NOTE} is the
    single word NOTE in Arial. Left as-is, every note in a detail arrives
    with its own formatting codes printed in front of it.
    """
    if not text:
        return text
    out = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]

        if char == "\\" and index + 1 < length:
            code = text[index + 1]
            if code in "\\{}":                  # an escaped literal
                out.append(code)
                index += 2
            elif code == "P":                   # new paragraph
                out.append("\n")
                index += 2
            elif code == "~":                   # non-breaking space
                out.append(" ")
                index += 2
            elif code == "U" and _UNICODE_ESCAPE.match(text, index):
                # \U+00B0 is one character written by its code point
                out.append(_unichr(int(text[index + 3:index + 7], 16)))
                index += 7
            elif code == "S":                   # stacked fraction
                stop = text.find(";", index)
                body = text[index + 2:stop if stop != -1 else length]
                for separator in ("^", "/", "#"):
                    if separator in body:
                        upper, lower = body.split(separator, 1)
                        body = (upper + "/" + lower) if lower else upper
                        break
                out.append(body.strip())
                index = (stop + 1) if stop != -1 else length
            elif code in _MTEXT_ARGUMENT_CODES:  # runs up to its ';'
                stop = text.find(";", index)
                index = (stop + 1) if stop != -1 else length
            elif code in _MTEXT_TOGGLE_CODES:
                index += 2
            else:
                index += 2                      # unknown code: drop it
            continue

        if char in "{}":                        # formatting group braces
            index += 1
            continue

        out.append(char)
        index += 1

    return "".join(out)


def decode_special_codes(text, unicode_escapes=True):
    """%%d, %%p and %%c as the degree, plus/minus and diameter signs, and
    the backslash-U-plus escapes as the character they name."""
    if not text:
        return text
    if unicode_escapes and "\\U+" in text:
        text = _UNICODE_ESCAPE.sub(
            lambda found: _unichr(int(found.group(1), 16)), text)
    if "%%" not in text:
        return text
    for code, glyph in SPECIAL_CODES:
        text = text.replace(code, glyph)
    return text


def _text_hidden(entity_type, groups, depth):
    """Text that is in the DXF but never shows on the drawing.

    An invisible attribute (flag 1) is hidden on purpose. A block's ATTDEF is
    only the template for the value: the INSERT carries the real value as an
    ATTRIB of its own, so reading the template as well prints the default
    ("00") on top of it. A constant attribute (flag 2) has no ATTRIB, so its
    ATTDEF is the one place the text lives.
    """
    if entity_type not in ("ATTRIB", "ATTDEF"):
        return False
    flags = _int(groups, 70, 0)
    if flags & 1:
        return True
    return entity_type == "ATTDEF" and depth > 0 and not flags & 2


def _text_record(entity_type, groups, frame=None):
    frame = frame or _IDENTITY_FRAME
    text = _group(groups, 1, "")
    if entity_type == "MTEXT":
        text = "".join(value for code, value in groups if code == 3) + text
        text = decode_mtext(text)       # reads the U+XXXX escapes itself
    text = decode_special_codes(text, entity_type != "MTEXT")

    point = _entity_point(groups, 10)
    # Single-line TEXT that is not left/baseline aligned is positioned by its
    # second alignment point (11/21/31), not by 10/20/30.
    if entity_type in ("TEXT", "ATTRIB", "ATTDEF"):
        aligned = _number(groups, 72, 0.0) or _number(groups, 73, 0.0)
        if aligned and _group(groups, 11) is not None:
            point = _entity_point(groups, 11)

    width = _group(groups, 41)
    try:
        width = float(width) if width is not None else None
    except (TypeError, ValueError):
        width = None

    return {
        "entity_type": entity_type,
        "text": text,
        "insertion_point": frame.apply(point),
        "height": _number(groups, 40, 0.0) * frame.text_scale,
        "rotation": _number(groups, 50, 0.0) + frame.text_rotation,
        "width": width,
        "layer": _group(groups, 8, "0"),
        "style": _group(groups, 7),
        "handle": (_group(groups, 5) or "").strip(),
        "leaders": [],
    }


def read_dxf(dxf_text):
    """Return (header, entities, blocks) read from ASCII DXF content.

    header  : {"$INSUNITS": [(code, value), ...], ...}
    entities: [{"type": "TEXT", "groups": [(code, value), ...]}, ...]
    blocks  : {"NAME": {"base": (x, y, z), "entities": [...]}, ...}

    Paper-space entities (code 67 = 1) are left out: Revit imports the model
    space only. A TEXT, ATTRIB, INSERT, DIMENSION or HATCH on a frozen or
    switched-off layer is flagged "hidden" (LAYER table: flag 1 is frozen, a
    negative colour is off), and _expand(), collect_hatches() and
    count_hatches() skip the flagged ones.
    """
    header = {}
    entities = []
    blocks = {}
    layers = {}                  # UPPER NAME -> (flags, colour)
    dimstyles = {}               # UPPER NAME -> text height in drawing units

    section = None
    awaiting_section_name = False
    header_var = None
    current = None
    block_name = None

    def store(entity):
        if entity is None:
            return
        if entity["type"] == "LAYER":
            name = (_group(entity["groups"], 2) or "").strip().upper()
            if name:
                layers[name] = (_int(entity["groups"], 70, 0),
                                _number(entity["groups"], 62, 0.0))
            return
        if entity["type"] == "DIMSTYLE":
            # DIMTXT (140) is the text height, DIMSCALE (40) multiplies it
            name = (_group(entity["groups"], 2) or "").strip().upper()
            height = (_number(entity["groups"], 140, 0.0)
                      * (_number(entity["groups"], 40, 1.0) or 1.0))
            if name and height > 0.0:
                dimstyles[name] = height
            return
        if entity["type"] == "BLOCK":
            name = _group(entity["groups"], 2) or _group(entity["groups"], 3)
            if name:
                blocks[name.strip()] = {
                    "base": _entity_point(entity["groups"], 10),
                    "entities": [],
                }
            return
        if _int(entity["groups"], 67, 0) == 1:
            return               # paper space
        if block_name and block_name in blocks:
            blocks[block_name]["entities"].append(entity)
        elif section == "ENTITIES":
            entities.append(entity)

    for code, value in _dxf_pairs(dxf_text):
        if code == 0:
            kind = value.strip()
            store(current)
            current = None

            if kind == "SECTION":
                awaiting_section_name = True
                section = None
                header_var = None
                continue
            if kind == "ENDSEC":
                section = None
                block_name = None
                continue
            if kind == "EOF":
                break
            if kind == "BLOCK":
                current = {"type": "BLOCK", "groups": []}
                block_name = None
                continue
            if kind == "ENDBLK":
                block_name = None
                continue
            if section in ("ENTITIES", "BLOCKS") \
                    or (section == "TABLES" and kind in ("LAYER", "DIMSTYLE")):
                current = {"type": kind, "groups": []}
            continue

        if awaiting_section_name and code == 2:
            section = value.strip()
            awaiting_section_name = False
            continue

        if section == "HEADER":
            if code == 9:
                header_var = value.strip()
                header.setdefault(header_var, [])
            elif header_var is not None:
                header[header_var].append((code, value))
            continue

        if current is not None:
            current["groups"].append((code, value))
            if current["type"] == "BLOCK" and code == 2:
                # The block's own entities follow until ENDBLK.
                name = value.strip()
                blocks.setdefault(name, {"base": (0.0, 0.0, 0.0),
                                         "entities": []})
                block_name = name

    store(current)

    hidden = set(name for name, (flags, colour) in layers.items()
                 if flags & 1 or colour < 0)
    _flag_entities(entities, hidden, dimstyles)
    for block in blocks.values():
        _flag_entities(block["entities"], hidden, dimstyles)
    return header, entities, blocks


def _flag_entities(entities, hidden, dimstyles):
    """Mark what sits on a frozen or switched-off layer, and give each
    dimension the text height its dimension style prescribes."""
    for entity in entities:
        kind = entity["type"]
        if kind not in _TEXT_TYPES and kind not in ("INSERT", "HATCH") \
                and kind not in _DIMENSION_TYPES:
            continue
        groups = entity["groups"]
        layer = (_group(groups, 8, "0") or "0").strip().upper()
        if layer in hidden:
            entity["hidden"] = True
        if kind in _DIMENSION_TYPES:
            style = (_group(groups, 3) or "").strip().upper()
            if style in dimstyles:
                entity["dim_height"] = dimstyles[style]


# --- DIMENSION entities -----------------------------------------------------
# A dimension's text is not on the DIMENSION: AutoCAD draws the whole thing
# (lines, arrows and the number) into an anonymous block, "*D12", that the
# DIMENSION names in group 2. The geometry of that block is already written
# in drawing coordinates (AutoCAD writes it as if inserted at the origin,
# unscaled and unrotated), and groups 10/41 on a DIMENSION are not an
# insertion point or a scale (10 is the definition point, 41 a line-spacing
# factor), so the block is read through the frame the DIMENSION itself sits
# in, with no offset of its own. Only the INSERT of a block that holds a
# DIMENSION moves it.

_DIMENSION_TYPES = ("DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION")


def _frame_signature(frame):
    """What a frame does, as numbers, so two ways of reaching the same
    block at the same place compare equal."""
    probes = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    return tuple(round(value, 6) for probe in probes
                 for value in frame.apply(probe)[:2])


def _plain_number(value):
    """1250.0 -> "1250", 12.5 -> "12.5": the measurement without a format."""
    text = ("%.4f" % value).rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _dimension_text(groups):
    """The text a DIMENSION shows, worked out from its own groups.

    Group 1 is the text the user typed: empty or "<>" means "show the
    measurement", a single space means "show nothing", anything else is
    shown as typed, with "<>" standing for the measurement. The measurement
    is group 42 (an angle in radians), written plainly.
    """
    typed = _group(groups, 1, "")
    if typed == " ":
        return None
    kind = _int(groups, 70, 0) & 7
    measured = None
    raw = _group(groups, 42)
    if raw is not None:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = None
        if value is not None:
            if kind in (2, 5):                       # angular
                measured = _plain_number(math.degrees(value)) + u"°"
            else:
                measured = _plain_number(value)
                if kind == 3:                        # diameter
                    measured = u"Ø" + measured
                elif kind == 4:                      # radius
                    measured = "R" + measured
    if typed.strip() in ("", "<>"):
        text = measured
    elif "<>" in typed:
        text = typed.replace("<>", measured or "")
    else:
        text = typed
    if not text or not text.strip():
        return None
    return decode_special_codes(decode_mtext(text))


def _dimension_rotation(groups):
    """Text angle in degrees, turned so it never reads upside down."""
    kind = _int(groups, 70, 0) & 7
    angle = 0.0
    if kind == 0:
        angle = _number(groups, 50, 0.0)
    elif kind == 1:
        angle = math.degrees(math.atan2(
            _number(groups, 24, 0.0) - _number(groups, 23, 0.0),
            _number(groups, 14, 0.0) - _number(groups, 13, 0.0)))
    angle = math.fmod(angle + _number(groups, 53, 0.0), 360.0)
    if angle < 0.0:
        angle += 360.0
    if 90.0 < angle <= 270.0:
        angle -= 180.0
    elif angle > 270.0:
        angle -= 360.0
    return angle


def _dimension_record(entity, frame):
    """A note for a dimension whose own block gave no text."""
    groups = entity["groups"]
    text = _dimension_text(groups)
    if text is None or _group(groups, 11) is None:
        return None
    return {
        "entity_type": "DIMENSION",
        "text": text,
        "insertion_point": frame.apply(_entity_point(groups, 11)),
        "height": entity.get("dim_height", 0.0) * frame.text_scale,
        "rotation": _dimension_rotation(groups) + frame.text_rotation,
        "width": None,
        "layer": _group(groups, 8, "0"),
        "style": None,
        "handle": (_group(groups, 5) or "").strip(),
        "leaders": [],
    }


def _mark_dimension(records, start, key):
    """Tag the records a dimension produced: they are its text, and `key` says
    which dimension, so what was made can be compared with
    dimension_keys() one dimension at a time (a block with two texts is still
    one dimension)."""
    for record in records[start:]:
        record.setdefault("source", "DIMENSION")
        record.setdefault("dimension", key)


def _dimension_key(entity, frame):
    """What identifies one dimension in the drawing: its anonymous block and
    where it sits, so reaching the same *D block twice (the DIMENSION, and an
    INSERT of it at the same place) is one dimension. A dimension with no
    block of its own is told apart by the entity itself."""
    name = (_group(entity["groups"], 2) or "").strip()
    if name:
        return (name.upper(), _frame_signature(frame))
    return ("#%d" % id(entity), _frame_signature(frame))


def _dimension_suppressed(entity):
    """A dimension whose text was switched off: a single space as its text."""
    return _group(entity["groups"], 1, "") == " "


def _insert_frame(groups, block, frame):
    """The frame the contents of a block are read through once the INSERT
    `groups` places it inside `frame`."""
    return _Frame(
        offset=_entity_point(groups, 10),
        scale=(_number(groups, 41, 1.0) or 1.0,
               _number(groups, 42, 1.0) or 1.0,
               _number(groups, 43, 1.0) or 1.0),
        rotation=_number(groups, 50, 0.0),
        base=block["base"],
        parent=frame if frame is not _IDENTITY_FRAME else None)


def _count_dimensions(entities, blocks, frame, depth, keys, seen):
    for entity in entities:
        if entity.get("hidden"):
            continue                 # frozen or switched-off layer
        etype = entity["type"]
        if etype in _DIMENSION_TYPES:
            if depth >= 6:
                continue
            key = _dimension_key(entity, frame)
            if key in seen:
                continue
            seen.add(key)
            if not _dimension_suppressed(entity):
                keys.append(key)
            continue
        if etype != "INSERT" or depth >= 6:
            continue
        name = _group(entity["groups"], 2)
        block = blocks.get(name.strip()) if name else None
        if not block:
            continue
        child = _insert_frame(entity["groups"], block, frame)
        if name.strip().upper().startswith("*D"):
            # an INSERT of a dimension's own block: one dimension if the
            # block shows any text at all
            key = (name.strip().upper(), _frame_signature(child))
            if key in seen:
                continue
            seen.add(key)
            trial = []
            _expand(block["entities"], blocks, child, depth + 1, trial)
            if trial:
                keys.append(key)
            continue
        _count_dimensions(block["entities"], blocks, child, depth + 1, keys,
                          seen)


def dimension_keys(entities, blocks):
    """One key per dimension the drawing shows, the same ones
    _expand() tags its dimension records with ("dimension").

    The criteria are the reader's own: DIMENSION, ARC_DIMENSION and
    LARGE_RADIAL_DIMENSION, blocks expanded through their INSERTs up to six
    deep, nothing on a frozen or switched-off layer or in paper space, a
    dimension whose text is suppressed (a single space) left out, and a
    dimension reached twice (its block and an INSERT of that block) counted
    once. Compare with the "dimension" of the records that became notes to
    know which dimensions' values did not come across.
    """
    keys = []
    _count_dimensions(entities, blocks, _IDENTITY_FRAME, 0, keys, set())
    return keys


def _expand_dimension(entity, blocks, frame, depth, records, seen):
    """The text of one DIMENSION, as ordinary text records marked
    "source": "DIMENSION" and with its "dimension" key.

    It comes from the TEXT/MTEXT of the dimension's own block; when that
    block is missing or has no text, from the text and measurement the
    DIMENSION carries. A dimension block that is also reached some other way
    (an INSERT of the same *D block at the same place) is emitted once.
    """
    groups = entity["groups"]
    name = (_group(groups, 2) or "").strip()
    key = _dimension_key(entity, frame)
    if name:
        if key in seen:
            return
        seen.add(key)
    start = len(records)
    block = blocks.get(name) if name else None
    if block:
        _expand(block["entities"], blocks, frame, depth + 1, records, seen)
    if len(records) == start:
        record = _dimension_record(entity, frame)
        if record is not None:
            records.append(record)
    _mark_dimension(records, start, key)


def _expand(entities, blocks, frame, depth, records, seen=None):
    """Append a text record for everything in `entities` that shows text.

    `seen` is what has already been expanded as a dimension block; leave it
    out on the outermost call.
    """
    if seen is None:
        seen = set()
    for entity in entities:
        etype = entity["type"]
        if etype in _TEXT_TYPES:
            if entity.get("hidden") \
                    or _text_hidden(etype, entity["groups"], depth):
                continue
            records.append(_text_record(etype, entity["groups"], frame))
            continue
        if etype in _DIMENSION_TYPES:
            if depth < 6 and not entity.get("hidden"):
                _expand_dimension(entity, blocks, frame, depth, records,
                                  seen)
            continue
        if etype != "INSERT" or depth >= 6 or entity.get("hidden"):
            continue
        name = _group(entity["groups"], 2)
        block = blocks.get(name.strip()) if name else None
        if not block:
            continue
        groups = entity["groups"]
        child = _Frame(
            offset=_entity_point(groups, 10),
            scale=(_number(groups, 41, 1.0) or 1.0,
                   _number(groups, 42, 1.0) or 1.0,
                   _number(groups, 43, 1.0) or 1.0),
            rotation=_number(groups, 50, 0.0),
            base=block["base"],
            parent=frame if frame is not _IDENTITY_FRAME else None)
        anonymous = name.strip().upper().startswith("*D")
        if anonymous:
            key = (name.strip().upper(), _frame_signature(child))
            if key in seen:
                continue
            seen.add(key)
        start = len(records)
        _expand(block["entities"], blocks, child, depth + 1, records, seen)
        if anonymous:
            _mark_dimension(records, start, key)


def parse_dxf_text(dxf_text):
    """Return dictionaries for every TEXT, MTEXT, ATTRIB and ATTDEF entity,
    and for the text of every DIMENSION.

    Input is ASCII DXF content as a string, not a file path. Entities in the
    ENTITIES section are read directly; entities inside blocks are reached
    through the INSERT entities that place them, with the insertion point,
    scale and rotation applied (nested blocks up to six deep).

    A DIMENSION's number is read from its anonymous *D block and arrives as
    an ordinary record with an extra key, "source": "DIMENSION"; with no
    block to read, the record is built from the DIMENSION's own text and
    measurement (entity_type "DIMENSION").

    Records contain entity_type, text, insertion_point (an x/y/z tuple),
    height, rotation (degrees), width, layer and style. Coordinates are in
    the drawing's own units - build_text_placement() maps them to the model.
    Text that is centre/right/middle aligned is positioned by its alignment
    point. Stray or unpaired lines are skipped instead of failing the read.
    """
    _, entities, blocks = read_dxf(dxf_text)
    records = []
    _expand(entities, blocks, _IDENTITY_FRAME, 0, records)
    return records


# --- HATCH entities ---------------------------------------------------------
# The Revit API hands over an imported hatch only as its drawn pattern lines,
# with no boundary at all. The DXF carries the real thing: the pattern name
# and every boundary path, islands included.
#
# These boundaries decide which linework gets thrown away, so every reader
# here errs the same way: anything it cannot represent faithfully makes the
# whole hatch unusable, and an unusable hatch simply converts as linework.

# Where the boundary data stops and the pattern definition begins.
_HATCH_AFTER_BOUNDARY = (75, 76, 52, 41, 77, 78, 47, 98, 450, 451, 452, 453,
                         460, 461, 462, 463, 470)
_HATCH_ARC_STEP = 6.0           # degrees per facet when flattening an arc


def _sweep_degrees(start_deg, end_deg, counter_clockwise):
    """How far an arc turns, or None when it does not turn at all.

    A boundary written as 0 to 360 is a whole circle whichever way round it
    is drawn - and a circular island read as a four-point speck would leave
    a hole unsubtracted, so the linework inside it would be deleted. Two
    equal angles, on the other hand, really are a degenerate edge and must
    stay one rather than being invented into a circle.
    """
    raw = end_deg - start_deg
    if abs(abs(raw) - 360.0) < 1e-9 or abs(raw) > 360.0:
        return 360.0
    sweep = raw if counter_clockwise else -raw
    sweep = math.fmod(sweep, 360.0)
    if sweep < 0.0:
        sweep += 360.0
    return None if sweep < 1e-9 else sweep


def _arc_ring(centre, radius, start_deg, end_deg, counter_clockwise):
    """A circular boundary edge as points."""
    if radius <= 0.0:
        return []
    sweep = _sweep_degrees(start_deg, end_deg, counter_clockwise)
    if sweep is None:
        return []
    steps = max(3, int(math.ceil(sweep / _HATCH_ARC_STEP)))
    points = []
    for step in range(steps + 1):
        angle = math.radians(
            start_deg + (sweep if counter_clockwise else -sweep)
            * step / float(steps))
        points.append((centre[0] + radius * math.cos(angle),
                       centre[1] + radius * math.sin(angle)))
    return points


def _ellipse_ring(centre, major, ratio, start_deg, end_deg,
                  counter_clockwise):
    """An elliptical boundary edge as points."""
    half_major = math.hypot(major[0], major[1])
    if half_major <= 0.0:
        return []
    sweep = _sweep_degrees(start_deg, end_deg, counter_clockwise)
    if sweep is None:
        return []
    half_minor = half_major * abs(ratio or 1.0)
    tilt = math.atan2(major[1], major[0])
    steps = max(3, int(math.ceil(sweep / _HATCH_ARC_STEP)))
    cos_t, sin_t = math.cos(tilt), math.sin(tilt)
    points = []
    for step in range(steps + 1):
        angle = math.radians(
            start_deg + (sweep if counter_clockwise else -sweep)
            * step / float(steps))
        x, y = half_major * math.cos(angle), half_minor * math.sin(angle)
        points.append((centre[0] + x * cos_t - y * sin_t,
                       centre[1] + x * sin_t + y * cos_t))
    return points


def _bulge_ring(start, end, bulge):
    """The arc a polyline bulge draws between two vertices.

    A bulge is how a polyline stores a rounded corner or a curved side. Read
    as a straight chord it would cover ground the hatch does not, and that
    ground is where linework would be deleted - so it is expanded properly.
    """
    chord = math.hypot(end[0] - start[0], end[1] - start[1])
    if abs(bulge) < 1e-9 or chord <= 0.0:
        return [start]
    included = 4.0 * math.atan(bulge)
    half = math.sin(included / 2.0)
    if abs(half) < 1e-12:
        return [start]
    radius = (chord / 2.0) / half
    ux, uy = (end[0] - start[0]) / chord, (end[1] - start[1]) / chord
    to_centre = radius * math.cos(included / 2.0)
    centre = ((start[0] + end[0]) / 2.0 - uy * to_centre,
              (start[1] + end[1]) / 2.0 + ux * to_centre)
    first = math.atan2(start[1] - centre[1], start[0] - centre[0])
    steps = max(2, int(math.ceil(abs(math.degrees(included))
                                 / _HATCH_ARC_STEP)))
    return [(centre[0] + abs(radius) * math.cos(first + included * s / steps),
             centre[1] + abs(radius) * math.sin(first + included * s / steps))
            for s in range(steps)]


def _polyline_ring(vertices, closed):
    """A polyline boundary path, with every bulge expanded."""
    if len(vertices) < 2:
        return [(x, y) for x, y, _ in vertices]
    ring = []
    last = len(vertices) if closed else len(vertices) - 1
    for index in range(last):
        start = vertices[index]
        end = vertices[(index + 1) % len(vertices)]
        ring.extend(_bulge_ring((start[0], start[1]), (end[0], end[1]),
                                start[2]))
    if not closed:
        ring.append((vertices[-1][0], vertices[-1][1]))
    return ring


def _hatch_polygons(groups):
    """Every boundary path of a DXF HATCH, as a ring of (x, y) points, or
    None when any path cannot be read faithfully.

    A path is written either as a polyline - a run of vertices, each able to
    bulge into an arc - or as a list of edges: lines, circular arcs,
    elliptical arcs and splines. Curved edges are flattened here, because
    all these rings are ever used for is telling what sits inside the hatch.

    A hatch with holes writes each island as a path of its own, so the rings
    come back as a set and an even/odd test reads the islands correctly.

    A spline edge is only trusted when the DXF carries the fit points the
    curve actually passes through. Its control points can sit well outside
    the curve, and a boundary that is too big would take real linework with
    it, so a spline without fit points makes the whole hatch unusable.
    """
    rings = []
    ring = None                  # points of an edge-built path
    vertices = None              # (x, y, bulge) of a polyline path
    closed = True
    reading = False
    polyline_path = True
    edge = None                  # kind of edge, None inside a polyline
    pending = {}                 # an arc or ellipse being read
    spline_fit = []              # points a spline really passes through
    edge_start = [0]             # where in `ring` the current edge began

    def flush():
        """Turn a finished curved edge into points."""
        if ring is None:
            pending.clear()
            return True
        if edge is not None and edge not in (1, 2, 3, 4):
            return False         # an edge shape we do not know: drop the hatch
        if edge == 1:
            # A line edge writes both its ends. One end is a truncated
            # boundary, and a boundary short of a side is one that would
            # delete whatever the missing side was keeping out.
            if len(ring) - edge_start[0] < 2:
                return False
        elif edge == 4:
            if len(spline_fit) < 2:
                return False     # a spline we cannot place: drop the hatch
            ring.extend(spline_fit)
        elif edge in (2, 3):
            centre, major = pending.get(10), pending.get(11)
            if not isinstance(centre, tuple):
                return False     # an edge missing its centre: drop the hatch
            turn = pending.get(73, 1.0) != 0.0
            if edge == 2:
                drawn = _arc_ring(centre, pending.get(40, 0.0),
                                  pending.get(50, 0.0),
                                  pending.get(51, 360.0), turn)
            elif isinstance(major, tuple):
                drawn = _ellipse_ring(centre, major, pending.get(40, 1.0),
                                      pending.get(50, 0.0),
                                      pending.get(51, 360.0), turn)
            else:
                drawn = []
            if not drawn:
                return False     # a curved edge we cannot draw: drop the hatch
            ring.extend(drawn)
        pending.clear()
        del spline_fit[:]
        edge_start[0] = len(ring) if ring is not None else 0
        return True

    def close_path():
        """Finish whichever kind of path was being read."""
        if vertices is not None:
            built = _polyline_ring(vertices, closed)
        else:
            built = ring
        if built and len(built) >= 3:
            rings.append(built)

    for code, value in groups:
        if not reading:
            if code == 91:
                reading = True
            continue
        if code in _HATCH_AFTER_BOUNDARY:
            break
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue

        if code == 92:
            if not flush():
                return None
            close_path()
            polyline_path = bool(int(number) & 2)
            ring = None if polyline_path else []
            vertices = [] if polyline_path else None
            edge_start[0] = 0
            closed = True
            edge = None
            pending.clear()
            del spline_fit[:]
            continue
        if ring is None and vertices is None:
            continue

        if polyline_path:
            if code == 73:
                closed = number != 0.0
            elif code == 10:
                pending[10] = number
            elif code == 20 and 10 in pending:
                vertices.append([pending.pop(10), number, 0.0])
            elif code == 42 and vertices:
                vertices[-1][2] = number
            continue

        if code == 72:
            if not flush():
                return None
            edge = int(number)
            pending.clear()
            continue
        if code in (10, 11):
            pending[code] = number
            continue
        if code in (20, 21):
            start = pending.pop(code - 10, None)
            if start is None:
                continue
            point = (start, number)
            if edge in (1, None):
                ring.append(point)            # a vertex, or a line's end
            elif edge == 4 and code == 21:
                spline_fit.append(point)      # a point the spline runs through
            elif edge in (2, 3):
                pending[code - 10] = point    # centre, or the major axis
            continue
        if edge in (2, 3) and code in (40, 50, 51, 73):
            pending[code] = number

    if not flush():
        return None
    close_path()
    return rings


# --- hatch pattern lines ----------------------------------------------------
# Revit 2025 does not hand over the stripes of an imported hatch, but the DXF
# carries the pattern itself, one definition per pattern line: 53 angle,
# 43/44 base point, 45/46 offset, 79 how many dash lengths, then the 49s.
# AutoCAD writes those values already scaled and rotated (codes 52 and 41,
# the hatch's own angle and scale, are only the record of how it got there),
# so they are in drawing units and are used as they stand.

# A wrong scale must not hang Revit. A hatch that needs more stripes than
# this is a pattern in the wrong scale (or a boundary far too big for it), not
# a drawing: the caller must not draw it, because half a hatch is false.
_HATCH_MAX_SEGMENTS = 3000
_HATCH_PATTERN_END = (47, 98, 450, 451, 452, 453, 460, 461, 462, 463, 470,
                      1001)


class _SegmentCap(Exception):
    """More stripes than _HATCH_MAX_SEGMENTS: generation stops there."""


def _hatch_pattern_lines(groups):
    """The pattern definition of a HATCH as
    [(angle_deg, (base_x, base_y), (offset_x, offset_y), [dash, ...]), ...].

    A dash is a length: positive draws, negative is a gap, 0 is a dot.
    Solid hatches and hatches that write no pattern give [].
    """
    lines = []
    current = None
    reading = False
    for code, value in groups:
        if not reading:
            if code == 78:                       # number of pattern lines
                reading = True
            continue
        if code in _HATCH_PATTERN_END:
            break
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if code == 53:
            current = [number, [0.0, 0.0], [0.0, 0.0], []]
            lines.append(current)
        elif current is None:
            continue
        elif code == 43:
            current[1][0] = number
        elif code == 44:
            current[1][1] = number
        elif code == 45:
            current[2][0] = number
        elif code == 46:
            current[2][1] = number
        elif code == 49:
            current[3].append(number)
    return [(angle, tuple(base), tuple(offset), dashes)
            for angle, base, offset, dashes in lines]


def _hatch_segments(rings, pattern_lines, limit=_HATCH_MAX_SEGMENTS):
    """The stripes of a hatch pattern as ((x1, y1), (x2, y2)) segments, and
    whether the cap stopped them.

    Every pattern line is a family of parallel lines: the one through the
    base point and its copies, each moved by the offset. Each is cut by the
    boundary with the even/odd rule across all the rings (so the islands
    stay empty) and then dashed. Everything is worked in the rings' own
    coordinates; the caller places the result.
    """
    segments = []
    rings = [ring for ring in rings if len(ring) >= 3]
    if not rings or not pattern_lines:
        return segments, False

    def add(ax, ay, bx, by):
        if len(segments) >= limit:
            raise _SegmentCap()
        segments.append(((ax, ay), (bx, by)))

    lines_done = [0]
    try:
        for angle, base, offset, dashes in pattern_lines:
            turn = math.radians(angle)
            dx, dy = math.cos(turn), math.sin(turn)
            nx, ny = -dy, dx
            step = offset[0] * nx + offset[1] * ny    # between two stripes
            shift = offset[0] * dx + offset[1] * dy   # along the stripe
            if abs(step) <= 1e-12:
                continue
            cycle = sum(abs(dash) for dash in dashes)
            if dashes and (cycle <= 1e-12 or not any(d > 0 for d in dashes)):
                continue                  # only dots and gaps: nothing to draw

            edges = []
            for ring in rings:
                for index in range(len(ring)):
                    a = ring[index]
                    b = ring[(index + 1) % len(ring)]
                    edges.append((a[0] * nx + a[1] * ny, a[0] * dx + a[1] * dy,
                                  b[0] * nx + b[1] * ny, b[0] * dx + b[1] * dy))
            across = [edge[0] for edge in edges] + [edge[2] for edge in edges]
            base_n = base[0] * nx + base[1] * ny
            base_d = base[0] * dx + base[1] * dy
            first = (min(across) - base_n) / step
            last = (max(across) - base_n) / step
            k = int(math.ceil(min(first, last)))
            k_last = int(math.floor(max(first, last)))

            while k <= k_last:
                lines_done[0] += 1
                if lines_done[0] > limit:
                    raise _SegmentCap()
                level = base_n + k * step
                crossings = []
                for a_n, a_d, b_n, b_d in edges:
                    above_a, above_b = a_n - level, b_n - level
                    if (above_a > 0.0) != (above_b > 0.0):
                        crossings.append(
                            a_d + (b_d - a_d) * above_a / (above_a - above_b))
                crossings.sort()
                origin = base_d + k * shift   # where this stripe's dashes start
                for index in range(0, len(crossings) - 1, 2):
                    start, end = crossings[index], crossings[index + 1]
                    if end - start <= 1e-12:
                        continue
                    if not dashes:
                        pieces = ((start, end),)
                    else:
                        if (end - start) / cycle > limit:
                            raise _SegmentCap()
                        pieces = []
                        at = origin + cycle * math.floor((start - origin)
                                                         / cycle)
                        while at < end:
                            for dash in dashes:
                                length = abs(dash)
                                if dash > 0.0:
                                    lo, hi = max(at, start), min(at + length,
                                                                 end)
                                    if hi - lo > 1e-12:
                                        pieces.append((lo, hi))
                                at += length
                                if at >= end:
                                    break
                    for lo, hi in pieces:
                        add(level * nx + lo * dx, level * ny + lo * dy,
                            level * nx + hi * dx, level * ny + hi * dy)
                k += 1
    except _SegmentCap:
        return segments, True
    return segments, False


def _hatch_record(entity, frame, inherited_layer=None):
    """One hatch as {rings, layer, pattern, solid, segments,
    segments_capped}, placed by its block.

    A hatch drawn on layer 0 inside a block takes the layer of the INSERT
    that placed it, which is the layer Revit will import its lines under.

    `segments` are the stripes of the hatch pattern, ((x1, y1), (x2, y2)),
    in the same placed coordinates as `rings` and already clipped to the
    boundary (islands left empty) and dashed. A solid hatch has none. When a
    hatch would need more than _HATCH_MAX_SEGMENTS stripes - a pattern in the
    wrong scale - `segments_capped` is True and `segments` holds what was
    made before the cap stopped it; do not trust that as a full hatch.
    """
    groups = entity["groups"]
    polygons = _hatch_polygons(groups)
    if not polygons:
        return None
    rings = []
    for ring in polygons:
        placed = []
        for x, y in ring:
            moved = frame.apply((x, y, 0.0))
            placed.append((moved[0], moved[1]))
        if len(placed) >= 3:
            rings.append(placed)
    if not rings:
        return None
    layer = (_group(groups, 8, "0") or "0").strip()
    if layer in ("0", "") and inherited_layer:
        layer = inherited_layer
    solid = _number(groups, 70, 0.0) != 0
    segments, capped = [], False
    if not solid:
        try:
            segments, capped = _hatch_segments(
                polygons, _hatch_pattern_lines(groups))
        except Exception:
            segments, capped = [], False      # the boundary is still good
        if frame is not _IDENTITY_FRAME:
            placed_segments = []
            for start, end in segments:
                a = frame.apply((start[0], start[1], 0.0))
                b = frame.apply((end[0], end[1], 0.0))
                placed_segments.append(((a[0], a[1]), (b[0], b[1])))
            segments = placed_segments
    return {"rings": rings, "layer": layer,
            "pattern": (_group(groups, 2, "") or "").strip(),
            "solid": solid, "segments": segments,
            "segments_capped": capped}


def collect_hatches(entities, blocks, frame=None, depth=0, found=None,
                    layer=None):
    """Every HATCH in the drawing, blocks expanded through their INSERTs."""
    frame = frame or _IDENTITY_FRAME
    if found is None:
        found = []
    for entity in entities:
        if entity.get("hidden"):
            continue                 # frozen or switched-off layer
        if entity["type"] == "HATCH":
            try:
                record = _hatch_record(entity, frame, layer)
            except Exception:
                record = None
            if record is not None:
                found.append(record)
            continue
        if entity["type"] != "INSERT" or depth >= 6:
            continue
        name = _group(entity["groups"], 2)
        block = blocks.get(name.strip()) if name else None
        if not block:
            continue
        groups = entity["groups"]
        child = _Frame(
            offset=_entity_point(groups, 10),
            scale=(_number(groups, 41, 1.0) or 1.0,
                   _number(groups, 42, 1.0) or 1.0,
                   _number(groups, 43, 1.0) or 1.0),
            rotation=_number(groups, 50, 0.0),
            base=block["base"],
            parent=frame if frame is not _IDENTITY_FRAME else None)
        own = (_group(groups, 8, "") or "").strip()
        collect_hatches(block["entities"], blocks, child, depth + 1, found,
                        own if own not in ("0", "") else layer)
    return found


def count_hatches(entities, blocks, depth=0):
    """How many HATCH entities the drawing shows: the same walk, the same
    skips (paper space, frozen or switched-off layers, blocks up to six deep)
    as collect_hatches(), so the difference between this and what
    collect_hatches() returned is exactly the hatches that could not be
    read."""
    total = 0
    for entity in entities:
        if entity.get("hidden"):
            continue
        if entity["type"] == "HATCH":
            total += 1
            continue
        if entity["type"] != "INSERT" or depth >= 6:
            continue
        name = _group(entity["groups"], 2)
        block = blocks.get(name.strip()) if name else None
        if block:
            total += count_hatches(block["entities"], blocks, depth + 1)
    return total


# --- LEADER / MULTILEADER ---------------------------------------------------
# An arrow drawn as a CAD leader should become a real Revit leader on the
# text it points from, not a pile of little lines and a filled triangle.

MLEADER_LINE_START = "LEADER_LINE{"


def _leader_points(groups):
    """The vertices of a LEADER entity, in drawing units."""
    points = []
    pending_x = None
    for code, value in groups:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if code == 10:
            pending_x = number
        elif code == 20 and pending_x is not None:
            points.append((pending_x, number))
            pending_x = None
    return points


def _mleader_lines(groups):
    """The leader lines of a MULTILEADER, one polyline per LEADER_LINE, in
    drawing units.

    A multileader carries a lot of other coordinates - the text position,
    the landing, the dogleg - so only the points inside its LEADER_LINE
    blocks are taken. A multileader with several arrows has one LEADER_LINE
    per arrow, and each is its own line: flattening them into one list would
    zigzag from one tip to the next. The most common multileader, a single
    segment, has just one point in its line (the arrow tip): its other end is
    the connection point, written in the LEADER{ block that holds the line
    (codes 10/20 before the first LEADER_LINE{). That point goes last on
    every line of that block, so each arrow ends where the text hangs and a
    one-segment multileader still has the two points a leader needs.
    """
    lines = []                   # every finished polyline
    branches = []                # the polylines of the LEADER{ being read
    branch = None                # the LEADER_LINE{ being read
    inside = False               # inside a LEADER_LINE{ block
    in_leader = False            # inside a LEADER{ block
    pending_x = None
    connection = None            # the LEADER{ block's connection point

    def close_leader():
        for line in branches:
            if not line:
                continue
            if connection is not None:
                line.append(connection)
            lines.append(line)
        del branches[:]

    for code, value in groups:
        text = value.strip() if hasattr(value, "strip") else value
        if code == 302 and text == "LEADER{":
            close_leader()       # a LEADER{ the file never closed
            in_leader, inside = True, False
            pending_x, connection = None, None
            continue
        if code == 303:
            close_leader()
            in_leader, inside = False, False
            pending_x, connection = None, None
            continue
        if code == 304 and text == MLEADER_LINE_START:
            inside = True
            pending_x = None
            branch = []
            branches.append(branch)
            continue
        if code == 305:
            inside = False
            pending_x = None
            continue
        if not inside and not in_leader:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if code == 10:
            pending_x = number
        elif code == 20 and pending_x is not None:
            if inside:
                branch.append((pending_x, number))
            else:
                connection = (pending_x, number)
            pending_x = None
    close_leader()
    return lines


def _mleader_text(groups):
    """The text a multileader carries, which lives inside the entity."""
    markers = ("CONTEXT_DATA{", "LEADER{", "LEADER_LINE{", "}")
    for code, value in groups:
        if code != 304:
            continue
        text = value.strip()
        if text and text not in markers and not text.endswith("{"):
            return decode_mtext(value)
    return None


_GROUP_IDS = itertools.count(1)


def _leader_records(entity, frame):
    """The leaders of one entity, each as {points, layer, arrow}, placed by
    its block frame.

    A LEADER is one line. A MULTILEADER is one record per arrow: every
    record carries the same text, and the same "group" id so that
    leader_text_records() makes one note out of all of them (the arrows of a
    multileader hang from a single text).
    """
    groups = entity["groups"]
    if entity["type"] == "LEADER":
        lines = [_leader_points(groups)]
        arrow = _number(groups, 71, 1.0) != 0
        text, group = None, None
    else:
        lines = _mleader_lines(groups)
        arrow = True
        text, group = _mleader_text(groups), next(_GROUP_IDS)
    made = []
    for points in lines:
        if len(points) < 2:
            continue
        placed = []
        for point in points:
            moved = frame.apply((point[0], point[1], 0.0))
            placed.append((moved[0], moved[1]))
        # drop repeats so a two-point leader is really two points
        cleaned = [placed[0]]
        for point in placed[1:]:
            if abs(point[0] - cleaned[-1][0]) > 1e-9 \
                    or abs(point[1] - cleaned[-1][1]) > 1e-9:
                cleaned.append(point)
        if len(cleaned) < 2:
            continue
        made.append({"points": cleaned, "layer": _group(groups, 8, "0"),
                     "arrow": arrow,
                     "annotation": (_group(groups, 340) or "").strip(),
                     "text": text, "group": group,
                     "height": _number(groups, 40, 0.0)})
    return made


def collect_leaders(entities, blocks, frame=None, depth=0, found=None):
    """Every LEADER and MULTILEADER, blocks expanded through their INSERTs."""
    frame = frame or _IDENTITY_FRAME
    if found is None:
        found = []
    for entity in entities:
        if entity["type"] in ("LEADER", "MULTILEADER", "MLEADER"):
            found.extend(_leader_records(entity, frame))
            continue
        if entity["type"] != "INSERT" or depth >= 6:
            continue
        name = _group(entity["groups"], 2)
        block = blocks.get(name.strip()) if name else None
        if not block:
            continue
        groups = entity["groups"]
        child = _Frame(
            offset=_entity_point(groups, 10),
            scale=(_number(groups, 41, 1.0) or 1.0,
                   _number(groups, 42, 1.0) or 1.0,
                   _number(groups, 43, 1.0) or 1.0),
            rotation=_number(groups, 50, 0.0),
            base=block["base"],
            parent=frame if frame is not _IDENTITY_FRAME else None)
        collect_leaders(block["entities"], blocks, child, depth + 1, found)
    return found


def attach_leaders(records, leaders):
    """Give every text record the leaders that point away from it.

    The DXF says outright which annotation a classic LEADER belongs to, so
    that link is used first. Otherwise the leader goes to the nearest text,
    but only within reach of it, so an arrow never jumps to a note on the
    other side of the sheet. A multileader carries its own text, so it
    arrives as a note of its own and is never attached to another one.
    """
    by_handle = {}
    for record in records:
        record["leaders"] = []
        if record.get("handle"):
            by_handle[record["handle"]] = record

    attached, orphans = 0, []
    for leader in leaders:
        if (leader.get("text") or "").strip():
            # A multileader that carries its own text is a note of its own
            # (leader_text_records): its arrow belongs to that text, not to
            # whatever note happens to sit nearest.
            continue
        target = by_handle.get(leader.get("annotation") or "")
        if target is None:
            tail = leader["points"][-1]
            best, best_distance = None, None
            for record in records:
                x, y = record["insertion_point"][0], record["insertion_point"][1]
                distance = math.sqrt((tail[0] - x) ** 2 + (tail[1] - y) ** 2)
                if best_distance is None or distance < best_distance:
                    best, best_distance = record, distance
            if best is not None:
                reach = max(best.get("height", 0.0) * LEADER_REACH,
                            abs(best.get("width") or 0.0) * 1.2)
                if reach > 0 and best_distance <= reach:
                    target = best
        if target is None:
            orphans.append(leader)
            continue
        target["leaders"].append(leader)
        attached += 1
    return attached, orphans


def leader_text_records(leaders):
    """Notes for the multileaders that carry their own text.

    That text is written inside the multileader, so nothing else in the DXF
    reader ever sees it - without this it is simply lost. A multileader with
    several arrows is several leader records sharing one text and one
    "group": it makes ONE note, holding all of its arrows.
    """
    by_group = {}
    for leader in leaders:
        if leader.get("group") is not None:
            by_group.setdefault(leader["group"], []).append(leader)
    made = []
    done = set()
    for leader in leaders:
        text = (leader.get("text") or "").strip()
        if not text or leader.get("used"):
            continue
        group = leader.get("group")
        if group is not None:
            if group in done:
                continue
            done.add(group)
            arrows = [other for other in by_group[group]
                      if not other.get("used")]
        else:
            arrows = [leader]
        landing = leader["points"][-1]
        made.append({
            "entity_type": "MULTILEADER", "text": leader["text"],
            "insertion_point": (landing[0], landing[1], 0.0),
            "height": leader.get("height", 0.0), "rotation": 0.0,
            "width": None, "layer": leader.get("layer", "0"),
            "style": None, "handle": "", "leaders": arrows,
        })
    return made


def header_unit_scale(header):
    """Feet per drawing unit from $INSUNITS, or None when unitless."""
    values = header.get("$INSUNITS") or []
    for code, value in values:
        if code != 70:
            continue
        try:
            return INSUNITS_TO_FEET.get(int(float(value)))
        except (TypeError, ValueError):
            return None
    return None


def robust_range(values, low=0.02, high=0.98):
    """(low, high) covering the bulk of `values`, ignoring stray outliers.

    Real drawings carry junk entities miles from the sheet; with a plain
    min/max a single one of them decides the scale for everything.
    """
    if not values:
        return None
    ordered = sorted(values)
    count = len(ordered)
    if count < 8:
        return ordered[0], ordered[-1]
    return (ordered[int(count * low)],
            ordered[min(count - 1, int(count * high))])


def robust_span(values):
    """How far the bulk of `values` reaches, or None."""
    limits = robust_range(values)
    if limits is None:
        return None
    return limits[1] - limits[0]


def entities_extent(entities):
    """Where the bulk of the drawing sits, in its own units.

    Matched against the size of the geometry Revit actually imported, this
    says how many feet one drawing unit is without trusting the header.
    Outliers are trimmed, so a stray entity cannot inflate the drawing.
    """
    xs, ys = [], []
    for entity in entities:
        # A HATCH writes its elevation point - fixed at 0,0 - before any
        # boundary vertex, so reading that would drag the drawing's extent
        # back towards the origin. Its boundary is where it really is.
        if entity.get("type") == "HATCH":
            try:
                rings = _hatch_polygons(entity["groups"]) or []
            except Exception:
                rings = []
            # Its two extreme corners, not every flattened vertex: a
            # single round boundary is sixty points, and sixty votes would
            # shrink the drawing to whatever is hatched.
            points = [point for ring in rings for point in ring]
            if points:
                xs.extend((min(p[0] for p in points),
                           max(p[0] for p in points)))
                ys.extend((min(p[1] for p in points),
                           max(p[1] for p in points)))
            continue
        for values, code in ((xs, 10), (ys, 20)):
            raw = _group(entity["groups"], code)
            if raw is None:
                continue
            try:
                values.append(float(raw))
            except (TypeError, ValueError):
                continue
    span_x = robust_range(xs)
    span_y = robust_range(ys)
    if span_x is None or span_y is None:
        return None
    if span_x[1] - span_x[0] <= 0 and span_y[1] - span_y[0] <= 0:
        return None
    return (span_x[0], span_y[0]), (span_x[1], span_y[1])


def header_extents(header):
    """($EXTMIN, $EXTMAX) as x/y/z tuples, or None when absent."""
    def point(name):
        values = header.get(name) or []
        got = {}
        for code, value in values:
            if code in (10, 20, 30):
                try:
                    got[code] = float(value)
                except (TypeError, ValueError):
                    return None
        if 10 not in got or 20 not in got:
            return None
        return (got[10], got[20], got.get(30, 0.0))

    low, high = point("$EXTMIN"), point("$EXTMAX")
    if low is None or high is None:
        return None
    if high[0] <= low[0] or high[1] <= low[1]:
        return None
    return low, high
