# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import hashlib
import io
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import policy_check as pc  # noqa: E402

CONFIG = {
    "tooling_dirs": [".github/"],
    "assemblies": ["RevitAPI", "RevitAPIUI", "AdWindows", "PresentationFramework",
                   "PresentationCore", "WindowsBase", "System.Xaml", "System",
                   "System.Core", "System.Drawing", "System.Windows.Forms",
                   "System.Private.Uri", "mscorlib"],
    "font_hashes": {},
    "png_max_bytes": 65536,
    "telemetry": {"module": None, "url": None},
}


def rules(findings):
    return sorted(set(f.rule for f in findings))


class Capture(object):
    """Stands in for sys.stdout, on Python 2 and 3."""

    def __init__(self):
        self.parts = []

    def write(self, text):
        self.parts.append(text)

    def flush(self):
        pass


def run_main(argv):
    """Exit code of pc.main(argv), with its printed report kept out of the test log."""
    saved, sys.stdout = sys.stdout, Capture()
    try:
        return pc.main(argv)
    finally:
        sys.stdout = saved


def tiny_png():
    def chunk(kind, data):
        body = kind + data
        return (struct.pack('>I', len(data)) + body +
                struct.pack('>I', zlib.crc32(body) & 0xffffffff))
    ihdr = struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0)
    idat = zlib.compress(b'\x00\x00\x00\x00\x00')
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) +
            chunk(b'IDAT', idat) + chunk(b'IEND', b''))


