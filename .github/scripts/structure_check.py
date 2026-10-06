# -*- coding: utf-8 -*-
"""Structure check of the pyRevit extension.

Catches what pyRevit would get wrong in silence (a tool missing from its
layout, without an icon or without a title) and the folder types that run
code nobody reviews here (extension hooks, URL buttons, ...).

Usage:
    python .github/scripts/structure_check.py --root . [--config .github/policy/policy.json]

Reads the "structure" section of the policy file. Exit code 0 when clean,
1 when there are findings, 2 when the configuration cannot be read.

Runs on Python 2.7 (the checks run in a python:2.7 container) and Python 3.
"""
from __future__ import print_function

import argparse
import ast
import json
import os
import sys

from ci_common import Finding, format_finding, list_files, load_json, read_text

try:
    import yaml
except ImportError:
    yaml = None

RULE = 'structure'
DEFAULT_CONFIG = '.github/policy/policy.json'
BUNDLE_YAML = 'bundle.yaml'
SCRIPT = 'script.py'
PUSHBUTTON = '.pushbutton'
TAB = '.tab'
# Bundle types whose children are placed by the layout in their bundle.yaml.
LAYOUT_TYPES = ('.tab', '.panel', '.stack', '.pulldown', '.splitbutton')
SEPARATOR = '---'
TEXT_TYPES = (type(u''), str)


def _join(*parts):
    return u'/'.join(parts)


def _describe(err):
    """One line of text about an exception, safe on Python 2 and 3."""
    try:
        text = u'{0}'.format(err)
    except UnicodeError:
        text = repr(err)
    return u' '.join(text.split())


def _suffix(name):
    """The bundle suffix of a folder name ('.pushbutton'), or '' if it has none."""
    index = name.rfind('.')
    return name[index:] if index >= 0 else ''


def _stem(name):
    return name[:len(name) - len(_suffix(name))]


def _finding(path, message, line=0):
    return Finding(path, line, RULE, message)


class _Tree(object):
    """Folders and files of the repository, derived from the file list."""

    def __init__(self, paths):
        self.files = set(paths)
        self.dirs = set()
        self._children = {}
        for path in paths:
            parts = path.split('/')
            for i in range(len(parts)):
                self._children.setdefault(u'/'.join(parts[:i]), set()).add(parts[i])
                if i:
                    self.dirs.add(u'/'.join(parts[:i]))

    def children(self, folder):
        return sorted(self._children.get(folder, ()))

    def subdirs(self, folder):
        return [name for name in self.children(folder)
                if _join(folder, name) in self.dirs]


def _in_tab(path):
    return path.split('/')[0].endswith(TAB)


def _check_root(tree, config):
    found = []
    for name in tree.children(u''):
        if name in config['root_entries']:
            continue
        if name in tree.dirs and name.endswith(TAB):
            continue
        found.append(_finding(
            name, 'not an allowed top-level entry (pyRevit runs code from folders '
                  'such as hooks and bin); if it is needed, add it to '
                  'structure.root_entries after a review'))
    return found


def _check_bundle_types(tree, config):
    found = []
    for folder in sorted(tree.dirs):
        name = folder.split('/')[-1]
        if not _in_tab(folder) or '.' not in name:
            continue
        if _suffix(name) not in config['bundle_types']:
            found.append(_finding(
                folder, u'bundle type {0} is not allowed: pyRevit would run or '
                        'open something nobody reviewed here (allowed: {1})'.format(
                            _suffix(name), ', '.join(config['bundle_types']))))
    return found


def _load_bundle(root, tree, folder):
    """(mapping, findings) of the bundle.yaml in folder; (None, []) if absent."""
    rel = _join(folder, BUNDLE_YAML)
    if rel not in tree.files:
        return None, []
    try:
        data = yaml.safe_load(read_text(os.path.join(root, *rel.split('/'))))
    except (yaml.YAMLError, UnicodeDecodeError) as err:
        return None, [_finding(rel, u'bundle.yaml cannot be read: {0}'.format(
            _describe(err)))]
    if not isinstance(data, dict):
        return None, [_finding(rel, 'bundle.yaml must be a mapping of keys')]
    return data, []


def _check_bundles(root, tree, config):
    """Layout of each container and keys of every bundle.yaml."""
    found = []
    for folder in sorted(tree.dirs):
        if not _in_tab(folder):
            continue
        suffix = _suffix(folder.split('/')[-1])
        needs_layout = suffix in LAYOUT_TYPES
        data, problems = _load_bundle(root, tree, folder)
        found.extend(problems)
        rel = _join(folder, BUNDLE_YAML)
        if data is not None:
            for key in sorted(data, key=lambda k: u'{0}'.format(k)):
                if key not in config['bundle_keys']:
                    found.append(_finding(
                        rel, u'bundle.yaml key "{0}" is not allowed (allowed: {1})'.format(
                            key, ', '.join(config['bundle_keys']))))
        if not needs_layout or problems:
            continue
        if data is None:
            found.append(_finding(
                folder, u'a {0} needs a {1} with a layout'.format(suffix, BUNDLE_YAML)))
        elif 'layout' not in data:
            found.append(_finding(rel, 'bundle.yaml has no layout'))
        elif not isinstance(data['layout'], list):
            found.append(_finding(rel, 'layout must be a list of names'))
        else:
            found.extend(_check_layout(tree, config, folder, data['layout']))
    return found


