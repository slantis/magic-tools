# -*- coding: utf-8 -*-
"""A tool window that stays open while Revit stays usable.

Design goal, 2026-09-05: "no Magic Tools tool should keep you from continuing
to use Revit; you should be able to keep the windows of any tool open and keep
working". This is the plumbing that makes a slantisui window modeless
without every tool rebuilding it: the pattern other tools carried inline
since 08-2026 and the tool windows (toolwindow.py) since 09-2026,
in one place.

What a modeless window changes, and what this module does about it:

* Show() returns at once, so the script ends while the window is up. The
  tool declares `__persistentengine__ = True` so its engine, its handlers
  and its module state outlive the click. Nothing here can do that for it.
* A click handler on a modeless window runs OUTSIDE a valid Revit API
  context: transactions, selection changes, view changes, even collectors
  are illegal there. `run(fn)` queues the work and Revit calls it back
  through an ExternalEvent, in a valid context, with the live UIApplication.
* The model can change or be swapped while the window sits there. `run()`
  takes the document the window was built for and refuses, with a message,
  when another one is active. Stale ElementIds are the tool's own problem.
* A window with no owner sinks behind Revit on the first click in the
  model. `show()` makes Revit its owner through the Win32 handle.
* A second click on the button would open a second window. `focus(key)` at
  the top of the script brings the live one to the front instead.
  `live(key)` and `close(key)` let the tool rebuild instead when the
  window it finds is stale.

Usage, in a tool:

    import modeless
    if modeless.focus(KEY):
        script.exit()
    ...build rows, build `win` with ui.parse...
    def on_apply(s, e):
        def work(uiapp):
            with Transaction(doc, ...) ...
        modeless.run(work, doc=doc, title=TITLE)
    modeless.show(win, KEY, doc=doc)

Never `HOST_APP.uiapp` for API work: the UIApplication Revit hands to
Execute is the one that is valid (bug of 2026-09-02, toolpane.run_command).
The one exception is the main window handle for ownership, which is not an
API call.
"""
import os
import traceback

import clr
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationFramework')
from Autodesk.Revit import UI
from System import AppDomain, Object
from System.Windows import WindowState
from System.Windows.Interop import WindowInteropHelper

from pyrevit import HOST_APP

from slantisui import ui

_HANDLER = None
_EVENT = None

# The registry of open windows lives in the AppDomain, not in this module
# (QA round 3, 2026-10-06). A module dict (`_OPEN`, until then) only
# held while every click ran in the same engine, and it did not: the trace
# showed "arm: ExternalEvent created" on each click while the first window
# kept answering, so each click got a fresh copy of this module and focus()
# never found the window. Five tools opened a second window (Inspect View
# piled up ten). The AppDomain outlives any engine, the same place
# lib/favbar.py keeps its state. Each entry also records the pyRevit session
# it was opened in: after a Reload the old window is closed and rebuilt, so
# the button never brings back a window running the code from before the
# Reload (the Cloud Manager bug of 2026-10-06, fc5373d). And the model it
# was built for (plus an optional tag, the view for a view-bound tool): a
# click in another model rebuilds instead of bringing back a window whose
# every action would be refused.
# The AppDomain is shared by every extension in the session, so the slots
# are scoped by this file's folder: the open source package and the team's
# Magic Tools (or MT Testing) side by side never hand each other a window.
_SCOPE = os.path.dirname(os.path.abspath(__file__)).lower() + '|'
_SLOT = 'magictools.modeless.win.'
_SLOT_SESSION = 'magictools.modeless.session.'
_SLOT_DOC = 'magictools.modeless.doc.'
_SLOT_TAG = 'magictools.modeless.tag.'


def _slot(prefix, key):
    return prefix + _SCOPE + key


def _session():
    try:
        from pyrevit.coreutils import envvars
        return envvars.get_pyrevit_env_var(envvars.SESSIONUUID_ENVVAR) or ''
    except Exception:
        return ''


def _get(key, prefix=_SLOT):
    try:
        return AppDomain.CurrentDomain.GetData(_slot(prefix, key))
    except Exception:
        return None


def _put(key, win, doc=None, tag=None):
    domain = AppDomain.CurrentDomain
    try:
        domain.SetData(_slot(_SLOT, key), win)
        domain.SetData(_slot(_SLOT_SESSION, key),
                       _session() if win is not None else None)
        domain.SetData(_slot(_SLOT_DOC, key), doc)
        domain.SetData(_slot(_SLOT_TAG, key), tag)
    except Exception:
        traceback.print_exc()