class PythonRules(unittest.TestCase):

    def check(self, src, path='lib/x.py', config=CONFIG):
        return pc.check_python_source(path, src, config)

    def test_clean_code_passes(self):
        src = ("# -*- coding: utf-8 -*-\nimport os\nimport clr\n"
               "clr.AddReference('RevitAPI')\nfrom pyrevit import revit\n"
               "x = os.path.join('a', 'b')\n")
        self.assertEqual(self.check(src), [])

    def test_network_imports(self):
        for src in ["import socket", "import urllib2",
                    "from httplib import HTTPConnection", "import ssl",
                    "from System.Net import WebClient", "import System.Net",
                    "from System import Net", "import webbrowser"]:
            self.assertIn('network', rules(self.check(src + "\n")), src)

    def test_network_attribute_chain(self):
        src = "import System\nc = System.Net.WebClient()\n"
        self.assertIn('network', rules(self.check(src)))

    def test_processes(self):
        for src in ["import subprocess",
                    "import os\nos.system('x')",
                    "import os\nos.popen('x')",
                    "import os\nos.startfile('x')",
                    "import os\nos.execv('a', [])",
                    "import os\nos.spawnl(0, 'a')",
                    "from System.Diagnostics import Process\nProcess.Start('x')",
                    "import System\nSystem.Diagnostics.Process.Start('x')",
                    "from System.Diagnostics import ProcessStartInfo"]:
            self.assertIn('process', rules(self.check(src + "\n")), src)

    def test_current_process_handle_is_allowed(self):
        src = ("import System.Diagnostics\n"
               "h = System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle\n")
        self.assertEqual(self.check(src), [])

    def test_debug_is_allowed(self):
        src = "from System.Diagnostics import Debug\nDebug.WriteLine('x')\n"
        self.assertEqual(self.check(src), [])

    def test_dynamic_code(self):
        for src in ["eval('1')", "exec('x = 1')", "execfile('a.py')",
                    "compile('1', 'a', 'eval')", "__import__('os')",
                    "input()", "import imp", "import importlib",
                    "import marshal", "import pickle",
                    "from cPickle import loads"]:
            self.assertIn('dynamic-code', rules(self.check(src + "\n")), src)

    def test_re_compile_is_allowed(self):
        self.assertEqual(self.check("import re\nR = re.compile(r'x')\n"), [])

    def test_native(self):
        for src in ["import ctypes",
                    "from System.Runtime.InteropServices import Marshal",
                    "from System.Reflection import Assembly\nAssembly.LoadFrom('a.dll')",
                    "import clr\nclr.AddReferenceToFileAndPath('a.dll')",
                    "import clr\nclr.AddReference('Evil')",
                    "import clr\nname = 'RevitAPI'\nclr.AddReference(name)",
                    "import System\nt = System.Type.GetTypeFromProgID('WScript.Shell')"]:
            self.assertIn('native', rules(self.check(src + "\n")), src)

    def test_allowed_assemblies(self):
        src = ("import clr\nclr.AddReference('AdWindows')\n"
               "clr.AddReference(\"System.Windows.Forms\")\n")
        self.assertEqual(self.check(src), [])

    def test_registry(self):
        for src in ["from Microsoft.Win32 import Registry",
                    "import Microsoft.Win32", "import _winreg"]:
            self.assertIn('registry', rules(self.check(src + "\n")), src)

    def test_long_encoded_strings(self):
        blob = 'QUJD' * 40
        self.assertIn('encoded-string', rules(self.check("X = '%s'\n" % blob)))
        hexblob = 'deadbeef' * 12
        self.assertIn('encoded-string', rules(self.check("X = '%s'\n" % hexblob)))

    def test_long_plain_text_is_allowed(self):
        text = ('Select every element of the same family visible in the '
                'active view, whatever its type. ') * 3
        self.assertEqual(self.check("X = '%s'\n" % text), [])

    def test_xaml_inside_a_python_string(self):
        src = ('X = """<Window xmlns="http://schemas.microsoft.com/winfx/2006/'
               'xaml/presentation"><ObjectDataProvider/></Window>"""\n')
        self.assertIn('xaml', rules(self.check(src)))

    def test_xaml_inside_a_python_string_is_decoded(self):
        src = ('X = """<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation">'
               '<Image Source="&#104;ttp://evil.example/a.png"/></Window>"""\n')
        self.assertIn('xaml', rules(self.check(src)))

    def test_syntax_error_is_a_finding(self):
        self.assertIn('parse', rules(self.check("def (:\n")))

    def test_utf8_bom_is_not_a_syntax_error(self):
        # Windows editors save it; IronPython accepts it.
        bom = b'\xef\xbb\xbf'.decode('utf-8')
        self.assertEqual(self.check(bom + "# -*- coding: utf-8 -*-\nimport os\n"), [])

    def test_findings_carry_line_numbers(self):
        found = self.check("x = 1\nimport socket\n")
        self.assertEqual(found[0].line, 2)
        self.assertEqual(found[0].path, 'lib/x.py')

    def test_telemetry_module_may_reach_its_url_only(self):
        cfg = dict(CONFIG, telemetry={"module": "lib/telemetry.py",
                                      "url": "https://example.org/hook"})
        ok = "import urllib2\nURL = 'https://example.org/hook'\n"
        self.assertEqual(pc.check_python_source('lib/telemetry.py', ok, cfg), [])
        other = "import urllib2\nURL = 'https://evil.example/x'\n"
        self.assertIn('network', rules(
            pc.check_python_source('lib/telemetry.py', other, cfg)))
        self.assertIn('network', rules(
            pc.check_python_source('lib/other.py', ok, cfg)))

    def test_import_aliases_are_resolved(self):
        for src, rule in [("import os as o\no.system('x')", 'process'),
                          ("import System as S\nc = S.Net.WebClient()", 'network')]:
            self.assertIn(rule, rules(self.check(src + "\n")), src)

    def test_wpf_file_dialogs_from_microsoft_win32_are_allowed(self):
        # slantisui uses Microsoft.Win32.SaveFileDialog: WPF's dialog, not the registry.
        src = ("from Microsoft.Win32 import OpenFileDialog, SaveFileDialog\n"
               "import Microsoft\nd = Microsoft.Win32.SaveFileDialog()\n")
        self.assertEqual(self.check(src), [])
        for src in ["from Microsoft.Win32 import *",
                    "import Microsoft\nk = Microsoft.Win32.Registry.CurrentUser"]:
            self.assertIn('registry', rules(self.check(src + "\n")), src)

    def test_nt_and_posix_mirror_os(self):
        for src in ["import nt", "import posix as p", "from nt import startfile"]:
            self.assertIn('process', rules(self.check(src + "\n")), src)
        for src in ["import nt\nnt.system('x')", "import posix\nposix.spawnv(0, 'a', [])"]:
            found = self.check(src + "\n")
            self.assertIn(2, [f.line for f in found if f.rule == 'process'], src)

    def test_star_imports(self):
        for src in ["from os import *", "from System.Diagnostics import *",
                    "from . import *", "from Autodesk.Revit.DBX import *"]:
            self.assertIn('star-import', rules(self.check(src + "\n")), src)
        for src in ["from Autodesk.Revit.DB import *", "from Autodesk.Revit.UI import *",
                    "from Autodesk.Revit.DB.Architecture import *"]:
            self.assertEqual(self.check(src + "\n"), [], src)

    def test_builtins_module_references(self):
        for src in ["import builtins", "import __builtin__", "from __builtin__ import open",
                    "f = __builtins__['eval']", "b = __builtins__", "f = __builtin__.open"]:
            self.assertIn('dynamic-code', rules(self.check(src + "\n")), src)

    def test_getattr_with_a_literal_name(self):
        for src, rule in [
                ("import os\nf = getattr(os, 'system')", 'process'),
                ("from System.Reflection import Assembly\n"
                 "f = getattr(Assembly, \"LoadFrom\", None)", 'native'),
                ("import System\nc = getattr(System, 'Net').WebClient()", 'network'),
                ("import System\n"
                 "s = getattr(getattr(System.Diagnostics, 'Process'), 'Start')", 'process'),
                ("f = getattr(__builtins__, 'eval')", 'dynamic-code'),
                ("import os\nf = getattr(os, 'path', eval('1'))", 'dynamic-code')]:
            self.assertIn(rule, rules(self.check(src + "\n")), src)

    def test_getattr_with_a_non_literal_or_harmless_name(self):
        src = ("import os\nname = 'system'\nf = getattr(os, name)\n"
               "g = getattr(os, 'path', None)\n")
        self.assertEqual(self.check(src), [])

    def test_process_construction(self):
        for src in ["from System.Diagnostics import Process\np = Process()",
                    "import System\np = System.Diagnostics.Process()",
                    "from System import Diagnostics\np = Diagnostics.Process()",
                    "from System import Activator, Type\n"
                    "p = Activator.CreateInstance(Type.GetType('System.Diagnostics.Process'))",
                    "import System\n"
                    "p = System.Activator.CreateInstance('System', 'System.Diagnostics.Process')"]:
            found = self.check(src + "\n")
            self.assertIn(2, [f.line for f in found if f.rule == 'process'], src)

    def test_getting_the_current_process_is_not_a_construction(self):
        src = ("from System.Diagnostics import Process\nh = Process.GetCurrentProcess().Id\n"
               "from System import Activator, Type\n"
               "s = Activator.CreateInstance(Type.GetType('System.Text.StringBuilder'))\n")
        self.assertEqual(self.check(src), [])

    def test_docstring_naming_xamlreader_is_allowed(self):
        # slantisui's styles_xaml() documents its use with XamlReader and xmlns.
        src = ('def styles():\n'
               '    """Merge it: XamlReader.Parse(\'<ResourceDictionary xmlns="...">\')."""\n')
        self.assertEqual(self.check(src), [])


