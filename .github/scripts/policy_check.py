# -*- coding: utf-8 -*-
"""Content policy check for the repository.

Rejects what an extension for Revit has no reason to ship: unexpected file
types, binaries other than the listed fonts and well-formed PNG icons,
unparseable JSON/YAML, and Python, XAML or SVG that reaches the network,
starts processes, runs dynamic code, loads native code, touches the
registry, hides a payload in a string or star-imports names out of sight.

The .github/ folder (config['tooling_dirs']) only gets the file type and
parse checks: the CI scripts there legitimately use the network.

Usage:
    python .github/scripts/policy_check.py --root . [--config FILE]

The default config is .github/policy/policy.json next to this script.
Runs on Python 2.7 (CI) and Python 3.
"""
# Known limits. This check is a backstop for human review, not a sandbox; it
# reads names, so it cannot see what only exists at run time:
# - getattr() with a name that is not a string literal (getattr(os, name));
# - a payload split into literals shorter than the encoded-string threshold
#   and joined at run time ('QUJD...' + 'QUJD...');
# - a type looked up by name in one statement and instantiated in another
#   (t = Type.GetType('System.Diagnostics.Process'); Activator.CreateInstance(t));
# - existing processes reached through a variable (p = Process.GetProcessById(n);
#   p.Kill());
# - XAML split across several Python string literals and joined at run time:
#   only a literal that holds xmlns is read as XAML.
from __future__ import print_function

import argparse
import ast
import bisect
import json
import os
import re
import struct
import subprocess
import sys
import warnings
import zlib

from ci_common import Finding, file_digest, format_finding, list_files, load_json
# Private, but it is the test list_files itself uses to decide what to list.
from ci_common import _in_git_work_tree

try:
    import yaml
except ImportError:  # each YAML file then gets a parse finding
    yaml = None

DEFAULT_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, 'policy', 'policy.json')

# --- File types ---------------------------------------------------------------

EXTENSION_SUFFIXES = ('.py', '.xaml', '.yaml', '.json', '.png', '.svg', '.ttf', '.md', '.txt')
EXTENSION_NAMES = ('LICENSE', 'LICENSE-CONTENT', '.gitattributes', '.gitignore')
TOOLING_SUFFIXES = ('.yml', '.yaml', '.py', '.md', '.json', '.txt', '.toml')
TOOLING_NAMES = ('CODEOWNERS',)

TEXT_SNIFF_BYTES = 8192
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'

# --- Python rules -------------------------------------------------------------

NETWORK_MODULES = frozenset([
    'socket', 'urllib', 'urllib2', 'urllib3', 'httplib', 'http', 'ssl', 'ftplib',
    'smtplib', 'telnetlib', 'poplib', 'imaplib', 'xmlrpclib', 'SocketServer', 'webbrowser'])
# nt and posix are the modules behind os (nt on IronPython for Windows).
OS_MODULES = ('os', 'nt', 'posix')
PROCESS_MODULES = frozenset(['subprocess', 'multiprocessing', 'popen2', 'commands', 'pty',
                             'nt', 'posix'])
DYNAMIC_MODULES = frozenset(['imp', 'importlib', 'marshal', 'pickle', 'cPickle', 'shelve',
                             'builtins', '__builtin__'])
# Any reference to these reaches eval and friends without naming them.
BUILTIN_NAMESPACES = frozenset(['__builtins__', '__builtin__'])
REGISTRY_MODULES = frozenset(['_winreg', 'winreg'])
DYNAMIC_CALLS = frozenset(['eval', 'exec', 'execfile', 'compile', '__import__', 'input'])
# pyRevit code commonly star-imports the Revit API; anything else must name what it imports.
STAR_IMPORT_ALLOWED = (['Autodesk', 'Revit', 'DB'], ['Autodesk', 'Revit', 'UI'])

