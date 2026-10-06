# -*- coding: utf-8 -*-
"""Boot work of this extension.

The extension's startup.py ends with a guarded `import lab_startup`, and this
file is that import. It resolves because sessionmgr.py:206-208 puts the
extension's library_path on sys.path before running its startup script.

What it does, once, on the first Idling after the load:

  1. toolpane.arm()          the ExternalEvents that run a tool and sync the
                             bar from the modeless window
  2. favbar.hide_strays()    any panel of the tab that is not Tools/Favorites
  3. favbar.sync()           the veil: the starred buttons of Favorites shown,
                             every other one hidden (Tools is never touched)

and then, staying subscribed for SETTLE_IDLES more Idlings, favbar.unfold(): Revit may
fold the Favorites panel into a drop-down AFTER that first pass, once it lays
the tab out with the flash of every button still counted (seen 2026-09-08,
after a Reload: the Favorites panel turned into a pulldown; sync() already
unfolds once and it was not enough). A handful of Idlings later the layout has
settled, and an unfold that finds the panel open costs nothing.

WHY THE FIRST IDLING. A startup script does not hold a UIApplication (at Revit
boot __revit__ is the UIControlledApplication) and the ribbon is not finished
when this runs (sessionmgr builds the UI after the startup scripts). The
Idling event's sender IS the UIApplication, it fires once the UI is up, and it
is a valid API context for ExternalEvent.Create. Each step is guarded on its
own: a failure in one prints its reason and the next one still runs, and a
failure in all of them leaves every button visible, which is the failure mode
this design chose (see lib/favbar.py).

No dockable panes any more (the 6.x loader broke the hiding they relied on):
the doors open windows, which need nothing at boot.
"""
from pyrevit import HOST_APP

import favbar
import toolpane


def _revit():
    """The `__revit__` pyRevit injects into builtins, or None.

    Read by BARE NAME, never `globals().get('__revit__')`: an imported module's
    globals() does not include builtins, so that lookup is always None here
    (found by the 2026-09-04 review; pyRevit's own compat.py:379 reads it the
    same bare way). HOST_APP.uiapp is no substitute either: it is None at a
    cold boot, when __revit__ is a UIControlledApplication (HOST_APP checks
    isinstance UIApplication). Both wrong candidates would have left the veil
    off until the first Reload.
    """
    try:
        return __revit__  # noqa: F821  (a builtin, injected by PyRevitLoader)
    except NameError:
        return None


def _host(member):
    """First object at hand that actually exposes `member`.

    At Revit boot we hold a UIControlledApplication, on a pyRevit Reload a
    UIApplication. Both expose Idling, so we ask for the member instead of
    guessing the type.
    """
    for candidate in (_revit(), HOST_APP.uiapp):
        if candidate is not None and hasattr(candidate, member):
            return candidate
    return None


SETTLE_IDLES = 6
_state = {"done": False, "left": 0}


def _first_idle(sender, args):
    """The veil on the first Idling, then favbar.unfold() on the next few.

    ONE handler that stays subscribed and counts itself down, not a second
    one armed from inside this callback: subscribing to Idling while Revit is
    raising it threw ("Exception has been thrown by the target of an
    invocation", cold boot, 2026-09-08 01:45), and the notice it printed
    opened the pyRevit output window over every Revit start (a notice at
    startup is unwanted). Unsubscribing from inside the callback is what this
    handler always did, and that works.
    """
    if _state["done"]:
        _state["left"] -= 1
        try:
            favbar.unfold()
        except Exception as ex:
            print("magic tools boot: could not unfold the Favorites panel -> {0}".format(ex))
        if _state["left"] <= 0:
            try:
                _host('Idling').Idling -= _first_idle
            except Exception:
                pass
        return
    _state["done"] = True
    _state["left"] = SETTLE_IDLES
    # `sender` is the live UIApplication, and it is the ONLY application object
    # anything below is handed: never HOST_APP.uiapp (toolpane.run_command).
    jobs = [("arm the launcher", lambda: toolpane.arm()),
            ("hide stray panels", lambda: favbar.hide_strays(sender)),
            ("veil the Favorites panel", lambda: favbar.sync(sender))]
    for label, job in jobs:
        try:
            job()
        except Exception as ex:
            print("magic tools boot: could not {0} -> {1}".format(label, ex))


try:
    _host('Idling').Idling += _first_idle
except Exception as sub_ex:
    print("magic tools boot: could not subscribe to Idling -> {0}".format(sub_ex))
