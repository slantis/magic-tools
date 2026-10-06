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

    # The name rule reads every text file outside .github/, and every path.

    def test_names_in_every_text_file_type(self):
        self.assertEqual(self.rules_for('lib/a.svg', '<!-- Zorbaq -->\n'), ['name'])
        self.assertEqual(self.rules_for('lib/a.txt', 'quillex\n'), ['name'])
        self.assertEqual(self.rules_for('lib/a.yml', 'owner: quillex\n'), ['name'])
        self.assertEqual(self.rules_for('LICENSE', 'Copyright Zorbaq\n'), ['name'])
        self.assertEqual(self.rules_for('.gitignore', '# Zorbaq\n'), ['name'])

    def test_spanish_is_still_limited_to_scan_exts(self):
        self.assertEqual(self.rules_for('LICENSE', 'esto es para que\n'), [])
        self.assertEqual(self.rules_for('lib/a.txt', 'esto es para que\n'), [])

    def test_text_under_dot_github_is_still_skipped_for_names(self):
        self.assertEqual(self.rules_for('.github/CODEOWNERS', '* @zorbaq\n'), [])

    def test_names_in_file_and_folder_names(self):
        self.write('lib/Zorbaq/a.png', 'x')
        self.write('lib/quillex_tools.png', 'x')
        self.write('lib/fine/b.png', 'x')
        found = lc.check_language(self.root, CONFIG)
        self.assertEqual([(f.path, f.line, f.rule) for f in found],
                         [('lib/Zorbaq/a.png', 0, 'name'),
                          ('lib/quillex_tools.png', 0, 'name')])

    def test_names_in_the_path_of_a_tooling_file(self):
        self.assertEqual(self.rules_for('.github/scripts/zorbaq.py', 'x = 1\n'), ['name'])

    # CamelCase is split in addition to the plain word split.

    def test_camel_case_names(self):
        for text in ('ZorbaqRule = 1\n', 'ruleOfZorbaq = 1\n', 'ZorbaqTools = 1\n',
                     'QUILLEXTools = 1\n', '# see ZorbaqTools\n'):
            self.assertEqual(self.rules_for('lib/a.py', text), ['name'], text)
        self.assertEqual(self.rules_for('README.md', 'See ZorbaqTools\n'), ['name'])

    def test_camel_case_does_not_flag_longer_words(self):
        for text in ('Zorbaqfilm = 1\n', 'ZORBAQFILM = 1\n', 'aZorbaqb = 1\n'):
            self.assertEqual(self.rules_for('lib/a.py', text), [], text)

    # String literals are decoded before the words are read.

    def test_escapes_in_strings_are_decoded(self):
        self.assertEqual(self.rules_for('lib/a.py', "x = u'acci\\u00f3n'\n"), ['spanish'])
        self.assertEqual(self.rules_for('lib/a.py', "x = u'Zorb\\xe1q'\n"), ['name', 'spanish'])
        self.assertEqual(self.rules_for('lib/a.py', "x = 'Zorb\\xe1q'\n"), ['name', 'spanish'])
        self.assertEqual(self.rules_for('lib/a.py', "x = 'a\\nzorbaq'\n"), ['name'])
        self.assertEqual(self.rules_for('lib/a.py', "x = b'acci\\xc3\\xb3n'\n"), ['spanish'])

    def test_plain_str_escapes_are_read_as_utf8_on_every_python(self):
        # Python 2 bytes: two escapes spell one letter. Python 3 text would
        # read them as two letters, so the check reads both the Python 2 way.
        self.assertEqual(self.rules_for('lib/a.py', "x = 'acci\\xc3\\xb3n'\n"), ['spanish'])
        # A UTF-8 byte order mark written as escapes is not an inverted mark.
        self.assertEqual(self.rules_for(
            'lib/a.py', 'if raw.startswith("\\xef\\xbb\\xbf"):  # tolerate a BOM\n'), [])

    def test_binary_data_is_not_read_as_letters(self):
        # Magic numbers and other binary data: a bytes literal that is not
        # UTF-8, or a plain str with control characters, is read as written
        # for the spanish rule (b'\001\332' would be an accented u).
        for literal in ("b'\\001\\332'", "'\\001\\332'", "b'\\xe1\\xe9'", "b'\\xff\\xd8\\xff\\xe0'",
                        "'\\x00\\xe1\\xe9'", "'\\x1b\\xf3'"):
            self.assertEqual(self.rules_for('lib/a.py', 'x = %s\n' % literal), [], literal)

    def test_plain_str_with_latin1_escapes_is_text(self):
        # IronPython's str is unicode: the button shows the accented text.
        for literal in ("'\\xbfSure?'", "'acci\\xf3n'", "'\\xe1\\xe9'", "'acci\\xf3n\\t'"):
            self.assertEqual(self.rules_for('lib/a.py', 'x = %s\n' % literal),
                             ['spanish'], literal)

    def test_name_in_a_bytes_literal_that_is_not_utf8(self):
        # Read as latin-1 for the name rule; bytes stay raw for spanish.
        self.assertEqual(self.rules_for('lib/a.py', "x = b'Zorb\\xe1q'\n"), ['name'])

    def test_plain_str_mixing_a_wide_character_and_an_escape(self):
        # One reading on Python 2 and 3: the literal is text, so every escape
        # is a code point. A character beyond latin-1 rules out reading the
        # escapes as UTF-8 bytes.
        self.assertEqual(self.rules_for('lib/a.py', "x = 'Zorb\\xe1q\\u20ac'\n"),
                         ['name', 'spanish'])
        self.assertEqual(self.rules_for('lib/a.py', "x = 'acci\\xc3\\xb3n\\u20ac'\n"), [])

    def test_literal_holding_the_internal_line_marker_keeps_its_lines(self):
        # An escaped x01 inside a docstring decodes to the character the
        # check uses internally for a newline of the source.
        self.write('lib/a.py',
                   'def f():\n    """First.\n    a\\x01b\n    esto es para que\n    """\n')
        found = lc.check_language(self.root, CONFIG)
        self.assertEqual([(f.line, f.rule) for f in found], [(4, 'spanish')])

    def test_escaped_newline_in_a_docstring_keeps_the_line_numbers(self):
        self.write('lib/a.py', 'def f():\n    """First.\n    Line\\nPatterns quillex\n'
                               '    esto es para que\n    """\n')
        found = lc.check_language(self.root, CONFIG)
        self.assertEqual([(f.line, f.rule) for f in found], [(3, 'name'), (4, 'spanish')])

    def test_escaped_text_is_reported_on_its_line(self):
        self.write('lib/a.py', "x = 1\ny = 'ok\\nesto es para que'\n")
        found = lc.check_language(self.root, CONFIG)
        self.assertEqual([(f.line, f.rule) for f in found], [(2, 'spanish')])

    def test_string_that_cannot_be_decoded_is_read_raw(self):
        self.assertEqual(self.rules_for(
            'lib/a.py', "x = '\\N{nope} esto es para que'\n"), ['spanish'])

    # The allow list: schema and what an entry silences.

    def test_empty_allow_text_is_a_config_error(self):
        for entry in ({'path': 'lib/a.py', 'text': ''}, {'path': '', 'text': 'x'},
                      {'path': 'lib/a.py'}):
            config = dict(CONFIG, allow=[entry])
            self.assertRaises(lc.ConfigError, lc.check_language, self.root, config)

    def test_bad_allow_rules_are_a_config_error(self):
        for rules in ([], ['other'], 'spanish'):
            config = dict(CONFIG, allow=[{'path': 'lib/a.py', 'text': 'x', 'rules': rules}])
            self.assertRaises(lc.ConfigError, lc.check_language, self.root, config)

    def test_allow_silences_only_spanish_by_default(self):
        config = dict(CONFIG, allow=[{'path': 'lib/a.py', 'text': 'esto es'}])
        self.write('lib/a.py', '# esto es para que zorbaq\n')
        found = lc.check_language(self.root, config)
        self.assertEqual([f.rule for f in found], ['name'])

    def test_allow_can_silence_a_name(self):
        config = dict(CONFIG, allow=[{'path': 'lib/a.py', 'text': 'zorbaq', 'rules': ['name']}])
        self.write('lib/a.py', '# esto es para que zorbaq\n')
        found = lc.check_language(self.root, config)
        self.assertEqual([f.rule for f in found], ['spanish'])

    def test_allow_can_silence_both(self):
        config = dict(CONFIG, allow=[{'path': 'lib/a.py', 'text': 'zorbaq',
                                      'rules': ['spanish', 'name']}])
        self.write('lib/a.py', '# esto es para que zorbaq\n')
        self.assertEqual(lc.check_language(self.root, config), [])

    def test_main_reports_a_bad_allow_entry(self):
        cfg_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg_dir)
        cfg = os.path.join(cfg_dir, 'language.json')
        with io.open(cfg, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(dict(CONFIG, allow=[{'path': 'lib/a.py', 'text': ''}])))
        code, out = self.run_main(['--root', self.root, '--config', cfg])
        self.assertEqual(code, 2)
        self.assertIn('allow', out)


if __name__ == '__main__':
    unittest.main()