def _forget(key, win=None):
    """Drop the entry; with `win`, only if it is still that window."""
    if win is None or Object.ReferenceEquals(_get(key), win):
        _put(key, None)


def _stale(key):
    """Opened before the last pyRevit Reload. An unreadable session id says
    nothing, so it never closes a healthy window."""
    opened_in = _get(key, _SLOT_SESSION)
    current = _session()
    return bool(opened_in) and bool(current) and opened_in != current


def _active_doc():
    try:
        return HOST_APP.uiapp.ActiveUIDocument.Document
    except Exception:
        return None


def _other_model(key, doc):
    """True when the window was built for another model than `doc` (the
    active one when None). Unknown on either side reads as the same model."""
    built_for = _get(key, _SLOT_DOC)
    if doc is None:
        doc = _active_doc()
    if built_for is None or doc is None:
        return False
    try:
        return not built_for.Equals(doc)
    except Exception:
        return True                 # its model was closed

# FORENSIC TRACE (2026-09-08). One line per step, appended to a file in
# %APPDATA%/pyRevit, because the pyRevit output window dies with Revit and the
# two crashes of today left no readable stack anywhere. Cheap, and it goes
# once the cause is closed.
import datetime
import os
TRACE = os.path.join(os.environ.get('APPDATA', '.'), 'pyRevit', '_magictools_modeless.log')


def _trace(msg):
    try:
        handle = open(TRACE, 'a')
        try:
            handle.write(("{0} {1}" + chr(10)).format(datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3], msg))
        finally:
            handle.close()
    except Exception:
        pass


class _Handler(UI.IExternalEventHandler):
    """Runs every queued job, in order, inside the context Revit gives it."""

    def __init__(self):
        self.queue = []

    def Execute(self, uiapp):
        # No print here: a print from Execute opens the pyRevit output window
        # of whichever tool queued the job, one per launch (2026-09-15, after
        # Inspect view opened two of them). The file trace keeps the line.
        _trace("Execute enter, {0} job(s)".format(len(self.queue)))
        # Drain the whole queue: Raise() coalesces, so two quick clicks can
        # arrive as one Execute, and a job left in the queue would wait for
        # the next click to run.
        while self.queue:
            fn, doc, title = self.queue.pop(0)
            try:
                if doc is not None and not _is_active(uiapp, doc):
                    _trace("job '{0}' refused: another document is active".format(title))
                    ui.alert(u"This window belongs to another model. Switch "
                             u"back to it and try again.", title=title)
                    continue
                _trace("job '{0}' start".format(title))
                fn(uiapp)
                _trace("job '{0}' done".format(title))
            except Exception:
                _trace("job '{0}' raised: {1}".format(title, traceback.format_exc().strip().splitlines()[-1]))
                traceback.print_exc()
                try:
                    ui.alert(traceback.format_exc(), title=title + u" - Error")
                except Exception:
                    pass

    def GetName(self):
        return "Magic Tools modeless window"


def _is_active(uiapp, doc):
    try:
        active = uiapp.ActiveUIDocument
        return active is not None and active.Document.Equals(doc)
    except Exception:
        return False


def arm():
    """Create the ExternalEvent once. Needs a valid API context: the button
    click that runs the script is one, a WPF click handler is not, so this
    is called at script start (show() calls it too, which is still in the
    click). Idempotent."""
    global _HANDLER, _EVENT
    if _EVENT is None:
        _HANDLER = _Handler()
        _EVENT = UI.ExternalEvent.Create(_HANDLER)
        _trace("arm: ExternalEvent created")
    return _EVENT


