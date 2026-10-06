# -*- coding: utf-8 -*-
"""The modeless window host for a tool gallery. Second surface, 2026-08-24.

Same tools, same tiles, same filter and same launcher as the dockable pane:
everything below the shell is toolpane.Gallery and this file is only the shell.
What changes is WHERE the tools live -- a normal window, which can be moved,
resized and parked on the second monitor, against a strip docked to the edge of
Revit. Both hosts read the same tool-set classes of toolpane.py, so a change to
what a set holds shows up in both without an edit here.

MODELESS, and the two things that make that work are not optional:

  1. Show(), NOT ShowDialog(). A modal dialog owns the UI thread, so Revit never
     reaches idle, so the ExternalEvent that runs a tool never fires. That is not
     a theory: the first window version of this launcher had to CLOSE ITSELF and
     run the tool after ShowDialog() returned, which is why that version was
     retired. Modeless keeps the window up and Revit usable underneath it,
     which is what the design calls for.

  2. __persistentengine__ = True, in the DOOR SCRIPT and not in this module.
     pyRevit reads that flag off the script it runs; without it the engine is
     torn down when the script returns and every global goes with it, handlers
     included -- the window stays on screen and no click does anything. The
     other modeless tools of this extension, Print Set Manager among them, are
     the production proof that the pair works: same flag, same Show().

ONE WINDOW PER TOOL SET, held in _OPEN. A second click on the door focuses the
window that is already up instead of stacking a second copy of it, and the entry
clears itself on Closed so the click after that opens a fresh one.

NOT THE ONLY POSSIBLE SURFACE. A floating search line over the same tiles, for
the user who already knows what every tool does, is a separate second level;
nothing here opens it or switches to it. It started life as a compact MODE of
this window (2026-09-04), then a footer button with a remembered flag
(2026-09-05), and the same day the two were split: some people never reach the
second level, so the levels are kept apart. The window is the first level; it
knows nothing of the second.
"""
import os
import traceback

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('WindowsBase')

from System import Action, TimeSpan
from System.Windows import (Duration, FontWeights, GridLength,
                            HorizontalAlignment, SystemParameters, Thickness,
                            UIElement, VerticalAlignment, Visibility,
                            WindowStartupLocation)
from System.Windows.Controls import Grid, Image, Orientation, StackPanel, TextBlock
from System.Windows.Input import Key
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Media import SolidColorBrush, ColorConverter, Stretch
from System.Windows.Media.Animation import DoubleAnimation
from System.Windows.Threading import (Dispatcher, DispatcherFrame,
                                      DispatcherPriority, DispatcherTimer)

from pyrevit import HOST_APP

import favorites
import toolinfo
import toolpane
from slantisui import ui


# 660 was the gallery alone: wide enough that the groups of the largest set open
# in gutter mode (the headers in a column on the left), tall enough for most of
# its 16 tiles. Not all of them: at six groups the last row sat below the fold by
# about 96px, and the regroup of 2026-09-01 (five rows) took that to 9.4px,
# measured. The height went 660 -> 676 in the same pass to spend the last of it,
# so the gallery finally opens with nothing to scroll to. Since 2026-09-01 the window also
# carries the detail column, so it is that same 660 plus the splitter and the
# panel. Resizable from there, and the split is draggable -- Gallery.relayout
# switches the header mode by itself as the gallery side gets narrow, whether
# that comes from the window or from the splitter.
CARD_GAP_MS = 200       # the card's fade, and then the tools play in

DETAIL_W = 268
SPLIT_W = 21            # 10px of air, a 1px rule, 10px of air
PAGE_PAD = 80           # what the slantisui page + card put around the body
GALLERY_W = 660         # the gallery column a set opens with, unless it says otherwise
WIN_H = 676             # see the height note above


def _gallery_w(spec):
    """The width the set asked for its gallery column, or the measured 660.

    660 is sized for the sixteen tiles of the largest set. A set of three big
    tiles in a column says `gallery_w` on its class and opens that much
    narrower; the detail column and the splitter do not move.
    """
    return getattr(spec, 'gallery_w', None) or GALLERY_W