OS_PROCESS_MEMBERS = re.compile(r'(?:system|popen\w*|exec\w*|spawn\w*|startfile|fork\w*|kill)$')
PROCESS_START = re.compile(r'Start$')
ACTIVATOR_CREATE = re.compile(r'CreateInstance$')
NATIVE_MEMBERS = (
    ('Assembly', re.compile(r'(?:Load\w*|UnsafeLoadFrom)$')),
    ('clr', re.compile(r'(?:AddReferenceToFile\w*|AddReferenceByName|AddReferenceByPartialName'
                       r'|LoadAssembly\w*)$')),
    ('Type', re.compile(r'(?:GetTypeFromProgID|GetTypeFromCLSID)$')),
    ('Marshal', re.compile(r'GetActiveObject$')),
)
# Microsoft.Win32 is mostly the registry, but WPF's file dialogs live there too.
WIN32_FILE_DIALOGS = frozenset(['OpenFileDialog', 'SaveFileDialog'])

BASE64_RUN = re.compile(r'[A-Za-z0-9+/]{100,}')
HEX_RUN = re.compile(r'[0-9A-Fa-f]{80,}')
HTTP_URL = re.compile(r'https?://', re.I)

# --- XAML and SVG rules -------------------------------------------------------
# Both are matched as text after decoding character references, so
# "&#104;ttp://" reads as "http://" and "&#97;ssembly=" as "assembly=". The
# rules about tags read a second decoding where escaped quotes and angle
# brackets are a space: escaped markup is text, and opens or closes nothing.
# Every pattern here must run in linear time (the Timing tests check it): a
# crafted file must not stall the gate.

# Significant digits are bounded (a longer reference is no character); zeros are not.
XML_REFERENCE = re.compile(
    r'&(?:#0*([0-9]{1,8})|#[xX]0*([0-9A-Fa-f]{1,8})|(amp|lt|gt|quot|apos));')
XML_ENTITIES = {'amp': u'&', 'lt': u'<', 'gt': u'>', 'quot': u'"', 'apos': u"'"}
MARKUP_CHARS = u'"\'<>'
# Browsers drop these inside a URL scheme ("java<TAB>script:"), so the scheme
# patterns let them sit between any two characters.
SCHEME_NOISE = re.compile(r'[\t\r\n\x00]')
NEWLINE = re.compile(r'\n')


def _scheme(word):
    return (SCHEME_NOISE.pattern + '*').join(re.escape(char) for char in word)


# Read with escaped quotes and angle brackets neutralised.
XAML_TAG_RULES = (
    (re.compile(r'ObjectDataProvider', re.I), 'ObjectDataProvider'),
    # x:Code, under whatever prefix the XAML namespace is bound to.
    (re.compile(r'<\s*[\w.-]+:Code\b', re.I), 'x:Code'),
    # XAML 2009 factory calls, which XamlReader honours: they can call any static
    # method, such as Process.Start, of a type from an allowed assembly.
    (re.compile(r'\bFactoryMethod\b', re.I), 'x:FactoryMethod'),
    (re.compile(r'<\s*(?:[\w.-]+:)?Arguments\b|:Arguments\b', re.I), 'x:Arguments'),
    # XamlReader inside a tag (such as {x:Type m:XamlReader}); prose that names
    # it, in a comment or in a Python docstring next to xmlns, is not markup.
    # Quoted values are skipped whole, so a '>' inside one does not end the tag.
    (re.compile(r'<(?!!)(?:[^<>"\']|"[^"]*"|\'[^\']*\')*(?:"[^"]*|\'[^\']*)?XamlReader',
                re.I), 'XamlReader'),
    (re.compile(r'<!DOCTYPE', re.I), '<!DOCTYPE'),
    (re.compile(r'<!ENTITY', re.I), '<!ENTITY'),
    # A value or element text that starts with // or \\ (protocol-relative or UNC).
    (re.compile(r'(?:=\s*["\']|>)\s*(?:\\\\|{0})[^\\/\s"\'<>]'.format(_scheme('//'))),
     'protocol-relative or UNC path'),
)
# Read fully decoded.
XAML_VALUE_RULES = (
    # UNC paths, also with mixed slashes: \\host\, \/host\, /\host\.
    (re.compile(r'(?:\\\\|\\/|/\\)[^\\/\s"\'<>]+[\\/]'), 'UNC path'),
)
XAML_CLR_ASSEMBLY = re.compile(
    r'clr-namespace:[^;"\'<>:]*;\s*assembly\s*=\s*([^"\'\s;,<>]*)', re.I)
