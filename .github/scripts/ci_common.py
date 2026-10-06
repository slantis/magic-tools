# -*- coding: utf-8 -*-
"""Helpers shared by the repository checks in .github/scripts/.

Runs on Python 2.7 (the checks run in a python:2.7 container, the same
grammar as the IronPython 2.7 that pyRevit uses) and on Python 3 (local
runs).
"""
from __future__ import print_function

import collections
import hashlib
import io
import json
import os
import subprocess

TEXT_EXTS = ('.py', '.xaml', '.yaml', '.yml', '.json', '.svg', '.md', '.txt')
TEXT_NAMES = ('LICENSE', 'LICENSE-CONTENT', 'CODEOWNERS', '.gitattributes', '.gitignore')
TOOLING_PREFIX = '.github/'

Finding = collections.namedtuple('Finding', 'path line rule message')


def _text(value):
    if isinstance(value, bytes):
        return value.decode('utf-8')
    return value


def format_finding(finding):
    return u'{0}:{1}: [{2}] {3}'.format(
        finding.path, finding.line or 0, finding.rule, finding.message)


def _in_git_work_tree(root):
    dotgit = os.path.join(root, '.git')
    if os.path.isfile(dotgit):
        # A linked worktree has a .git file that points at the real one.
        with open(dotgit, 'rb') as fh:
            return fh.read(7) == b'gitdir:'
    return os.path.isfile(os.path.join(dotgit, 'HEAD'))


def list_files(root):
    """Repo-relative POSIX paths of the files to check, sorted.

    Inside a git work tree: tracked files plus untracked ones that are not
    ignored, so local leftovers (__pycache__, editor folders) stay out.
    Elsewhere (test fixtures, an unpacked release): every file under root
    except the .git folder.
    """
    root = _text(root)
    if _in_git_work_tree(root):
        out = subprocess.check_output(
            ['git', '-C', root, 'ls-files', '-z', '--cached', '--others',
             '--exclude-standard'])
        paths = set(p for p in _text(out).split(u'\0') if p)
        # --cached still lists a tracked file deleted from the work tree.
        return sorted(p for p in paths if os.path.isfile(os.path.join(root, p)))
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != '.git']
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            found.append(rel.replace(os.sep, '/'))
    return sorted(found)


def is_text(path):
    name = path.replace('\\', '/').rsplit('/', 1)[-1]
    return name.lower().endswith(TEXT_EXTS) or name in TEXT_NAMES


def is_tooling(relpath):
    return relpath.startswith(TOOLING_PREFIX)


def file_digest(path):
    """SHA-256 of a file. Text is hashed with LF line endings, so a Windows
    checkout (CRLF) and the repository (LF) give the same digest."""
    with open(path, 'rb') as fh:
        data = fh.read()
    if is_text(path):
        data = data.replace(b'\r\n', b'\n')
    return hashlib.sha256(data).hexdigest()


def read_text(path):
    """File contents as text; raises UnicodeDecodeError when it is not UTF-8."""
    with io.open(path, encoding='utf-8') as fh:
        return fh.read()


def load_json(path):
    return json.loads(read_text(path))