# NOT ONE COLOUR IN HERE, exactly as in ToolPane.xaml and for the same reason:
# slantisui owns the palette, Gallery.paint() assigns the two brushes this shell
# needs, and a hex written here would be the one thing a change of ACCENT could
# not retone.
BODY = """
  <Grid>
    <Grid.ColumnDefinitions>
      <ColumnDefinition x:Name="colGallery" Width="*" MinWidth="300"/>
      <ColumnDefinition Width="Auto"/>
      <ColumnDefinition x:Name="colDetail" MinWidth="200"/>
    </Grid.ColumnDefinitions>

    <Grid x:Name="galleryCell" Grid.Column="0">
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="*"/>
      </Grid.RowDefinitions>

      <!-- Search, and the scan next to it. The hint is a TextBlock laid over
           the box (WPF has no placeholder) and it is not hit-testable, so a
           click still lands on the TextBox underneath, which is why the two
           share column 0 and the button gets its own.
           The Padding is overridden and that is not decoration: BtnGhost pads
           18,8 for a footer button that sizes itself (34.6 px), and this one is
           pinned to 28 to match the search box. Measured 2026-09-04 in WPF: at
           padding 8 the content area is 10 px for a line that needs 16.63, so
           the word Scan comes out cut along the bottom. At 18,0 the inner box
           is 26.6 and the text fits whole. The row cannot simply grow to 34.6
           either: it would take 6.6 px off the scroller and the gallery, which
           fits today at 430 against 436.6, would start scrolling.
           Scan is opt in, by design: a button that says Scan, so the user
           chooses whether to see the numbers or not. It reads the whole
           model, so it is never something the window does to the user on
           open. -->
      <Grid Grid.Row="0" Margin="0,0,0,10">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <TextBox x:Name="txtSearch" Grid.Column="0" Height="28" Padding="8,0"
                 VerticalContentAlignment="Center"/>
        <TextBlock x:Name="lblHint" Grid.Column="0" Text="Search tools"
                   FontSize="12" Margin="11,0,0,0" VerticalAlignment="Center"
                   IsHitTestVisible="False"/>
        <Button x:Name="btnScan" Grid.Column="1" Content="Scan" Height="28"
                MinWidth="88" Margin="8,0,0,0" Padding="18,0"
                Style="{StaticResource BtnGhost}"/>
      </Grid>

      <!-- The heading over the picking area (2026-09-07: CUSTOMIZE YOUR
           RIBBON, in orange letters). The same cut as the group headers over
           the tiles (SectionHead, 10.5 Medium): it first went out at 18
           SemiBold and was sent back on 2026-09-08 as too heavy, to look like
           the titles below it. The colour is painted by ToolWindow, like every
           other colour of this shell.
           Only the set with chips shows it: a door over one group is not
           where the ribbon gets customised. -->
      <TextBlock x:Name="lblCustomize" Grid.Row="1" Text="CUSTOMIZE YOUR RIBBON"
                 Style="{StaticResource SectionHead}" Margin="0,2,0,8"
                 Visibility="Collapsed"/>

      <!-- The presets: one chip per group, filled by Gallery._build_chips
           and collapsed by it for a set that has none. -->
      <WrapPanel x:Name="presets" Grid.Row="2" Margin="0,0,0,4"
                 Visibility="Collapsed"/>

      <ScrollViewer x:Name="scroller" Grid.Row="3"
                    VerticalScrollBarVisibility="Auto"
                    HorizontalScrollBarVisibility="Disabled">
        <StackPanel x:Name="host"/>
      </ScrollViewer>
    </Grid>

    <!-- The divider is two elements in the same cell and not one, because a
         GridSplitter resizes the columns of ITS OWN parent Grid: wrapped in a
         panel to pair it with the rule, it would resize that panel's single
         column, which is to say nothing. So both sit directly in column 1, the
         rule painted 1px and the splitter wide and transparent over it, because
         a 1px grab handle is not a grab handle.
         (And no double dash in here: XML forbids it inside a comment, and
         XamlReader.Parse dies on the whole window. Caught 2026-09-01.) -->
    <Border x:Name="rule" Grid.Column="1" Width="1"
            HorizontalAlignment="Center"/>
    <GridSplitter x:Name="split" Grid.Column="1" Background="Transparent"
                  ResizeDirection="Columns" ResizeBehavior="PreviousAndNext"
                  HorizontalAlignment="Center" VerticalAlignment="Stretch"/>

    <DockPanel x:Name="detail" Grid.Column="2" LastChildFill="True"/>
  </Grid>
"""

