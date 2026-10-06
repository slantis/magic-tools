# -*- coding: utf-8 -*-
"""Write release-manifest.json: the SHA-256 of every file in a release.

The release workflow runs this over the unpacked release ZIP. The manifest
is attested and attached to the release, and the sync to the private
repository checks each file it imports against it.

    python .github/scripts/release_manifest.py --root DIR --tag T --commit C --out FILE

Text files are hashed with LF line endings (see ci_common.file_digest), so
the digests do not depend on how a checkout converted them.

Runs on Python 2.7 and 3.
"""
from __future__ import print_function

import argparse
import io
import json
import os
import sys

import ci_common

SCHEMA = 1


def build_manifest(root, tag, commit):
    if isinstance(root, bytes):  # a Python 2 command-line argument
        root = root.decode('utf-8')
    files = []
    for rel in ci_common.list_files(root):
        digest = ci_common.file_digest(os.path.join(root, *rel.split('/')))
        files.append({'path': rel, 'sha256': digest})
    return {
        'schema': SCHEMA,
        'tag': tag,
        'commit': commit,
        'files': sorted(files, key=lambda f: f['path']),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--root', required=True,
                        help='folder holding the files of the release')
    parser.add_argument('--tag', required=True, help='release tag, e.g. v0.1.0')
    parser.add_argument('--commit', required=True,
                        help='commit the release was built from')
    parser.add_argument('--out', required=True, help='manifest file to write')
    args = parser.parse_args(argv)

    manifest = build_manifest(args.root, args.tag, args.commit)
    # Plain ASCII (non-ASCII paths are \u-escaped), so Python 2 and 3 write
    # the same bytes. newline='\n' keeps LF endings on Windows.
    text = json.dumps(manifest, indent=1, sort_keys=True,
                      separators=(',', ': '))
    if isinstance(text, bytes):
        text = text.decode('ascii')
    with io.open(args.out, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text + u'\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
