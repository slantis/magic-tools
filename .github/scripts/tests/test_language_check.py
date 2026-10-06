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

import language_check as lc  # noqa: E402


def h(word):
    return hashlib.sha256(word.encode('utf-8')).hexdigest()


CONFIG = {
    "scan_exts": [".py", ".xaml", ".yaml", ".json", ".md"],
    "stopwords": ["que", "los", "para", "con", "la", "el", "en", "no", "se", "es"],
    "strong": ["tambien", "porque"],
    "name_hashes": [h("zorbaq"), h("quillex")],
    "allow": [{"path": "lib/allowed.py", "text": "Cotizacion"}],
}


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


class Language(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, rel, text):
        path = os.path.join(self.root, *rel.split('/'))
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with io.open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)

    def rules_for(self, rel, text):
        self.write(rel, text)
        return sorted(set(f.rule for f in lc.check_language(self.root, CONFIG)))

    def test_english_comment_passes(self):
        self.assertEqual(self.rules_for(
            'lib/a.py', '# Select every wall of the same type in the view\nx = 1\n'), [])

    def test_spanish_comment(self):
        self.assertEqual(self.rules_for(
            'lib/a.py', '# esto es para que no se rompa con la vista\nx = 1\n'), ['spanish'])

    def test_one_common_word_is_not_enough(self):
        self.assertEqual(self.rules_for('lib/a.py', '# the con of this approach\n'), [])

    def test_code_tokens_are_not_read_as_spanish(self):
        self.assertEqual(self.rules_for(
            'lib/a.py', 'for el in elements:\n    la = el.en\n'), [])

    def test_strong_word(self):
        self.assertEqual(self.rules_for('lib/a.py', '# see tambien\n'), ['spanish'])

    def test_inverted_marks_and_accents(self):
        self.assertEqual(self.rules_for('lib/a.py', "msg = u'\u00bfSure?'\n"), ['spanish'])
        self.assertEqual(self.rules_for('lib/b.py', "# acci\u00f3n\n"), ['spanish'])

    def test_spanish_in_a_docstring(self):
        self.assertEqual(self.rules_for(
            'lib/a.py', 'def f():\n    """Devuelve la vista que se usa."""\n'), ['spanish'])

    def test_names_in_comments_and_code(self):
        self.assertEqual(self.rules_for('lib/a.py', "# Zorbaq's rule\n"), ['name'])
        self.assertEqual(self.rules_for('lib/b.py', 'quillex_rule = 1\n'), ['name'])

    def test_name_with_accent_and_case(self):
        self.assertEqual(self.rules_for('lib/a.py', '# ZORB\u00c1Q said\n'),
                         ['name', 'spanish'])

    def test_name_inside_a_longer_word_is_fine(self):
        self.assertEqual(self.rules_for('lib/a.py', '# zorbaqfilm\n'), [])

    def test_allow_entry(self):
        self.assertEqual(self.rules_for(
            'lib/allowed.py', "LABEL = 'Cotizacion para el cliente'\n"), [])

    def test_tooling_and_other_types_are_skipped(self):
        self.assertEqual(self.rules_for('.github/policy/x.json', '["que", "para"]\n'), [])
        self.assertEqual(self.rules_for('lib/a.svg', '<!-- esto es para que -->\n'), [])

    def test_markdown_is_scanned(self):
        self.assertEqual(self.rules_for('README.md', 'Esto es para la vista\n'), ['spanish'])

    def test_findings_have_lines(self):
        self.write('lib/a.py', 'x = 1\n# esto es para que\n')
        found = lc.check_language(self.root, CONFIG)
        self.assertEqual([(f.path, f.line) for f in found], [('lib/a.py', 2)])

    def test_hash_name(self):
        self.assertEqual(lc.name_hash('Zorb\u00e1q'), h('zorbaq'))

    # Beyond the brief: edges of the rules above.

    def run_main(self, argv):
        saved, sys.stdout = sys.stdout, Capture()
        try:
            return lc.main(argv), sys.stdout.getvalue()
        finally:
            sys.stdout = saved

    def test_letters_written_as_two_code_points(self):
        # a letter plus a combining acute accent, as some editors save it
        self.assertEqual(self.rules_for('lib/a.py', '# acci\u006f\u0301n\n'), ['spanish'])
        self.assertEqual(self.rules_for('lib/b.py', '# zorba\u0301q\n'), ['name', 'spanish'])

    def test_each_line_of_a_docstring_is_reported(self):
        self.write('lib/a.py',
                   'def f():\n    """Line one.\n    esto es para que\n    quillex\n    """\n')
        found = lc.check_language(self.root, CONFIG)
        self.assertEqual([(f.line, f.rule) for f in found], [(3, 'spanish'), (4, 'name')])

    def test_python_that_cannot_be_tokenized_is_read_as_text(self):
        self.assertEqual(self.rules_for('lib/a.py', 's = """esto es para que\n'), ['spanish'])

    def test_file_that_is_not_utf8_is_skipped(self):
        path = os.path.join(self.root, 'a.md')
        with open(path, 'wb') as fh:
            fh.write(b'esto es para que \xff\n')
        self.assertEqual(lc.check_language(self.root, CONFIG), [])

    def test_names_are_not_echoed_in_findings(self):
        self.write('lib/a.py', '# Zorbaq\n')
        found = lc.check_language(self.root, CONFIG)
        self.assertEqual([f.rule for f in found], ['name'])
        self.assertNotIn('zorbaq', found[0].message.lower())

    def test_main_exit_codes(self):
        cfg_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg_dir)
        cfg = os.path.join(cfg_dir, 'language.json')
        with io.open(cfg, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(CONFIG))
        self.write('lib/a.py', '# fine\n')
        self.assertEqual(self.run_main(['--root', self.root, '--config', cfg])[0], 0)
        self.write('lib/a.py', '# esto es para que\n')
        code, out = self.run_main(['--root', self.root, '--config', cfg])
        self.assertEqual(code, 1)
        self.assertIn('lib/a.py:1: [spanish]', out)
        missing = os.path.join(cfg_dir, 'missing.json')
        self.assertEqual(self.run_main(['--root', self.root, '--config', missing])[0], 2)

    def test_hash_name_command(self):
        code, out = self.run_main(['--hash-name', 'Zorb\u00e1q'])
        self.assertEqual((code, out.strip()), (0, h('zorbaq')))


if __name__ == '__main__':
    unittest.main()