XAML_CLR_NAMESPACE = re.compile(r'clr-namespace:\s*([\w.]*)', re.I)
# Namespaces whose types run processes, load code, reach the network, the file
# system or the registry: not reachable from XAML, whatever the assembly.
XAML_RISKY_NAMESPACES = ('System.Diagnostics', 'System.Reflection', 'System.Net', 'System.IO',
                         'System.Runtime.InteropServices', 'Microsoft.Win32')
# Any URL, pack://siteoforigin (files next to the host program) and file: in
# any form; "file:" needs something after it, so "Pick a file: x" is text.
XAML_URI = re.compile(r'\b(?:(?:{0})[^\s"\'<>]*|{1}[^\s"\'<>]+)'.format(
    '|'.join(_scheme(s) for s in ('https://', 'http://', 'ftp://', 'pack://siteoforigin')),
    _scheme('file:')), re.I)
XAML_NAMESPACE_PREFIX = 'http://schemas.microsoft.com/'
XAML_NAMESPACES = ('http://schemas.openxmlformats.org/markup-compatibility/2006',)

# Read with escaped quotes and angle brackets neutralised.
SVG_TAG_RULES = (
    (re.compile(r'<(?:[\w.-]+:)?script', re.I), '<script> element'),
    (re.compile(r'<foreignObject', re.I), '<foreignObject> element'),
    (re.compile(r'<!ENTITY', re.I), 'entity declaration'),
    (re.compile(r'\bon\w+\s*=', re.I), 'event handler attribute'),
)
# Read fully decoded.
SVG_VALUE_RULES = (
    (re.compile(_scheme('javascript:'), re.I), 'javascript: URI'),
    (re.compile(r'\bhref\s*=\s*["\']?\s*(?:{0})'.format(
        '|'.join(_scheme(s) for s in ('http', '//', 'file:', 'data:'))), re.I),
     'external or data: href'),
)


# --- Shared helpers -----------------------------------------------------------

def _describe(exc):
    try:
        text = u'{0}'.format(exc)
    except UnicodeError:
        text = exc.__class__.__name__
    return u' '.join(text.split())


def _line_numbers(text):
    """A function from an offset in text to its 1-based line number."""
    breaks = [m.start() for m in NEWLINE.finditer(text)]
    return lambda offset: bisect.bisect_left(breaks, offset) + 1


def _matches(text, rules, line_of):
    """(line, message) for every match of the (pattern, what) rules in text."""
    return [(line_of(m.start()), u'{0} is not allowed'.format(what))
            for pattern, what in rules for m in pattern.finditer(text)]


def _text_findings(relpath, problems, rule):
    """Findings for (line, message) problems, sorted, without duplicates."""
    return [Finding(relpath, line, rule, message) for line, message in sorted(set(problems))]


def _hit_findings(relpath, hits):
    """Findings for (line, rule, message) hits, sorted, without duplicates."""
    return [Finding(relpath, line, rule, message) for line, rule, message in sorted(set(hits))]


def _decode_references(text, neutral=False):
    """Text with numeric character references and the five XML entities decoded.

    Tab, CR, LF and NUL decode to nothing: line numbers stay put, and a scheme
    split by them is joined the way browsers join it. With neutral, quotes and
    angle brackets decode to a space.
    """
    def decode(match):
        number, hexadecimal, name = match.groups()
        if name:
            char = XML_ENTITIES[name]
        else:
            code = int(number) if number else int(hexadecimal, 16)
            try:
                char = struct.pack('<I', code).decode('utf-32-le')
            except ValueError:  # not a character
                return u''
        if SCHEME_NOISE.match(char):
            return u''
        return u' ' if neutral and char in MARKUP_CHARS else char
    return XML_REFERENCE.sub(decode, text)


# --- XAML ---------------------------------------------------------------------

def _allowed_xaml_uri(uri):
    uri = uri.lower()
    return uri.startswith(XAML_NAMESPACE_PREFIX) or uri in XAML_NAMESPACES


def _risky_clr_namespace(namespace):
    namespace = namespace.lower()
    return any(namespace == risky.lower() or namespace.startswith(risky.lower() + '.')
               for risky in XAML_RISKY_NAMESPACES)


