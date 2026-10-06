# -*- coding: utf-8 -*-
"""slantisui.ui - /slantis light-brand WPF theme.

A FAITHFUL rendering of the Claude Design handoff, folded into a single library.
Every tool builds its window with ui.parse()/ui.show() and INHERITS the chrome
plus the control styles; it never repeats a style or an inline hex. A fix is made
here once and every tool picks it up ("no re-polishing").

Usage:
    from slantisui import ui

    win = ui.parse("My Tool", "subtitle", BODY_XAML, footer=FOOTER_XAML)
    grid = win.FindName("grid")
    grid.ItemsSource = items
    win.ShowDialog()

Styles available to every tool's body/footer (via StaticResource, or implicitly
by TargetType -- already injected into Window.Resources):
    - Button:   Style="{StaticResource BtnPrimary}"  (orange pill)
                Style="{StaticResource BtnGhost}"    (white pill, light border)
    - RadioButton: default = BrandRadio (15px circle, orange when checked,
      dimmed when disabled). A tool's own style still wins.
    - CheckBox: default = BrandCheck (15px, radius 4, orange when checked,
                dash on accent when indeterminate/tri-state, optional label);
                also Style="{StaticResource BrandCheck}".
    - TextBox / ComboBox / DataGrid* : by TargetType, declare nothing.

ALWAYS pass multiselect= to pick_list(). It defaults to True and then hands back
a LIST: a pick-one caller that omits it compares a string to a list forever after
and dies quietly.

Layout (the handoff's shared window anatomy):
    +-----------------------------------------+
    | = 3px orange accent bar                 |
    |  Title                                  |
    |  Subtitle                               |
    |  +- work-surface card (#FAF8F5, r10) -+ |   <- BODY_XAML
    |  |  (search, grid, etc.)             | |
    |  +-----------------------------------+ |
    |  ------ footer divider --------------- |
    |  count                  [Cancel] [OK]   |   <- FOOTER_XAML
    +-----------------------------------------+
"""

import os
import re

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')
from System.Windows.Markup import XamlReader
from System.Windows import Visibility, RoutedEventHandler
from System.Windows.Controls import CheckBox as _CheckBox
# System.Uri is NOT in the default reference set of the netcore engine that pyRevit
# uses on Revit 2025+ (that engine folder ships no System.dll facade, so the root
# System namespace only exposes what the core library has: Guid resolves, Uri does
# not). A bare import here raised ImportError and killed EVERY tool that imports
# slantisui -- for a font, which is cosmetic. It degrades instead, and the string
# overload of FontFamily below keeps the branding even with no Uri at all.
try:
    from System import Uri, UriKind
except ImportError:
    Uri, UriKind = None, None
    try:
        clr.AddReference('System.Private.Uri')
        from System import Uri, UriKind
    except Exception:
        pass
from System.Windows.Media import FontFamily

# -- Embedded DM Sans (loaded from slantisui/fonts in CODE, not by a XAML URI and
#    not from a font installed on the machine). The handoff's method: family from
#    base dir + '#DM Sans'. If the .ttf files are missing, WPF falls back to the
#    default without breaking. ------------------------------------------------
try:
    import os as _os
    _FONT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'fonts')
except Exception:
    _FONT_DIR = None


def _apply_dm_sans(window):
    if not _FONT_DIR:
        return
    loc = u'file:///' + _FONT_DIR.replace('\\', '/') + u'/'
    try:
        if Uri is not None:
            window.FontFamily = FontFamily(Uri(loc, UriKind.Absolute), u'./#DM Sans')
        else:
            # WPF also takes the whole thing as one string: "<location>#<family>".
            window.FontFamily = FontFamily(loc + u'#DM Sans')
    except Exception:
        pass

# -- Brand & palette ---------------------------------------------------------
# THE ACCENT IS ONE SINGLE CONSTANT AND THE WHOLE ORANGE FAMILY DERIVES FROM IT
# (hover, dark, pale, disabled, selection, row ARGB). You cannot move one orange
# without moving the rest: changing ACCENT retones the entire system coherently,
# and it becomes impossible for one surface to drift out of alignment.
#
# Why ACCENT is not the brand hex: browsers colour-manage (display profile) and
# WPF does not, so the mockup's #FF7700 reads calm in the browser and strident in
# Revit -- above all on large surfaces like the title bar. ACCENT is the brand
# orange *rendered for WPF*.
#   softer -> "#EC8A4A"   ·   stronger -> "#F57F28"   ·   raw -> BRAND_ACCENT
BRAND_ACCENT = "#FF7700"   # /slantis brand hex (handoff reference)
ACCENT       = "#E8833C"   # the orange that actually gets painted


def _rgb(h):
    h = h.lstrip('#')
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hx(r, g, b):
    return "#%02X%02X%02X" % (max(0, min(255, int(round(r)))),
                              max(0, min(255, int(round(g)))),
                              max(0, min(255, int(round(b)))))


def _darken(h, f):
    """Darken while keeping the hue (scales the 3 channels)."""
    r, g, b = _rgb(h)
    return _hx(r * f, g * f, b * f)


def _mix(h, base, t):
    """Mix h toward base (t = share of base). The anchor is the whole point:
    the same factor against white gives a PALE tint, and against a dark card
    gives a dark wash. See the pale accents below."""
    r, g, b = _rgb(h)
    rb, gb, bb = _rgb(base)
    return _hx(r + (rb - r) * t, g + (gb - g) * t, b + (bb - b) * t)


def _lighten(h, t):
    """Lighten by mixing with white (t = share of white)."""
    return _mix(h, "#FFFFFF", t)


# -- Theme (light | dark) ----------------------------------------------------
# THE DARK THEME IS ONE TRANSLATION TABLE, NOT A SECOND XAML. Everything below is
# written with the LIGHT neutrals (readable, greppable) and _theme() maps them to
# the dark family in ONE pass over the finished window, exactly the way _retone()
# maps the canonical orange to the real accent family.
#
# The dark neutrals are the "slate" set: the card IS Revit's dark ribbon
# (#3B4453, measured with an eyedropper), so a dialog sits at 1.00:1 from the
# host instead of near it. Chosen 2026-09-07 AFTER seeing the graphite set
# rendered in Revit, which REVERSED the desk decision taken the same
# afternoon: a grey that is almost the ribbon's but not quite looks off and
# jars the eye. Graphite held more text contrast (10.4:1
# on its card vs 7.8:1 here, 15.3:1 in light) and its near-miss is exactly
# what made it worse to look at: a deliberate match reads as intent, a 1.46:1
# miss reads as a mistake. Contrast still clears AA on the card everywhere it
# has to (text 7.81:1, dim 4.56:1, status 4.78-5.87:1, accent 3.62:1).
# The cost taken on purpose: the set is tied to Autodesk's hex, so if they
# move their slate this table moves with it. That is one constant, by design.
#
# The 3 status colours are REDRAWN, not lifted: on a dark card #C0392B falls to
# 1.81:1 (unreadable). Warn moves toward yellow on purpose -- just lightening it
# lands on #DCA666, which sits 1.25:1 from ACCENT, and a warning that reads as
# brand orange stops being a warning (see the note on STATUS_WARN below).
_DARK = {
    "#FFFFFF": "#333B49",   # WIN_BG      window background
    "#FAF8F5": "#3B4453",   # CARD_BG     the ribbon's own slate: 1.00:1 from it
    "#ECE9E4": "#4A5466",   # CARD_BD     card border
    "#E4E0DA": "#232935",   # WIN_BD      window border
    "#F0EEE9": "#434D5E",   # ROW_DIV     divider between rows
    "#EFEDE9": "#434D5E",   # FOOT_DIV    footer divider
    "#D8D4CD": "#556074",   # INPUT_BD    ghost / input border
    "#C9C4BC": "#6B7689",   # CHECK_BD    unchecked checkbox border
    "#202022": "#E7E5E1",   # TEXT         7.81:1 on the card
    "#77736C": "#AAB1BE",   # TEXT_DIM     4.56:1 on the card, 5.23:1 on the window
    "#A6A199": "#7E8797",   # TEXT_MUTED   2.71:1 (placeholder only)
    "#8A857D": "#9098A7",   # TEXT_HIDDEN  3.38:1
    "#3AA652": "#89CA97",   # STATUS_OK    2.93:1 light -> 5.13:1 dark
    "#C46A00": "#E8C46B",   # STATUS_WARN  yellow shift: 5.87:1, 1.62:1 off ACCENT
    "#C0392B": "#E5A5A0",   # STATUS_BAD   1.81:1 unusable -> 4.78:1
}

# WHITE DOES TWO DIFFERENT JOBS in this file: it is a SURFACE (window, card,
# unchecked checkbox, combo item, tooltip: 13 sites) and it is INK ON THE ACCENT
# (the pill's label, the checkbox tick and dash, the highlighted combo item, the
# /slantis mark and the chrome glyphs on the orange bar: 6 sites). The ink sites
# are spelled __ON_ACCENT__ so the table can move the surfaces without turning
# the primary button's label dark on orange. Do not "tidy" them back to #FFFFFF.
# Same class of bug as the icon knockout, found the same day (2026-09-07).
#
# And it resolves to #FEFEFE, not #FFFFFF, ON PURPOSE: pure white IS a key of
# the table (the window surface), so ink spelled #FFFFFF would be painted dark
# by a second pass over an already themed string. One bit off white cannot be
# told apart on screen and keeps _theme() idempotent. The guard below is the
# whole reason the value is odd, so it fails loudly if someone "fixes" it.
ON_ACCENT = "#FEFEFE"

