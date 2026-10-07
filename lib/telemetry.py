# -*- coding: utf-8 -*-
"""Opt-in usage data: the state on disk, the event queue and the sender.

Nothing here records or sends anything until the user said yes to the prompt
(lib/usage.py asks, on the first click of any tool, never while Revit loads),
and nothing at all while DO_NOT_TRACK or MAGIC_TOOLS_TELEMETRY turns it off
(env_disabled). TELEMETRY.md, at the root of the repository, is the public
description of what is sent: keep the two in step.

This is the only module of the extension that reaches the network, and only
the endpoint below: .github/policy/policy.json lists the same URL and the
policy check rejects any other. Every request runs on a background thread
with a 5 s timeout, and every exception is swallowed: nothing here may block
or break Revit. No Revit API call is made off the UI thread; the
versions are read by collect_info(), on the UI thread, and kept in the state.

Per Windows user, in %APPDATA%/pyRevit/magic-tools-telemetry/:

    state.json  the answer to the prompt, the random install ID, whether its
                "install" event was queued, the UTC day of the last heartbeat,
                the retry backoff and the versions last read
    queue.json  the events waiting to be sent, each with its event_id already
                assigned, so a retry resends the same one (the server
                deduplicates on it); at most MAX_QUEUE, the oldest go first

Both files are rewritten whole, through a temporary file, under a lock that
every thread and every Revit process of the user shares (_Lock).

Every event goes to the one endpoint, with no credentials: the server creates
or updates the row of its installation from any event. The "install" event is
queued once per install ID, first in the queue, when the user says yes.

ADDING A TOOL. The server counts only the tool_run events whose tool is on its
whitelist (its "tools" table): the folder name of the .pushbutton without the
suffix, as tool_name() reads it. A new tool needs its name added there by a
maintainer of the server before it is released, or its runs are not counted.
"""
import io
import json
import os
import re
import sys
import threading
import time

# Public on purpose, with no key or token: the client is open source, so the
# server protects itself (validation, rate limits, a maximum body size).
URL = "https://ydedbgryazrwxtwzjauo.supabase.co/functions/v1/magic-tools-telemetry"

TIMEOUT_MS = 5000
MAX_BATCH = 50                  # events per request, the server's limit
MAX_BODY_BYTES = 60000          # the server takes bodies under 64 KB
MAX_QUEUE = 1000
MAX_REQUESTS_PER_FLUSH = 10     # the server allows 30 requests a minute per IP
BACKOFF_SECONDS = (60, 300, 900, 3600)  # after a 429, 60 s at least
LOCK_WAIT_MS = 3000
SCHEMA_VERSION = 1

OPTIONAL_KEYS = ('revit_version', 'pyrevit_version')
VERSION_RE = re.compile(r'^[A-Za-z0-9_.\-+ ]+$')
NOT_VERSION_CHARS = re.compile(r'[^A-Za-z0-9_.\-+ ]')
TOOL_FORBIDDEN = u'<>"\'`'
PUSHBUTTON = '.pushbutton'

OFF_WORDS = ('0', 'false', 'no', 'off')
EXT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'),
                         'pyRevit', 'magic-tools-telemetry')

DEBUG = False


def _debug(message):
    # A print from a background thread or an ExternalEvent opens the pyRevit
    # output window, so nothing is printed unless DEBUG is set by hand.
    if DEBUG:
        try:
            print(u"magic tools usage data: {0}".format(message))
        except Exception:
            pass


# --- Switches -----------------------------------------------------------------

def env_disabled(environ=None):
    """Name of the environment variable that turns usage data off, or None.

    DO_NOT_TRACK set to anything but an "off" word (0, false, no, off), or
    MAGIC_TOOLS_TELEMETRY set to one of them, for IT to turn it off for a
    whole studio. They win over the answer to the prompt: while either is set
    nothing is asked, recorded or sent.
    """
    env = os.environ if environ is None else environ
    dnt = (env.get('DO_NOT_TRACK') or '').strip().lower()
    if dnt and dnt not in OFF_WORDS:
        return 'DO_NOT_TRACK'
    own = (env.get('MAGIC_TOOLS_TELEMETRY') or '').strip().lower()
    if own in OFF_WORDS + ('disable', 'disabled'):
        return 'MAGIC_TOOLS_TELEMETRY'
    return None


# --- What is sent ---------------------------------------------------------------

