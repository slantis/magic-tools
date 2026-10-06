# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ci_common  # noqa: E402


def _force_remove(func, path, _exc_info):
    # git marks its object files read-only, which Windows refuses to delete.
    os.chmod(path, stat.S_IWRITE)
    func(path)


class Files(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, onerror=_force_remove)

    def write(self, rel, data):
        path = os.path.join(self.root, *rel.split('/'))
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, 'wb') as fh:
            fh.write(data)
        return path

    def test_list_files_without_git(self):
        self.write('b.py', b'x')
        self.write('A.tab/a b.pushbutton/script.py', b'x')
        self.write('.git/config', b'x')
        self.assertEqual(ci_common.list_files(self.root),
                         ['A.tab/a b.pushbutton/script.py', 'b.py'])

    def test_list_files_in_a_git_work_tree_skips_ignored(self):
        self.write('.gitignore', b'*.pyc\n')
        self.write('a.py', b'x')
        self.write('a.pyc', b'x')
        self.write('lib/new.py', b'x')
        subprocess.check_call(['git', 'init', '-q', self.root])
        subprocess.check_call(['git', '-C', self.root, '-c', 'core.autocrlf=false',
                               'add', 'a.py', '.gitignore'])
        self.assertEqual(ci_common.list_files(self.root),
                         ['.gitignore', 'a.py', 'lib/new.py'])

    def test_text_digest_ignores_crlf(self):
        crlf = self.write('a.py', b'x = 1\r\ny = 2\r\n')
        lf = self.write('b.py', b'x = 1\ny = 2\n')
        self.assertEqual(ci_common.file_digest(crlf), ci_common.file_digest(lf))
        self.assertEqual(ci_common.file_digest(lf),
                         hashlib.sha256(b'x = 1\ny = 2\n').hexdigest())

    def test_binary_digest_is_raw(self):
        data = b'\x89PNG\r\n\x1a\n\r\n'
        path = self.write('i.png', data)
        self.assertEqual(ci_common.file_digest(path), hashlib.sha256(data).hexdigest())

    def test_is_text(self):
        self.assertTrue(ci_common.is_text('lib/a.py'))
        self.assertTrue(ci_common.is_text('LICENSE'))
        self.assertTrue(ci_common.is_text('.github/CODEOWNERS'))
        self.assertFalse(ci_common.is_text('a/icon.png'))
        self.assertFalse(ci_common.is_text('lib/f.ttf'))

    def test_is_tooling(self):
        self.assertTrue(ci_common.is_tooling('.github/scripts/x.py'))
        self.assertFalse(ci_common.is_tooling('lib/x.py'))

    def test_format_finding(self):
        f = ci_common.Finding('lib/a.py', 3, 'network', 'imports socket')
        self.assertEqual(ci_common.format_finding(f), 'lib/a.py:3: [network] imports socket')


if __name__ == '__main__':
    unittest.main()