THEME_OVERRIDE = None   # "light" / "dark" forces it; None = follow the host


def _host_theme():
    """light|dark of the host. UIThemeManager only exists from Revit 2024 on.

    Same guard pyRevit itself uses to resolve icon.dark.png
    (pyrevitlib/pyrevit/revit/ui.py:160), so the dialogs and the ribbon icons
    can never disagree about which theme is on.
    """
    if THEME_OVERRIDE in ("light", "dark"):
        return THEME_OVERRIDE
    try:
        env = os.environ.get("SLANTISUI_THEME", "").lower()
        if env in ("light", "dark"):
            return env
    except Exception:
        pass
    try:
        from pyrevit import HOST_APP
        from pyrevit.revit.ui import get_current_theme
        from Autodesk.Revit.UI import UITheme
        if HOST_APP.is_newer_than(2024, True) and get_current_theme() == UITheme.Dark:
            return "dark"
    except Exception:
        pass
    return "light"


THEME = _host_theme()


# The factors are the ratios the handoff already had against #FF7700
# (hover .91 · dark .78 · pale 79% white · disabled 66% · selection 72%).
#
# THE PALE ACCENTS SPLIT BY ROLE, and only the dark theme makes the split
# visible. ACCENT_PL is INK (the title's secondary text on the orange bar), so
# it stays pale in both themes -- the bar it sits on is orange either way.
# ACCENT_DIS and ACCENT_SEL are SURFACES that carry text on top, and "pale
# orange under dark text" inverts in the dark theme: keeping them pale put the
# selected pill at 1.06:1 (reported from Revit 2026-09-07, third sighting of
# this bug class in one day after the icon knockout and the #FFFFFF split).
#
# So the surfaces keep the FACTOR and change the ANCHOR: mixed toward white in
# light, toward the dark card in dark. Selection goes #F9DCC8 -> #654937, which
# holds 6.64:1 of text and sits 1.56:1 off its card -- the same subtlety the
# light theme has (1.23:1), rather than a brighter mark than the light one.
#
# This CANNOT be fixed in the theme table: _STYLES is retoned at import time
# (bottom of this file), so the orange family is already resolved by the time
# _theme() runs. The theme has to be known HERE, which is why the block above
# was moved up. The table entries for these two hexes follow along anyway, for
# the XAML a tool writes itself.
_PALE_BASE = _DARK["#FAF8F5"] if THEME == "dark" else "#FFFFFF"

ACCENT_HOV = _darken(ACCENT, 0.91)    # hover of the primary and of the chrome buttons
ACCENT_DK  = _darken(ACCENT, 0.78)    # hover of the close button
ACCENT_PL  = _lighten(ACCENT, 0.79)   # INK on the orange bar: pale in both themes
ACCENT_DIS = _mix(ACCENT, _PALE_BASE, 0.66)   # SURFACE: disabled primary
ACCENT_SEL = _mix(ACCENT, _PALE_BASE, 0.72)   # SURFACE: selection (TextBox + tools)

# The INK of the disabled primary, and the asymmetry here is the point. A
# disabled button says so by having its label almost erased, and in LIGHT that
# already happens for free: the surface rises toward the near-white ink and the
# label lands at 1.37:1 with no help. In DARK the surface falls AWAY from the
# ink instead, so a near-white label reads at 7.35:1, as loud as an enabled
# one, and the button looks clickable while being blocked. So in dark the ink
# has to come down to its own surface: mixed 75% toward ACCENT_DIS -> 1.85:1,
# the same erased feel the light theme gets by itself.
#
# Light keeps ON_ACCENT verbatim, which is what makes this change cost the
# light theme exactly zero. Written as a placeholder (see __ON_ACCENT_DIS__ in
# BtnPrimary) because the resolved value must not go through the hex table.
ON_ACCENT_DIS = _mix(ON_ACCENT, ACCENT_DIS, 0.75) if THEME == "dark" else ON_ACCENT

# Canonical -> real map. The XAML below is written with the handoff's hexes
# (readable, greppable) and _retone() translates them into the derived family in
# one single place. ARGB first, so the 6-digit rule cannot swallow them.
_RAMP = (
    ("#0DFF7700", "#0D" + ACCENT[1:]),   # row hover     (accent @5%)
    ("#21FF7700", "#21" + ACCENT[1:]),   # row selected  (accent @13%)
    ("#FF7700", ACCENT),
    ("#E96C00", ACCENT_HOV),
    ("#C85E00", ACCENT_DK),
    ("#FFE3CB", ACCENT_PL),
    ("#FFD2A6", ACCENT_DIS),
    ("#FFD9B3", ACCENT_SEL),
)


def _retone(s):
    """Translate the handoff's canonical hexes into the family derived from ACCENT."""
    for canonical, real in _RAMP:
        s = s.replace(canonical, real)
    return s


ROW_HOVER  = _retone("#0DFF7700")  # row hover    (ARGB)
ROW_SEL    = _retone("#21FF7700")  # row selected (ARGB)


# The active table: the accent family always, plus the dark neutrals when dark.
_TABLE = dict((c.upper(), r) for c, r in _RAMP)
if THEME == "dark":
    _TABLE.update(_DARK)

assert ON_ACCENT.upper() not in _DARK, "ON_ACCENT cannot be a theme key"
assert ON_ACCENT_DIS.upper() not in _DARK, "ON_ACCENT_DIS cannot be a theme key"

_HEX_RE = re.compile(r"#[0-9A-Fa-f]{8}|#[0-9A-Fa-f]{6}")


def _theme(s):
    """Translate a finished XAML string into the active theme.

    ONE pass, so a value the table produces can never be translated again, and
    the result is idempotent (the keys are gone afterwards). It runs over the
    WHOLE window -- shared styles plus the body/footer the tool passed in -- so
    XAML a tool writes itself inherits the theme without knowing it exists.

    Known limit: it rewrites literal hexes in the markup, so a hex that is
    *content* (a tool showing "#FFFFFF" as a value) has to arrive through a
    Binding, not as a literal in the XAML. Bindings are never touched.
    """
    s = _HEX_RE.sub(lambda m: _TABLE.get(m.group(0).upper(), m.group(0)), s)
    # AFTER the table, never before: resolved first, the ink would be a plain
    # #FFFFFF by the time the table runs and the table would paint it dark.
    # The LONGER placeholder goes first on purpose, so a future rename that
    # makes one a prefix of the other cannot silently half-resolve it.
    s = s.replace("__ON_ACCENT_DIS__", ON_ACCENT_DIS)
    return s.replace("__ON_ACCENT__", ON_ACCENT)


def _tint(h):
    """The active value of a canonical neutral (for code that needs the hex)."""
    return _TABLE.get(h.upper(), h)


# Declared with the LIGHT value and resolved through the active table, so a tool
# that reads ui.TEXT gets the themed hex without asking which theme is on.
WIN_BG     = _tint("#FFFFFF")   # window background
CARD_BG    = _tint("#FAF8F5")   # work-surface card
CARD_BD    = _tint("#ECE9E4")   # card border
WIN_BD     = _tint("#E4E0DA")   # window border
ROW_DIV    = _tint("#F0EEE9")   # divider between rows
FOOT_DIV   = _tint("#EFEDE9")   # footer divider
INPUT_BD   = _tint("#D8D4CD")   # ghost / input border
CHECK_BD   = _tint("#C9C4BC")   # unchecked checkbox border

TEXT       = _tint("#202022")   # text (everything)
TEXT_DIM   = _tint("#77736C")   # subtitle / subtle
TEXT_MUTED = _tint("#A6A199")   # muted / placeholder
TEXT_HIDDEN= _tint("#8A857D")   # Hidden state

STATUS_OK  = _tint("#3AA652")   # Report only
# The Report's warn is semantic (text, not a brand surface), so it does NOT join
# the retone: if it follows the accent it stops reading as a warning.
STATUS_WARN= _tint("#C46A00")   # Report only
STATUS_BAD = _tint("#C0392B")   # Report only -- destructive action / "differs" / error

