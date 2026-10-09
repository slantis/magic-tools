# -*- coding: utf-8 -*-
"""Tests of lib/telemetry.py, the opt-in usage data of the extension.

The module is plain Python except for the .NET calls, which only run under
IronPython (sys.platform 'cli'), so its logic runs here on Python 2.7 and 3.
No test reaches the network: every send goes through a fake post(), and
_post and _spawn are replaced so that nothing can start a real request.
"""
from __future__ import unicode_literals

import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir, os.pardir))
sys.path.insert(0, os.path.join(REPO, 'lib'))

import telemetry as t  # noqa: E402

UUID4 = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')
STAMP = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
INFO = {'addin_version': '0.2.0', 'channel': 'git', 'revit_version': '2025',
        'pyrevit_version': '5.0.1.25181+1416'}
DAY = 1791331200.0          # 2026-10-07T00:00:00Z
NOON = DAY + 12 * 3600

# The tool names the server whitelist has to hold: the folder name of every
# .pushbutton. A new tool makes test_known_tools fail on purpose: add its name
# to the server whitelist (see ADDING A TOOL in lib/telemetry.py), then here.
KNOWN_TOOLS = sorted([
    'All Magic Tools', 'Clean Explode CAD', 'Cloud Manager', 'Create Type Filter', 'Find Room',
    'Goodbye Filter', 'Inspect Element Graphics', 'Inspect Model Overrides',
    'Inspect View Overrides', 'Next Sheet', 'Parent Sheet', 'Previous Sheet',
    'Print Set Manager', 'Rename Families', 'Select Same Family',
    'Select Same Type', 'Selection Manager', 'View Template Manager',
])


class FakeServer(object):
    """Stands in for _post: records every request and answers from a script
    of status codes (the last one repeats). A status can be a function of the
    payload."""

    def __init__(self, *statuses):
        self.statuses = list(statuses) or [202]
        self.calls = []

    def __call__(self, url, payload):
        # What goes out is what json can carry: no sets, no objects.
        payload = json.loads(t.dumps(payload))
        if url != t.URL:
            raise AssertionError('unexpected URL ' + url)
        self.calls.append(payload)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return status(payload) if callable(status) else status

    def sent_events(self):
        return [event for payload in self.calls for event in payload]