def _check_layout(tree, config, folder, layout):
    found = []
    rel = _join(folder, BUNDLE_YAML)
    listed = set()
    for entry in layout:
        entry = entry if isinstance(entry, TEXT_TYPES) else u'{0}'.format(entry)
        if not entry.startswith(SEPARATOR):
            listed.add(entry)
    present = set()
    for name in tree.subdirs(folder):
        if _suffix(name) not in config['bundle_types']:
            continue
        present.add(_stem(name))
        if _stem(name) not in listed:
            found.append(_finding(
                _join(folder, name),
                u'"{0}" is missing from the layout of {1}: pyRevit would not '
                'place it where you expect'.format(_stem(name), rel)))
    for entry in sorted(listed - present):
        found.append(_finding(
            rel, u'layout entry "{0}" has no folder in {1}'.format(entry, folder)))
    return found


def _module_names(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _check_script(root, rel):
    try:
        source = read_text(os.path.join(root, *rel.split('/')))
        # Python 2 refuses a unicode string with an encoding line: parse bytes.
        tree = ast.parse(source.encode('utf-8'), rel)
    except UnicodeDecodeError:
        return [_finding(rel, 'script.py is not valid UTF-8')]
    except (SyntaxError, ValueError) as err:
        return [_finding(rel, u'script.py cannot be parsed: {0}'.format(_describe(err)),
                         getattr(err, 'lineno', None) or 0)]
    found = []
    names = _module_names(tree)
    if '__title__' not in names:
        found.append(_finding(rel, 'script.py does not assign __title__ at module '
                                   'level: the button would have no title'))
    if '__doc__' not in names and ast.get_docstring(tree, clean=False) is None:
        found.append(_finding(rel, 'script.py has no module docstring and does not '
                                   'assign __doc__: the button would have no tooltip'))
    return found


def _check_pushbuttons(root, tree, config):
    found = []
    for folder in sorted(tree.dirs):
        if not _in_tab(folder) or not folder.endswith(PUSHBUTTON):
            continue
        script = _join(folder, SCRIPT)
        if script in tree.files:
            found.extend(_check_script(root, script))
        else:
            found.append(_finding(folder, u'missing {0}'.format(SCRIPT)))
        for icon in config['icons']:
            if _join(folder, icon) not in tree.files:
                found.append(_finding(folder, u'missing {0}'.format(icon)))
    return found


def _check_groups(root, tree, config):
    """Every tool of the pool panel is in the groups file, and the reverse."""
    path = config.get('groups')
    if not path or path not in tree.files:
        return []
    try:
        groups = json.loads(read_text(os.path.join(root, *path.split('/'))))
    except (ValueError, UnicodeDecodeError) as err:
        return [_finding(path, u'groups file cannot be read: {0}'.format(_describe(err)))]
    listed = set()
    found = []
    if not isinstance(groups, list):
        return [_finding(path, 'groups file must be a list of {"group", "tools"}')]
    for group in groups:
        tools = group.get('tools') if isinstance(group, dict) else None
        if not isinstance(tools, list):
            found.append(_finding(path, 'every group needs a "tools" list'))
            continue
        listed.update(tool for tool in tools if isinstance(tool, TEXT_TYPES))
    pool = {}
    for folder in sorted(tree.dirs):
        if _in_tab(folder) and folder.split('/')[-1] == config['pool_panel']:
            for name in tree.subdirs(folder):
                if name.endswith(PUSHBUTTON):
                    pool[_stem(name)] = _join(folder, name)
    for name in sorted(pool):
        if name not in listed:
            found.append(_finding(
                pool[name], u'"{0}" is on {1} but missing from {2}'.format(
                    name, config['pool_panel'], path)))
    for name in sorted(listed - set(pool)):
        found.append(_finding(
            path, u'"{0}" is listed here but {1} has no such pushbutton'.format(
                name, config['pool_panel'])))
    return found


def check_structure(root, config):
    """Findings (rule "structure") for the repository under root.

    config is the "structure" section of policy.json.
    """
    if yaml is None:
        return [_finding(BUNDLE_YAML, 'PyYAML is not installed')]
    tree = _Tree(list_files(root))
    found = []
    found.extend(_check_root(tree, config))
    found.extend(_check_bundle_types(tree, config))
    found.extend(_check_bundles(root, tree, config))
    found.extend(_check_pushbuttons(root, tree, config))
    found.extend(_check_groups(root, tree, config))
    return sorted(set(found), key=lambda f: (f.path, f.line, f.message))


def _print(text):
    try:
        print(text)
    except UnicodeEncodeError:
        # A Python 2 pipe has no encoding: keep the log readable in ASCII.
        print(text.encode('ascii', 'backslashreplace').decode('ascii'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Structure check of the extension.')
    parser.add_argument('--root', default='.', help='repository root (default: .)')
    parser.add_argument('--config', default=None,
                        help='policy file (default: <root>/' + DEFAULT_CONFIG + ')')
    args = parser.parse_args(argv)
    config_path = args.config or os.path.join(args.root, *DEFAULT_CONFIG.split('/'))
    try:
        config = load_json(config_path)['structure']
    except (IOError, OSError, ValueError, KeyError) as err:
        _print(u'structure: cannot read the "structure" section of {0}: {1}'.format(
            config_path, _describe(err)))
        return 2
    found = check_structure(args.root, config)
    for finding in found:
        _print(format_finding(finding))
    _print(u'{0} finding(s)'.format(len(found)) if found else u'structure: OK')
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