# -- Shared styles (injected into every window's Window.Resources) -----------
_STYLES = """
  <!-- Primary button (orange pill) -->
  <Style x:Key="BtnPrimary" TargetType="Button">
    <Setter Property="Foreground"      Value="__ON_ACCENT__"/>
    <Setter Property="Background"      Value="#FF7700"/>
    <Setter Property="FontSize"        Value="12.5"/>
    <Setter Property="FontWeight"      Value="Medium"/>
    <Setter Property="Cursor"          Value="Hand"/>
    <Setter Property="Padding"         Value="22,8"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="Button">
          <Border x:Name="bd" Background="{TemplateBinding Background}"
                  CornerRadius="8" Padding="{TemplateBinding Padding}">
            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
          </Border>
          <ControlTemplate.Triggers>
            <Trigger Property="IsMouseOver" Value="True">
              <Setter TargetName="bd" Property="Background" Value="#E96C00"/>
            </Trigger>
            <Trigger Property="IsEnabled" Value="False">
              <Setter TargetName="bd" Property="Background" Value="#FFD2A6"/>
              <Setter Property="Foreground" Value="__ON_ACCENT_DIS__"/>
            </Trigger>
          </ControlTemplate.Triggers>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>

  <!-- Ghost button (white pill, light border) -->
  <Style x:Key="BtnGhost" TargetType="Button">
    <Setter Property="Foreground"      Value="#202022"/>
    <Setter Property="Background"      Value="#FFFFFF"/>
    <Setter Property="FontSize"        Value="12.5"/>
    <Setter Property="FontWeight"      Value="Medium"/>
    <Setter Property="Cursor"          Value="Hand"/>
    <Setter Property="Padding"         Value="18,8"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="Button">
          <Border x:Name="bd" Background="{TemplateBinding Background}"
                  BorderBrush="#D8D4CD" BorderThickness="1"
                  CornerRadius="8" Padding="{TemplateBinding Padding}">
            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
          </Border>
          <ControlTemplate.Triggers>
            <Trigger Property="IsMouseOver" Value="True">
              <Setter TargetName="bd" Property="Background" Value="#FAF8F5"/>
            </Trigger>
          </ControlTemplate.Triggers>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>

  <!-- Checkbox 15px, radius 4, orange when checked (optional label). Implicit
       by TargetType so the DataGrid checkboxes and the labelled ones inherit it;
       x:Key="BrandCheck" is the alias for explicit use. -->
  <Style x:Key="BrandCheck" TargetType="CheckBox">
    <Setter Property="Foreground" Value="#202022"/>
    <Setter Property="FontSize"   Value="13"/>
    <Setter Property="VerticalContentAlignment" Value="Center"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="CheckBox">
          <StackPanel Orientation="Horizontal" Background="Transparent">
            <Border x:Name="box" Width="15" Height="15" CornerRadius="4"
                    Background="#FFFFFF" BorderBrush="#C9C4BC" BorderThickness="1.5"
                    VerticalAlignment="Center">
              <Grid>
                <TextBlock x:Name="tick" Text="&#10003;" FontSize="10" FontWeight="Bold"
                           Foreground="__ON_ACCENT__" HorizontalAlignment="Center"
                           VerticalAlignment="Center" Visibility="Collapsed"/>
                <Rectangle x:Name="dash" Width="7" Height="2" Fill="__ON_ACCENT__"
                           HorizontalAlignment="Center" VerticalAlignment="Center"
                           Visibility="Collapsed"/>
              </Grid>
            </Border>
            <ContentPresenter x:Name="cp" Margin="7,0,0,0" VerticalAlignment="Center"/>
          </StackPanel>
          <ControlTemplate.Triggers>
            <Trigger Property="IsChecked" Value="True">
              <Setter TargetName="box"  Property="Background"  Value="#FF7700"/>
              <Setter TargetName="box"  Property="BorderBrush" Value="#FF7700"/>
              <Setter TargetName="tick" Property="Visibility"  Value="Visible"/>
            </Trigger>
            <!-- Tri-state: indeterminate shows a dash on accent (partial selection) -->
            <Trigger Property="IsChecked" Value="{x:Null}">
              <Setter TargetName="box"  Property="Background"  Value="#FF7700"/>
              <Setter TargetName="box"  Property="BorderBrush" Value="#FF7700"/>
              <Setter TargetName="dash" Property="Visibility"  Value="Visible"/>
            </Trigger>
            <!-- Disabled. Without this the box keeps its live tones, because a
                 replaced template never gets the grey WPF paints by default:
                 a locked choice used to read as an open one. -->
            <Trigger Property="IsEnabled" Value="False">
              <Setter TargetName="box" Property="BorderBrush" Value="#D8D4CD"/>
              <Setter Property="Foreground" Value="#A6A199"/>
            </Trigger>
            <MultiTrigger>
              <MultiTrigger.Conditions>
                <Condition Property="IsChecked" Value="True"/>
                <Condition Property="IsEnabled" Value="False"/>
              </MultiTrigger.Conditions>
              <Setter TargetName="box"  Property="Background"  Value="#FFD2A6"/>
              <Setter TargetName="box"  Property="BorderBrush" Value="#FFD2A6"/>
              <Setter TargetName="tick" Property="Foreground"  Value="__ON_ACCENT_DIS__"/>
            </MultiTrigger>
          </ControlTemplate.Triggers>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>
  <Style TargetType="CheckBox" BasedOn="{StaticResource BrandCheck}"/>

  <!-- Radio 15px, same geometry and tones as BrandCheck so a radio and a
       checkbox line up in the same column; only the shape changes. Implicit by
       TargetType, so a tool that writes a bare <RadioButton> gets it without
       asking; a tool with its own style (an explicit x:Key, or an implicit one
       in a nested Resources) keeps that one. x:Key="BrandRadio" is the alias
       for explicit use. -->
  <Style x:Key="BrandRadio" TargetType="RadioButton">
    <Setter Property="Foreground" Value="#202022"/>
    <Setter Property="FontSize"   Value="13"/>
    <Setter Property="VerticalContentAlignment" Value="Center"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="RadioButton">
          <StackPanel Orientation="Horizontal" Background="Transparent">
            <Border x:Name="box" Width="15" Height="15" CornerRadius="8"
                    Background="#FFFFFF" BorderBrush="#C9C4BC" BorderThickness="1.5"
                    VerticalAlignment="Center">
              <Ellipse x:Name="dot" Width="5" Height="5" Fill="__ON_ACCENT__"
                       HorizontalAlignment="Center" VerticalAlignment="Center"
                       Visibility="Collapsed"/>
            </Border>
            <ContentPresenter x:Name="cp" Margin="7,0,0,0" VerticalAlignment="Center"/>
          </StackPanel>
          <ControlTemplate.Triggers>
            <Trigger Property="IsChecked" Value="True">
              <Setter TargetName="box" Property="Background"  Value="#FF7700"/>
              <Setter TargetName="box" Property="BorderBrush" Value="#FF7700"/>
              <Setter TargetName="dot" Property="Visibility"  Value="Visible"/>
            </Trigger>
            <Trigger Property="IsEnabled" Value="False">
              <Setter TargetName="box" Property="BorderBrush" Value="#D8D4CD"/>
              <Setter Property="Foreground" Value="#A6A199"/>
            </Trigger>
            <MultiTrigger>
              <MultiTrigger.Conditions>
                <Condition Property="IsChecked" Value="True"/>
                <Condition Property="IsEnabled" Value="False"/>
              </MultiTrigger.Conditions>
              <Setter TargetName="box" Property="Background"  Value="#FFD2A6"/>
              <Setter TargetName="box" Property="BorderBrush" Value="#FFD2A6"/>
              <Setter TargetName="dot" Property="Fill"        Value="__ON_ACCENT_DIS__"/>
            </MultiTrigger>
          </ControlTemplate.Triggers>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>
  <Style TargetType="RadioButton" BasedOn="{StaticResource BrandRadio}"/>

  <!-- TextBox: light border r8, orange focus + caret -->
  <Style TargetType="TextBox">
    <Setter Property="Background"      Value="#FFFFFF"/>
    <Setter Property="Foreground"      Value="#202022"/>
    <Setter Property="FontSize"        Value="13"/>
    <Setter Property="CaretBrush"      Value="#FF7700"/>
    <Setter Property="SelectionBrush"  Value="#FFD9B3"/>
    <Setter Property="BorderBrush"     Value="#D8D4CD"/>
    <Setter Property="BorderThickness" Value="1"/>
    <Setter Property="Padding"         Value="9,7"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="TextBox">
          <Border x:Name="bd" Background="{TemplateBinding Background}"
                  BorderBrush="{TemplateBinding BorderBrush}"
                  BorderThickness="{TemplateBinding BorderThickness}"
                  CornerRadius="8">
            <!-- Do NOT bind Margin to Padding on the content host: it already
                 honours TextBox.Padding on its own, so binding it again applies
                 the padding TWICE and a 34px high box is left with ~4px of text
                 area. The glyphs get clipped away entirely and typing looks
                 like it does nothing (bug found 2026-07-27). -->
            <ScrollViewer x:Name="PART_ContentHost" VerticalAlignment="Center"/>
          </Border>
          <ControlTemplate.Triggers>
            <Trigger Property="IsKeyboardFocused" Value="True">
              <Setter TargetName="bd" Property="BorderBrush" Value="#FF7700"/>
            </Trigger>
          </ControlTemplate.Triggers>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>

  <!-- ComboBox: r8, white popup. The custom ControlTemplate is what kills the
       system's blue highlight while scrolling (together with the ComboBoxItem below). -->
  <Style TargetType="ComboBox">
    <Setter Property="Foreground"      Value="#202022"/>
    <Setter Property="Background"      Value="#FFFFFF"/>
    <Setter Property="BorderBrush"     Value="#D8D4CD"/>
    <Setter Property="BorderThickness" Value="1"/>
    <Setter Property="Padding"         Value="9,6"/>
    <Setter Property="FontSize"        Value="13"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="ComboBox">
          <Grid>
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="*"/>
              <ColumnDefinition Width="22"/>
            </Grid.ColumnDefinitions>
            <Border Grid.ColumnSpan="2" Background="{TemplateBinding Background}"
                    BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="{TemplateBinding BorderThickness}" CornerRadius="8"/>
            <!-- ContentTemplate is required: without it the closed box gets the object
                 but not how to show it, and falls back to ToString() (in IronPython that
                 prints "IronPython.NewTypes.System.Object_1$1"). The drop-down still looks
                 fine, so the bug only shows after picking. Do not remove. -->
            <ContentPresenter Grid.Column="0" Margin="{TemplateBinding Padding}"
                              VerticalAlignment="Center"
                              Content="{TemplateBinding SelectionBoxItem}"
                              ContentTemplate="{TemplateBinding SelectionBoxItemTemplate}"
                              ContentStringFormat="{TemplateBinding SelectionBoxItemStringFormat}"
                              IsHitTestVisible="False"/>
            <TextBlock Grid.Column="1" Text="&#x25BE;" Foreground="#77736C"
                       VerticalAlignment="Center" HorizontalAlignment="Center"
                       IsHitTestVisible="False"/>
            <ToggleButton Grid.ColumnSpan="2"
                          IsChecked="{Binding IsDropDownOpen,
                                      RelativeSource={RelativeSource TemplatedParent}, Mode=TwoWay}">
              <ToggleButton.Template>
                <ControlTemplate TargetType="ToggleButton">
                  <Border Background="Transparent"/>
                </ControlTemplate>
              </ToggleButton.Template>
            </ToggleButton>
            <Popup Grid.ColumnSpan="2" IsOpen="{TemplateBinding IsDropDownOpen}"
                   AllowsTransparency="True" Focusable="False" Placement="Bottom">
              <Border Background="#FFFFFF" BorderBrush="#ECE9E4" BorderThickness="1"
                      MinWidth="{Binding ActualWidth, RelativeSource={RelativeSource TemplatedParent}}">
                <ScrollViewer MaxHeight="240" VerticalScrollBarVisibility="Auto">
                  <ItemsPresenter/>
                </ScrollViewer>
              </Border>
            </Popup>
          </Grid>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>
  <Style TargetType="ComboBoxItem">
    <Setter Property="Background" Value="#FFFFFF"/>
    <Setter Property="Foreground" Value="#202022"/>
    <Setter Property="Padding"    Value="9,6"/>
    <Setter Property="FontSize"   Value="13"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="ComboBoxItem">
          <Border x:Name="bd" Background="{TemplateBinding Background}"
                  Padding="{TemplateBinding Padding}">
            <ContentPresenter VerticalAlignment="Center"/>
          </Border>
          <ControlTemplate.Triggers>
            <Trigger Property="IsHighlighted" Value="True">
              <Setter TargetName="bd" Property="Background" Value="#FF7700"/>
              <Setter Property="Foreground" Value="__ON_ACCENT__"/>
            </Trigger>
          </ControlTemplate.Triggers>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>

  <Style TargetType="DataGrid">
    <Setter Property="Background"                Value="Transparent"/>
    <Setter Property="Foreground"                Value="#202022"/>
    <Setter Property="BorderThickness"           Value="0"/>
    <Setter Property="RowBackground"             Value="Transparent"/>
    <Setter Property="AlternatingRowBackground"  Value="Transparent"/>
    <Setter Property="GridLinesVisibility"       Value="None"/>
    <Setter Property="SelectionMode"             Value="Extended"/>
    <Setter Property="CanUserResizeRows"         Value="False"/>
    <Setter Property="RowHeaderWidth"            Value="0"/>
    <Setter Property="HeadersVisibility"         Value="Column"/>
    <Setter Property="AutoGenerateColumns"       Value="False"/>
    <Setter Property="IsReadOnly"                Value="True"/>
  </Style>

  <!-- Column header: card background, 10.5 Medium, 1px orange underline -->
  <Style TargetType="DataGridColumnHeader">
    <Setter Property="Background"      Value="#FAF8F5"/>
    <Setter Property="Foreground"      Value="#202022"/>
    <Setter Property="FontWeight"      Value="Medium"/>
    <Setter Property="FontSize"        Value="10.5"/>
    <Setter Property="Padding"         Value="12,6"/>
    <Setter Property="BorderBrush"     Value="#FF7700"/>
    <Setter Property="BorderThickness" Value="0,0,0,1"/>
    <Setter Property="HorizontalContentAlignment" Value="Left"/>
  </Style>

  <!-- Row: #F0EEE9 divider, orange hover 5%, orange selection 13% (ARGB) -->
  <Style TargetType="DataGridRow">
    <Setter Property="Background"      Value="Transparent"/>
    <Setter Property="BorderBrush"     Value="#F0EEE9"/>
    <Setter Property="BorderThickness" Value="0,0,0,1"/>
    <Style.Triggers>
      <Trigger Property="IsMouseOver" Value="True">
        <Setter Property="Background" Value="#0DFF7700"/>
      </Trigger>
      <Trigger Property="IsSelected" Value="True">
        <Setter Property="Background" Value="#21FF7700"/>
        <Setter Property="Foreground" Value="#202022"/>
      </Trigger>
    </Style.Triggers>
  </Style>

  <Style TargetType="DataGridCell">
    <Setter Property="Foreground"      Value="#202022"/>
    <Setter Property="FontSize"        Value="13"/>
    <Setter Property="BorderThickness" Value="0"/>
    <Setter Property="Padding"         Value="12,5"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="DataGridCell">
          <Border Padding="{TemplateBinding Padding}" Background="Transparent">
            <ContentPresenter VerticalAlignment="Center"/>
          </Border>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>

  <Style TargetType="ScrollBar">
    <Setter Property="Background" Value="Transparent"/>
    <Setter Property="Width"      Value="8"/>
  </Style>

  <!-- Section heading inside the card (long forms): 10.5 Medium, muted. The
       tool writes the text in UPPERCASE. -->
  <!-- Section heading: ink, not the placeholder grey (2026-09-19: grey,
       muted headings made the whole dialog look washed out). -->
  <Style x:Key="SectionHead" TargetType="TextBlock">
    <Setter Property="FontSize"   Value="11"/>
    <Setter Property="FontWeight" Value="SemiBold"/>
    <Setter Property="Foreground" Value="#202022"/>
    <Setter Property="Margin"     Value="0,0,0,9"/>
  </Style>

  <!-- Slider: #ECE9E4 rail, orange travelled span, white thumb with a ring -->
  <Style TargetType="Slider">
    <Setter Property="Height"  Value="22"/>
    <Setter Property="Cursor"  Value="Hand"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="Slider">
          <Grid VerticalAlignment="Center">
            <Border Height="4" CornerRadius="2" Background="#ECE9E4"
                    VerticalAlignment="Center"/>
            <Track x:Name="PART_Track">
              <Track.DecreaseRepeatButton>
                <RepeatButton Command="Slider.DecreaseLarge">
                  <RepeatButton.Template>
                    <ControlTemplate TargetType="RepeatButton">
                      <Border Height="4" CornerRadius="2" Background="#FF7700"
                              VerticalAlignment="Center"/>
                    </ControlTemplate>
                  </RepeatButton.Template>
                </RepeatButton>
              </Track.DecreaseRepeatButton>
              <Track.IncreaseRepeatButton>
                <RepeatButton Command="Slider.IncreaseLarge">
                  <RepeatButton.Template>
                    <ControlTemplate TargetType="RepeatButton">
                      <Border Background="Transparent"/>
                    </ControlTemplate>
                  </RepeatButton.Template>
                </RepeatButton>
              </Track.IncreaseRepeatButton>
              <Track.Thumb>
                <Thumb Width="14" Height="14">
                  <Thumb.Template>
                    <ControlTemplate TargetType="Thumb">
                      <Border Width="14" Height="14" CornerRadius="7"
                              Background="#FFFFFF" BorderBrush="#FF7700"
                              BorderThickness="3"/>
                    </ControlTemplate>
                  </Thumb.Template>
                </Thumb>
              </Track.Thumb>
            </Track>
          </Grid>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>

  <!-- ToolTip: in a tile gallery there is no room for the description, so the
       tooltip stops being decoration and becomes where the text lives.
       Measured 2026-08-19 against the longest real description (425 chars): with
       no style at all WPF gives a tooltip no width limit and no wrapping, so it
       stretches to the whole screen width, 1707px here, on ONE line, and the
       rest of the text is simply lost. MaxWidth
       alone does NOT fix that: the default ContentPresenter still does not wrap,
       so the text is silently clipped to one line (320x26). Wrapping needs
       BOTH the MaxWidth and a ContentTemplate whose TextBlock wraps: that is
       this style, and the same text then measures 315x133.
       Known limit, not a bug to hunt: a ToolTip renders inside its own Popup, so
       the DM Sans that _apply_dm_sans() sets on the window root may not reach it.
       And keep XML comments free of double hyphens: XamlReader refuses them, and
       one bad comment in here breaks EVERY window in the system at once. -->
  <Style TargetType="ToolTip">
    <Setter Property="Background"    Value="#FFFFFF"/>
    <Setter Property="Foreground"    Value="#202022"/>
    <Setter Property="FontSize"      Value="11"/>
    <Setter Property="MaxWidth"      Value="320"/>
    <Setter Property="HasDropShadow" Value="True"/>
    <Setter Property="ContentTemplate">
      <Setter.Value>
        <DataTemplate>
          <TextBlock Text="{Binding}" TextWrapping="Wrap"/>
        </DataTemplate>
      </Setter.Value>
    </Setter>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="ToolTip">
          <Border Background="#FFFFFF" BorderBrush="#E4E0DA" BorderThickness="1"
                  CornerRadius="4" Padding="10,7">
            <ContentPresenter/>
          </Border>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>
"""

