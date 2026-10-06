# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import pyflakes_check as pfc  # noqa: E402


class Pyflakes(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def path(self, text):
        p = os.path.join(self.root, 'a.py')
        with io.open(p, 'w', encoding='utf-8') as fh:
            fh.write(text)
        return p

    def test_undefined_name_is_an_error(self):
        errors, _ = pfc.check_paths([self.path('x = undefined_thing\n')])
        self.assertEqual(len(errors), 1)

    def test_unused_import_is_a_warning(self):
        errors, warnings = pfc.check_paths([self.path('import os\n')])
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_syntax_error_is_an_error(self):
        errors, _ = pfc.check_paths([self.path('def (:\n')])
        self.assertEqual(len(errors), 1)

    def test_pyrevit_builtins(self):
        os.environ['PYFLAKES_BUILTINS'] = '__revit__'
        try:
            errors, _ = pfc.check_paths([self.path('app = __revit__.Application\n')])
        finally:
            del os.environ['PYFLAKES_BUILTINS']
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
