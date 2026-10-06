# -*- coding: utf-8 -*-
"""Language check: no Spanish text and no team member names in the public code.

Two rules:

- spanish: a line of text that looks Spanish. It does when it has an inverted
  mark, a word that only Spanish uses, a letter with an accent or n-tilde, or
  two different common Spanish words. Read: files whose extension is in
  scan_exts, outside .github/. In Python only comments and strings count (a
  string is read after its escapes are decoded, so '\\xe1' is seen as an
  accent); in the other types every line does.
- name: a word that is the name of a team member. The names are not in the
  repository: language.json keeps only the SHA-256 of each folded name (see
  --hash-name). Read: every text file outside .github/ (the types and names
  that ci_common.is_text accepts), all of each file, and the name of every
  file and folder on every path, .github/ included. CamelCase is split too, so
  ZorbaqRule, ruleOfZorbaq and ZorbaqTools are caught; Zorbaqfilm is not.

language.json:

    scan_exts    extensions the spanish rule reads, such as ".py"
    stopwords    common Spanish words, folded (no accents, lower case)
    strong       words that only Spanish uses, folded
    name_hashes  name_hash() of each team member name
    allow        [{"path": "lib/a.py", "text": "some text on the line",
                   "rules": ["spanish"]}, ...]

An allow entry silences the listed rules, on the lines of that file that
contain its text. "rules" is optional and defaults to ["spanish"]: a name
needs "rules": ["name"] (or both) to be silenced. "path" and "text" must not
be empty. A name found in a file or folder name cannot be allowed: rename it.

Usage:
    python .github/scripts/language_check.py --root . [--config .github/policy/language.json]
    python .github/scripts/language_check.py --hash-name NAME

Exit code 0 when clean, 1 when there are findings, 2 when the configuration
cannot be read or is not valid.

Runs on Python 2.7 (the checks run in a python:2.7 container) and Python 3.
"""
from __future__ import print_function

import argparse
import ast
import codecs
import hashlib
import io
import json
import os
import re
import sys
import tokenize
import unicodedata
import warnings

from ci_common import (Finding, format_finding, is_text, is_tooling, list_files,
                       load_json, read_text)

DEFAULT_CONFIG = '.github/policy/language.json'
RULES = ('spanish', 'name')
# Letters made of one or two code points: compare after NFC, before folding.
ACCENTED = u'\xe1\xe9\xed\xf3\xfa\xf1'
INVERTED_MARKS = u'\xbf\xa1'
BOM = codecs.BOM_UTF8.decode('utf-8')
LINE_BREAK = u'\x01'  # stands for a newline of the source inside a string literal
NEWLINES = u'\t\r\n' + LINE_BREAK
WORD = re.compile(r'[^\W\d_]+', re.UNICODE)
# Tokens whose text is prose: comments and strings (f-string pieces, from
# Python 3.12, are named FSTRING_MIDDLE).
TEXT_TOKENS = ('COMMENT', 'STRING')


class ConfigError(ValueError):
    """language.json is not valid."""


def _text(value):
    # Same as the private ci_common._text, which this change does not touch.
    return value.decode('utf-8') if isinstance(value, bytes) else value


def fold(word):
    """Lower case and no diacritical marks: an accented a folds to a."""
    decomposed = unicodedata.normalize('NFKD', _text(word))
    return u''.join(c for c in decomposed if not unicodedata.combining(c)).lower()


def name_hash(word):
    return hashlib.sha256(fold(word).encode('utf-8')).hexdigest()


def _words(text):
    return WORD.findall(unicodedata.normalize('NFC', text))


def _camel_parts(word):
    """'ruleOfZorbaq' -> rule, Of, Zorbaq; 'QUILLEXTools' -> QUILLEX, Tools."""
    parts, start = [], 0
    for i in range(1, len(word)):
        before, here, after = word[i - 1], word[i], word[i + 1:i + 2]
        if here.isupper() and (before.islower() or (before.isupper() and after.islower())):
            parts.append(word[start:i])
            start = i
    parts.append(word[start:])
    return parts


def _name_words(text):
    """The words of text, and the parts of each CamelCase word."""
    found = []
    for word in _words(text):
        found.append(word)
        parts = _camel_parts(word)
        if len(parts) > 1:
            found.extend(parts)
    return found


def _has_name(text, config):
    return any(name_hash(w) in config['name_hashes'] for w in _name_words(text))


def _prefix(literal):
    return literal[:len(literal) - len(literal.lstrip(u'rRuUbBfF'))].lower()