def clean_version(value, limit):
    """value as the server accepts a version (allowed characters, at most
    `limit` of them), or None when nothing is left."""
    if value is None:
        return None
    try:
        text = u'{0}'.format(value)
    except Exception:
        return None
    text = NOT_VERSION_CHARS.sub(u'', text).strip()[:limit].strip()
    return text or None


def addin_version(ext_dir=None):
    """The "version" of extension.json, or "unknown" when it is missing or
    is not a valid version string."""
    try:
        with io.open(os.path.join(ext_dir or EXT_DIR, 'extension.json'),
                     encoding='utf-8') as fh:
            value = json.load(fh).get('version')
    except Exception:
        value = None
    text = value.strip() if isinstance(value, (type(u''), str)) else u''
    if text and len(text) <= 50 and VERSION_RE.match(text):
        return text
    return u'unknown'


def channel(ext_dir=None):
    """"git" for a git checkout of the extension, "zip" for anything else."""
    return 'git' if os.path.exists(os.path.join(ext_dir or EXT_DIR, '.git')) else 'zip'


def _revit_version(uiapp):
    if uiapp is not None:
        try:
            return uiapp.Application.VersionNumber
        except Exception:
            pass
    try:
        from pyrevit import HOST_APP
        return HOST_APP.version
    except Exception:
        return None


def _pyrevit_version():
    try:
        from pyrevit import versionmgr
        return versionmgr.get_pyrevit_version().get_formatted()
    except Exception:
        return None


def collect_info(uiapp=None):
    """The versions and the channel, read now. Call it on Revit's UI thread:
    it asks the Revit application for its version."""
    info = {'addin_version': addin_version(), 'channel': channel()}
    for key, value, limit in (('revit_version', _revit_version(uiapp), 50),
                              ('pyrevit_version', _pyrevit_version(), 60)):
        value = clean_version(value, limit)
        if value:
            info[key] = value
    return info


def normalize_info(info, previous=None):
    """The four fields the server takes, valid, from `info`. A version that
    `info` lacks is taken from `previous`; the two required fields fall back
    to what the extension folder says."""
    info = info or {}
    previous = previous or {}
    version = info.get('addin_version') or previous.get('addin_version')
    version = clean_version(version, 50) if version else None
    out = {'addin_version': version if version and VERSION_RE.match(version)
           else addin_version(),
           'channel': info.get('channel') or previous.get('channel') or channel()}
    if out['channel'] not in ('git', 'zip'):
        out['channel'] = channel()
    for key, limit in zip(OPTIONAL_KEYS, (50, 60)):
        value = clean_version(info.get(key) or previous.get(key), limit)
        if value:
            out[key] = value
    return out


def clean_tool(name):
    """name as the server takes a tool name, or None."""
    if not name:
        return None
    name = u'{0}'.format(name).strip()
    if not name or len(name) > 100 or any(char in name for char in TOOL_FORBIDDEN):
        return None
    return name


def tool_name(path):
    """"Select Same Family" for .../Select Same Family.pushbutton/script.py (or
    for the folder itself); None when the path is not inside a pushbutton."""
    try:
        path = u'{0}'.format(path).replace('\\', '/').rstrip('/')
        if path.lower().endswith('.py'):
            path = path.rsplit('/', 1)[0]
        name = path.rsplit('/', 1)[-1]
    except Exception:
        return None
    if not name.lower().endswith(PUSHBUTTON):
        return None
    return clean_tool(name[:-len(PUSHBUTTON)])


def iso_utc(now):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))


def utc_day(now):
    return time.strftime('%Y-%m-%d', time.gmtime(now))


def new_uuid():
    """A random (version 4) UUID, lower case."""
    if sys.platform == 'cli':
        try:
            from System import Guid
            return str(Guid.NewGuid()).lower()
        except Exception:
            pass
    import uuid
    return str(uuid.uuid4())


def make_event(install_id, info, event_type, tool=None, ok=True, now=None):
    """One event, with its event_id. `tool` and `result` only
    exist on a tool_run: the server drops an install or a heartbeat that
    carries them."""
    info = normalize_info(info)
    event = {'event_id': new_uuid(), 'install_id': install_id,
             'event_type': event_type,
             'occurred_at': iso_utc(time.time() if now is None else now),
             'addin_version': info['addin_version'], 'channel': info['channel'],
             'schema_version': SCHEMA_VERSION}
    for key in OPTIONAL_KEYS:
        if info.get(key):
            event[key] = info[key]
    if event_type == 'tool_run':
        event['tool'] = tool
        event['result'] = 'ok' if ok else 'error'
    return event