FOOTER = """
  <Grid>
    <TextBlock x:Name="lblCount" VerticalAlignment="Center" FontSize="11"/>
    <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
      <StackPanel x:Name="footerActions" Orientation="Horizontal"/>
      <Button x:Name="btnDetails" Content="Hide details" MinWidth="110"
              Margin="0,0,8,0" Style="{StaticResource BtnGhost}"/>
      <Button x:Name="btnClose" Content="Close" MinWidth="88"
              Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""

from System.Windows import WindowState


def _brush(hex_colour):
    return SolidColorBrush(ColorConverter.ConvertFromString(hex_colour))


def pump_to_paint(window):
    """Drain the dispatcher down to Loaded priority, then return.

    This is what ShowDialog does for free and Show() does not: a modeless Show()
    queues the layout and the render pass and comes straight back, so the caller
    keeps the UI thread and NOTHING IS PAINTED until the whole door script
    returns. Measured inside Revit on 2026-09-02: the first painted frame landed
    1600ms after the click, and the window's own work accounted for 4 to 11ms of
    that. Pumping here puts the paint at about 230ms.

    Why it is safe. The stop is posted at Loaded (6), which is BELOW Render (7)
    and DataBind (8) so layout and the render pass run first, and ABOVE Input
    (5) so no user click is processed inside the nested frame. That ordering is
    the whole argument: a nested pump that never touches input cannot let a
    second ribbon command re-enter while this one is still on the stack.

    Called from open_window() and not from the window, because it belongs to the
    act of showing: whoever calls Show() is who owes the paint.
    """
    frame = DispatcherFrame()

    def _stop():
        frame.Continue = False

    window.Dispatcher.BeginInvoke(DispatcherPriority.Loaded, Action(_stop))
    Dispatcher.PushFrame(frame)


def _title_card(win, spec):
    """The set presenting itself, over the gallery. Returns a fade-out, or None.

    WHAT IT IS FOR, and it is not decoration. Even with the pump above, the
    first frame arrives about 230ms in and then the UI thread stalls for about
    1.4 SECONDS inside pyRevit's own per-command work -- every extension, every
    tool, independent of what the script does, and still unexplained at the time
    of writing. Nothing can animate during that stall, so the honest options
    were a window that looks frozen or a window whose first frame looks
    finished. The requirement was the second one: the wait should not be
    noticed, or should look intentional. A static card reads as a title, not
    as a wait.

    It draws over the gallery cell, so the search box stays live above it, and it
    is not hit-testable, so a click during the card lands on whatever is under
    it. Opt-in: without card_pose and card_line on the tool set there is no
    card and on_rendered goes straight to the entrance. Non-fatal too -- a pose
    that is not on disk returns None rather than blocking the window.
    """
    pose = getattr(spec, 'card_pose', None)
    line = getattr(spec, 'card_line', None)
    if not pose or not line:
        return None
    path = pose if os.path.isabs(pose) else os.path.join(toolinfo.SALEM_DIR,
                                                         pose)
    if not os.path.isfile(path):
        return None

    # Side by side needs about 580px (the cat, then two lines of 32/18pt); a
    # narrow gallery gets the cat ABOVE the words, both a size down.
    narrow = _gallery_w(spec) < 560
    card = StackPanel()
    card.Orientation = Orientation.Vertical if narrow else Orientation.Horizontal
    card.HorizontalAlignment = HorizontalAlignment.Center
    card.VerticalAlignment = VerticalAlignment.Center
    card.IsHitTestVisible = False

    salem = Image()
    salem.Height = 150 if narrow else 204
    salem.Source = toolinfo.bitmap(path, px=salem.Height * 2)
    salem.Stretch = Stretch.Uniform
    card.Children.Add(salem)

    words = StackPanel()
    # Nudged up against the cat's own baseline: he is drawn sitting, so the
    # optical centre of the picture is above its geometric one.
    words.Margin = Thickness(0, 8, 0, 0) if narrow else Thickness(9, 0, 0, 24)
    words.VerticalAlignment = VerticalAlignment.Center

    name = TextBlock()
    name.Text = spec.panel_title
    name.FontSize = 26 if narrow else 32
    name.FontWeight = FontWeights.SemiBold
    name.Foreground = _brush(ui.TEXT)
    words.Children.Add(name)

    sub = TextBlock()
    sub.Text = line
    sub.FontSize = 15 if narrow else 18
    sub.Foreground = _brush(ui.TEXT_DIM)
    sub.Margin = Thickness(0, 5, 0, 0)
    words.Children.Add(sub)
    if narrow:
        for block in (name, sub):
            block.HorizontalAlignment = HorizontalAlignment.Center

    card.Children.Add(words)

    # Into the gallery cell, in the scroller's own row and column, which is how
    # it lands OVER the tiles instead of next to them: two children of the same
    # Grid cell stack in z-order, last on top.
    scroller = win.FindName('scroller')
    cell = win.FindName('galleryCell')
    Grid.SetRow(card, Grid.GetRow(scroller))
    Grid.SetColumn(card, Grid.GetColumn(scroller))
    cell.Children.Add(card)

    def fade_out(seconds=0.22):
        card.BeginAnimation(
            UIElement.OpacityProperty,
            DoubleAnimation(1, 0, Duration(TimeSpan.FromSeconds(seconds))))

    return fade_out


def _model_name():
    """The open model, for the window's context line. Empty if there is none."""
    try:
        return HOST_APP.doc.Title
    except Exception:
        return u""