def _has_control(text):
    """True when text has a control character other than tab, CR and LF."""
    return any(unicodedata.category(c) == 'Cc' and c not in NEWLINES for c in text)


def _readings(literal):
    """(spanish, names): the texts the two rules read for one string literal.

    Each newline of the source is kept as LINE_BREAK, so that the lines of a
    text are the lines of the file whatever the escapes do. A literal that
    cannot be evaluated is read as it is written (the raw token).

    A plain str literal is text, as it is in IronPython and on Python 3, and
    it is read as text on Python 2 too (it is evaluated as a u'' literal, so
    every escape is a code point and a typed character beyond latin-1 cannot
    change the reading). Its text is looked at in two ways: when the code
    points are the bytes of valid UTF-8 ('\\xc3\\xb3' spells one letter), the
    UTF-8 reading; otherwise the text itself, which is latin-1 ('\\xe1' is an
    accented a). Bytes with control characters are binary data, a magic number
    for instance: the spanish rule reads such a literal as written, so that
    it does not turn into accented letters.

    A bytes literal is text only when it is valid UTF-8; otherwise the spanish
    rule reads it as written. The name rule reads, as well as the spanish
    reading, the raw token and the latin-1 reading of anything that is not
    UTF-8: binary data that spells exactly a name is not a concern.
    """
    protected = literal.replace(u'\n', LINE_BREAK)
    prefix = _prefix(literal)
    source = protected if any(c in prefix for c in 'bur') else u'u' + protected
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')  # invalid escapes warn on Python 3.12+
            value = ast.literal_eval(source)
    except Exception:  # whatever does not evaluate is read as it is written
        return protected, [protected]
    if 'b' in prefix:
        if not isinstance(value, bytes):
            return protected, [protected]
        try:
            text = value.decode('utf-8')
        except UnicodeDecodeError:
            return protected, [protected, value.decode('latin-1')]
        return text, [text, protected]
    if isinstance(value, bytes):  # a raw str literal on Python 2
        try:
            value = value.decode('utf-8')
        except UnicodeDecodeError:
            value = value.decode('latin-1')
    if not isinstance(value, type(u'')):
        return protected, [protected]
    if u'u' in prefix:
        return value, [value, protected]
    reading = value
    try:
        reading = value.encode('latin-1').decode('utf-8')
    except UnicodeError:
        pass  # not UTF-8 spelled in code points: the text itself
    spanish = protected if _has_control(reading) else reading
    return spanish, [reading, value, protected]


def _is_prose_token(kind):
    name = tokenize.tok_name.get(kind, '')
    return name in TEXT_TOKENS or name.endswith('STRING_MIDDLE')


def _fit(text, raw_lines):
    """The lines of a reading, or the raw lines of the token if they do not match."""
    lines = text.split(LINE_BREAK)
    return lines if len(lines) == len(raw_lines) else raw_lines


def _python_lines(text):
    """(prose, everything): line number -> text of the line, from the tokens.

    prose has only comments and strings, as the spanish rule reads them;
    everything has every token, with each reading of a string for the name
    rule. Returns None when the file cannot be tokenized.
    """
    prose, everything = {}, {}
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, ValueError):
        return None
    for token in tokens:
        kind, string, row = token[0], token[1], token[2][0]
        if not string.strip():
            continue
        raw = string.split(u'\n')
        if tokenize.tok_name.get(kind) == 'STRING':
            spanish, names = _readings(string)
            readings = [(_fit(spanish, raw), True)] + [(_fit(t, raw), False) for t in names]
        else:
            readings = [(raw, _is_prose_token(kind))]
        for lines, is_prose in readings:
            for offset, part in enumerate(lines):
                everything.setdefault(row + offset, []).append(part)
                if is_prose:
                    prose.setdefault(row + offset, []).append(part)
    return (dict((n, u' '.join(p)) for n, p in prose.items()),
            dict((n, u' '.join(p)) for n, p in everything.items()))


def _spanish_reason(text, config):
    """Why text looks Spanish, or None."""
    if any(mark in text for mark in INVERTED_MARKS):
        return u'inverted question or exclamation mark'
    words = _words(text)
    strong = sorted(set(fold(w) for w in words) & config['strong'])
    if strong:
        return u'Spanish word: {0}'.format(u', '.join(strong))
    if any(c in ACCENTED for w in words for c in w.lower()):
        return u'accented letter or n-tilde'
    common = sorted(set(fold(w) for w in words) & config['stopwords'])
    if len(common) >= 2:
        return u'common Spanish words: {0}'.format(u', '.join(common))
    return None