# One single translation point: from here on _STYLES already holds the real family.
_STYLES = _retone(_STYLES)

# -- Title bar (custom chrome) -----------------------------------------------
# TITLE_BAR picks the chrome skin for EVERY window in the system:
#   "accent" -> brand orange title bar, with the minimize/maximize/close buttons
#               drawn by us (no Windows frame). This is the brand skin.
#   "light"  -> same custom bar but white, with the 3px orange accent line on top
#               (the sober variant).
#   "system" -> native Windows frame + the 3px accent line (the original).
# Changing this constant re-themes every tool at once: the lib IS the system.
TITLE_BAR = "accent"

_CHROME_STYLES = """
  <!-- Custom chrome buttons (Segoe MDL2 Assets glyphs) -->
  <Style x:Key="ChromeBtn" TargetType="Button">
    <Setter Property="Width"      Value="44"/>
    <Setter Property="Height"     Value="38"/>
    <Setter Property="Foreground" Value="__GLYPH__"/>
    <Setter Property="FontFamily" Value="Segoe MDL2 Assets"/>
    <Setter Property="FontSize"   Value="10"/>
    <Setter Property="Cursor"     Value="Hand"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="Button">
          <Border x:Name="bd" Background="Transparent">
            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
          </Border>
          <ControlTemplate.Triggers>
            <Trigger Property="IsMouseOver" Value="True">
              <Setter TargetName="bd" Property="Background" Value="__HOVER__"/>
            </Trigger>
          </ControlTemplate.Triggers>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>
  <Style x:Key="ChromeClose" TargetType="Button" BasedOn="{StaticResource ChromeBtn}">
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="Button">
          <Border x:Name="bd" Background="Transparent">
            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
          </Border>
          <ControlTemplate.Triggers>
            <Trigger Property="IsMouseOver" Value="True">
              <Setter TargetName="bd" Property="Background" Value="__CLOSE_HOVER__"/>
            </Trigger>
          </ControlTemplate.Triggers>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>
"""