def _own_by_revit(win):
    """Make Revit the owner of this window, through the Win32 handle.

    A modeless WPF window with no owner is a peer of Revit's main window, so the
    first click back in the model sends it BEHIND Revit and the launcher is lost.
    Owned, it floats above Revit, minimises with it and comes back with it --
    which is the whole point of a window you keep open while you work.

    Revit's main window is an HWND and not a WPF Window, so Owner cannot be
    assigned directly; WindowInteropHelper is the bridge. Guarded and non-fatal:
    if it fails the window still works, it just sinks behind Revit.
    """
    try:
        WindowInteropHelper(win).Owner = HOST_APP.uiapp.MainWindowHandle
    except Exception:
        traceback.print_exc()


class ToolWindow(object):
    """One slantisui window around one Gallery."""

    def __init__(self, spec):
        self.spec = spec
        self.win = ui.parse(
            title=spec.panel_title,
            subtitle=u"Click a tool to read what it does, Run to run it. Star a "
                     u"tool to keep it on the ribbon. Revit stays usable while "
                     u"this window is open.",
            body=BODY, footer=FOOTER,
            width=_gallery_w(spec) + SPLIT_W + DETAIL_W, height=WIN_H,
            context=_model_name())
        find = self.win.FindName
        # The split geometry lives in the constants above and is applied here, so
        # the window width and the column agree on one number instead of two.
        find('colDetail').Width = GridLength(DETAIL_W)
        find('split').Width = SPLIT_W
        find('rule').Background = SolidColorBrush(
            ColorConverter.ConvertFromString(ui.CARD_BD))
        # A floor under the drag. The two columns declare MinWidth (300 + 200)
        # and the splitter takes its own, so below that sum WPF stops shrinking
        # the Grid and starts clipping it instead -- the detail panel walks off
        # the right edge with no scrollbar to get it back. PAGE_PAD is what the
        # slantisui card puts around the body.
        self.win.MinWidth = 300 + SPLIT_W + 200 + PAGE_PAD
        self.win.MinHeight = 420

        self.info = toolinfo.ToolInfo(find('detail'), self.on_run, self.on_star)
        # The detail column shows by default and Hide details folds it away
        # (requested 2026-09-05: a Hide button for the side column with the
        # details, shown by default). Per window, not remembered: the default
        # is the request.
        self.details_on = True
        self._detail_w = DETAIL_W
        find('btnDetails').Click += self.on_details_click
        # on_pick is what turns a click from RUN into SELECT: the Gallery runs on
        # a click only when nobody asked to be told about the pick, which is
        # exactly the dockable pane, where there is no room for a description.
        # Double-click and Enter stay as the shortcut for whoever knows the tool.
        # fade=True: this host OPENS, so there is a moment for an entrance. The
        # pane passes False because it is toggled and draws once for the session.
        # The Scan button only for the set whose tiles toolscan can count
        # (`scan = True` on the set): it leaked into the other sets on
        # 2026-09-04 and was caught the next morning.
        scan = getattr(spec, 'scan', False)
        if not scan:
            find('btnScan').Visibility = Visibility.Collapsed
        self.gallery = toolpane.Gallery(spec, find('host'), find('txtSearch'),
                                        find('lblHint'), find('lblCount'),
                                        fade=True, on_pick=self.info.show,
                                        scan_button=(find('btnScan') if scan
                                                     else None),
                                        presets=find('presets'))
        self.gallery.paint()
        if getattr(spec, 'presets', False):
            heading = find('lblCustomize')
            heading.Foreground = _brush(ui.ACCENT)
            heading.Visibility = Visibility.Visible
        self.gallery.on_fav_change = self.info.refresh_star
        find('btnClose').Click += self.on_close_click
        # The window comes back where it was closed. Only when that place is
        # still on a screen: a monitor unplugged since would otherwise park it
        # out of reach, with no way to grab it back.
        place = favorites.window_place(spec.panel_title)
        if place is not None and _on_screen(place):
            self.win.WindowStartupLocation = WindowStartupLocation.Manual
            self.win.Left, self.win.Top = place
        self.win.Closed += self.on_closed
        # PreviewKeyDown on the WINDOW, so it fires while the caret is in the
        # search box too: the tunnel starts at the root. Typing to filter and
        # hitting Enter to run the tool you were looking for is the whole point.
        self.win.PreviewKeyDown += self.on_key
        # Loaded is the NET now, not the builder: __init__ builds before Show()
        # (see the block at the end of this method) and build() is idempotent.
        self.win.Loaded += self.on_loaded
        # The entrance, though, waits for ContentRendered -- the first frame
        # actually painted. Started on Loaded (which is what the first version
        # did) it runs against an empty screen: measured inside Revit on
        # 2026-08-24, the first paint arrived with the opacities already at
        # 0.945-1.0, so the fade was over before anything was visible. build()
        # only arms the rows; this is what plays them.
        #
        # And it STAYS on ContentRendered, on purpose, after two measured
        # attempts to start it earlier (probe in the door, 2026-09-02). WPF
        # posts ContentRendered at Input priority, below what pump_to_paint()
        # drains, so it fires right after pyRevit's post-command stall: the
        # card holds the screen through the stall (1.4s, frozen but whole) and
        # the fade plays on a thread that is free. Pumping the choreography
        # through nested frames instead, so it played before the script
        # returned, ran the animations inside the pump but stretched the stall
        # from 1.4s to 3.0s and broke the look (it was reported as a broken
        # animation). The stall is pyRevit's and does not shrink by spending it
        # here.
        self.win.ContentRendered += self.on_rendered
        # The StackPanel's own SizeChanged and not the window's: it fires with
        # host.ActualWidth already updated, which is the number Gallery measures.
        find('host').SizeChanged += self.on_host_resize
        self._built = False
        # BUILT HERE, BEFORE Show(), and it is a user-visible decision rather
        # than a tidiness one. Until 2026-09-01 this ran on Loaded, which fires
        # AFTER the window is on screen: the shell appeared empty, the UI thread
        # then blocked long enough to raise the busy cursor, and the tools
        # arrived about a second later. Watching it open: an empty window, a
        # wait of a second, a rough user experience. Building first does not
        # make the work cheaper, it spends it before there is anything to look
        # at, which is how every other pyRevit tool behaves -- and the entrance
        # then plays on the first painted frame instead of a beat after it,
        # which is the whole ask.
        #
        # The width that Loaded was there to provide is not lost. With
        # ActualWidth still 0 the gallery keeps its default gutter mode
        # (_wants_side in lib/toolpane.py returns self.side at width 0), which
        # is the mode this window opens in anyway; and the host's SizeChanged
        # fires during the layout pass Show() triggers, BEFORE the first paint,
        # so a relayout() lands with nothing on screen to flicker.
        self.build()
        # AFTER build(), and that order is the point: build() arms the rows at
        # opacity 0 (toolpane._arm_entrance), so the frame the card covers is an
        # empty shell either way. The card is opaque over that empty gallery and
        # _arrive() is what hands the screen over.
        self._fade_card = _title_card(self.win, spec)
        # The panel's cat is NOT built here on purpose -- see _arrive().
        self._pose = getattr(spec, 'empty_pose', None)

    def build(self):
        """Fill the gallery. Idempotent: whoever gets here second does nothing."""
        if self._built:
            return
        self._built = True
        try:
            self.gallery.build()
        except Exception:
            traceback.print_exc()
            self.win.FindName('lblCount').Text = "failed to load the tool list"

    def on_loaded(self, sender, args):
        # The net, for the case __init__'s build threw before setting up.
        self.build()

    def on_rendered(self, sender, args):
        """First painted frame: hand the screen from the card to the tools."""
        try:
            if self._fade_card is None:
                self._arrive()
                return
            self._fade_card()
            # A timer and not a chained animation, because what follows is not
            # an animation: the entrance walks the rows one group at a time and
            # has to start after the card is out of the way, or the two play
            # over each other and the cat is on screen twice (he cannot be in
            # two places at once: it loses the magic).
            timer = DispatcherTimer()
            timer.Interval = TimeSpan.FromMilliseconds(CARD_GAP_MS)

            def go(sender_, args_):
                timer.Stop()
                self._arrive()

            timer.Tick += go
            timer.Start()
        except Exception:
            traceback.print_exc()

    def _arrive(self):
        """The tools play in, and the panel's cat arrives with them.

        The cat is BUILT here and not in __init__, and that is timing rather
        than tidiness: a pose is a PNG that has to be decoded on the UI thread,
        and from __init__ that decode lands before Show(), inside the one
        stretch of the whole open where there is no window on screen yet. The
        prototype in the door did it from here by accident -- it called the
        helper after open_window() had returned -- and the difference was felt
        the first time the promoted version ran: it was faster before that
        last change. Nothing about the look changes; the cat fades in from
        opacity 0 either way.
        """
        try:
            self.gallery.entrance()
        except Exception:
            traceback.print_exc()
        if self._pose:
            try:
                fade_in = self.info.add_empty_cat(self._pose)
                if fade_in is not None:
                    fade_in()
            except Exception:
                traceback.print_exc()

    def on_host_resize(self, sender, args):
        try:
            self.gallery.relayout()
        except Exception:
            traceback.print_exc()

    def on_run(self, tile):
        """The Run button of the detail panel. The gallery owns the launcher."""
        try:
            self.gallery.launch(tile)
        except Exception:
            traceback.print_exc()

    def on_star(self, tile):
        """The star button of the detail panel. The gallery owns the stars."""
        try:
            self.gallery.star(tile)
        except Exception:
            traceback.print_exc()

    def on_details_click(self, sender, args):
        try:
            self.show_details(not self.details_on)
        except Exception:
            traceback.print_exc()

    def show_details(self, on):
        """Fold the detail column away, or bring it back at the width it had.

        The column, the splitter and the rule go together, and the window
        gives back (or takes) exactly that width so the gallery does not
        stretch to fill the hole: hiding the details is asking for a smaller
        window, not a wider gallery. Maximised, the width is left alone and
        the gallery does take the room.
        """
        if on == self.details_on:
            return
        find = self.win.FindName
        col = find('colDetail')
        parts = (find('detail'), find('split'), find('rule'))
        normal = self.win.WindowState == WindowState.Normal
        if on:
            col.MinWidth = 200
            col.Width = GridLength(self._detail_w)
            for part in parts:
                part.Visibility = Visibility.Visible
            self.win.MinWidth = 300 + SPLIT_W + 200 + PAGE_PAD
            if normal:
                self.win.Width = self.win.ActualWidth + self._detail_w + SPLIT_W
            find('btnDetails').Content = u"Hide details"
            self.info.dock(find('detail'), compact=False)
        else:
            if col.ActualWidth > 0:
                self._detail_w = col.ActualWidth
            for part in parts:
                part.Visibility = Visibility.Collapsed
            col.MinWidth = 0
            col.Width = GridLength(0)
            self.win.MinWidth = 300 + PAGE_PAD
            if normal:
                self.win.Width = max(self.win.MinWidth,
                                     self.win.ActualWidth - self._detail_w
                                     - SPLIT_W)
            find('btnDetails').Content = u"Show details"
            # Run and the star follow the user into the footer: a folded
            # panel must not take the one button that runs the tool with it.
            self.info.dock(find('footerActions'), compact=True)
        self.details_on = on

    def on_key(self, sender, args):
        """Enter runs the selection; Escape clears the filter, then closes.

        Escape in two steps and not one, because a filter box trains the other
        habit: hitting Escape to drop what you typed should not take the window
        with it. With the box already empty there is nothing to undo and Escape
        means what it means everywhere else.
        """
        try:
            if args.Key == Key.Escape:
                if self.gallery.search.Text:
                    self.gallery.search.Text = ""
                else:
                    self.win.Close()
                args.Handled = True
            elif args.Key == Key.Enter:
                # Gallery.selected is (button, tile): the button is how the tint
                # gets cleared when the pick moves, the tile is what runs.
                picked = self.gallery.selected
                if picked is not None and picked[1].enabled:
                    self.gallery.launch(picked[1])
                    args.Handled = True
        except Exception:
            traceback.print_exc()

    def on_close_click(self, sender, args):
        self.win.Close()

    def on_closed(self, sender, args):
        try:
            favorites.set_window_place(self.spec.panel_title,
                                       self.win.Left, self.win.Top)
        except Exception:
            traceback.print_exc()

    def focus_search(self):
        """Caret in the box, text selected: the next thing typed replaces it."""
        try:
            box = self.gallery.search
            box.Focus()
            box.SelectAll()
        except Exception:
            traceback.print_exc()