class XamlRules(unittest.TestCase):

    HEAD = ('<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" '
            'xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">')

    def check(self, body):
        return pc.check_xaml_source('lib/a.xaml', self.HEAD + body + '</Window>', CONFIG)

    def test_clean(self):
        self.assertEqual(self.check('<Grid/>'), [])

    def test_object_data_provider(self):
        self.assertIn('xaml', rules(self.check('<ObjectDataProvider MethodName="Start"/>')))

    def test_x_code(self):
        self.assertIn('xaml', rules(self.check('<x:Code>bad</x:Code>')))

    def test_foreign_assembly(self):
        self.assertIn('xaml', rules(self.check(
            '<Grid xmlns:e="clr-namespace:Evil;assembly=Evil"/>')))

    def test_allowed_assembly(self):
        self.assertEqual(self.check(
            '<Grid xmlns:s="clr-namespace:System;assembly=mscorlib"/>'), [])

    def test_external_uri(self):
        self.assertIn('xaml', rules(self.check(
            '<Image Source="https://evil.example/a.png"/>')))

    def test_character_references_are_decoded(self):
        for body in ['<Grid xmlns:e="clr-namespace:E;&#97;ssembly=Evil"/>',
                     '<Image Source="h&#116;tps://evil.example/a.png"/>',
                     '<Image Source="&#104;ttp://evil.example/a.png"/>',
                     '<Image Source="&#x68;ttp://evil.example/a.png"/>',
                     '<Image Source="h&#10;ttp://evil.example/a.png"/>',
                     '<Image Source="ht\ttp://evil.example/a.png"/>']:
            self.assertIn('xaml', rules(self.check(body)), body)

    def test_decoding_keeps_line_numbers(self):
        found = pc.check_xaml_source(
            'lib/a.xaml', self.HEAD + '<Grid Tag="&#10;&#xA;&#13;"/>\n'
            '<Image Source="https://evil.example/a.png"/></Window>', CONFIG)
        self.assertEqual([f.line for f in found], [2])

    def test_site_of_origin_and_unc_paths(self):
        for body in ['<Image Source="pack://siteoforigin:,,,/a.png"/>',
                     '<Image Source="\\\\host\\share\\a.png"/>',
                     '<Image Source="file:\\\\host\\share\\a.png"/>',
                     '<Image Source="file:///C:/a.png"/>']:
            self.assertIn('xaml', rules(self.check(body)), body)
        self.assertEqual(self.check('<Image Source="pack://application:,,,/a.png"/>'
                                    '<TextBlock Text="Pick a file: any"/>'), [])

    def test_unprefixed_directives(self):
        for body in ['<s:Object><Arguments><x:String>calc</x:String></Arguments></s:Object>',
                     '<s:Object FactoryMethod="Start"/>']:
            self.assertIn('xaml', rules(self.check(body)), body)

    def test_escaped_quotes_do_not_end_a_tag_early(self):
        for body in ['<Grid Tag="&quot;>" Name="{x:Type m:XamlReader}"/>',
                     '<Grid Tag="&#34;>" Name="{x:Type m:XamlReader}"/>',
                     "<Grid Tag='&apos;>' Name=\"{x:Type m:XamlReader}\"/>"]:
            self.assertIn('xaml', rules(self.check(body)), body)

    def test_escaped_markup_in_a_value_is_text(self):
        self.assertEqual(self.check('<TextBlock Text="&lt;x:Code&gt;"/>'), [])

    def test_more_unc_forms(self):
        for body in ['<Image Source="\\/host\\share\\a.png"/>',
                     '<Image Source="/\\host\\share\\a.png"/>',
                     '<Image><Image.Source>//host/share/a.png</Image.Source></Image>',
                     '<Image><Image.Source>\\\\host\\share\\a.png</Image.Source></Image>']:
            self.assertIn('xaml', rules(self.check(body)), body)

    def test_xaml_2009_factory_calls(self):
        for body in ['<s:Object x:FactoryMethod="s:Process.Start"/>',
                     '<s:Object X:factorymethod="Start"/>',
                     '<s:Object><x:Arguments><x:String>calc</x:String></x:Arguments></s:Object>',
                     '<s:Object y:Arguments="calc"/>']:
            self.assertIn('xaml', rules(self.check(body)), body)

    def test_risky_clr_namespaces_whatever_the_assembly(self):
        for ns in ['System.Diagnostics', 'System.Reflection.Emit', 'System.Net', 'System.IO',
                   'System.Runtime.InteropServices', 'Microsoft.Win32']:
            for decl in ['clr-namespace:{0};assembly=mscorlib', 'clr-namespace:{0}']:
                body = '<Grid xmlns:s="{0}"/>'.format(decl.format(ns))
                self.assertIn('xaml', rules(self.check(body)), body)
        self.assertEqual(self.check(
            '<Grid xmlns:c="clr-namespace:System.Windows.Controls;assembly=PresentationFramework"/>'),
            [])

    def test_xamlreader_after_a_gt_inside_a_quoted_value(self):
        for body in ['<Grid Tag="a>b" Name="{x:Type m:XamlReader}"/>',
                     '<Grid Tag=\'a&gt;b\' Name="{x:Type m:XamlReader}"/>']:
            self.assertIn('xaml', rules(self.check(body)), body)

    def test_dtd(self):
        found = pc.check_xaml_source(
            'lib/a.xaml', '<!DOCTYPE Window [<!ENTITY x "y">]>' + self.HEAD + '</Window>', CONFIG)
        self.assertEqual(sorted(f.message for f in found),
                         ['<!DOCTYPE is not allowed', '<!ENTITY is not allowed'])

    def test_protocol_relative_values(self):
        for body in ['<Image Source="//evil.example/a.png"/>',
                     '<Image Source=\'//host/share/a.png\'/>']:
            self.assertIn('xaml', rules(self.check(body)), body)

    def test_xamlreader_in_markup(self):
        self.assertIn('xaml', rules(self.check('<Grid Tag="{x:Type m:XamlReader}"/>')))

    def test_xamlreader_in_a_comment_is_allowed(self):
        self.assertEqual(self.check('<!-- XamlReader refuses double hyphens --><Grid/>'), [])