class Base(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = (t.STATE_DIR, t._post, t._spawn, os.environ.copy())
        t.STATE_DIR = os.path.join(self.dir, 'magic-tools-telemetry')
        self.spawned = []
        t._spawn = self.spawned.append

        def no_network(url, payload):
            raise AssertionError('a test reached the real sender')
        t._post = no_network
        for name in ('DO_NOT_TRACK', 'MAGIC_TOOLS_TELEMETRY'):
            os.environ.pop(name, None)

    def tearDown(self):
        t.STATE_DIR, t._post, t._spawn, environ = self.saved
        os.environ.clear()
        os.environ.update(environ)
        shutil.rmtree(self.dir)

    def state(self):
        return t._snapshot()[0]

    def queue(self):
        return t._snapshot()[1]

    def opt_in(self):
        t.set_consent(True, INFO)
        return self.state()['install_id']


class Switches(Base):

    def test_do_not_track(self):
        for value in ('1', 'true', 'yes', 'anything'):
            self.assertEqual(t.env_disabled({'DO_NOT_TRACK': value}), 'DO_NOT_TRACK')
        for value in ('', '0', 'false', 'No', ' off '):
            self.assertIsNone(t.env_disabled({'DO_NOT_TRACK': value}))

    def test_studio_switch(self):
        for value in ('0', 'off', 'OFF', 'false', 'no', 'disabled'):
            self.assertEqual(t.env_disabled({'MAGIC_TOOLS_TELEMETRY': value}),
                             'MAGIC_TOOLS_TELEMETRY')
        for value in ('', '1', 'on'):
            self.assertIsNone(t.env_disabled({'MAGIC_TOOLS_TELEMETRY': value}))

    def test_prompt_once_whatever_the_answer(self):
        for answer in (True, False):
            shutil.rmtree(self.dir)
            self.assertTrue(t.prompt_needed())
            self.assertIsNone(t.consent())
            t.set_consent(answer, INFO)
            self.assertFalse(t.prompt_needed())
            self.assertEqual(t.consent(), answer)

    def test_nothing_before_consent(self):
        t.record_tool_run('Find Room', True, INFO)
        t.startup(INFO)
        server = FakeServer()
        t.flush(server)
        self.assertEqual(server.calls, [])
        self.assertEqual(self.spawned, [])
        self.assertFalse(os.path.exists(t.STATE_DIR))

    def test_nothing_after_a_no(self):
        t.set_consent(False)
        t.record_tool_run('Find Room', True, INFO)
        t.startup(INFO)
        server = FakeServer()
        t.flush(server)
        self.assertEqual(server.calls, [])
        self.assertEqual(self.state(), {'consent': False})
        self.assertEqual(self.queue(), [])

    def test_environment_wins_over_consent(self):
        self.opt_in()
        del self.spawned[:]
        for name, value in (('DO_NOT_TRACK', '1'), ('MAGIC_TOOLS_TELEMETRY', 'off')):
            os.environ[name] = value
            self.assertFalse(t.prompt_needed())
            self.assertFalse(t.is_enabled())
            t.record_tool_run('Find Room', True, INFO)
            t.startup(INFO, now=NOON)
            server = FakeServer()
            t.flush(server, now=NOON)
            t.flush_async()
            self.assertEqual(server.calls, [])
            self.assertEqual(self.spawned, [])
            self.assertEqual([e['event_type'] for e in self.queue()], ['install'])
            del os.environ[name]
        self.assertTrue(t.is_enabled())

    def test_revoke_deletes_everything_and_a_new_yes_starts_over(self):
        first = self.opt_in()
        t.record_tool_run('Find Room', True, INFO)
        t.set_consent(False)
        self.assertEqual(self.state(), {'consent': False})
        self.assertEqual(self.queue(), [])
        second = self.opt_in()
        self.assertTrue(UUID4.match(second))
        self.assertNotEqual(first, second)

    def test_opt_in_keeps_the_install_id(self):
        first = self.opt_in()
        self.assertEqual(self.opt_in(), first)


class Fields(Base):

    def test_tool_name_is_the_pushbutton_folder(self):
        folder = os.path.join('x', 'Magic-tools.tab', 'Tools.panel', 'Selection.stack',
                              'Select Same Family.pushbutton')
        self.assertEqual(t.tool_name(os.path.join(folder, 'script.py')), 'Select Same Family')
        self.assertEqual(t.tool_name(folder), 'Select Same Family')
        self.assertEqual(t.tool_name('C:\\a\\All Magic Tools.pushbutton\\script.py'),
                         'All Magic Tools')
        self.assertIsNone(t.tool_name(os.path.join('lib', 'usage.py')))
        self.assertIsNone(t.tool_name(os.path.join('A<b>.pushbutton', 'script.py')))
        self.assertIsNone(t.tool_name(os.path.join('x' * 101 + '.pushbutton', 'script.py')))
        self.assertIsNone(t.tool_name(None))

    def test_tool_run_carries_tool_and_result(self):
        event = t.make_event('id', INFO, 'tool_run', tool='Find Room', ok=False, now=NOON)
        self.assertEqual(event['tool'], 'Find Room')
        self.assertEqual(event['result'], 'error')
        self.assertEqual(t.make_event('id', INFO, 'tool_run', tool='X')['result'], 'ok')

    def test_install_and_heartbeat_never_carry_tool_or_result(self):
        for kind in ('install', 'heartbeat'):
            event = t.make_event('id', INFO, kind, tool='Find Room', ok=False, now=NOON)
            self.assertNotIn('tool', event)
            self.assertNotIn('result', event)

    def test_event_fields(self):
        event = t.make_event('iid', INFO, 'heartbeat', now=NOON)
        self.assertTrue(UUID4.match(event['event_id']))
        self.assertEqual(event['occurred_at'], '2026-10-07T12:00:00Z')
        self.assertTrue(STAMP.match(t.make_event('iid', INFO, 'heartbeat')['occurred_at']))
        self.assertEqual(sorted(event), sorted([
            'event_id', 'install_id', 'event_type', 'occurred_at', 'addin_version',
            'channel', 'revit_version', 'pyrevit_version', 'schema_version']))
        self.assertEqual(event['schema_version'], 1)
        self.assertNotEqual(event['event_id'],
                            t.make_event('iid', INFO, 'heartbeat')['event_id'])

    def test_unknown_versions_are_left_out(self):
        info = {'addin_version': '0.2.0', 'channel': 'zip'}
        event = t.make_event('iid', info, 'heartbeat')
        self.assertNotIn('revit_version', event)
        self.assertNotIn('pyrevit_version', event)
        self.assertNotIn(None, event.values())
        self.assertEqual((event['channel'], event['addin_version']), ('zip', '0.2.0'))

    def test_versions_follow_the_server_rules(self):
        self.assertEqual(t.clean_version('5.0.1.25181+1416 (beta)', 60),
                         '5.0.1.25181+1416 beta')
        self.assertEqual(t.clean_version('x' * 80, 60), 'x' * 60)
        self.assertIsNone(t.clean_version('()', 50))
        self.assertIsNone(t.clean_version(None, 50))
        self.assertEqual(t.clean_version(2025, 50), '2025')
        info = t.normalize_info({'addin_version': '<bad>', 'channel': 'svn',
                                 'revit_version': '2025'}, {'pyrevit_version': '5.0'})
        self.assertTrue(t.VERSION_RE.match(info['addin_version']))
        self.assertIn(info['channel'], ('git', 'zip'))
        self.assertEqual(info['revit_version'], '2025')
        self.assertEqual(info['pyrevit_version'], '5.0')

    def test_addin_version_comes_from_extension_json(self):
        folder = os.path.join(self.dir, 'ext')
        os.makedirs(folder)
        path = os.path.join(folder, 'extension.json')
        for data, expected in (({'version': '1.2.3'}, '1.2.3'),
                               ({'version': 'v1 <beta>'}, 'unknown'),
                               ({}, 'unknown')):
            with io.open(path, 'w', encoding='utf-8') as fh:
                fh.write(u'' + json.dumps(data))
            self.assertEqual(t.addin_version(folder), expected)
        self.assertEqual(t.channel(folder), 'zip')
        os.makedirs(os.path.join(folder, '.git'))
        self.assertEqual(t.channel(folder), 'git')

    def test_this_extension_json_has_a_valid_version(self):
        self.assertNotEqual(t.addin_version(REPO), 'unknown')


class Batching(Base):

    def events(self, count, pad=0):
        return [dict(t.make_event('iid', INFO, 'tool_run', tool='T'), pad='x' * pad)
                for _ in range(count)]

    def test_at_most_fifty_per_request(self):
        sizes = [len(batch) for batch in t.batches(self.events(120))]
        self.assertEqual(sizes, [50, 50, 20])

    def test_bodies_stay_under_the_limit(self):
        events = self.events(40, pad=5000)
        found = t.batches(events)
        self.assertEqual(sum(len(batch) for batch in found), 40)
        for batch in found:
            self.assertLessEqual(len(t.dumps(batch)), t.MAX_BODY_BYTES)
        self.assertGreater(len(found), 1)

    def test_an_event_too_big_goes_alone(self):
        events = self.events(1) + self.events(1, pad=70000) + self.events(1)
        self.assertEqual([len(batch) for batch in t.batches(events)], [1, 1, 1])

    def test_status_codes(self):
        for status in (200, 202, 204):
            self.assertEqual(t.outcome(status), 'sent')
        for status in (400, 413):
            self.assertEqual(t.outcome(status), 'drop')
        for status in (0, 401, 403, 404, 422, 429, 500, 502, 503):
            self.assertEqual(t.outcome(status), 'retry')

    def test_backoff(self):
        self.assertEqual([t.backoff_seconds(n) for n in range(1, 7)],
                         [60, 300, 900, 3600, 3600, 3600])

    def test_413_is_split_down_to_single_events_which_are_dropped(self):
        batch = self.events(5)
        bad = batch[3]['event_id']
        posted = []

        def post(url, part):
            posted.append(len(part))
            if len(part) > 1:
                return 413
            return 413 if part[0]['event_id'] == bad else 202
        done, retry = t.send_events(batch, post)
        self.assertFalse(retry)
        self.assertEqual(sorted(done), sorted(e['event_id'] for e in batch))
        self.assertEqual(max(posted[1:]), 3)

    def test_a_retryable_status_stops_the_batch(self):
        batch = self.events(4)
        statuses = [413, 202, 503]
        done, retry = t.send_events(batch, lambda url, part: statuses.pop(0))
        self.assertTrue(retry)
        self.assertEqual(done, [e['event_id'] for e in batch[:2]])


class Sending(Base):

    def test_the_install_event_comes_first(self):
        install_id = self.opt_in()
        self.assertEqual(len(self.spawned), 1)        # the send started
        self.assertEqual([e['event_type'] for e in self.queue()], ['install'])
        t.record_tool_run('Find Room', True, INFO, now=NOON)
        server = FakeServer()
        t.flush(server, now=NOON)
        self.assertEqual(len(server.calls), 1)
        sent = server.sent_events()
        self.assertEqual([e['event_type'] for e in sent], ['install', 'tool_run'])
        self.assertTrue(all(e['install_id'] == install_id for e in sent))
        self.assertEqual(sent[0]['revit_version'], '2025')
        self.assertEqual(self.queue(), [])

    def test_one_install_event_per_install_id(self):
        self.opt_in()
        self.opt_in()
        t.startup(INFO, now=NOON)
        t.record_tool_run('Find Room', True, INFO, now=NOON)
        kinds = [e['event_type'] for e in self.queue()]
        self.assertEqual(kinds.count('install'), 1)
        self.assertEqual(kinds[0], 'install')

    def test_a_020_state_gets_its_install_event_and_loses_the_registration(self):
        install_id = self.opt_in()

        def as_020(state, queue):
            del queue[:]
            state.pop('install_event')
            state['registered'] = {'install_id': install_id}
            return True
        t._transact(as_020)
        t.startup(INFO, now=NOON)
        self.assertEqual([e['event_type'] for e in self.queue()], ['install', 'heartbeat'])
        self.assertNotIn('registered', self.state())

    def test_a_network_error_backs_off(self):
        self.opt_in()
        server = FakeServer(0)
        t.flush(server, now=NOON)
        self.assertEqual(len(server.calls), 1)
        self.assertEqual(self.state()['next_attempt_at'], NOON + 60)
        self.assertEqual(len(self.queue()), 1)

    def test_backoff_is_kept_and_grows(self):
        self.opt_in()
        server = FakeServer(503, 429, 202)
        t.flush(server, now=NOON)
        t.flush(server, now=NOON + 59)              # still waiting
        self.assertEqual(len(server.calls), 1)
        t.flush(server, now=NOON + 60)
        self.assertEqual(self.state()['next_attempt_at'], NOON + 60 + 300)
        t.flush(server, now=NOON + 360)
        self.assertNotIn('next_attempt_at', self.state())
        self.assertEqual(len(server.calls), 3)
        self.assertEqual(self.queue(), [])

    def test_retries_resend_the_same_event_ids(self):
        self.opt_in()
        t.record_tool_run('Find Room', False, INFO, now=NOON)
        server = FakeServer(500, 202)
        t.flush(server, now=NOON)
        first = [e['event_id'] for e in server.sent_events()]
        self.assertEqual(len(self.queue()), 2)
        t.flush(server, now=NOON + 61)
        second = [e['event_id'] for e in server.sent_events()][len(first):]
        self.assertEqual(first, second)
        self.assertEqual(self.queue(), [])

    def test_bad_batches_are_dropped(self):
        self.opt_in()
        t.record_tool_run('Find Room', True, INFO, now=NOON)
        server = FakeServer(400, 202)
        t.flush(server, now=NOON)
        self.assertEqual(self.queue(), [])
        self.assertNotIn('next_attempt_at', self.state())
        self.assertEqual(len(server.calls), 1)

    def test_one_heartbeat_per_utc_day(self):
        self.opt_in()
        for now in (DAY + 60, NOON, DAY + 86399):
            t.startup(INFO, now=now)
        self.assertEqual([e['event_type'] for e in self.queue()], ['install', 'heartbeat'])
        t.startup(INFO, now=DAY + 86400)
        self.assertEqual([e['event_type'] for e in self.queue()],
                         ['install'] + ['heartbeat'] * 2)

    def test_queue_keeps_the_newest_thousand(self):
        install_id = self.opt_in()
        events = [t.make_event(install_id, INFO, 'tool_run', tool='T') for _ in range(1005)]

        def fill(state, queue):
            queue.extend(events)
            return True
        t._transact(fill)
        kept = self.queue()
        self.assertEqual(len(kept), t.MAX_QUEUE)
        self.assertEqual(kept[0]['event_id'], events[5]['event_id'])

    def test_a_flush_has_a_request_budget(self):
        install_id = self.opt_in()
        events = [t.make_event(install_id, INFO, 'tool_run', tool='T') for _ in range(600)]
        t._transact(lambda state, queue: queue.extend(events) or True)
        server = FakeServer()
        t.flush(server, now=NOON)
        self.assertEqual(len(server.calls), t.MAX_REQUESTS_PER_FLUSH)
        self.assertEqual(len(self.queue()), 601 - 50 * t.MAX_REQUESTS_PER_FLUSH)

    def test_opting_out_while_sending_keeps_nothing(self):
        self.opt_in()
        t.record_tool_run('Find Room', True, INFO, now=NOON)

        def revoke_then_answer(payload):
            t.set_consent(False)
            return 202
        server = FakeServer(revoke_then_answer)
        t.flush(server, now=NOON)
        self.assertEqual(self.state(), {'consent': False})
        self.assertEqual(self.queue(), [])

    def test_what_goes_out_is_only_the_documented_fields(self):
        self.opt_in()
        t.startup(INFO, now=NOON)
        t.record_tool_run('Find Room', True, INFO, now=NOON)
        server = FakeServer()
        t.flush(server, now=NOON)
        allowed = set(['event_id', 'install_id', 'event_type', 'occurred_at',
                       'addin_version', 'channel', 'revit_version', 'pyrevit_version',
                       'schema_version', 'tool', 'result'])
        for event in server.sent_events():
            self.assertTrue(set(event) <= allowed, sorted(event))
            has_tool = event['event_type'] == 'tool_run'
            self.assertEqual('tool' in event, has_tool)
            self.assertEqual('result' in event, has_tool)
        self.assertEqual([e['event_type'] for e in server.sent_events()],
                         ['install', 'heartbeat', 'tool_run'])

    def test_state_files_are_plain_json(self):
        self.opt_in()
        t.record_tool_run('Find Room', True, INFO, now=NOON)
        for name in ('state.json', 'queue.json'):
            with io.open(os.path.join(t.STATE_DIR, name), encoding='utf-8') as fh:
                json.loads(fh.read())
            self.assertFalse(os.path.exists(os.path.join(t.STATE_DIR, name + '.tmp')))


class Repository(unittest.TestCase):

    def pushbuttons(self):
        found = []
        for root, dirs, files in os.walk(REPO):
            dirs[:] = [d for d in dirs if d not in ('.git', '.github')]
            if root.endswith('.pushbutton') and 'script.py' in files:
                found.append(os.path.join(root, 'script.py'))
        return found

    def test_known_tools(self):
        names = sorted(t.tool_name(path) for path in self.pushbuttons())
        self.assertEqual(names, KNOWN_TOOLS)

    def test_every_tool_counts_its_runs(self):
        for path in self.pushbuttons():
            with io.open(path, encoding='utf-8') as fh:
                source = fh.read()
            self.assertIn('\nimport usage\n', source, path)
            self.assertEqual(source.count('\nwith usage.tool_run(__file__) as run:\n'), 1,
                             path)

    def test_the_policy_allows_exactly_the_endpoint(self):
        with io.open(os.path.join(REPO, '.github', 'policy', 'policy.json'),
                     encoding='utf-8') as fh:
            telemetry = json.loads(fh.read())['telemetry']
        self.assertEqual(telemetry['module'], 'lib/telemetry.py')
        self.assertEqual(telemetry['urls'], [t.URL])


if __name__ == '__main__':
    unittest.main()
