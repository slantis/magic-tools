# -*- coding: utf-8 -*-
"""The detail column of a tool gallery: what the selected tool is, and Run.

Third piece of the launcher, 2026-09-01. lib/toolpane.py discovers and draws the
tools, lib/toolwindow.py is the modeless shell, and this is the panel that says
what a tile actually DOES before it is run -- which is the thing a grid of 80px
tiles cannot: a label of two lines and an icon of 22px are an aide-memoire for
whoever already knows the tool, and nothing at all for whoever does not.

WHY IT EXISTS AT ALL, given that the description was already on screen as a
tooltip: a tooltip is a hover, so it cannot be read while the mouse travels
anywhere else, it cannot be compared against the next tool's, and it disappears
the moment you go to click. The same string in a column that stays put is a
different thing to use, and it is what turns the window from a launcher into
something you can browse.

BUILT IN CODE, NOT IN XAML, for the same reason the tiles are: what it shows is
discovered at runtime, and the pieces have to be reachable to be refilled on
every pick. And NOT ONE COLOUR IN HERE either -- every brush comes from
slantisui, so a change of ACCENT retones this panel with the rest.

  > Note for whoever edits this file: the no-hex rule holds here by hand, not
  > by gate, so check it whenever a brush changes.

WHAT IT DOES NOT DO: run anything. The Run button calls back into the gallery,
which owns the ExternalEvent -- a click on a modeless surface is not a valid
Revit API context and this panel is no more valid than a tile is.
"""
import os

from System import TimeSpan, Uri, UriKind
from System.Windows import (Duration, FontWeights, HorizontalAlignment,
                            TextWrapping, Thickness, UIElement, Visibility)
from System.Windows.Controls import (Button, Dock, DockPanel, Image,
                                     Orientation, ScrollBarVisibility,
                                     ScrollViewer, StackPanel, TextBlock)
from System.Windows.Media import (BitmapScalingMode, RenderOptions,
                                  SolidColorBrush, ColorConverter, Stretch)
from System.Windows.Media.Animation import DoubleAnimation
from System.Windows.Media.Imaging import BitmapCacheOption, BitmapImage

from slantisui import ui

import favorites


# The icon is a 96px PNG, so 40 is still a downscale and stays crisp. Big enough
# to be the panel's anchor, small enough that the name sits next to it.
ICON_PX = 40

EMPTY_TEXT = u"Pick a tool to see what it does."

# Where the poses live. One owner for the path, because lib/toolwindow.py draws
# from the same folder for the title card and two answers to "where is the cat"
# is how one of them goes stale.
SALEM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'salem')


def bitmap(path, px=None):
    """A BitmapImage that does NOT keep the PNG file open.

    px is an optional decode width: WPF decodes straight to that many pixels
    (keeping the aspect ratio, because only one axis is set) instead of reading
    the full image and downscaling it afterwards. It matters for the cat
    poses, which are 1024 px PNGs drawn at 100 or 204 tall: the full decode plus a
    high-quality downscale is real time on the UI thread. It is a sharpness
    hint and never a layout one -- WPF stretches whatever it decoded -- so a
    wrong number costs pixels, not geometry. Ask for twice the layout size, to
    leave something for a 150% DPI screen.

    WPF's default cache option is OnDemand, which holds the stream open for as
    long as the image lives: with a gallery on screen every tile icon stays
    locked, and anything that tries to replace one gets a sharing violation.
    Measured on 2026-09-01 -- the sixteen icons of an open pane were the only
    locked files in the whole extension tree, while the fifty-five icons the
    ribbon itself draws were free. OnLoad reads the bytes once and closes the
    file; Freeze is what makes the image safe to hand to another thread.
    """
    image = BitmapImage()
    image.BeginInit()
    image.UriSource = Uri(path, UriKind.Absolute)
    image.CacheOption = BitmapCacheOption.OnLoad
    if px:
        image.DecodePixelWidth = int(px)
    image.EndInit()
    image.Freeze()
    return image


def _brush(hex_colour):
    return SolidColorBrush(ColorConverter.ConvertFromString(hex_colour))