def _xaml_problems(text, config):
    """(line, message) for the XAML problems in text, references not yet decoded."""
    neutral = _decode_references(text, neutral=True)
    problems = _matches(neutral, XAML_TAG_RULES, _line_numbers(neutral))
    text = _decode_references(text)
    line_of = _line_numbers(text)
    problems.extend(_matches(text, XAML_VALUE_RULES, line_of))
    allowed = set(name.lower() for name in config['assemblies'])
    for m in XAML_CLR_ASSEMBLY.finditer(text):
        if m.group(1).lower() not in allowed:
            problems.append((line_of(m.start()), u'clr-namespace from assembly "{0}", which is '
                                                 u'not in the policy'.format(m.group(1))))
    for m in XAML_CLR_NAMESPACE.finditer(text):
        if _risky_clr_namespace(m.group(1)):
            problems.append((line_of(m.start()),
                             u'clr-namespace {0} is not allowed'.format(m.group(1))))
    for m in XAML_URI.finditer(text):
        uri = SCHEME_NOISE.sub(u'', m.group(0))
        if not _allowed_xaml_uri(uri):
            problems.append((line_of(m.start()), u'external URI {0}'.format(uri)))
    return problems


def check_xaml_source(relpath, text, config):
    return _text_findings(relpath, _xaml_problems(text, config), 'xaml')


# --- SVG ----------------------------------------------------------------------

def check_svg_source(relpath, text):
    neutral = _decode_references(text, neutral=True)
    text = _decode_references(text)
    problems = (_matches(neutral, SVG_TAG_RULES, _line_numbers(neutral)) +
                _matches(text, SVG_VALUE_RULES, _line_numbers(text)))
    return _text_findings(relpath, problems, 'svg')


# --- Python -------------------------------------------------------------------

def _under(parts, prefix):
    return parts[:len(prefix)] == prefix


def _member_of(parts, owner, members):
    """True when `owner.<member>` appears in the chain, member matching the regex."""
    return any(a == owner and members.match(b) for a, b in zip(parts, parts[1:]))


def _network_name(parts, imported):
    return (imported and parts[0] in NETWORK_MODULES) or _under(parts, ['System', 'Net'])


def _process_name(parts, imported):
    return ((imported and parts[0] in PROCESS_MODULES)
            or any(_member_of(parts, module, OS_PROCESS_MEMBERS) for module in OS_MODULES)
            or _member_of(parts, 'Process', PROCESS_START)
            or 'ProcessStartInfo' in parts)


def _dynamic_name(parts, imported):
    return (imported and parts[0] in DYNAMIC_MODULES) or parts[0] in BUILTIN_NAMESPACES


def _native_name(parts, imported):
    return ((imported and parts[0] == 'ctypes')
            or _under(parts, ['System', 'Runtime', 'InteropServices'])
            or any(_member_of(parts, owner, members) for owner, members in NATIVE_MEMBERS))


def _registry_name(parts, imported):
    if imported and parts[0] in REGISTRY_MODULES:
        return True
    return _under(parts, ['Microsoft', 'Win32']) and (
        len(parts) < 3 or parts[2] not in WIN32_FILE_DIALOGS)


NAME_RULES = (
    ('network', _network_name),
    ('process', _process_name),
    ('dynamic-code', _dynamic_name),
    ('native', _native_name),
    ('registry', _registry_name),
)


if sys.version_info >= (3, 8):
    _CONSTANT = getattr(ast, 'Constant')

    def _string_value(node):
        if isinstance(node, _CONSTANT) and isinstance(node.value, (bytes, str)):
            return node.value
        return None
else:
    _STRING_NODES = tuple(getattr(ast, name) for name in ('Str', 'Bytes') if hasattr(ast, name))

    def _string_value(node):
        return node.s if isinstance(node, _STRING_NODES) else None


def _string_text(node):
    """A string literal's text, bytes read as Latin-1; None for anything else."""
    value = _string_value(node)
    if isinstance(value, bytes):
        value = value.decode('latin-1')
    return value


def _dotted(parts):
    return u'.'.join(part or u'(...)' for part in parts)


def _getattr_literal(node):
    """(object, name) for getattr(object, 'name'[, default]); None otherwise."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == 'getattr' and len(node.args) in (2, 3)):
        return None
    name = _string_text(node.args[1])
    return None if name is None else (node.args[0], name)


def _names_a_process_type(call):
    """True when a string literal among the call's arguments names a Process type."""
    for arg in _call_arguments(call):
        for node in ast.walk(arg):
            text = _string_text(node)
            if text is not None and 'process' in text.lower():
                return True
    return False