_TITLEBAR = """
    <Border DockPanel.Dock="Top" x:Name="__slui_bar__" Height="38"
            Background="__BAR_BG__">
      <Grid>
        <StackPanel Orientation="Horizontal" VerticalAlignment="Center" Margin="14,0,0,0">
          <TextBlock Text="/slantis" FontSize="13.5" FontWeight="Bold"
                     Foreground="__BRAND_FG__"/>
          <TextBlock x:Name="__slui_bartitle__" FontSize="11.5" Margin="10,1,0,0"
                     Foreground="__TITLE_FG__" VerticalAlignment="Center"/>
        </StackPanel>
        <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
          <Button x:Name="__slui_min__"   Style="{StaticResource ChromeBtn}"
                  Content="&#xE921;" ToolTip="Minimize"/>
          <Button x:Name="__slui_max__"   Style="{StaticResource ChromeBtn}"
                  Content="&#xE922;" ToolTip="Maximize"/>
          <Button x:Name="__slui_close__" Style="{StaticResource ChromeClose}"
                  Content="&#xE8BB;" ToolTip="Close"/>
        </StackPanel>
      </Grid>
    </Border>
"""

_ACCENT_STRIP = """
    <Border DockPanel.Dock="Top" Height="3" Background="#FF7700"/>
"""

# variant -> (bar bg, brand fg, title fg, glyph, hover, close hover, strip)
# Written with the CANONICAL hexes; _retone() moves them to the real family.
_BAR_SKINS = {
    "accent": ("#FF7700", "__ON_ACCENT__", "#FFE3CB", "__ON_ACCENT__",
               "#E96C00", "#C85E00", False),
    "light":  ("#FFFFFF", "#202022", "#A6A199", "#202022", "#F0EEE9", "#FFE3CB", True),
}
# "light" is written in canonical neutrals, so the theme table turns it into a
# DARK bar by itself: same skin, no second entry. The alias says so out loud.
_BAR_SKINS["neutral"] = _BAR_SKINS["light"]


def _chrome_parts(variant):
    """Return (styles, top_xaml, window_attrs) for the requested chrome skin."""
    if variant not in _BAR_SKINS:
        return "", _ACCENT_STRIP, 'ResizeMode="CanResize"'
    bg, brand, title, glyph, hover, close_hover, strip = _BAR_SKINS[variant]
    styles = (_CHROME_STYLES
              .replace("__GLYPH__", glyph)
              .replace("__HOVER__", hover)
              .replace("__CLOSE_HOVER__", close_hover))
    top = (_ACCENT_STRIP if strip else "") + (_TITLEBAR
                                              .replace("__BAR_BG__", bg)
                                              .replace("__BRAND_FG__", brand)
                                              .replace("__TITLE_FG__", title))
    attrs = ('WindowStyle="None" ResizeMode="CanResize" '
             'BorderBrush="#E4E0DA" BorderThickness="1"')
    return _retone(styles), _retone(top), attrs


# -- Window template ---------------------------------------------------------
_TEMPLATE = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="__WIN_TITLE__" Width="__WIN_W__" __WIN_H_ATTR__
        WindowStartupLocation="CenterScreen"
        FontSize="13"
        Background="#FFFFFF" Foreground="#202022"
        __WIN_CHROME_ATTR__>
  <Window.Resources>
__STYLES__
  </Window.Resources>

  <DockPanel LastChildFill="True">
__TOP__

    <!-- Header -->
    <StackPanel DockPanel.Dock="Top" Margin="16,13,16,11">
      <TextBlock x:Name="__slui_title__"
                 FontSize="18" FontWeight="SemiBold" Foreground="#202022"/>
      <TextBlock x:Name="__slui_subtitle__"
                 FontSize="11.5" Foreground="#77736C" Margin="0,2,0,0"/>
__CONTEXT__
    </StackPanel>

    <!-- Footer slot (with a divider on top) -->
    <Border DockPanel.Dock="Bottom" Height="1" Background="#EFEDE9"/>
    <Grid DockPanel.Dock="Bottom" Margin="16,12">
__FOOTER__
    </Grid>

    <!-- Work-surface card -->
    <Border Margin="16,4,16,10"
            Background="#FAF8F5" BorderBrush="#ECE9E4" BorderThickness="1"
            CornerRadius="10" Padding="14">
__BODY__
    </Border>
  </DockPanel>