class ToolInfo(object):
    """The detail panel for one gallery. Fill it with show(), empty it with clear()."""

    def __init__(self, host, on_run, on_star=None):
        """host: the DockPanel this fills. on_run / on_star: called with the
        shown Tile. Without on_star the panel shows no star button (a gallery
        whose tiles cannot be starred)."""
        self.host = host
        self.on_run = on_run
        self.on_star = on_star
        self.tile = None
        # The panel's cat, if this tool set asked for one. Drawn by
        # add_empty_cat() and then STAYS: the sentence under him comes and goes
        # with the pick, he does not (requested 2026-09-02: the cat that looks
        # down from the details panel must not disappear when you see a tool's
        # details). He watches the empty column and he watches
        # the tool you picked; the details render under him either way.
        self.cat = None

        # Run at the BOTTOM and docked first: in a DockPanel the last child
        # fills what is left, so the button has to be added before the scroller
        # or it would be the one doing the filling.
        # The two actions, docked at the bottom, added FIRST so the scroller
        # (last child) fills what is left. Run, and under it the star
        # (requested 2026-09-05: a picked tool should offer Run and Add to
        # favorites, or a star in the details section itself, as another way to
        # add to favorites). Both start collapsed until a tile is picked.
        actions = StackPanel()
        actions.Margin = Thickness(0, 12, 0, 0)
        DockPanel.SetDock(actions, Dock.Bottom)
        host.Children.Add(actions)
        self.actions = actions
        self.compact = False    # True while the strip lives in the footer

        self.run = Button()
        self.run.Content = u"Run"
        self.run.HorizontalAlignment = HorizontalAlignment.Stretch
        self.run.Visibility = Visibility.Collapsed
        self.run.Click += self.on_run_click
        actions.Children.Add(self.run)

        self.star = Button()
        self.star.Margin = Thickness(0, 6, 0, 0)
        self.star.HorizontalAlignment = HorizontalAlignment.Stretch
        self.star.Visibility = Visibility.Collapsed
        self.star.Click += self.on_star_click
        actions.Children.Add(self.star)

        scroller = ScrollViewer()
        scroller.VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        scroller.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled
        box = StackPanel()
        scroller.Content = box
        host.Children.Add(scroller)
        self.box = box

        self.empty = TextBlock()
        self.empty.Text = EMPTY_TEXT
        self.empty.TextWrapping = TextWrapping.Wrap
        self.empty.FontSize = 12
        self.empty.Foreground = _brush(ui.TEXT_MUTED)
        box.Children.Add(self.empty)

        # Everything that a pick fills lives under one panel, so showing and
        # hiding the whole detail is one Visibility and never six.
        self.body = StackPanel()
        self.body.Visibility = Visibility.Collapsed
        box.Children.Add(self.body)

        self.icon = Image()
        self.icon.Width = self.icon.Height = ICON_PX
        self.icon.HorizontalAlignment = HorizontalAlignment.Left
        self.icon.Margin = Thickness(0, 2, 0, 10)
        RenderOptions.SetBitmapScalingMode(self.icon, BitmapScalingMode.HighQuality)
        self.body.Children.Add(self.icon)

        self.title = TextBlock()
        self.title.TextWrapping = TextWrapping.Wrap
        self.title.FontSize = 15
        self.title.Foreground = _brush(ui.TEXT)
        self.body.Children.Add(self.title)

        self.group = TextBlock()
        self.group.Margin = Thickness(0, 6, 0, 10)
        try:
            # Same uppercase small caps the gallery's group headers use, found
            # by walking UP the tree so it resolves whether the styles were
            # merged into a Page or written into a Window by slantisui.
            self.group.Style = host.FindResource('SectionHead')
        except Exception:
            self.group.FontSize = 10.5
            self.group.Foreground = _brush(ui.TEXT_MUTED)
        self.body.Children.Add(self.group)

        # The scan's number, as a sentence. Empty until the gallery has been
        # scanned, and empty forever for a tool with no counter -- which is why
        # it is Collapsed and not just blank: a stray gap over the description
        # would read as something that failed to load.
        #
        # The badge on the tile says 34, this says WHAT the 34 are. The tile has
        # no room for that and the number alone is ambiguous on half the tools
        # (34 what, in a tile labelled "Cleaner"?), so the pair is the design:
        # the grid is for comparing at a glance, this is for reading.
        self.count = TextBlock()
        self.count.TextWrapping = TextWrapping.Wrap
        self.count.FontSize = 12
        self.count.FontWeight = FontWeights.Bold
        self.count.Margin = Thickness(0, 0, 0, 10)
        self.count.Visibility = Visibility.Collapsed
        self.body.Children.Add(self.count)

        self.description = TextBlock()
        self.description.TextWrapping = TextWrapping.Wrap
        self.description.FontSize = 12
        self.description.LineHeight = 17
        self.description.Foreground = _brush(ui.TEXT_DIM)
        self.body.Children.Add(self.description)

        try:
            self.run.Style = host.FindResource('BtnPrimary')
            self.star.Style = host.FindResource('BtnGhost')
        except Exception:
            pass

    # -- the empty state --------------------------------------------------
    def add_empty_cat(self, pose, height=100):
        """Put the cat at the top RIGHT of the panel. Returns a fade-in, or None.

        The panel opens with nothing picked, which is a column of white with one
        grey line in it. The cat is what makes that read as a state and not as a
        thing that failed to load, and it is the same device the window's title
        card uses -- one cat presenting the set, one watching the column. He
        keeps watching once a tool is picked: only the sentence gives way to
        the details, so the panel never loses the one thing on it with a face.

        STARTS INVISIBLE, and the caller decides when it arrives. That is not
        polish: the title card is on screen while this is being built, and the
        rule about it is exact: the cat cannot be in two places at once, it
        loses the magic. So this one fades in as the card fades out, and the
        two are never both on screen.

        Opt-in and non-fatal: a set that names no pose, or names one that is not
        on disk, gets the plain sentence and no complaint.

        Sized by HEIGHT, not width, and tucked into the right corner. The first
        cut fixed the width at 152 and let the height follow: fine for a
        lying-down pose (about 108 tall), not for a sitting
        profile (170 tall, near 200 with margins),
        which pushed a long description well down the column. The request,
        2026-09-03: smaller and over on the right, so it does not push the
        text down so much. A cap on the height is what every pose pays
        the same for, whatever its shape; the width follows.
        """
        path = pose if os.path.isabs(pose) else os.path.join(SALEM_DIR, pose)
        if not os.path.isfile(path):
            return None
        salem = Image()
        # bitmap() takes a decode WIDTH; three times the height covers twice
        # the layout size for any pose up to 1.5 wide per tall (the widest in
        # the set is 1.41). Only sharpness rides on it, never geometry.
        salem.Source = bitmap(path, px=height * 3)
        salem.Height = height
        salem.Stretch = Stretch.Uniform
        salem.HorizontalAlignment = HorizontalAlignment.Right
        salem.Margin = Thickness(0, 4, 4, 6)
        # Not hit-testable: it sits over nothing, but a picture that swallows a
        # click is a picture that feels like a broken button.
        salem.IsHitTestVisible = False
        RenderOptions.SetBitmapScalingMode(salem, BitmapScalingMode.HighQuality)
        salem.Opacity = 0
        # Above the sentence and the details, in the corner he looks down from.
        # Insert and not Add: self.body is already in the box and the cat
        # belongs before it. The sentence keeps its left edge: with the cat
        # off to the right there is nothing to centre it under.
        self.box.Children.Insert(0, salem)
        self.cat = salem

        def fade_in(seconds=0.4):
            salem.BeginAnimation(
                UIElement.OpacityProperty,
                DoubleAnimation(0, 1, Duration(TimeSpan.FromSeconds(seconds))))

        return fade_in

    def _blank(self, visible):
        """Show or hide the empty sentence. The cat above it stays either way."""
        self.empty.Visibility = (Visibility.Visible if visible
                                 else Visibility.Collapsed)

    # -- filling ----------------------------------------------------------
    def show(self, tile):
        """Put one tile's detail on screen. Safe to call with the same tile."""
        self.tile = tile
        if tile is None:
            self.clear()
            return

        if tile.icon and os.path.isfile(tile.icon):
            self.icon.Source = bitmap(tile.icon)
            self.icon.Visibility = Visibility.Visible
        else:
            # A tool with no icon.png gets no placeholder: the gap reads as a
            # missing file, which is what it is.
            self.icon.Source = None
            self.icon.Visibility = Visibility.Collapsed

        self.title.Text = tile.label
        if tile.group:
            # .upper() like the gallery's own group headers do it (toolpane
            # :430): the SectionHead style carries the size and the colour, not
            # the casing, so without this the same word reads two ways on one
            # screen.
            self.group.Text = tile.group.upper()
            self.group.Visibility = Visibility.Visible
        else:
            self.group.Visibility = Visibility.Collapsed
        line = getattr(tile, 'count_line', None)
        if line:
            self.count.Text = line
            # Muted at zero, accent otherwise, the same rule the badge follows.
            # Zero is the one answer that means "nothing to do here", and it
            # should not be the loudest thing in the column.
            self.count.Foreground = _brush(
                ui.TEXT_MUTED if getattr(tile, 'count', None) == 0
                else ui.ACCENT)
            self.count.Visibility = Visibility.Visible
        else:
            self.count.Visibility = Visibility.Collapsed
        # The FULL description, unlike the tile, which shows a two-line label.
        # It is the tool's own __doc__, read off the command by the gallery.
        self.description.Text = tile.tooltip or u"This tool has no description."

        self._blank(False)
        self.body.Visibility = Visibility.Visible
        # A tile that is not loaded in this session can be shown and explained,
        # but there is nothing to run.
        self.run.Visibility = (Visibility.Visible if tile.enabled
                               else Visibility.Collapsed)
        self.refresh_star()

    def dock(self, host, compact):
        """Move the Run + star strip into `host`: the panel, or the footer.

        First try of Hide details, 2026-09-05: the Run button disappeared
        with the column, so it has to be relocated along with the hide. So
        when the column folds, Run goes to the footer beside Hide details
        and comes back here with Show details. Second pass, same day: just
        Run, not the name of the tool, and the favorites button leaves with
        the details: in the footer the button says just Run and the star is
        not shown at all (every tile has its own star). The strip is
        reparented, not rebuilt: same button, same handler.
        """
        parent = self.actions.Parent
        if parent is not None:
            parent.Children.Remove(self.actions)
        self.compact = compact
        if compact:
            self.actions.Orientation = Orientation.Horizontal
            self.actions.Margin = Thickness(0, 0, 12, 0)
            self.run.Margin = Thickness(0)
            host.Children.Add(self.actions)
        else:
            self.actions.Orientation = Orientation.Vertical
            self.actions.Margin = Thickness(0, 12, 0, 0)
            self.run.Margin = Thickness(0)
            self.star.Margin = Thickness(0, 6, 0, 0)
            # First child of the DockPanel and docked Bottom: the scroller
            # stays last, which is what lets it fill the rest.
            DockPanel.SetDock(self.actions, Dock.Bottom)
            host.Children.Insert(0, self.actions)
        self.refresh_star()

    def refresh_star(self):
        """The star button says what the store holds for the shown tile.

        Called by show() and by the gallery after any change of the stars
        (a tile's star, Clear stars), so the two stars never disagree. No
        button for a tile of another ribbon (it cannot go on the bar) or one
        that cannot run here. None either while the strip sits in the
        footer: there the tile's own star is the way to favorites.
        """
        tile = self.tile
        can = (tile is not None and self.on_star is not None
               and not self.compact
               and tile.enabled and not getattr(tile, 'foreign', False))
        if not can:
            self.star.Visibility = Visibility.Collapsed
            return
        starred = favorites.is_fav(tile.key)
        self.star.Content = (u"\u2605  In favorites" if starred
                             else u"\u2606  Add to favorites")
        self.star.ToolTip = (u"Take it off the ribbon" if starred
                             else u"Keep it on the ribbon")
        self.star.Visibility = Visibility.Visible

    def clear(self):
        self.tile = None
        self.body.Visibility = Visibility.Collapsed
        self.run.Visibility = Visibility.Collapsed
        self.star.Visibility = Visibility.Collapsed
        self._blank(True)

    # -- running ----------------------------------------------------------
    def on_run_click(self, sender, args):
        if self.tile is not None and self.on_run is not None:
            self.on_run(self.tile)

    def on_star_click(self, sender, args):
        if self.tile is not None and self.on_star is not None:
            self.on_star(self.tile)
        self.refresh_star()

