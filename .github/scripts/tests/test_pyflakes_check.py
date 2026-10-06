# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import io
import os
import shutil
import subprocess
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

    def path(self, text, name='a.py'):
        p = os.path.join(self.root, name)
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

    def test_error_that_only_compile_finds_is_an_error(self):
        # ast.parse accepts a nonlocal at module level on Python 3 (the
        # statement does not exist on Python 2); compile() rejects it.
        errors, _ = pfc.check_paths([self.path('nonlocal x\n')])
        self.assertEqual(len(errors), 1)
        self.assertIn('SyntaxError', errors[0])

    def test_future_feature_error_is_an_error(self):
        # Python 2's ast.parse accepts this; its compile() rejects it.
        errors, _ = pfc.check_paths([self.path('from __future__ import braces\n')])
        self.assertEqual(len(errors), 1)

    def test_return_with_a_value_in_a_generator_follows_the_interpreter(self):
        # Python 2's ast.parse accepts it and its compile() raises a
        # SyntaxError; Python 3 allows it.
        errors, _ = pfc.check_paths([self.path('def f():\n    yield 1\n    return 2\n')])
        self.assertEqual(len(errors), 1 if sys.version_info[0] == 2 else 0)

    def test_compile_does_not_inherit_the_flags_of_the_checker(self):
        # The checker itself uses print_function; the checked code is still
        # compiled with the grammar of the interpreter it runs on.
        errors, _ = pfc.check_paths([self.path('print "hello"\n')])
        self.assertEqual(len(errors), 0 if sys.version_info[0] == 2 else 1)

    def test_non_ascii_file_name(self):
        path = self.path('x = undefined_thing\n', name='café.py')
        errors, _ = pfc.check_paths([path])
        self.assertEqual(len(errors), 1)
        self.assertIn('café.py', errors[0])

    def test_main_prints_a_non_ascii_file_name_to_a_pipe(self):
        self.path('x = undefined_thing\n', name='café.py')
        script = os.path.join(os.path.dirname(HERE), 'pyflakes_check.py')
        proc = subprocess.Popen([sys.executable, script, '--root', self.root],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate()
        self.assertEqual(proc.returncode, 1, err)
        self.assertIn(b'caf\\xe9.py', out)

    def test_pyrevit_builtins(self):
        os.environ['PYFLAKES_BUILTINS'] = '__revit__'
        try:
            errors, _ = pfc.check_paths([self.path('app = __revit__.Application\n')])
        finally:
            del os.environ['PYFLAKES_BUILTINS']
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