def _star_import_allowed(node):
    module = (node.module or '').split('.')
    return not node.level and any(_under(module, prefix) for prefix in STAR_IMPORT_ALLOWED)


def _call_arguments(node):
    args = list(node.args) + [kw.value for kw in node.keywords]
    for extra in (getattr(node, 'starargs', None), getattr(node, 'kwargs', None)):  # Python 2
        if extra is not None:
            args.append(extra)
    return args


class _PythonScanner(ast.NodeVisitor):
    """Records (line, rule, message) hits for imports, attribute chains and calls.

    Names bound by imports are resolved, so `import os as o; o.system()` reads
    as os.system and `from System.Diagnostics import Process` makes
    `Process.Start` read as System.Diagnostics.Process.Start.
    """

    def __init__(self, assemblies):
        self.assemblies = assemblies
        self.aliases = {}
        self.hits = []

    def _hit(self, node, rule, message):
        self.hits.append((getattr(node, 'lineno', 0), rule, message))

    def _check(self, node, parts, imported):
        verb = u'imports' if imported else u'uses'
        name = _dotted(parts)
        for rule, matches in NAME_RULES:
            if matches(parts, imported):
                self._hit(node, rule, u'{0} {1}'.format(verb, name))

    def _chain(self, node):
        """Parts of an attribute chain, aliases resolved; getattr(X, 'a') reads
        as X.a. Also returns the nodes still to visit: the root expression when
        it is not a name (its part is then '') and any getattr defaults."""
        attrs, rest = [], []
        while True:
            if isinstance(node, ast.Attribute):
                attrs.append(node.attr)
                node = node.value
                continue
            literal = _getattr_literal(node)
            if literal is None:
                break
            rest.extend(node.args[2:])
            node, name = literal
            attrs.append(name)
        attrs.reverse()
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id).split('.') + attrs, rest
        return [''] + attrs, rest + [node]

    def _visit_chain(self, node):
        # Only the longest chain is checked, then whatever it did not cover.
        parts, rest = self._chain(node)
        self._check(node, parts, False)
        for child in rest:
            self.visit(child)

    visit_Attribute = _visit_chain

    def visit_Import(self, node):
        for alias in node.names:
            if alias.asname:
                self.aliases[alias.asname] = alias.name
            self._check(node, alias.name.split('.'), True)

    def visit_ImportFrom(self, node):
        # `from M import a` is checked as M.a, which always covers M as well.
        for alias in node.names:
            full = '.'.join(part for part in (node.module, alias.name) if part)
            if alias.name != '*':
                self.aliases[alias.asname or alias.name] = full
            elif not _star_import_allowed(node):
                self._hit(node, 'star-import', u'from {0}{1} import * hides the names it '
                                               u'brings in'.format('.' * (node.level or 0),
                                                                   node.module or ''))
            self._check(node, full.split('.'), True)

    def visit_Name(self, node):
        if node.id in BUILTIN_NAMESPACES:
            self._hit(node, 'dynamic-code', u'uses {0}'.format(node.id))

    def visit_Call(self, node):
        if _getattr_literal(node) is not None:
            return self._visit_chain(node)
        if isinstance(node.func, (ast.Name, ast.Attribute)) or _getattr_literal(node.func):
            self._check_call(node, self._chain(node.func)[0])
        self.generic_visit(node)

    def visit_Exec(self, node):  # the Python 2 exec statement
        self._hit(node, 'dynamic-code', u'exec statement')
        self.generic_visit(node)

    def _check_call(self, node, parts):
        name = _dotted(parts)  # '(...).x' when the chain does not start at a name
        if parts[-1] == 'Process':
            # The constructor; Process.GetCurrentProcess() ends in another name.
            self._hit(node, 'process', u'constructs {0}'.format(name))
        elif _member_of(parts, 'Activator', ACTIVATOR_CREATE) and _names_a_process_type(node):
            self._hit(node, 'process', u'creates a Process through {0}'.format(name))
        elif name in DYNAMIC_CALLS:
            self._hit(node, 'dynamic-code', u'calls {0}()'.format(name))
        elif name == 'clr.AddReference':
            for arg in _call_arguments(node):
                value = _string_value(arg)
                if value is None or value not in self.assemblies:
                    self._hit(node, 'native', u'clr.AddReference of something other than '
                                              u'a literal assembly name from the policy')


