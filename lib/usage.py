# -*- coding: utf-8 -*-
"""The visible side of the opt-in usage data: the prompt, the wrapper every
tool script runs in, and the "Share usage data" switch of All Magic Tools.

lib/telemetry.py keeps the state and does the sending; TELEMETRY.md says what
is sent. Every tool script wraps its run like this:

    import usage

    with usage.tool_run(__file__) as run:
        try:
            ...the tool...
        except Exception:
            run.error()             # a catch-all still counts as an error
            traceback.print_exc()

That is one tool_run event per click, recorded when the block ends: "error"
when the block raised or called run.error(), "ok" otherwise. script.exit()
raises SystemExit, which is a normal end: it counts as "ok" and goes on up to
pyRevit as before. A modeless tool's block ends once its window is shown, so it
counts once, when it opens. A tool started from All Magic Tools runs its own
script, so it counts as itself and the window does not count it again.

The first click of any tool, while the user has not answered, shows the prompt
before the tool runs. Nothing is recorded or sent before a yes, and nothing
here may break a tool: every failure of the usage data is swallowed.
"""
TELEMETRY_URL = u"https://github.com/slantis/magic-tools/blob/main/TELEMETRY.md"
SWITCH_TEXT = u"Share usage data"

try:
    import telemetry
except Exception:   # a broken telemetry module must not take the tools with it
    telemetry = None

_INFO = {}


def _info():
    """The versions and the channel, read once per engine, on the UI thread
    (a tool script and the gallery's switch both run there)."""
    if not _INFO:
        _INFO.update(telemetry.collect_info())
    return dict(_INFO)


class tool_run(object):
    """Counts one run of the tool whose script is `script_path` (pass
    __file__). See the module docstring."""

    def __init__(self, script_path):
        self.tool = None
        self.failed = False
        self.info = None
        try:
            self.tool = telemetry.tool_name(script_path)
        except Exception:
            pass

    def __enter__(self):
        try:
            if self.tool and telemetry.env_disabled() is None:
                if telemetry.prompt_needed():
                    ask_consent()
                if telemetry.consent() is True:
                    self.info = _info()
        except Exception:
            pass
        return self

    def error(self):
        """Mark this run as failed, for a tool that catches its own errors."""
        self.failed = True

    def __exit__(self, kind, value, trace):
        try:
            if self.info is not None:
                ok = not self.failed and (kind is None or issubclass(kind, SystemExit))
                telemetry.record_tool_run(self.tool, ok, self.info)
        except Exception:
            pass
        return False        # whatever the block raised goes on up, as before


# --- The prompt -------------------------------------------------------------------

_PROMPT_BODY = u"""
  <StackPanel>
    <TextBlock TextWrapping="Wrap" Foreground="#202022" Margin="0,0,0,14"
               Text="Magic Tools can send a little usage data to the people who make it, so they can count installs and see which tools are used. Nothing is sent unless you choose to share."/>
    <TextBlock Text="WHAT IS SENT" Style="{StaticResource SectionHead}" Margin="0,0,0,4"/>
    <TextBlock TextWrapping="Wrap" Foreground="#202022" Margin="0,0,0,14"
               Text="A random install ID made on this computer, the Magic Tools, Revit and pyRevit versions, whether Magic Tools was installed with git or from a ZIP, and for each tool you run: its name, whether it finished or failed, and when."/>
    <TextBlock Text="WHAT IS NEVER SENT" Style="{StaticResource SectionHead}" Margin="0,0,0,4"/>
    <TextBlock TextWrapping="Wrap" Foreground="#202022" Margin="0,0,0,14"
               Text="Your name, your Windows user or your computer's name, the names of your files, models or projects, file paths, or anything inside your models."/>
    <TextBlock TextWrapping="Wrap" Foreground="#77736C" FontSize="12" Margin="0,0,0,12"
               Text="The install ID is random but stays the same, so the data is pseudonymous, not anonymous, and the server sees your IP address when it arrives. Change your mind at any time with &quot;Share usage data&quot; at the bottom of All Magic Tools: turning it off also deletes the install ID and anything not sent yet."/>
    <Grid>
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="Auto"/>
      </Grid.ColumnDefinitions>
      <TextBox x:Name="txtLink" IsReadOnly="True" Height="30" FontSize="12"
               VerticalContentAlignment="Center"/>
      <Button x:Name="btnCopy" Grid.Column="1" Content="Copy link" MinWidth="96"
              Margin="8,0,0,0" Style="{StaticResource BtnGhost}"/>
    </Grid>
  </StackPanel>
"""

_PROMPT_FOOTER = u"""
  <Grid>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnShare" Content="Share usage data" Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
      <Button x:Name="btnNo" Content="Don't share" Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""


def ask_consent():
    """Ask once, store the answer, return it. Closing the window is a no. A
    window that could not be shown stores nothing: the next click asks."""
    answer = [False]
    shown = False
    try:
        from slantisui import ui
        win = ui.parse(u"Share usage data?",
                       u"Asked once. Your answer applies to every Magic Tool.",
                       _PROMPT_BODY, _PROMPT_FOOTER, width=540)
        link = win.FindName("txtLink")
        link.Text = TELEMETRY_URL
        copy = win.FindName("btnCopy")

        def on_copy(sender, args):
            try:
                from System.Windows import Clipboard
                Clipboard.SetText(TELEMETRY_URL)
                copy.Content = u"Copied"
            except Exception:
                link.Focus()
                link.SelectAll()

        def on_share(sender, args):
            answer[0] = True
            win.Close()

        copy.Click += on_copy
        win.FindName("btnShare").Click += on_share
        win.FindName("btnNo").Click += lambda sender, args: win.Close()
        shown = True
        win.ShowDialog()
    except Exception:
        if not shown:
            return False
    info = None
    if answer[0]:
        try:
            info = _info()
        except Exception:
            info = None
    telemetry.set_consent(answer[0], info)
    return answer[0]


# --- The switch in All Magic Tools ------------------------------------------------

def attach_switch(box):
    """Wire the "Share usage data" CheckBox of a gallery window. Checked means
    sharing. Unchecking stops it and deletes the queue and the install ID;
    checking again starts over with a new install ID. Disabled, with the
    reason as its tooltip, while an environment variable turns it off."""
    try:
        box.Content = SWITCH_TEXT
        reason = telemetry.env_disabled()
        if reason:
            box.IsChecked = False
            box.IsEnabled = False
            box.ToolTip = (u"Turned off on this computer by the {0} environment "
                           u"variable.".format(reason))
            return
        box.IsChecked = telemetry.consent() is True
        # Read here, while the window is built inside the click of its door,
        # and not in the WPF handler below, which is no Revit API context.
        _info()
        box.ToolTip = (u"Sends a random install ID, the Magic Tools, Revit and pyRevit "
                       u"versions, and the name and result of each tool you run. "
                       u"Nothing about you, your computer or your models. Turn it "
                       u"off to stop and delete what has not been sent. Details: "
                       u"TELEMETRY.md in the Magic Tools repository on GitHub.")
    except Exception:
        return

    def on_click(sender, args):
        try:
            on = bool(box.IsChecked)
            telemetry.set_consent(on, _info() if on else None)
        except Exception:
            pass

    box.Click += on_click