def dumps(payload):
    """The request body. ASCII only, so its length is its size in bytes."""
    return json.dumps(payload, separators=(',', ':'), sort_keys=True)


def batches(events, max_items=MAX_BATCH, max_bytes=MAX_BODY_BYTES):
    """events cut into lists of at most max_items whose body is at most
    max_bytes. An event too big on its own still gets a list of its own: the
    server answers 413 and it is dropped."""
    out, current, size = [], [], 2          # 2: the brackets of the array
    for event in events:
        item = len(dumps(event))
        grown = size + item + (1 if current else 0)
        if current and (len(current) >= max_items or grown > max_bytes):
            out.append(current)
            current, grown = [], 2 + item
        current.append(event)
        size = grown
    if current:
        out.append(current)
    return out


def outcome(status):
    """What a status code means for what was sent: "sent" (202, or 200 for
    an empty array; an event the server rejects on its own is kept there with
    the reason, so sending it again changes nothing), "drop" (the server will
    never take it: 400, 413) or "retry" (anything else: 429, 503, 0 being a
    network error or a timeout)."""
    if 200 <= status < 300:
        return 'sent'
    if status in (400, 413):
        return 'drop'
    return 'retry'


def backoff_seconds(failures):
    """The wait after `failures` failed attempts in a row: 1 min, 5 min,
    15 min, then 1 h."""
    index = min(max(failures, 1), len(BACKOFF_SECONDS)) - 1
    return BACKOFF_SECONDS[index]


def send_events(batch, post):
    """Post one batch, halving it on 413 down to single events, which are
    then dropped. Returns (event_ids done with, retry): sent and dropped
    events are both done; on a retryable status the rest of the batch stays
    queued and the caller backs off."""
    done, parts = [], [batch]
    while parts:
        part = parts.pop(0)
        status = post(URL, part)
        result = outcome(status)
        if status == 413 and len(part) > 1:
            half = len(part) // 2
            parts[:0] = [part[:half], part[half:]]
        elif result == 'retry':
            return done, True
        else:
            done.extend(event.get('event_id') for event in part)
    return done, False


# --- The files ------------------------------------------------------------------

class _Lock(object):
    """A lock that every thread and every Revit process of this user shares.

    A named Mutex under IronPython: pyRevit loads this module once per engine,
    and two Revit sessions share the files, so a threading lock would only
    cover its own copy. A threading lock elsewhere (the tests). A Mutex
    belongs to the thread that took it, so every acquire() here is released
    by the same function, on the same thread.
    """

    def __init__(self, name):
        self.name = name
        self._mutex = None
        self._local = threading.Lock()

    def acquire(self, wait_ms):
        if sys.platform != 'cli':
            return self._local.acquire(bool(wait_ms))
        try:
            if self._mutex is None:
                from System.Threading import Mutex
                self._mutex = Mutex(False, self.name)
            return bool(self._mutex.WaitOne(wait_ms))
        except Exception as ex:
            # AbandonedMutexException: a thread ended while it held the
            # mutex. WaitOne has still made it ours.
            return 'Abandoned' in type(ex).__name__

    def release(self):
        try:
            if self._mutex is not None:
                self._mutex.ReleaseMutex()
            else:
                self._local.release()
        except Exception:
            pass


_STATE_LOCK = _Lock('Local\\MagicToolsTelemetryState')
_SEND_LOCK = _Lock('Local\\MagicToolsTelemetrySend')


def _paths():
    return (os.path.join(STATE_DIR, 'state.json'),
            os.path.join(STATE_DIR, 'queue.json'))


def _read(path, default):
    """The JSON in path; default when there is no file or it does not parse;
    None when it exists but cannot be read now."""
    if not os.path.exists(path):
        return default
    try:
        with io.open(path, encoding='utf-8') as fh:
            text = fh.read()
    except (IOError, OSError):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def _replace(source, target):
    if hasattr(os, 'replace'):                      # Python 3
        os.replace(source, target)
        return
    if sys.platform == 'cli':
        try:
            from System.IO import File
            if File.Exists(target):
                File.Replace(source, target, None)
            else:
                File.Move(source, target)
            return
        except Exception:
            pass
    if os.name == 'nt' and os.path.exists(target):
        os.remove(target)
    os.rename(source, target)


def _write(path, value):
    if not os.path.isdir(STATE_DIR):
        os.makedirs(STATE_DIR)
    temp = path + '.tmp'
    with io.open(temp, 'w', encoding='utf-8') as fh:
        fh.write(u'' + json.dumps(value, sort_keys=True))
    _replace(temp, path)