def run(fn, doc=None, title=u"Magic Tools"):
    """Queue fn(uiapp) for Revit to run in a valid API context; returns now.

    `doc` is the document the window was built for: with another one active
    the job is skipped and the user told. `title` names the alert. Anything
    the job raises is shown, not swallowed.
    """
    # A window can queue work while it is still being built (a ListBox that
    # selects its first row in __init__, before show() ran): that is still
    # the command's context, so arming here is legal. From a click handler
    # with nothing armed, Create() throws its own "not in a valid context",
    # which is the right error to see.
    # DIAGNOSTIC (2026-09-08): every job queued from a tool window did
    # nothing from the ribbon and took Revit down from All Magic Tools, while
    # the same module run from the Routes engine executed fine. So the state
    # Raise() returns and any failure to arm are put ON SCREEN until the cause
    # is found; the alert names the state so it can be read back.
    try:
        arm()
    except Exception as ex:
        _trace("run '{0}': arm failed {1}".format(title, ex))
        ui.alert(u"modeless: could not arm the ExternalEvent from this click "
                 u"-> {0}".format(ex), title=title)
        return
    _HANDLER.queue.append((fn, doc, title))
    try:
        state = _EVENT.Raise()
        _trace("run '{0}': Raise -> {1}, queue {2}".format(title, state, len(_HANDLER.queue)))
    except Exception as ex:
        _trace("run '{0}': Raise threw {1}".format(title, ex))
        ui.alert(u"modeless: Raise() threw -> {0}".format(ex), title=title)
        return
    if state == UI.ExternalEventRequest.Pending:
        # Not a failure: Raise() coalesces, so a job queued while Revit is
        # still draining a previous one comes back Pending and runs on the
        # same pass -- normal under a burst of quick clicks. Showing this to
        # the user reads as a broken tool when nothing is wrong (reported in
        # QA on Print Set Manager, 2026-09-24); keep it in the trace
        # file only, and still alert on a real failure (Denied, TimedOut, or
        # anything else that is not Accepted).
        pass
    elif state != UI.ExternalEventRequest.Accepted:
        ui.alert(u"modeless: Revit did not take the job (Raise -> {0}); "
                 u"the queue holds {1}.".format(state, len(_HANDLER.queue)),
                 title=title)


def focus(key, doc=None, tag=None):
    """True if a window with this key is open, after bringing it to front.
    Call it first thing: when it is True the script has nothing to build.
    `doc` and `tag` as in live()."""
    win = live(key, doc=doc, tag=tag)
    if win is None:
        return False
    try:
        if win.WindowState == WindowState.Minimized:
            win.WindowState = WindowState.Normal
        win.Activate()
        return True
    except Exception:
        _forget(key)                # closed or dead: let the caller rebuild
        return False


def live(key, doc=None, tag=None):
    """The open window registered under `key`, or None. For a tool that wants
    to look at its own window before deciding between focus() and close().

    A window that no longer fits is closed here, so the caller builds a new
    one: opened before a pyRevit Reload (it runs the old code), built for
    another model than `doc` (the active one when None), or, when `tag` is
    given, shown with another tag (Scan Current View passes the view). If its
    own Closing refuses (unsaved edits, the user chose Cancel), it stays and
    is returned: one window, not two."""
    win = _get(key)
    if win is None:
        return None
    why = None
    if _stale(key):
        why = "window from before a Reload"
    elif _other_model(key, doc):
        why = "window of another model"
    elif tag is not None and _get(key, _SLOT_TAG) != tag:
        why = "window shown for another tag"
    if why is not None:
        _trace("live '{0}': {1}, closing it".format(key, why))
        close(key)
        return _get(key)
    return win


def close(key):
    """Close the window registered under `key`; True if there was one.

    For a tool that finds its window built for a state that no longer holds
    and would rather rebuild than refocus: Inspect View on another view,
    2026-09-15, when Inspect Model hands it a view. Same UI thread as show(),
    so Close() is legal here. The entry goes only once the window is really
    gone: a Closing handler that cancels (Print Set Manager and View Template
    Manager with unsaved edits) leaves it open and still registered.
    """
    win = _get(key)
    if win is None:
        return False
    try:
        win.Close()
    except Exception:
        traceback.print_exc()
    try:
        still_open = win.IsVisible
    except Exception:
        still_open = False
    if not still_open:
        _forget(key, win)
    return True


def own_by_revit(win):
    """Revit's main window is an HWND, not a WPF Window, so Owner cannot be
    assigned directly; WindowInteropHelper is the bridge. Non-fatal: if it
    fails the window works, it just sinks behind Revit."""
    try:
        WindowInteropHelper(win).Owner = HOST_APP.uiapp.MainWindowHandle
    except Exception:
        traceback.print_exc()


def show(win, key, doc=None, on_closed=None, tag=None):
    """Show `win` modeless, owned by Revit, remembered under `key`, with the
    model it is built for (`doc`) and an optional `tag` that live() and
    focus() compare against.

    `on_closed(sender, args)` runs when the window closes, after the
    registry forgot it: the place to drop references the persistent engine
    would otherwise keep alive. Returns the window.
    """
    arm()
    _put(key, win, doc=doc, tag=tag)

    def closed(sender, args):
        _forget(key, win)
        if on_closed is not None:
            try:
                on_closed(sender, args)
            except Exception:
                traceback.print_exc()

    win.Closed += closed
    own_by_revit(win)
    win.Show()
    return win