class SvgRules(unittest.TestCase):

    def check(self, body):
        return pc.check_svg_source(
            'a/icon.svg', '<svg xmlns="http://www.w3.org/2000/svg">' + body + '</svg>')

    def test_clean(self):
        self.assertEqual(self.check('<path d="M0 0h1"/>'), [])

    def test_bad_content(self):
        for body in ['<script>alert(1)</script>', '<rect onload="x()"/>',
                     '<foreignObject/>', '<use href="https://evil.example/a.svg#x"/>',
                     '<use xlink:href="//evil.example/a.svg"/>',
                     '<a href="javascript:x()"/>']:
            self.assertIn('svg', rules(self.check(body)), body)

    def test_character_references_and_scheme_whitespace(self):
        for body in ['<a href="&#106;avascript:x()"/>',
                     '<a href="java\tscript:x()"/>',
                     '<a href="java&#9;script:x()"/>',
                     '<use href="&#104;ttp://evil.example/a.svg#x"/>',
                     '<use href="&#x68;ttps://evil.example/a.svg#x"/>',
                     '<use xlink:href="h\nttp://evil.example/a.svg#x"/>',
                     '<image href="&#100;ata:image/png;base64,AAAA"/>']:
            self.assertIn('svg', rules(self.check(body)), body)

    def test_escaped_markup_in_text_is_text(self):
        self.assertEqual(self.check('<text>&lt;script&gt;alert(1)&lt;/script&gt;</text>'), [])

    def test_prefixed_script_element(self):
        for body in ['<svg:script>alert(1)</svg:script>', '<x:SCRIPT>alert(1)</x:SCRIPT>']:
            self.assertIn('svg', rules(self.check(body)), body)

    def test_entity(self):
        found = pc.check_svg_source('a/icon.svg', '<!DOCTYPE svg [<!ENTITY x "y">]><svg/>')
        self.assertIn('svg', rules(found))