def _on_screen(place):
    """Is (left, top) inside the virtual desktop, with room for a title bar?"""
    try:
        left, top = place
        return (SystemParameters.VirtualScreenLeft <= left
                <= SystemParameters.VirtualScreenLeft
                + SystemParameters.VirtualScreenWidth - 120
                and SystemParameters.VirtualScreenTop <= top
                <= SystemParameters.VirtualScreenTop
                + SystemParameters.VirtualScreenHeight - 80)
    except Exception:
        return False


_OPEN = {}


def _forget(key):
    def handler(sender, args):
        _OPEN.pop(key, None)
    return handler


def open_window(spec):
    """Show the gallery of one tool set in a modeless window.

    arm() belongs here rather than in the window: a pushbutton IS a valid Revit
    API context, so this is the one place in the window's life where the
    ExternalEvent can simply be created. The pane cannot do that -- it is
    registered from a startup script that may hold no UIApplication yet, so it
    borrows the first Idling fire instead. arm() is idempotent and there is one
    event for both surfaces, so calling it from both costs nothing.

    A failure to arm is not fatal and does not stop the window: the tiles still
    draw, and the click reports why it cannot run instead of doing nothing.
    """
    key = spec.panel_title
    live = _OPEN.get(key)
    if live is not None:
        try:
            live.win.Activate()
            # The door (or a Revit keyboard shortcut bound to it) is how the
            # window is called back: land in the search box, ready to type.
            live.focus_search()
            return live
        except Exception:
            # Closed behind our back, or its engine went away with a Reload.
            _OPEN.pop(key, None)

    try:
        toolpane.arm()
    except Exception:
        traceback.print_exc()

    window = ToolWindow(spec)
    _OPEN[key] = window
    window.win.Closed += _forget(key)
    # Before Show(): the handle has to be set while the window is still unshown,
    # or WindowInteropHelper hands back a handle that is already live and the
    # assignment throws.
    _own_by_revit(window.win)
    window.win.Show()
    # Show() comes back before anything is painted. Without this the window is
    # invisible until the whole door script returns, which measured 1600ms.
    pump_to_paint(window.win)
    window.focus_search()
    return window
