# -*- coding: utf-8 -*-
"""Run pyflakes over the Python files of the repository.

    PYFLAKES_BUILTINS=__revit__,... python .github/scripts/pyflakes_check.py --root .

pyRevit injects names such as __revit__ into every script, so they are
declared with PYFLAKES_BUILTINS, a comma separated list of names.

Only messages that mean the script will fail when it runs fail the check:
undefined names, a name used before it is assigned, misplaced return, yield,
break or continue, duplicate arguments, and syntax errors (including those
that only the compiler finds, so this fails on the same files as compileall).
Everything else (unused imports and variables, redefinitions...) is printed
as a warning and does not change the exit code.

Python files under .github/ are tooling, not part of the extension, and are
skipped. Runs on Python 2.7 (inside the CI container, where pyflakes 2.4.0
is installed) and on Python 3.
"""
from __future__ import print_function

import argparse
import ast
import os
import sys

from pyflakes import checker

import ci_common

# pyflakes message classes that fail the check, by class name.
ERROR_MESSAGES = frozenset([
    'UndefinedName', 'UndefinedExport', 'UndefinedLocal', 'DuplicateArgument',
    'ReturnOutsideFunction', 'YieldOutsideFunction', 'ContinueOutsideLoop',
    'BreakOutsideLoop',
])


def _text(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value


def _custom_builtins():
    # pyflakes 2.x reads PYFLAKES_BUILTINS once, when it is imported; reading
    # it here makes a change of the variable count on every call.
    names = os.environ.get('PYFLAKES_BUILTINS', '')
    return [name.strip() for name in names.split(',') if name.strip()]


def _native(path):
    # Python 2 compile() and ast.parse() want a byte-string file name and raise
    # UnicodeEncodeError on a non-ASCII unicode one. Python 3 takes text.
    if str is bytes and not isinstance(path, bytes):
        return path.encode(sys.getfilesystemencoding() or 'utf-8')
    return path


def _finding(path, line, rule, message):
    return ci_common.format_finding(
        ci_common.Finding(_text(path), line, rule, _text(message)))


def check_paths(paths):
    """Check the files at paths; returns (errors, warnings), two lists of strings."""
    builtins = _custom_builtins()
    errors = []
    warnings = []
    for path in paths:
        native = _native(path)
        with open(native, 'rb') as fh:
            # Bytes, not text: Python 2 refuses a unicode string that has an
            # encoding declaration, and bytes keep the declaration working.
            source = fh.read()
        try:
            tree = ast.parse(source, native)
            # ast.parse stops before the compiler's own checks (on Python 2, a
            # "return x" inside a generator; on Python 3, a nonlocal at module
            # level). compileall runs the compiler, so this does too, and both
            # jobs fail on the same files. dont_inherit keeps this script's
            # __future__ flags out of the checked code.
            compile(source, native, 'exec', 0, True)
        except SyntaxError as exc:
            errors.append(_finding(path, exc.lineno, 'SyntaxError', exc.msg))
            continue
        except Exception as exc:  # e.g. a null byte in the source
            errors.append(_finding(path, 0, 'Unparsable', str(exc)))
            continue
        # No file_tokens: that only turns "# type:" comments into checked code.
        messages = checker.Checker(tree, filename=native, builtins=builtins).messages
        for message in sorted(messages, key=lambda m: m.lineno):
            rule = type(message).__name__
            finding = _finding(path, message.lineno, rule,
                               message.message % message.message_args)
            (errors if rule in ERROR_MESSAGES else warnings).append(finding)
    return errors, warnings


def _say(text):
    # An ASCII-only print: Python 2 cannot write non-ASCII text to a pipe.
    print(text.encode('ascii', 'backslashreplace').decode('ascii'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--root', default='.', help='repository root (default: .)')
    args = parser.parse_args(argv)

    root = _text(args.root)
    paths = [os.path.normpath(os.path.join(root, rel))
             for rel in ci_common.list_files(root)
             if rel.endswith('.py') and not ci_common.is_tooling(rel)]
    errors, warnings = check_paths(paths)
    for line in warnings:
        _say(u'warning: ' + line)
    for line in errors:
        _say(u'error: ' + line)
    _say(u'pyflakes: {0} files, {1} errors, {2} warnings'.format(
        len(paths), len(errors), len(warnings)))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