def _check_text(path, text, config, spanish_on, name_on):
    lines = text.split(u'\n')
    scanned = _python_lines(text) if path.lower().endswith('.py') else None
    if scanned is None:
        prose = everything = dict((i + 1, line) for i, line in enumerate(lines))
    else:
        prose, everything = scanned
    entries = [a for a in config['allow'] if a['path'] == path]
    found = []
    for number in sorted(everything):
        physical = lines[number - 1] if 0 < number <= len(lines) else u''
        silenced = set()
        for entry in entries:
            if entry['text'] in physical:
                silenced.update(entry['rules'])
        if name_on and 'name' not in silenced and _has_name(everything[number], config):
            found.append(Finding(
                path, number, 'name',
                u'a team member name: leave names out of the public code'))
        if spanish_on and 'spanish' not in silenced:
            reason = _spanish_reason(prose.get(number, u''), config)
            if reason:
                found.append(Finding(
                    path, number, 'spanish', u'looks like Spanish ({0})'.format(reason)))
    return found


def _allow_entry(entry):
    if not isinstance(entry, dict) or not entry.get('path') or not entry.get('text'):
        raise ConfigError(
            u'every "allow" entry needs a non-empty "path" and "text" (an empty '
            u'text would silence every line of the file): {0}'.format(json.dumps(entry)))
    rules = entry.get('rules', ['spanish'])
    if not isinstance(rules, list) or not rules or any(r not in RULES for r in rules):
        raise ConfigError(
            u'"rules" of an "allow" entry must be a non-empty list of {0}: {1}'.format(
                u', '.join(RULES), json.dumps(entry)))
    return {'path': entry['path'], 'text': entry['text'], 'rules': set(rules)}


def _prepare(config):
    return {
        'scan_exts': set(e.lower() for e in config['scan_exts']),
        'stopwords': set(fold(w) for w in config['stopwords']),
        'strong': set(fold(w) for w in config['strong']),
        'name_hashes': set(config['name_hashes']),
        'allow': [_allow_entry(e) for e in config['allow']],
    }


def check_language(root, config):
    """Findings (rules "spanish" and "name") for the files under root.

    config is the content of language.json; ConfigError when it is not valid.
    """
    config = _prepare(config)
    found = []
    for path in list_files(root):
        if _has_name(path, config):
            found.append(Finding(
                path, 0, 'name',
                u'a team member name in a file or folder name: rename it'))
        if is_tooling(path):
            continue
        spanish_on = os.path.splitext(path)[1].lower() in config['scan_exts']
        name_on = is_text(path)
        if not (spanish_on or name_on):
            continue
        try:
            text = read_text(os.path.join(root, *path.split('/')))
        except UnicodeDecodeError:
            continue  # policy_check reports a file that is not UTF-8
        if text.startswith(BOM):
            text = text[len(BOM):]
        found.extend(_check_text(path, text, config, spanish_on, name_on))
    return sorted(found, key=lambda f: (f.path, f.line, f.rule))


def _print(text):
    # The same few lines are in structure_check.py. They stay a copy in each
    # script because ci_common.py is not edited by this change; move them there
    # in a follow-up.
    try:
        print(text)
    except UnicodeEncodeError:
        # A Python 2 pipe has no encoding: keep the log readable in ASCII.
        print(text.encode('ascii', 'backslashreplace').decode('ascii'))


def _explain(err):
    if isinstance(err, KeyError):
        return u'missing key {0}'.format(err)
    try:
        text = u'{0}'.format(err)
    except UnicodeError:
        text = repr(err)
    return u' '.join(text.split())


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='No Spanish text and no team member names in the public code.')
    parser.add_argument('--root', default='.', help='repository root (default: .)')
    parser.add_argument('--config', default=None,
                        help='language file (default: <root>/' + DEFAULT_CONFIG + ')')
    parser.add_argument('--hash-name', metavar='NAME', default=None,
                        help='print the hash to put in name_hashes, and exit')
    args = parser.parse_args(argv)
    if args.hash_name is not None:
        _print(name_hash(args.hash_name))
        return 0
    config_path = args.config or os.path.join(args.root, *DEFAULT_CONFIG.split('/'))
    try:
        config = load_json(config_path)
        found = check_language(args.root, config)
    except (IOError, OSError, ValueError, KeyError) as err:
        _print(u'language: cannot use {0}: {1}'.format(config_path, _explain(err)))
        return 2
    for finding in found:
        _print(format_finding(finding))
    _print(u'{0} finding(s)'.format(len(found)) if found else u'language: OK')
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