class RepoRules(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, rel, data):
        path = os.path.join(self.root, *rel.split('/'))
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        if not isinstance(data, bytes):
            data = data.encode('utf-8')
        with open(path, 'wb') as fh:
            fh.write(data)

    def check(self, config=CONFIG):
        return pc.check_repo(self.root, config)

    def test_clean_repo(self):
        self.write('README.md', '# Hi\n')
        self.write('LICENSE', 'MIT\n')
        self.write('lib/a.py', 'import os\n')
        self.write('lib/a.json', '{"a": 1}\n')
        self.write('A.tab/bundle.yaml', 'layout:\n  - B\n')
        self.write('A.tab/B.panel/X.pushbutton/icon.png', tiny_png())
        self.write('.github/workflows/c.yml', 'on: push\n')
        self.write('.github/CODEOWNERS', '* @someone\n')
        self.assertEqual(self.check(), [])

    def test_disallowed_extensions(self):
        for rel in ['lib/run.bat', 'lib/a.dll', 'lib/a.cs', 'x.yml', '.github/a.exe']:
            self.write(rel, 'x')
        found = self.check()
        self.assertEqual(sorted(f.path for f in found if f.rule == 'file-type'),
                         ['.github/a.exe', 'lib/a.cs', 'lib/a.dll', 'lib/run.bat', 'x.yml'])

    def test_font_hashes(self):
        data = b'\x00\x01\x00\x00font'
        self.write('lib/f.ttf', data)
        good = dict(CONFIG, font_hashes={'lib/f.ttf': hashlib.sha256(data).hexdigest()})
        self.assertEqual(self.check(good), [])
        self.assertIn('binary', rules(self.check()))
        bad = dict(CONFIG, font_hashes={'lib/f.ttf': '0' * 64})
        self.assertIn('binary', rules(self.check(bad)))

    def test_png_rules(self):
        self.write('a.png', tiny_png())
        self.assertEqual(self.check(), [])
        self.assertIn('binary', rules(self.check(dict(CONFIG, png_max_bytes=10))))
        self.write('a.png', tiny_png() + b'MZ\x90\x00')
        self.assertIn('binary', rules(self.check()))
        self.write('a.png', b'not a png at all')
        self.assertIn('binary', rules(self.check()))

    def test_binary_inside_a_text_file(self):
        self.write('lib/a.txt', b'abc\x00def')
        self.assertIn('binary', rules(self.check()))

    def test_unparseable_json_and_yaml(self):
        self.write('lib/a.json', '{')
        self.write('A.tab/bundle.yaml', 'layout: [\n')
        self.assertEqual(sorted(f.path for f in self.check() if f.rule == 'parse'),
                         ['A.tab/bundle.yaml', 'lib/a.json'])

    def test_python_rules_skip_the_tooling_zone(self):
        self.write('.github/scripts/x.py', 'import subprocess\n')
        self.assertEqual(self.check(), [])

    def test_python_rules_apply_to_the_extension(self):
        self.write('lib/x.py', 'import subprocess\n')
        self.assertIn('process', rules(self.check()))

    def test_main_exit_codes(self):
        self.write('lib/a.py', 'import os\n')
        cfg = os.path.join(self.root, 'policy.json')
        with io.open(cfg, 'w', encoding='utf-8') as fh:
            fh.write('{"tooling_dirs": [".github/"], "assemblies": [], "font_hashes": {}, '
                     '"png_max_bytes": 65536, "telemetry": {"module": null, "url": null}}')
        # policy.json itself sits in the extension zone of this fixture: allowed (.json)
        self.assertEqual(run_main(['--root', self.root, '--config', cfg]), 0)
        self.write('lib/b.py', 'import socket\n')
        self.assertEqual(run_main(['--root', self.root, '--config', cfg]), 1)

    def test_symlink_on_disk(self):
        self.write('lib/a.py', 'import os\n')
        try:
            os.symlink('a.py', os.path.join(self.root, 'lib', 'b.py'))
        except (AttributeError, NotImplementedError, OSError):
            self.skipTest('symlinks cannot be created here')
        self.assertEqual([(f.path, f.rule, f.message) for f in self.check()],
                         [('lib/b.py', 'file-type', 'symlinks are not allowed')])


