# -*- coding: utf-8 -*-
"""Language check: no Spanish text and no team member names in the public code.

Two rules:

- spanish: a line of text (a comment or string in Python, any line in the
  other file types) that looks Spanish. It does when it has an inverted
  mark, a word that only Spanish uses, a letter with an accent or n-tilde,
  or two different common Spanish words.
- name: a word that is the name of a team member. The names are not in the
  repository: language.json keeps only the SHA-256 of each folded name (see
  --hash-name).

Usage:
    python .github/scripts/language_check.py --root . [--config .github/policy/language.json]
    python .github/scripts/language_check.py --hash-name NAME

Exit code 0 when clean, 1 when there are findings, 2 when the configuration
cannot be read.

Runs on Python 2.7 (the checks run in a python:2.7 container) and Python 3.
"""
from __future__ import print_function

import argparse
import hashlib
import io
import os
import re
import sys
import tokenize
import unicodedata

from ci_common import Finding, format_finding, is_tooling, list_files, load_json, read_text

DEFAULT_CONFIG = '.github/policy/language.json'
# Letters made of one or two code points: compare after NFC, before folding.
ACCENTED = u'\u00e1\u00e9\u00ed\u00f3\u00fa\u00f1'
INVERTED_MARKS = u'\u00bf\u00a1'
WORD = re.compile(r'[^\W\d_]+', re.UNICODE)
# Tokens whose text is prose: comments and strings (f-string pieces, from
# Python 3.12, are named FSTRING_MIDDLE).
TEXT_TOKENS = ('COMMENT', 'STRING')


def _text(value):
    return value.decode('utf-8') if isinstance(value, bytes) else value


def fold(word):
    """Lower case and no diacritical marks: an accented a folds to a."""
    decomposed = unicodedata.normalize('NFKD', _text(word))
    return u''.join(c for c in decomposed if not unicodedata.combining(c)).lower()


def name_hash(word):
    return hashlib.sha256(fold(word).encode('utf-8')).hexdigest()


def _words(text):
    return WORD.findall(unicodedata.normalize('NFC', text))


def _is_prose_token(kind):
    name = tokenize.tok_name.get(kind, '')
    return name in TEXT_TOKENS or name.endswith('STRING_MIDDLE')


def _python_lines(text):
    """(prose, everything): line number -> text of the line, from the tokens.

    prose has only comments and strings; everything has every token. Returns
    None when the file cannot be tokenized.
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
        for offset, part in enumerate(string.split(u'\n')):
            everything.setdefault(row + offset, []).append(part)
            if _is_prose_token(kind):
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


def _has_name(text, config):
    return any(name_hash(w) in config['name_hashes'] for w in _words(text))


def _check_text(path, text, config):
    lines = text.split(u'\n')
    scanned = _python_lines(text) if path.lower().endswith('.py') else None
    if scanned is None:
        prose = everything = dict((i + 1, line) for i, line in enumerate(lines))
    else:
        prose, everything = scanned
    allowed = [a['text'] for a in config['allow'] if a['path'] == path]
    found = []
    for number in sorted(everything):
        physical = lines[number - 1] if 0 < number <= len(lines) else u''
        if any(snippet in physical for snippet in allowed):
            continue
        if _has_name(everything[number], config):
            found.append(Finding(
                path, number, 'name',
                u'a team member name: leave names out of the public code'))
        reason = _spanish_reason(prose.get(number, u''), config)
        if reason:
            found.append(Finding(
                path, number, 'spanish', u'looks like Spanish ({0})'.format(reason)))
    return found


def check_language(root, config):
    """Findings (rules "spanish" and "name") for the files under root.

    config is the content of language.json.
    """
    config = {
        'scan_exts': set(e.lower() for e in config['scan_exts']),
        'stopwords': set(fold(w) for w in config['stopwords']),
        'strong': set(fold(w) for w in config['strong']),
        'name_hashes': set(config['name_hashes']),
        'allow': config['allow'],
    }
    found = []
    for path in list_files(root):
        if is_tooling(path) or os.path.splitext(path)[1].lower() not in config['scan_exts']:
            continue
        try:
            text = read_text(os.path.join(root, *path.split('/')))
        except UnicodeDecodeError:
            continue  # policy_check reports a file that is not UTF-8
        found.extend(_check_text(path, text.lstrip(u'\ufeff'), config))
    return sorted(found, key=lambda f: (f.path, f.line, f.rule))


def _print(text):
    try:
        print(text)
    except UnicodeEncodeError:
        # A Python 2 pipe has no encoding: keep the log readable in ASCII.
        print(text.encode('ascii', 'backslashreplace').decode('ascii'))


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
        _print(u'language: cannot use {0}: {1!r}'.format(config_path, err))
        return 2
    for finding in found:
        _print(format_finding(finding))
    _print(u'{0} finding(s)'.format(len(found)) if found else u'language: OK')
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