</Window>
"""


def _xml_esc(s):
    return (s.replace('&', '&amp;').replace('<', '&lt;')
             .replace('>', '&gt;').replace('"', '&quot;')
             .replace("'", '&apos;'))


def styles_xaml():
    """The shared control styles, already retoned -- for hosts that are not a
    Window built by build().

    A pyRevit dockable pane is a Page, not a Window, so it cannot go through
    build()/parse(); without this it would have to hand-copy the palette (the
    4-copies debt). Merge it instead:

        rd = XamlReader.Parse('<ResourceDictionary xmlns="...">'
                              + ui.styles_xaml() + '</ResourceDictionary>')
        page.Resources.MergedDictionaries.Add(rd)

    Safe to inject anywhere: these are control styles only (no Window setter),
    and the single StaticResource inside them is BrandCheck, defined right there.
    """
    return _theme(_STYLES)


def build(title, subtitle, body, footer="", width=720, height=None, context=""):
    """Return the full /slantis window XAML string.

    Pass height=None (default) to let the window auto-size to its content.
    """
    safe_title = _xml_esc(title)
    if context:
        ctx_xml = (u'      <TextBlock TextWrapping="Wrap" FontSize="12" '
                   u'Foreground="#77736C" Margin="0,8,0,0" Text="{}"/>'.format(
                       _xml_esc(context)))
    else:
        ctx_xml = ""
    if height is None:
        h_attr = 'SizeToContent="Height" MinHeight="180"'
    else:
        h_attr = 'Height="{}"'.format(height)
    chrome_styles, top_xaml, chrome_attr = _chrome_parts(TITLE_BAR)
    # The theme pass is applied HERE, to the assembled window, so it covers the
    # template, the shared styles, the chrome and the body/footer of the tool.
    return _theme(_TEMPLATE
            .replace("__WIN_TITLE__",   safe_title)
            .replace("__WIN_W__",       str(width))
            .replace("__WIN_H_ATTR__",  h_attr)
            .replace("__WIN_CHROME_ATTR__", chrome_attr)
            .replace("__STYLES__",      _STYLES + chrome_styles)
            .replace("__TOP__",         top_xaml)
            .replace("__BODY__",        body)
            .replace("__FOOTER__",      footer)
            .replace("__CONTEXT__",     ctx_xml))


def _wire_chrome(win, title, resizable):
    """Wire up the custom title bar: drag, min/max/close, double click.

    Without the Windows frame the window loses native dragging and snapping: we
    give them back with DragMove + WindowChrome. If WindowChrome is unavailable
    it degrades to a frameless but working window.

    WindowChrome does NOT make maximizing respect the taskbar (measured
    2026-10-01): a WindowStyle=None window maximizes to the whole monitor plus
    a resize-border overhang on every side, so the footer ended up 61 px under
    a 72 px taskbar and Apply/Close were hidden. _fit_work_area pads the
    content back inside the monitor's work area on every maximize, however it
    was triggered (button, double click, Win+Up, dragging to the top edge).
    """
    bar = win.FindName("__slui_bar__")
    if bar is None:
        return

    try:
        from System.Windows.Shell import WindowChrome
        from System.Windows import Thickness
        ch = WindowChrome()
        ch.CaptionHeight = 0                      # we handle the drag ourselves
        ch.ResizeBorderThickness = Thickness(6)   # grabbable border for resizing
        ch.GlassFrameThickness = Thickness(0)
        ch.UseAeroCaptionButtons = False
        WindowChrome.SetWindowChrome(win, ch)
    except Exception:
        pass

    from System.Windows import WindowState

    lbl = win.FindName("__slui_bartitle__")
    if lbl is not None:
        lbl.Text = title

    btn_max = win.FindName("__slui_max__")

    def _apply_fit():
        root = win.Content
        if root is None:
            return
        from System.Windows import Thickness
        if win.WindowState != WindowState.Maximized:
            root.Margin = Thickness(0)
            return
        try:
            clr.AddReference("System.Windows.Forms")
            from System.Windows.Forms import Screen
            from System.Windows.Interop import WindowInteropHelper
            from System.Windows import Point, PresentationSource
            wa = Screen.FromHandle(WindowInteropHelper(win).Handle).WorkingArea
            tl = win.PointToScreen(Point(0, 0))               # device px
            br = win.PointToScreen(Point(win.ActualWidth, win.ActualHeight))
            m = PresentationSource.FromVisual(win).CompositionTarget.TransformFromDevice
            sx, sy = m.M11, m.M22                              # px -> DIP
            root.Margin = Thickness(max(0.0, (wa.Left - tl.X) * sx),
                                    max(0.0, (wa.Top - tl.Y) * sy),
                                    max(0.0, (br.X - wa.Right) * sx),
                                    max(0.0, (br.Y - wa.Bottom) * sy))
        except Exception:
            pass

    def _fit_work_area(s=None, e=None):
        # Measure after the maximized layout lands, not inside StateChanged.
        try:
            from System import Action
            from System.Windows.Threading import DispatcherPriority
            win.Dispatcher.BeginInvoke(DispatcherPriority.Loaded, Action(_apply_fit))
        except Exception:
            _apply_fit()

    win.StateChanged += _fit_work_area
    win.Loaded += _fit_work_area      # a window that opens already maximized

    def _toggle_max(s=None, e=None):
        if win.WindowState == WindowState.Maximized:
            win.WindowState = WindowState.Normal
            btn_max.Content = u""   # MDL2 maximize
        else:
            win.WindowState = WindowState.Maximized
            btn_max.Content = u""   # MDL2 restore

    def _on_bar_down(s, e):
        if e.ClickCount == 2 and resizable:
            _toggle_max()
        else:
            try:
                win.DragMove()
            except Exception:
                pass

    bar.MouseLeftButtonDown += _on_bar_down
    win.FindName("__slui_min__").Click += (
        lambda s, e: setattr(win, 'WindowState', WindowState.Minimized))
    win.FindName("__slui_close__").Click += lambda s, e: win.Close()

    if resizable:
        btn_max.Click += _toggle_max
    else:
        # window that sizes itself to its content: maximizing makes no sense
        btn_max.Visibility = Visibility.Collapsed


def parse(title, subtitle, body, footer="", width=720, height=None, context=""):
    """Build, parse, and return a WPF Window ready for wiring (DM Sans applied)."""
    xaml = build(title, subtitle, body, footer, width, height, context)
    win = XamlReader.Parse(xaml)
    _apply_dm_sans(win)
    win.FindName("__slui_title__").Text = title
    win.FindName("__slui_subtitle__").Text = subtitle
    _wire_chrome(win, title, resizable=(height is not None))
    return win


def show(title, subtitle, body, footer="", width=720, height=None, context=""):
    """Build, parse, and ShowDialog."""
    return parse(title, subtitle, body, footer, width, height, context).ShowDialog()


# -- Reusable /slantis dialogs -----------------------------------------------

_PICK_BODY = """
  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
    </Grid.RowDefinitions>
    __FILTERBAR__
    <TextBox x:Name="txtFilter" Grid.Row="1" Height="34" Margin="0,0,0,12"
             ToolTip="Type to filter"/>
    <Border Grid.Row="2" CornerRadius="8" Background="#FFFFFF"
            BorderBrush="#ECE9E4" BorderThickness="1" ClipToBounds="True">
      <DataGrid x:Name="grid" AutoGenerateColumns="False"
                HeadersVisibility="__HDRS__" SelectionMode="Single"
                CanUserAddRows="False" CanUserDeleteRows="False"
                CanUserSortColumns="False"
                CanUserResizeRows="False" CanUserResizeColumns="False">
        <DataGrid.Columns>
          __CHECKCOL__
          __COLS__
        </DataGrid.Columns>
      </DataGrid>
    </Border>
  </Grid>
"""

# The toggle bar of filters=, injected above the search box. Every box starts
# ticked, so the list opens showing everything: a filter you have to switch ON
# hides rows the caller never asked to hide.
_PICK_FILTERBAR = """
    <WrapPanel Grid.Row="0" Margin="0,0,0,12">
__BOXES__
    </WrapPanel>
"""

_PICK_FILTERBOX = """
      <CheckBox x:Name="__N__" Style="{StaticResource BrandCheck}"
                Content="__L__" IsChecked="True" Margin="0,0,16,0"/>
"""

_PICK_FILTERSEP = """
      <Border Width="1" Background="#ECE9E4" Margin="0,2,16,2"/>
"""

# The one-column layout: what every caller without columns= gets, unchanged.
_PICK_NAMECOL = """
          <DataGridTextColumn Width="*" Binding="{Binding Name}" IsReadOnly="True">
            <DataGridTextColumn.ElementStyle>
              <Style TargetType="TextBlock">
                <Setter Property="VerticalAlignment" Value="Center"/>
              </Style>
            </DataGridTextColumn.ElementStyle>
          </DataGridTextColumn>
"""

# The tick column, injected only when multiselect is on. Same idiom as Print Set
# Manager: a CheckBox in the CellTemplate bound TwoWay to the row object, so the
# state lives on the item and survives a filter refresh.
_PICK_CHECKCOL = """
          <DataGridTemplateColumn Width="44">
            <DataGridTemplateColumn.CellTemplate>
              <DataTemplate>
                <CheckBox Style="{StaticResource BrandCheck}"
                          IsChecked="{Binding Checked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                          HorizontalAlignment="Center"/>
              </DataTemplate>
            </DataGridTemplateColumn.CellTemplate>
          </DataGridTemplateColumn>
"""

_PICK_FOOTER = """
  <Grid>
    <TextBlock x:Name="lblCount" VerticalAlignment="Center" Foreground="#A6A199" FontSize="11.5"/>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnAll"    Content="Select all" Style="{StaticResource BtnGhost}" Margin="0,0,8,0"/>
      <Button x:Name="btnOK"     Content="__BTN__"    Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
      <Button x:Name="btnCancel" Content="Cancel"     Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""

_ALERT_BODY = """
  <Grid>
    <TextBlock x:Name="lblMsg" TextWrapping="Wrap" FontSize="13"
               Foreground="#202022" VerticalAlignment="Center"/>
  </Grid>
"""

_ALERT_FOOTER = """
  <Grid>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnOK" Content="OK" Style="{StaticResource BtnPrimary}"/>
    </StackPanel>
  </Grid>
"""