PATTERN_TYPE = type(re.compile(''))


def _patterns(value):
    """Compiled patterns in a module value, including those nested in tuples."""
    if isinstance(value, PATTERN_TYPE):
        return [value]
    if isinstance(value, (tuple, list)):
        return [pattern for item in value for pattern in _patterns(item)]
    return []


def _scan(pattern):
    return lambda text: list(pattern.finditer(text))


class Timing(unittest.TestCase):
    """A crafted file must not stall the gate: every pattern runs in linear time."""

    SIZE = 200 * 1024
    LIMIT = 1.0  # seconds

    def inputs(self):
        n = self.SIZE
        return {
            'one tag, one long word': '<' + 'a' * n,
            'long word run': 'a' * n,
            'tags of long words': ('<' + 'a' * 99) * (n // 100),
            'prefixed long words': ('<' + 'a' * 99 + ':') * (n // 101),
            'alternating quotes': '"\'' * (n // 2),
            'one tag of alternating quotes': '<' + '"\'' * (n // 2),
            'open tags and quotes': '<"' * (n // 2),
            'values left open': ('<a="' + "b='") * (n // 8),
            'repeated clr-namespace': 'clr-namespace:' * (n // 14),
            'reference digits': '&#' + '0' * n,
            'references': '&#1' * (n // 3),
            'backslashes': '\\' * n,
            'slashes': '/' * n,
            'equals and spaces': '=' + ' ' * n,
            'repeated scheme': 'http://' * (n // 7),
            'words starting with on': ' on' * (n // 3),
            'base64 runs just short': ('A' * 99 + ' ') * (n // 100),
            'hex runs just short': ('a' * 79 + ' ') * (n // 80),
            'many findings on many lines': '<script <x:Code\n' * (n // 16),
        }

    def slow(self, label, run):
        found = []
        for name, text in sorted(self.inputs().items()):
            start = time.time()
            run(text)
            elapsed = time.time() - start
            if elapsed > self.LIMIT:
                found.append('{0} on "{1}": {2:.1f}s'.format(label, name, elapsed))
        return found

    def test_every_pattern_is_linear(self):
        slow = []
        for name, value in sorted(vars(pc).items()):
            for pattern in _patterns(value):
                slow.extend(self.slow('{0} {1!r}'.format(name, pattern.pattern), _scan(pattern)))
        self.assertEqual(slow, [])

    def test_checks_are_linear(self):
        slow = self.slow('xaml', lambda text: pc.check_xaml_source('a.xaml', text, CONFIG))
        slow += self.slow('svg', lambda text: pc.check_svg_source('a.svg', text))
        slow += self.slow('python', lambda text: pc.check_python_source(
            'a.py', 'X = %r\n' % ('xmlns ' + text), CONFIG))
        self.assertEqual(slow, [])


def _force_remove(func, path, _exc_info):
    # git marks its object files read-only, which Windows refuses to delete.
    os.chmod(path, stat.S_IWRITE)
    func(path)


class GitSymlinks(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, onerror=_force_remove)

    def test_symlink_recorded_in_the_git_index(self):
        # A Windows checkout without symlink support writes it as a plain file.
        with open(os.path.join(self.root, 'a.py'), 'wb') as fh:
            fh.write(b'import os\n')
        subprocess.check_call(['git', 'init', '-q', self.root])
        git = ['git', '-C', self.root, '-c', 'core.autocrlf=false']
        blob = subprocess.check_output(git + ['hash-object', '-w', 'a.py']).decode('ascii').strip()
        subprocess.check_call(git + ['update-index', '--add', '--cacheinfo',
                                     '120000,{0},lib/link.py'.format(blob)])
        self.assertEqual([(f.path, f.rule) for f in pc.check_repo(self.root, CONFIG)],
                         [('lib/link.py', 'file-type')])


if __name__ == '__main__':
    unittest.main()
