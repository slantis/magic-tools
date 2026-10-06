# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import release_manifest as rm  # noqa: E402


class Manifest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, rel, data):
        path = os.path.join(self.root, *rel.split('/'))
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, 'wb') as fh:
            fh.write(data)

    def test_lists_every_file_sorted_with_sha256(self):
        self.write('b.py', b'x = 1\n')
        self.write('A.tab/a b.pushbutton/script.py', b'y = 2\n')
        m = rm.build_manifest(self.root, 'v1.2.3', 'c' * 40)
        self.assertEqual(m['schema'], 1)
        self.assertEqual(m['tag'], 'v1.2.3')
        self.assertEqual(m['commit'], 'c' * 40)
        self.assertEqual([f['path'] for f in m['files']],
                         ['A.tab/a b.pushbutton/script.py', 'b.py'])
        self.assertEqual(m['files'][1]['sha256'], hashlib.sha256(b'x = 1\n').hexdigest())

    def test_text_is_hashed_with_lf(self):
        self.write('a.py', b'x = 1\r\ny = 2\r\n')
        m = rm.build_manifest(self.root, 'v1', 'c' * 40)
        self.assertEqual(m['files'][0]['sha256'],
                         hashlib.sha256(b'x = 1\ny = 2\n').hexdigest())

    def test_binary_is_hashed_raw(self):
        data = b'\x89PNG\r\n\x1a\n\r\n'
        self.write('i.png', data)
        m = rm.build_manifest(self.root, 'v1', 'c' * 40)
        self.assertEqual(m['files'][0]['sha256'], hashlib.sha256(data).hexdigest())

    def test_cli_writes_the_json(self):
        self.write('a.py', b'x\n')
        out = os.path.join(tempfile.mkdtemp(), 'release-manifest.json')
        self.assertEqual(rm.main(['--root', self.root, '--tag', 'v1', '--commit', 'c' * 40,
                                  '--out', out]), 0)
        with io.open(out, encoding='utf-8') as fh:
            data = json.load(fh)
        self.assertEqual(data['files'][0]['path'], 'a.py')
        with open(out, 'rb') as fh:
            self.assertNotIn(b'\r\n', fh.read())
        shutil.rmtree(os.path.dirname(out))


if __name__ == '__main__':
    unittest.main()