_CONFIRM_FOOTER = """
  <Grid>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnYes" Content="__YES__" Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
      <Button x:Name="btnNo"  Content="Cancel"  Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""


class _PickItem(object):
    """One row of the picker. `Name` is both the single-column text and the
    haystack the filter box matches against, so a multi-column row keeps its
    cells in C0..Cn (same idiom as _TableRow) and joins them into Name."""

    def __init__(self, value, display=None, cells=None):
        self._value = value
        if cells is not None:
            texts = [u'' if c is None else u'{}'.format(c) for c in cells]
            for i, t in enumerate(texts):
                setattr(self, 'C%d' % i, t)
            self.Name = u' '.join(texts)
        else:
            self.Name = display if display is not None else str(value)
        self.Checked = False   # bound TwoWay by the tick column (multiselect only)


def pick_list(items, title, subtitle="", button_name="OK",
              multiselect=True, width=480, height=460, name_fn=None, context="",
              columns=None, filters=None):
    """Show a /slantis list picker.

    Args:
        items:       List of strings OR objects.
        name_fn:     Optional callable to extract display text from each item.
                     If omitted, str(item) is used as the display name.
                     Example: name_fn=lambda x: x.Name
        multiselect: If True, shows a tick column and returns the list of ticked
                     values; otherwise the row is picked by selection and one
                     value comes back.
        columns:     Optional list of headers, or of (header, width) pairs, to
                     show the list as a real grid with headers instead of one
                     line of text per row. Width is a WPF value ("*", "2*" or
                     a pixel number), same as show_table. With columns set,
                     name_fn must return one cell per column (a sequence); the
                     filter box still matches across every cell of the row.
                     Example: columns=[("View", "2*"), ("Type", 90)].
        filters:     Optional toggle bar above the search box. A list of
                     GROUPS, each group a list of (label, test) pairs, where
                     test(item) says whether that item belongs to the label.
                     Every box starts ticked. A row survives when, in EVERY
                     group, at least one ticked test matches it: within a
                     group the boxes are an OR, between groups an AND. Untick
                     a whole group and the list goes empty, which is the
                     honest reading of "none of these".
                     Example: filters=[[("Drafting", is_drafting),
                                        ("Detail", is_detail)],
                                       [("On a sheet", on_sheet),
                                        ("Unplaced", not_on_sheet)]]

    Returns:
        List of ticked values (multiselect=True), single value (multiselect=False),
        or None if cancelled.
    """
    from System.Collections.ObjectModel import ObservableCollection

    footer = _PICK_FOOTER.replace("__BTN__", _xml_esc(button_name))
    if columns:
        cols = []
        for i, c in enumerate(columns):
            if isinstance(c, (tuple, list)):
                header, w = c[0], c[1]
            else:
                header, w = c, "*"
            cols.append(_TABLE_COL
                        .replace("__H__", _xml_esc(u'{}'.format(header)))
                        .replace("__W__", _xml_esc(u'{}'.format(w)))
                        .replace("__B__", 'C%d' % i))
        body = _PICK_BODY.replace("__COLS__", ''.join(cols))                          .replace("__HDRS__", "Column")
    else:
        body = _PICK_BODY.replace("__COLS__", _PICK_NAMECOL)                          .replace("__HDRS__", "None")
    body = body.replace("__CHECKCOL__", _PICK_CHECKCOL if multiselect else "")

    fgroups = [list(g) for g in filters] if filters else []
    if fgroups:
        boxes = []
        for gi, group in enumerate(fgroups):
            if gi:
                boxes.append(_PICK_FILTERSEP)
            for bi, pair in enumerate(group):
                boxes.append(_PICK_FILTERBOX
                             .replace("__N__", 'flt%d_%d' % (gi, bi))
                             .replace("__L__", _xml_esc(u'{}'.format(pair[0]))))
        body = body.replace("__FILTERBAR__",
                            _PICK_FILTERBAR.replace("__BOXES__", ''.join(boxes)))
    else:
        body = body.replace("__FILTERBAR__", "")

    win = parse(title, subtitle, body, footer, width, height, context)
    grid      = win.FindName("grid")
    txtFilter = win.FindName("txtFilter")
    lblCount  = win.FindName("lblCount")
    btnAll    = win.FindName("btnAll")
    btnOK     = win.FindName("btnOK")
    btnCancel = win.FindName("btnCancel")

    if columns:
        all_items = [_PickItem(i, cells=(name_fn(i) if name_fn is not None
                                         else (i,))) for i in items]
    elif name_fn is not None:
        all_items = [_PickItem(i, display=name_fn(i)) for i in items]
    else:
        all_items = [_PickItem(i) for i in items]
    state = {'result': None, 'filtered': all_items}

    if not multiselect:
        btnAll.Visibility = Visibility.Collapsed

    def _to_col(lst):
        c = ObservableCollection[object]()
        for it in lst:
            c.Add(it)
        return c

    def _update_count():
        shown = len(state['filtered'])
        if multiselect:
            ticked = sum(1 for it in all_items if it.Checked)
            lblCount.Text = "{} selected   |   {} of {} items".format(
                ticked, shown, len(all_items))
            # The button always says what the click will do, over what is on
            # screen: with a filter typed it acts on the visible rows only.
            visible_all_on = bool(state['filtered']) and all(
                it.Checked for it in state['filtered'])
            btnAll.Content = "Clear all" if visible_all_on else "Select all"
        else:
            lblCount.Text = "{} of {} items".format(shown, len(all_items))

    def _passes(it):
        for gi, group in enumerate(fgroups):
            hit = False
            for bi, pair in enumerate(group):
                box = fboxes[(gi, bi)]
                if box.IsChecked and pair[1](it._value):
                    hit = True
                    break
            if not hit:
                return False
        return True

    def _refresh():
        q = txtFilter.Text.strip().lower()
        base = [it for it in all_items if _passes(it)] if fgroups else all_items
        if not q:
            state['filtered'] = base
        else:
            state['filtered'] = [it for it in base if q in it.Name.lower()]
        grid.ItemsSource = _to_col(state['filtered'])
        _update_count()

    fboxes = {}
    for gi, group in enumerate(fgroups):
        for bi, _pair in enumerate(group):
            fboxes[(gi, bi)] = win.FindName('flt%d_%d' % (gi, bi))

    _refresh()

    def on_filter(s, e):
        _refresh()

    def on_toggle(s, e):
        _refresh()

    for box in fboxes.values():
        box.Click += RoutedEventHandler(on_toggle)

    def on_all(s, e):
        if not multiselect:
            grid.SelectAll()
            return
        turn_on = not (state['filtered'] and
                       all(it.Checked for it in state['filtered']))
        for it in state['filtered']:
            it.Checked = turn_on
        _refresh()   # rebinds the rows so the ticks redraw

    def on_check(s, e):
        # CheckBox.ClickEvent, not Checked/Unchecked: those also fire while WPF
        # renders virtualized rows, which would miscount on scroll.
        _update_count()

    def on_ok(s, e):
        if multiselect:
            picked = [it._value for it in all_items if it.Checked]
            if not picked:
                return
            state['result'] = picked
        else:
            sel = grid.SelectedItem
            if sel is None:
                return
            state['result'] = sel._value
        win.Close()

    def on_dblclick(s, e):
        # A shortcut for the single-value case only. With ticks on screen,
        # double-click meaning "take just this one" would contradict them.
        if multiselect:
            return
        sel = grid.SelectedItem
        if sel is None:
            return
        state['result'] = sel._value
        win.Close()

    txtFilter.TextChanged   += on_filter
    btnAll.Click            += on_all
    btnOK.Click             += on_ok
    btnCancel.Click         += lambda s, e: win.Close()
    grid.MouseDoubleClick   += on_dblclick
    if multiselect:
        grid.AddHandler(_CheckBox.ClickEvent, RoutedEventHandler(on_check), True)

    txtFilter.Focus()
    win.ShowDialog()
    return state['result']


def alert(message, title="Alert", width=440, height=None, context=""):
    """Show a /slantis alert dialog."""
    win = parse(title, "", _ALERT_BODY, _ALERT_FOOTER, width, height, context)
    win.FindName("lblMsg").Text = message
    win.FindName("btnOK").Click += lambda s, e: win.Close()
    win.ShowDialog()


_INPUT_BODY = """
  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>
    <TextBlock x:Name="lblPrompt" Grid.Row="0" TextWrapping="Wrap"
               FontSize="13" Foreground="#202022" Margin="0,0,0,12"/>
    <TextBox x:Name="txtInput" Grid.Row="1" Height="34"/>
  </Grid>
"""

_INPUT_FOOTER = """
  <Grid>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnOK"     Content="OK"     Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
      <Button x:Name="btnCancel" Content="Cancel" Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""


def ask_for_string(prompt="", title="Input", default="",
                   width=440, height=None, context=""):
    """Show a /slantis text-input dialog. Returns string or None on cancel."""
    win = parse(title, "", _INPUT_BODY, _INPUT_FOOTER, width, height, context)
    win.FindName("lblPrompt").Text = prompt
    txt = win.FindName("txtInput")
    txt.Text = default
    txt.SelectAll()
    state = {'result': None}

    def on_ok(s, e):
        state['result'] = txt.Text
        win.Close()

    win.FindName("btnOK").Click     += on_ok
    win.FindName("btnCancel").Click += lambda s, e: win.Close()
    txt.Focus()
    win.ShowDialog()
    return state['result']


def confirm(message, title="Confirm", yes_text="Continue",
            width=500, height=None, context=""):
    """Show a /slantis confirmation dialog. Returns True if confirmed."""
    footer = _CONFIRM_FOOTER.replace("__YES__", _xml_esc(yes_text))
    win = parse(title, "", _ALERT_BODY, footer, width, height, context)
    win.FindName("lblMsg").Text = message
    state = {'ok': False}

    def on_yes(s, e):
        state['ok'] = True
        win.Close()

    win.FindName("btnYes").Click += on_yes
    win.FindName("btnNo").Click  += lambda s, e: win.Close()
    win.ShowDialog()
    return state['ok']


# -- Report dialog -----------------------------------------------------------

_REPORT_BODY = """
  <Grid>
    <ScrollViewer VerticalScrollBarVisibility="Auto"
                  HorizontalScrollBarVisibility="Auto">
      <TextBox x:Name="lblReport" IsReadOnly="True" AcceptsReturn="True"
               BorderThickness="0" Background="Transparent"
               Foreground="#202022" FontSize="12.5"
               TextWrapping="NoWrap" IsReadOnlyCaretVisible="False"/>
    </ScrollViewer>
  </Grid>
"""

_REPORT_FOOTER = """
  <Grid>
    <TextBlock x:Name="lblSummary" VerticalAlignment="Center"
               FontSize="13" Foreground="#77736C"/>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
      <Button x:Name="btnOK" Content="OK" Style="{StaticResource BtnPrimary}"/>
    </StackPanel>
  </Grid>
"""


def show_report(text, title="Report", subtitle="", summary="",
                width=640, height=520, context=""):
    """Show a /slantis scrollable report dialog.

    Args:
        text:     Multi-line report body (newlines preserved, selectable/copyable).
        subtitle: Header subtitle line.
        summary:  Short one-line tally rendered in the footer (e.g. "OK 3  WARN 1").
    """
    win = parse(title, subtitle, _REPORT_BODY, _REPORT_FOOTER, width, height, context)
    win.FindName("lblReport").Text = text
    lbl_sum = win.FindName("lblSummary")
    if lbl_sum is not None:
        lbl_sum.Text = summary or ""
    win.FindName("btnOK").Click += lambda s, e: win.Close()
    win.ShowDialog()