def _transact(change, with_queue=True):
    """change(state, queue) under the lock, both saved when it returns True.
    Returns what change returned; None when the files could not be used.
    Without with_queue the queue is not read, change gets None for it and
    must not ask for a save."""
    if not _STATE_LOCK.acquire(LOCK_WAIT_MS):
        return None
    try:
        state_path, queue_path = _paths()
        state = _read(state_path, {})
        queue = _read(queue_path, []) if with_queue else []
        if state is None or queue is None:
            return None
        if not with_queue:
            return change(state, None)
        result = change(state, queue)
        if result:
            _write(state_path, state)
            _write(queue_path, queue[-MAX_QUEUE:])
        return result
    except Exception as ex:
        _debug(ex)
        return None
    finally:
        _STATE_LOCK.release()


def _snapshot(with_queue=True):
    """(state, queue) as saved now, or None when they cannot be read. Without
    with_queue the queue is not read (it can hold a thousand events) and
    comes back None."""
    copy = {}

    def read(state, queue):
        copy['state'], copy['queue'] = state, queue
        return False
    _transact(read, with_queue)
    if 'state' not in copy:
        return None
    return copy['state'], copy['queue']


def _consented(state):
    return state.get('consent') is True and bool(state.get('install_id'))


def _backoff(state, now):
    failures = int(state.get('failures') or 0) + 1
    state['failures'] = failures
    state['next_attempt_at'] = now + backoff_seconds(failures)


def _reset_backoff(state):
    state.pop('failures', None)
    state.pop('next_attempt_at', None)


def _queue_install(state, queue, now):
    """The "install" event, first in the queue, once per install ID."""
    install_id = state['install_id']
    if state.get('install_event') != install_id:
        queue.insert(0, make_event(install_id, state.get('info'), 'install', now=now))
        state['install_event'] = install_id


# --- What the extension calls -----------------------------------------------------

def consent():
    """True or False once the user answered, None before (or when the state
    cannot be read now, so nobody is asked twice by mistake: see
    prompt_needed)."""
    snap = _snapshot(with_queue=False)
    if snap is None:
        return None
    value = snap[0].get('consent')
    return value if isinstance(value, bool) else None


def prompt_needed():
    """True when the prompt has to be shown: no environment variable turns
    usage data off, the state can be read, and the user never answered."""
    if env_disabled():
        return False
    snap = _snapshot(with_queue=False)
    return snap is not None and not isinstance(snap[0].get('consent'), bool)


def is_enabled():
    return env_disabled() is None and consent() is True


def set_consent(granted, info=None):
    """Store the answer. Yes: a new install ID and its "install" event, then
    the sending starts on its own thread. No, or a later opt-out: the queue,
    the install ID and everything else is deleted; only the "no" is kept, so
    nobody is asked again. A later yes starts over with a new install ID."""
    def change(state, queue):
        if granted and _consented(state):
            return False
        state.clear()
        del queue[:]
        state['consent'] = bool(granted)
        if granted:
            state['install_id'] = new_uuid()
            state['info'] = normalize_info(info)
            _queue_install(state, queue, time.time())
        return True
    _transact(change)
    if granted:
        flush_async()


def record_tool_run(tool, ok, info, now=None):
    """Queue one tool_run event and start sending. Nothing without consent."""
    tool = clean_tool(tool)
    if not tool or env_disabled():
        return

    def change(state, queue):
        if not _consented(state):
            return False
        state['info'] = normalize_info(info, state.get('info'))
        queue.append(make_event(state['install_id'], state['info'], 'tool_run',
                                tool=tool, ok=ok, now=now))
        return True
    if _transact(change):
        flush_async()


def startup(info, now=None):
    """At extension load: the day's heartbeat, if there was none yet, then a
    send of whatever is queued, including what an earlier session left.
    Nothing without consent."""
    if env_disabled():
        return
    now = time.time() if now is None else now

    def change(state, queue):
        if not _consented(state):
            return False
        state['info'] = normalize_info(info, state.get('info'))
        for key in ('registered', 'rejected'):      # left by 0.2.0
            state.pop(key, None)
        _queue_install(state, queue, now)
        day = utc_day(now)
        if state.get('heartbeat_day') != day:
            queue.append(make_event(state['install_id'], state['info'],
                                    'heartbeat', now=now))
            state['heartbeat_day'] = day
        return True
    if _transact(change):
        flush_async()