def _parse_python(source):
    if source[:1] == u'\ufeff':
        source = source[1:]
    if sys.version_info[0] < 3 and not isinstance(source, bytes):
        # Python 2 rejects a coding line in a unicode source.
        source = source.encode('utf-8')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')  # invalid escape sequences and the like
        return ast.parse(source)


def _literals(tree):
    """(line, text) for every string literal; bytes are read as Latin-1."""
    found = []
    for node in ast.walk(tree):
        text = _string_text(node)
        if text is not None:
            found.append((getattr(node, 'lineno', 0), text))
    return found


def _literal_hits(literals, config):
    hits = []
    for line, text in literals:
        if BASE64_RUN.search(text) or HEX_RUN.search(text):
            hits.append((line, 'encoded-string', u'string literal looks like encoded data'))
        if 'xmlns' in text.lower():
            hits.extend((line, 'xaml', message) for _, message in _xaml_problems(text, config))
    return hits


def _telemetry_urls(telemetry):
    """The endpoints the telemetry module may name: "urls", a list, and the
    older single "url"; null or missing means none."""
    urls = list(telemetry.get('urls') or [])
    if telemetry.get('url'):
        urls.append(telemetry['url'])
    return urls


def _telemetry_url_hits(literals, urls):
    return [(line, 'network', u'URL other than the telemetry endpoints')
            for line, text in literals if HTTP_URL.search(text) and text.strip() not in urls]


def check_python_source(relpath, source, config):
    try:
        tree = _parse_python(source)
    except Exception as exc:  # SyntaxError, or a source too deep to parse
        return [Finding(relpath, getattr(exc, 'lineno', 0) or 0, 'parse',
                        u'Python does not parse: {0}'.format(_describe(exc)))]
    scanner = _PythonScanner(config['assemblies'])
    scanner.visit(tree)
    literals = _literals(tree)
    hits = scanner.hits + _literal_hits(literals, config)
    telemetry = config['telemetry']
    if telemetry.get('module') == relpath:
        hits = [hit for hit in hits if hit[1] != 'network']
        hits.extend(_telemetry_url_hits(literals, _telemetry_urls(telemetry)))
    return _hit_findings(relpath, hits)


# --- Binaries -----------------------------------------------------------------

def _png_problem(data, max_bytes):
    if len(data) > max_bytes:
        return u'PNG is larger than {0} bytes'.format(max_bytes)
    if not data.startswith(PNG_SIGNATURE):
        return u'not a PNG file'
    pos, kind = len(PNG_SIGNATURE), None
    while kind != b'IEND':
        if pos + 12 > len(data):
            return u'PNG is truncated or has no IEND chunk'
        length = struct.unpack('>I', data[pos:pos + 4])[0]
        crc_at = pos + 8 + length
        if crc_at + 4 > len(data):
            return u'PNG chunk runs past the end of the file'
        body = data[pos + 4:crc_at]
        if zlib.crc32(body) & 0xffffffff != struct.unpack('>I', data[crc_at:crc_at + 4])[0]:
            return u'PNG chunk has a bad CRC'
        kind = body[:4]
        if pos == len(PNG_SIGNATURE) and kind != b'IHDR':
            return u'PNG does not start with an IHDR chunk'
        pos = crc_at + 4
    if pos != len(data):
        return u'PNG has data after its IEND chunk'
    return None


def _font_problem(path, relpath, config):
    expected = config['font_hashes'].get(relpath)
    if expected is None:
        return u'font is not listed in the policy font_hashes'
    if file_digest(path) != expected:
        return u'font does not match its hash in the policy'
    return None


def _text_problem(data):
    if b'\x00' in data[:TEXT_SNIFF_BYTES]:
        return u'NUL byte in a text file'
    try:
        data.decode('utf-8')
    except UnicodeDecodeError:
        return u'text file is not valid UTF-8'
    return None


# --- Files --------------------------------------------------------------------

def _is_tooling(relpath, config):
    return any(relpath.startswith(folder) for folder in config['tooling_dirs'])


