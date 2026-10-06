# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import structure_check as sc  # noqa: E402

CONFIG = {
    "pool_panel": "Favorites.panel",
    "groups": "lib/groups.json",
    "icons": ["icon.png", "icon.dark.png", "icon.svg", "icon.dark.svg"],
    "root_entries": [".github", "lib", "README.md", "CHANGELOG.md", "LICENSE",
                     "LICENSE-CONTENT", "extension.json", "startup.py",
                     ".gitattributes", ".gitignore"],
    "bundle_types": [".tab", ".panel", ".stack", ".pulldown", ".splitbutton",
                     ".pushbutton"],
    "bundle_keys": ["layout", "title", "tooltip", "author"],
}
SCRIPT = '__title__ = "X"\n__doc__ = "Does X."\n'
TAB = 'Magic-tools.tab'


class Capture(object):
    """Stands in for sys.stdout, on Python 2 and 3."""

    def __init__(self):
        self.parts = []

    def write(self, text):
        self.parts.append(text)

    def flush(self):
        pass

    def getvalue(self):
        return ''.join(self.parts)


class Structure(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.write(TAB + '/bundle.yaml', 'layout:\n  - Tools\n  - Favorites\n')
        self.write(TAB + '/Tools.panel/bundle.yaml', 'layout:\n  - Gallery\n  - Nav\n')
        self.button(TAB + '/Tools.panel/Gallery.pushbutton')
        self.write(TAB + '/Tools.panel/Nav.stack/bundle.yaml', 'layout:\n  - Next\n')
        self.button(TAB + '/Tools.panel/Nav.stack/Next.pushbutton')
        self.write(TAB + '/Favorites.panel/bundle.yaml',
                   'layout:\n  - Rename\n  - "--- Analysis"\n  - Inspect\n')
        self.button(TAB + '/Favorites.panel/Rename.pushbutton')
        self.button(TAB + '/Favorites.panel/Inspect.pushbutton')
        self.groups([{"group": "Actions", "tools": ["Rename"]},
                     {"group": "Analysis", "tools": ["Inspect"]}])
        self.write('README.md', '# x\n')
        self.write('startup.py', 'import os\n')

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, rel, text):
        path = os.path.join(self.root, *rel.split('/'))
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with io.open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)

    def remove(self, rel):
        path = os.path.join(self.root, *rel.split('/'))
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)

    def button(self, rel, script=SCRIPT):
        self.write(rel + '/script.py', script)
        for icon in CONFIG['icons']:
            self.write(rel + '/' + icon, 'x')

    def groups(self, data):
        self.write('lib/groups.json', json.dumps(data))

    def check(self):
        return sc.check_structure(self.root, CONFIG)

    def run_main(self, argv):
        """Exit code of main(argv), with its printed report kept out of the test log."""
        saved, sys.stdout = sys.stdout, Capture()
        try:
            return sc.main(argv)
        finally:
            sys.stdout = saved

    def assertFlags(self, needle):
        found = self.check()
        self.assertTrue(any(needle in f.path or needle in f.message for f in found),
                        [sc.format_finding(f) for f in found])
        self.assertTrue(all(f.rule == 'structure' for f in found))

    def test_valid_tree_passes(self):
        self.assertEqual(self.check(), [])

    def test_missing_icon(self):
        self.remove(TAB + '/Favorites.panel/Rename.pushbutton/icon.dark.svg')
        self.assertFlags('icon.dark.svg')

    def test_missing_script(self):
        self.remove(TAB + '/Tools.panel/Nav.stack/Next.pushbutton/script.py')
        self.assertFlags('Next.pushbutton')

    def test_missing_title(self):
        self.button(TAB + '/Favorites.panel/Rename.pushbutton', '__doc__ = "x"\n')
        self.assertFlags('__title__')

    def test_missing_doc(self):
        self.button(TAB + '/Favorites.panel/Rename.pushbutton', '__title__ = "x"\n')
        self.assertFlags('__doc__')

    def test_module_docstring_counts_as_doc(self):
        self.button(TAB + '/Favorites.panel/Rename.pushbutton',
                    '"""Does X."""\n__title__ = "x"\n')
        self.assertEqual(self.check(), [])

    def test_folder_missing_from_layout(self):
        self.button(TAB + '/Tools.panel/Nav.stack/Orphan.pushbutton')
        self.assertFlags('Orphan')

    def test_layout_entry_without_folder(self):
        self.write(TAB + '/Tools.panel/bundle.yaml', 'layout:\n  - Gallery\n  - Nav\n  - Ghost\n')
        self.assertFlags('Ghost')

    def test_panel_missing_from_tab_layout(self):
        self.write(TAB + '/bundle.yaml', 'layout:\n  - Tools\n')
        self.assertFlags('Favorites')

    def test_pool_tool_missing_from_groups(self):
        self.groups([{"group": "Actions", "tools": ["Rename"]}])
        self.assertFlags('Inspect')

    def test_groups_entry_without_folder(self):
        self.groups([{"group": "Actions", "tools": ["Rename", "Inspect", "Ghost"]}])
        self.assertFlags('Ghost')

    def test_unknown_bundle_type(self):
        self.write(TAB + '/Tools.panel/Web.urlbutton/bundle.yaml', 'hyperlink: "x"\n')
        self.assertFlags('Web.urlbutton')

    def test_unknown_root_entry(self):
        self.write('hooks/doc-opened.py', 'x = 1\n')
        self.assertFlags('hooks')

    def test_unknown_bundle_key(self):
        self.write(TAB + '/Tools.panel/Nav.stack/bundle.yaml',
                   'layout:\n  - Next\nengine:\n  clean: true\n')
        self.assertFlags('engine')

    def test_main_exit_codes(self):
        cfg = os.path.join(self.root, 'lib', 'policy.json')
        with io.open(cfg, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps({'structure': CONFIG}, ensure_ascii=False))
        self.assertEqual(self.run_main(['--root', self.root, '--config', cfg]), 0)
        self.remove(TAB + '/Favorites.panel/Rename.pushbutton/icon.png')
        self.assertEqual(self.run_main(['--root', self.root, '--config', cfg]), 1)

    # Beyond the brief: edges of the rules above.

    def test_no_groups_file_means_no_group_checks(self):
        self.remove('lib/groups.json')
        self.assertEqual(self.check(), [])

    def test_unreadable_bundle_yaml(self):
        self.write(TAB + '/Tools.panel/bundle.yaml', 'layout: [Gallery\n')
        self.assertFlags('bundle.yaml')

    def test_script_that_does_not_parse(self):
        self.button(TAB + '/Favorites.panel/Rename.pushbutton', 'def broken(:\n')
        self.assertFlags('cannot be parsed')

    def test_script_with_an_encoding_line(self):
        self.button(TAB + '/Favorites.panel/Rename.pushbutton',
                    '# -*- coding: utf-8 -*-\n__title__ = "X"\n__doc__ = "Does X."\n')
        self.assertEqual(self.check(), [])

    def test_title_inside_a_function_is_not_module_level(self):
        self.button(TAB + '/Favorites.panel/Rename.pushbutton',
                    'def f():\n    __title__ = "x"\n__doc__ = "x"\n')
        self.assertFlags('__title__')

    def test_unreadable_config_exits_with_2(self):
        missing = os.path.join(self.root, 'missing.json')
        self.assertEqual(self.run_main(['--root', self.root, '--config', missing]), 2)

    # pyRevit finds script.py, config.py and bundle.yaml by suffix (the first
    # os.listdir entry that ends with the name on the legacy loader, a
    # case-insensitive match on the new one), so a decoy with a prefix can
    # replace the real file.

    def test_decoy_bundle_yaml_in_a_pushbutton(self):
        self.write(TAB + '/Favorites.panel/Rename.pushbutton/0bundle.yaml',
                   'engine:\n  clean: true\n')
        self.assertFlags('0bundle.yaml')

    def test_decoy_bundle_yaml_in_a_panel(self):
        self.write(TAB + '/Favorites.panel/0bundle.yaml', 'layout:\n  - Inspect\n')
        self.assertFlags('0bundle.yaml')

    def test_decoy_script_next_to_script(self):
        self.write(TAB + '/Favorites.panel/Rename.pushbutton/0script.py', 'x = 1\n')
        self.assertFlags('0script.py')

    def test_config_py_in_a_pushbutton(self):
        self.write(TAB + '/Favorites.panel/Rename.pushbutton/config.py', 'x = 1\n')
        self.assertFlags('config.py')

    def test_decoy_names_are_caught_in_any_folder_of_a_tab(self):
        # A folder with no script.py or bundle.yaml of its own, so that a name
        # that differs only in case cannot overwrite them on Windows.
        for name in ('Ascript.py', 'xconfig.py', 'script.cs', 'Script.py',
                     'config.py.bak', 'Nobundle.yaml', 'Bundle.yaml'):
            folder = TAB + '/Tools.panel/Spare.pushbutton/'
            self.write(folder + name, 'x = 1\n')
            self.assertFlags(name)
            self.remove(folder + name)

    def test_names_that_only_look_similar_are_fine(self):
        self.write(TAB + '/Tools.panel/Nav.stack/scripts.txt', 'x\n')
        self.write(TAB + '/Tools.panel/Nav.stack/configuration.md', 'x\n')
        self.assertEqual(self.check(), [])

    def test_pushbutton_may_hold_a_bundle_yaml(self):
        self.write(TAB + '/Favorites.panel/Rename.pushbutton/bundle.yaml', 'title: Rename\n')
        self.assertEqual(self.check(), [])

    def test_pushbutton_with_any_other_file(self):
        self.write(TAB + '/Favorites.panel/Rename.pushbutton/notes.txt', 'x\n')
        self.assertFlags('notes.txt')

    def test_pushbutton_with_a_subfolder(self):
        self.button(TAB + '/Favorites.panel/Rename.pushbutton/Inner.pushbutton')
        self.assertFlags('Rename.pushbutton/Inner.pushbutton')

    def test_folder_without_a_bundle_suffix_in_a_tab(self):
        self.write(TAB + '/Tools.panel/bin/helper.txt', 'x\n')
        self.assertFlags('bin')

    def test_lib_folder_inside_a_pushbutton(self):
        self.write(TAB + '/Favorites.panel/Rename.pushbutton/lib/helper.txt', 'x\n')
        self.assertFlags('Rename.pushbutton/lib')

    def test_script_file_outside_a_tab_is_not_a_bundle_file(self):
        self.write('lib/0script.py', 'x = 1\n')
        self.assertEqual(self.check(), [])


if __name__ == '__main__':
    unittest.main()
