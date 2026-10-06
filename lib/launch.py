# -*- coding: utf-8 -*-
"""Run one pyRevit command from inside an ExternalEvent.

Born in toolpane.py (the tool galleries, 2026-09-02) and moved here on
2026-09-15, the day Inspect Model needed to hand a view to Inspect View: an
audit tool has no business importing the 2000-line gallery module for eight
lines. toolpane imports run_command from here; its LaunchHandler is untouched.

Three entry points: run_command(uid, uiapp) runs a command by its pyRevit
unique id right here, run_when_idle(uid, uiapp) runs it on Revit's next Idling
instead, and uid_for_bundle(dir) finds that id for a sibling .pushbutton folder.
All three are only valid inside an ExternalEvent (a modeless.run job) or the
button click itself: never from a WPF handler directly.
"""
import os

from pyrevit import DB
from pyrevit import UI
from pyrevit import framework
from pyrevit.loader import sessionmgr

from slantisui import ui


def run_command(uid, uiapp):
    """Run one pyRevit command by uid, from inside an ExternalEvent.

    NOT sessionmgr.execute_command, and that is the whole point of this
    function. The two are the same three lines except for where the
    UIApplication comes from, and that one difference is a bug that cost an
    afternoon on 2026-09-02.

    What was observed: tools ran fine from the window one after another, then
    one failed with "could not run that tool: Object reference not set to an
    instance of an object", and from that moment EVERY tool failed, including
    ones that had just worked. Reopening the window from the ribbon healed it.

    What it was. sessionmgr.execute_command fabricates the ExternalCommandData
    that pyRevit's own runtime needs, with exactly one field set:

        tmp_cmd_data.Application = HOST_APP.uiapp

    and HOST_APP.uiapp is `__revit__ if isinstance(__revit__, UI.UIApplication)
    else None` (pyrevit/__init__.py:201). In the engine our handler happens to
    run in, __revit__ is not always a UIApplication, so that goes out NULL, and
    the next engine that has to Start() dies reading it:

        System.NullReferenceException
          at PyRevitLabs.PyRevit.Runtime.ScriptRuntime.get_App()
          at ...IronPythonEngine.SetupBuiltins(ScriptRuntime&)
          at ...IronPythonEngine.Start(ScriptRuntime&)

    Start() only runs for a FRESH engine, which is why nine launches in a row
    could work and why, once the pool was poisoned, nothing ran again until a
    real ribbon click re-primed it.

    The fix does not ask the engine for the application at all.
    IExternalEventHandler.Execute is HANDED a live UIApplication by Revit --
    that is what an external event is for -- so the command data is built with
    that one. Everything else stays as pyRevit does it: the uninitialised
    object, and MimicExecFromUI = True, which is what sessionmgr's own
    execute_command_cls defaults to (exec_from_ui=True, sessionmgr.py:692).
    Until 2026-09-08 this function set it False, believing that was pyRevit's
    value; it was not, so the launcher differed from a ribbon click in TWO
    things instead of one. With True, a tool launched from the window is
    executed with the same flag a click gives it. Only the source of the
    UIApplication is ours.

    It also stops swallowing the missing-command case. execute_command logs
    that one through its own logger and RETURNS, so a launch that found nothing
    looked exactly like a click that did nothing at all (sessionmgr.py:546).
    Here it raises and the caller puts it on screen.
    """
    cmd = sessionmgr.find_pyrevitcmd(uid)
    if cmd is None:
        raise Exception("that tool is not in this session any more")
    data = framework.FormatterServices.GetUninitializedObject(
        UI.ExternalCommandData)
    data.Application = uiapp
    instance = cmd()
    instance.ExecConfigs.MimicExecFromUI = True
    instance.Execute(data, '', DB.ElementSet())


def uid_for_bundle(bundle_dir):
    """Unique id of the loaded command whose bundle folder is `bundle_dir`,
    or None when this session did not load it.

    Exact path first: an install can load the same clone twice (for example
    through a pair of junctions onto it), so both tabs hold a command with the
    same folder name and only the full path picks the one from the tab you
    clicked in. When no path matches (an install that resolved a junction
    differently), the folder name decides.
    """
    want = os.path.normcase(os.path.abspath(bundle_dir))
    name = os.path.basename(want)
    by_name = []
    for cmd in sessionmgr.find_all_commands():
        path = cmd.script
        if not path:
            continue
        bundle = os.path.normcase(os.path.dirname(os.path.abspath(path)))
        if bundle == want:
            return cmd.unique_id
        if os.path.basename(bundle) == name:
            by_name.append(cmd.unique_id)
    return by_name[0] if by_name else None


def run_when_idle(uid, uiapp, title=u"Magic Tools"):
    """Run the command on Revit's next Idling instead of right here.

    For a launch that happens inside another ExternalEvent's Execute, as
    Inspect Model's "Inspect view" does (2026-09-15). A tool run nested in the
    launcher's Execute is suspected of not surviving its return: the window
    shows, and the first Raise() from it finds a dead handler. That cause is
    not closed, so this is a guard, not a fix: the command runs once Revit is
    idle, in Revit's own callback and nobody's Execute, which is the shield
    that was proposed for that suspect. It also lets the view switch settle
    first: by the time Idling fires the ActiveView setter has done its work.

    Unsubscribing from inside the callback is the idiom lab_startup uses and
    the one that works (subscribing from inside one threw, 2026-09-08). The
    handler comes off the object it went on, not the sender.
    """
    def once(sender, args):
        try:
            uiapp.Idling -= once
        except Exception:
            pass
        try:
            run_command(uid, sender)
        except Exception as ex:
            ui.alert(u"Could not run that tool: {0}".format(ex), title=title)
    uiapp.Idling += once