# --- Sending ----------------------------------------------------------------------

_WANTED = [False]


def flush_async():
    """flush() on a background thread. A call that finds a send under way
    leaves it a note to go round once more."""
    if env_disabled():
        return
    _WANTED[0] = True
    _spawn(_drain)


def _drain():
    while _WANTED[0]:
        if not _SEND_LOCK.acquire(0):
            return
        try:
            _WANTED[0] = False
            _flush(_post, time.time())
        except Exception as ex:
            _debug(ex)
        finally:
            _SEND_LOCK.release()


def flush(post=None, now=None):
    """Send the queue. Blocking: the extension calls flush_async().
    post(url, payload) returns a status code, 0 for a network error or a
    timeout."""
    if env_disabled() or not _SEND_LOCK.acquire(0):
        return
    try:
        _flush(post or _post, time.time() if now is None else now)
    except Exception as ex:
        _debug(ex)
    finally:
        _SEND_LOCK.release()


def _flush(post, now):
    requests = 0
    while requests < MAX_REQUESTS_PER_FLUSH:
        snap = _snapshot()
        if snap is None:
            return
        state, queue = snap
        if not _consented(state) or now < (state.get('next_attempt_at') or 0):
            return
        install_id = state['install_id']
        pending = [event for event in queue if event.get('install_id') == install_id]
        if not pending:
            return
        batch = batches(pending[:MAX_BATCH])[0]
        done, retry = send_events(batch, post)
        requests += 1
        _transact(lambda st, q: _after_events(st, q, done, retry, now))
        if retry:
            return


def _after_events(state, queue, done, retry, now):
    if not _consented(state):
        return False
    done = set(done)
    install_id = state['install_id']
    queue[:] = [event for event in queue if event.get('event_id') not in done
                and event.get('install_id') == install_id]
    if retry:
        _backoff(state, now)
    else:
        _reset_backoff(state)
    return True


def _spawn(job):
    def body():
        try:
            job()
        except:  # noqa: E722 -- an exception that leaves a .NET thread ends Revit
            pass
    if sys.platform == 'cli':
        try:
            from System.Threading import Thread, ThreadStart
            thread = Thread(ThreadStart(body))
            thread.IsBackground = True
            thread.Start()
            return
        except Exception as ex:
            _debug(ex)
    try:
        thread = threading.Thread(target=body)
        thread.daemon = True
        thread.start()
    except Exception as ex:
        _debug(ex)


_TLS_CHECKED = [False]


def _allow_tls12():
    """Add TLS 1.2 to the protocols .NET Framework may use (Revit 2022-2024),
    and only when the process pinned a list without it: SystemDefault (0)
    already lets the OS pick, and replacing it would narrow every add-in."""
    if _TLS_CHECKED[0]:
        return
    _TLS_CHECKED[0] = True
    try:
        from System.Net import ServicePointManager, SecurityProtocolType
        current = ServicePointManager.SecurityProtocol
        if int(current) != 0 and not int(current) & int(SecurityProtocolType.Tls12):
            ServicePointManager.SecurityProtocol = current | SecurityProtocolType.Tls12
    except Exception as ex:
        _debug(ex)


def _post(url, payload):
    """POST payload as JSON. The status code, or 0 on a network error or a
    timeout. Never raises."""
    try:
        return _dotnet_post(url, payload)
    except Exception as ex:
        _debug(ex)
        return 0


def _dotnet_post(url, payload):
    import clr
    try:
        # Revit 2025+ runs .NET 8, where HttpWebRequest lives in its own
        # assembly; .NET Framework has it in System, which is already loaded.
        clr.AddReference('System.Net.Requests')
    except Exception:
        pass
    from System.Net import HttpWebRequest, WebException
    from System.Text import Encoding
    _allow_tls12()
    data = Encoding.UTF8.GetBytes(dumps(payload))
    request = HttpWebRequest.Create(url)
    request.Method = "POST"
    request.ContentType = "application/json"
    request.Timeout = TIMEOUT_MS
    request.ReadWriteTimeout = TIMEOUT_MS
    request.ContentLength = data.Length
    stream = request.GetRequestStream()
    try:
        stream.Write(data, 0, data.Length)
    finally:
        stream.Close()
    try:
        response = request.GetResponse()
    except WebException as ex:
        response = ex.Response
        if response is None:
            return 0
    try:
        return int(response.StatusCode)
    finally:
        response.Close()