# -- Results table -----------------------------------------------------------
# show_report() prints a text block; an audit that produces COLUMNS deserves the
# same DataGrid every other tool uses -- and it must live here, not in each tool,
# or the fourth audit re-invents the grid a fourth time. This is the surface the
# tools that used to dump into the pyRevit output window land on.

_TABLE_BODY = """
  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
    </Grid.RowDefinitions>
    <TextBox x:Name="txtFilter" Grid.Row="0" Height="34" Margin="0,0,0,12"
             ToolTip="Type to filter"/>
    <Border Grid.Row="1" CornerRadius="8" Background="#FFFFFF"
            BorderBrush="#ECE9E4" BorderThickness="1" ClipToBounds="True">
      <DataGrid x:Name="grid" AutoGenerateColumns="False"
                CanUserAddRows="False" CanUserDeleteRows="False"
                CanUserResizeRows="False" CanUserSortColumns="True">
        <DataGrid.Columns>
__COLS__
        </DataGrid.Columns>
      </DataGrid>
    </Border>
  </Grid>
"""

_TABLE_COL = """
          <DataGridTextColumn Header="__H__" Width="__W__"
                              Binding="{Binding __B__}" IsReadOnly="True">
            <DataGridTextColumn.ElementStyle>
              <Style TargetType="TextBlock">
                <Setter Property="VerticalAlignment" Value="Center"/>
                <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
              </Style>
            </DataGridTextColumn.ElementStyle>
          </DataGridTextColumn>
"""

_TABLE_FOOTER = """
  <Grid>
    <TextBlock x:Name="lblSummary" VerticalAlignment="Center"
               FontSize="11.5" Foreground="#A6A199"/>
    <StackPanel HorizontalAlignment="Right" Orientation="Horizontal">
__EXPORT__
__ACTION__
      <Button x:Name="btnClose" Content="Close" Style="{StaticResource BtnGhost}"/>
    </StackPanel>
  </Grid>
"""

_TABLE_EXPORT = """
      <Button x:Name="btnExport" Content="Export CSV"
              Style="{StaticResource BtnGhost}" Margin="0,0,8,0"/>
"""

_TABLE_ACTION = """
      <Button x:Name="btnAction" Content="__TXT__"
              Style="{StaticResource BtnPrimary}" Margin="0,0,8,0"/>
"""


def _csv_cell(v):
    """One CSV field, Excel-safe: quotes doubled and the whole field quoted
    when it carries a comma, a quote or a newline."""
    t = u'' if v is None else u'{}'.format(v)
    for bad in (u',', u'"', chr(10), chr(13)):
        if bad in t:
            return u'"' + t.replace(u'"', u'""') + u'"'
    return t


class _TableRow(object):
    """One row: the cells as C0..Cn (what the XAML binds) plus the original
    tuple, which is what the caller gets back from the action.

    Numbers are kept as numbers, not str()'d: the DataGrid sorts by the bound
    value, so a stringified count column would sort 10 before 9.
    """

    def __init__(self, values):
        self._values = values
        for i, v in enumerate(values):
            if isinstance(v, (int, long, float)):
                setattr(self, 'C%d' % i, v)
            else:
                setattr(self, 'C%d' % i, u'' if v is None else u'{}'.format(v))

    def _haystack(self):
        return u' '.join(u'{}'.format(getattr(self, 'C%d' % i, u''))
                         for i in range(len(self._values))).lower()


def show_table(rows, columns, title, subtitle="", summary="", width=760,
               height=520, context="", action_text="", export=True):
    """Show a /slantis results table (filterable, sortable DataGrid).

    Args:
        rows:        list of sequences, one per row (cells are str()'d for display).
        columns:     list of headers, or of (header, width) pairs. Width is a
                     WPF value: "*", "2*" or a pixel number. Default "*".
        summary:     one-line tally for the footer (e.g. "12 rows  |  3 MIXED").
        action_text: when given, adds a primary button next to Close; the return
                     value is then the list of SELECTED rows (as passed in), or
                     None if the window was just closed. Use it for
                     "Select in Revit"-style follow-ups.
        export:      on by default: an "Export CSV" button that writes what is
                     ON SCREEN (the filter and the sort applied, headers
                     included) to a file the user picks. Pass export=False for
                     a table that has no business leaving Revit.

    Returns:
        List of selected rows if the action button was clicked, else None.
    """
    from System.Collections.ObjectModel import ObservableCollection

    cols = []
    headers = []
    for i, c in enumerate(columns):
        if isinstance(c, (tuple, list)):
            header, w = c[0], c[1]
        else:
            header, w = c, "*"
        headers.append(header)
        cols.append(_TABLE_COL
                    .replace("__H__", _xml_esc(u'{}'.format(header)))
                    .replace("__W__", _xml_esc(u'{}'.format(w)))
                    .replace("__B__", 'C%d' % i))

    body = _TABLE_BODY.replace("__COLS__", ''.join(cols))
    footer = _TABLE_FOOTER.replace(
        "__ACTION__",
        _TABLE_ACTION.replace("__TXT__", _xml_esc(action_text)) if action_text else "")
    footer = footer.replace("__EXPORT__", _TABLE_EXPORT if export else "")

    win = parse(title, subtitle, body, footer, width, height, context)
    grid = win.FindName("grid")
    txtFilter = win.FindName("txtFilter")
    lblSummary = win.FindName("lblSummary")
    btnExport = win.FindName("btnExport")

    all_rows = [_TableRow(r) for r in rows]
    state = {'result': None}

    def _to_col(lst):
        c = ObservableCollection[object]()
        for it in lst:
            c.Add(it)
        return c

    def _refresh(s=None, e=None):
        q = txtFilter.Text.strip().lower()
        shown = all_rows if not q else [r for r in all_rows if q in r._haystack()]
        grid.ItemsSource = _to_col(shown)
        base = summary or u'{} rows'.format(len(all_rows))
        lblSummary.Text = (base if len(shown) == len(all_rows)
                           else u'{}   |   {} shown'.format(base, len(shown)))

    _refresh()

    def on_action(s, e):
        state['result'] = [r._values for r in grid.SelectedItems]
        win.Close()

    def on_export(s, e):
        # Microsoft.Win32, NOT System.Windows.Forms: PresentationFramework is
        # already loaded here, and the netcore engine that pyRevit uses from
        # Revit 2025 does not carry WinForms (same lesson as the System.Uri
        # import that killed every tool on 2026-09-10).
        from Microsoft.Win32 import SaveFileDialog
        dlg = SaveFileDialog()
        dlg.Filter = "CSV file (*.csv)|*.csv|All files (*.*)|*.*"
        dlg.DefaultExt = ".csv"
        dlg.AddExtension = True
        dlg.FileName = (re.sub(r'[^\w\-. ]', '_', u'{}'.format(title)).strip()
                        or 'report')
        if not dlg.ShowDialog():
            return
        # What is ON SCREEN: grid.Items carries the filter and the sort, so the
        # file matches what the user is looking at, not the untouched input.
        shown = [r for r in grid.Items]
        lines = [u','.join([_csv_cell(h) for h in headers])]
        for r in shown:
            lines.append(u','.join(
                [_csv_cell(getattr(r, 'C%d' % i, u''))
                 for i in range(len(headers))]))
        # BOM + CRLF so Excel opens it with the accents and the rows right.
        eol = chr(13) + chr(10)
        blob = (unichr(65279) + eol.join(lines) + eol).encode('utf-8')
        fh = open(dlg.FileName, 'wb')
        try:
            fh.write(blob)
        finally:
            fh.close()
        lblSummary.Text = u'Exported {} row(s) to {}'.format(
            len(shown), os.path.basename(dlg.FileName))

    txtFilter.TextChanged += _refresh
    win.FindName("btnClose").Click += lambda s, e: win.Close()
    if action_text:
        win.FindName("btnAction").Click += on_action
    if btnExport is not None:
        btnExport.Click += RoutedEventHandler(on_export)

    win.ShowDialog()
    return state['result']


# -- Progress bar ------------------------------------------------------------

class ProgressBar(object):
    """Top-anchored pyRevit progress bar.

    Plain pass-through to ``pyrevit.forms.ProgressBar``. The window anchors
    to the TOP edge of the Revit window (pyRevit default). Kept as a class
    wrapper so callers continue to use ``ui.ProgressBar(...)`` as before.

    Usage (identical to forms.ProgressBar):

        from slantisui import ui

        with ui.ProgressBar(title="My Tool", cancellable=True) as pb:
            for i, item in enumerate(items):
                if pb.cancelled:
                    break
                # pyRevit re-formats the title on every tick, so any brace that
                # comes from the model has to be escaped or the tool dies mid-run.
                name = (item.Name or u"").replace(u"{", u"{{").replace(u"}", u"}}")
                pb.title = u"My Tool - {}/{} - {}".format(i+1, n, name)
                # ... process ...
                pb.update_progress(i + 1, n)
    """

    def __new__(cls, *args, **kwargs):
        from pyrevit import forms
        return forms.ProgressBar(*args, **kwargs)