def _allowed_type(relpath, tooling):
    name = relpath.rsplit('/', 1)[-1]
    suffixes, names = ((TOOLING_SUFFIXES, TOOLING_NAMES) if tooling
                       else (EXTENSION_SUFFIXES, EXTENSION_NAMES))
    return name in names or name.lower().endswith(suffixes)


def _parse_problem(relpath, text):
    lower = relpath.lower()
    try:
        if lower.endswith('.json'):
            json.loads(text)
        elif lower.endswith(('.yaml', '.yml')):
            if yaml is None:
                return u'PyYAML is not installed'
            yaml.safe_load(text)
    except Exception as exc:  # any parser failure on untrusted input
        return u'does not parse: {0}'.format(_describe(exc))
    return None


def _read_bytes(path):
    with open(path, 'rb') as fh:
        return fh.read()


def _check_tooling_file(path, relpath):
    if not relpath.lower().endswith(('.json', '.yaml', '.yml')):
        return []
    try:
        text = _read_bytes(path).decode('utf-8')
    except UnicodeDecodeError:
        return [Finding(relpath, 0, 'parse', u'file is not valid UTF-8')]
    problem = _parse_problem(relpath, text)
    return [Finding(relpath, 0, 'parse', problem)] if problem else []


def _check_extension_file(path, relpath, config):
    lower = relpath.lower()
    if lower.endswith('.ttf'):
        problem = _font_problem(path, relpath, config)
    elif lower.endswith('.png'):
        problem = _png_problem(_read_bytes(path), config['png_max_bytes'])
    else:
        data = _read_bytes(path)
        problem = _text_problem(data)
        if problem is None:
            return _check_extension_text(relpath, data.decode('utf-8'), config)
    return [Finding(relpath, 0, 'binary', problem)] if problem else []


def _check_extension_text(relpath, text, config):
    problem = _parse_problem(relpath, text)
    if problem:
        return [Finding(relpath, 0, 'parse', problem)]
    lower = relpath.lower()
    if lower.endswith('.py'):
        return check_python_source(relpath, text, config)
    if lower.endswith('.xaml'):
        return check_xaml_source(relpath, text, config)
    if lower.endswith('.svg'):
        return check_svg_source(relpath, text)
    return []


def _index_symlinks(root):
    """Paths the git index records as symlinks (mode 120000). A checkout
    without symlink support writes them as plain files, and list_files skips
    a link to a folder or to nothing, so os.path.islink alone misses them."""
    if not _in_git_work_tree(root):
        return set()
    out = subprocess.check_output(['git', '-C', root, 'ls-files', '-s', '-z'])
    return set(entry.split(u'\t', 1)[1] for entry in out.decode('utf-8').split(u'\0')
               if entry.startswith(u'120000 '))


def _check_file(root, relpath, config, symlinks):
    path = os.path.join(root, *relpath.split('/'))
    if relpath in symlinks or os.path.islink(path):
        return [Finding(relpath, 0, 'file-type', u'symlinks are not allowed')]
    tooling = _is_tooling(relpath, config)
    if not _allowed_type(relpath, tooling):
        where = u'.github/' if tooling else u'the extension'
        return [Finding(relpath, 0, 'file-type', u'file type not allowed in {0}'.format(where))]
    if tooling:
        return _check_tooling_file(path, relpath)
    return _check_extension_file(path, relpath, config)


def check_repo(root, config):
    symlinks = _index_symlinks(root)
    findings = []
    for relpath in sorted(set(list_files(root)) | symlinks):
        findings.extend(_check_file(root, relpath, config, symlinks))
    return findings


# --- CLI ----------------------------------------------------------------------

def _emit(line):
    # ASCII only, so a non-ASCII path cannot crash the output on any console.
    print(line.encode('ascii', 'backslashreplace').decode('ascii'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Content policy check for the repository.')
    parser.add_argument('--root', default='.', help='repository root to check')
    parser.add_argument('--config', default=DEFAULT_CONFIG, help='policy.json to apply')
    args = parser.parse_args(argv)
    findings = check_repo(args.root, load_json(args.config))
    for finding in findings:
        _emit(format_finding(finding))
    if findings:
        _emit(u'{0} finding(s)'.format(len(findings)))
        return 1
    _emit(u'policy: OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
