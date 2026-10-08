# Changelog

All notable changes to Magic Tools are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- New README with a header, a picture of the ribbon and the mascot on each section; the
  images live in `.github/readme/` and stay out of the release ZIP. The policy check takes
  PNG and GIF there, each one checked to be a well-formed image.
- **Cloud Manager** is the new version: one dashboard window that stays open while you
  work in Revit (it no longer closes after each action), with show, hide, tag, style, move
  and delete, and a log of what each action did. Views and clouds owned by another user
  in a workshared model are skipped and named, instead of failing the whole action.
- **Create Type Filter** lists what it left out (elements with no type, categories Revit
  cannot filter) in its own warning block, and counts in the singular ("1 Room").
- **Goodbye Filter** says "Temporary view properties turned off" when you restore a view
  that you put in that mode by hand, and explains why a view without a view template
  cannot have its filters turned off temporarily.
- **Rename Families** leaves codes exactly as typed (W12, HSS-4X4, MTT-A), keeps the "x"
  of a size lower case (36" x 84"), and knows GFCI.
- **Select Same Type** and **Select Same Family**: the pick prompt says that a click adds
  or removes and that Finish confirms (Enter does not confirm in Revit 2025).
- **View Template Manager**: the selected row reads the same with or without focus in the
  dark theme.

### Fixed

- A second click on a tool whose window is already open brings that window to the front
  instead of opening another one. `lib/modeless.py` keeps its registry of open windows in
  the AppDomain, so it survives each click's new engine, and rebuilds the window after a
  pyRevit Reload or in another model. Inspect Element Graphics opens one window per click
  on purpose, to compare two elements side by side.
- Inspect Model Overrides could close Revit if the ribbon was clicked during a scan; the
  ribbon is disabled while it scans. Its counts live only in the footer, so they no longer
  disagree with the subtitle after Refresh.
- Inspect View Overrides shows its strip when you switch views.
- Inspect Element Graphics names the Solid line pattern instead of showing an element Id.
- Print Set Manager: the New Print Set window no longer cuts off the Empty option when the
  set name is long, and closing, New or switching sets with unsaved changes all ask the
  same question: Save, Discard or Cancel.

## [0.2.1] - 2026-10-07

### Changed

- Usage data goes to one endpoint on Supabase instead of two webhooks, with no token. The
  `install` event is queued as soon as you say yes, with no separate registration first.
  Waits between failed sends now go from 1 minute up to an hour. Nothing changes in what
  is sent. See [TELEMETRY.md](TELEMETRY.md).
- The policy check allows that one endpoint. The secrets check still allows the old public
  token, which stays in the git history.

## [0.2.0] - 2026-10-07

### Added

- **Usage data, only if you say yes.** The first click of any tool asks once whether to
  share usage data: a random install ID, the Magic Tools, Revit and pyRevit versions, the
  install channel (git or ZIP), and the name, result and time of each tool run. Nothing
  is asked while Revit loads and nothing is sent before a yes. **Share usage data** at the
  bottom of All Magic Tools turns it off (and deletes the install ID and anything not sent
  yet) or back on; `DO_NOT_TRACK=1` or `MAGIC_TOOLS_TELEMETRY=0` turns it off for a whole
  computer or office. See [TELEMETRY.md](TELEMETRY.md).
- `extension.json` has a `version` (0.2.0), the one the usage data reports. Raise it with
  each release.

### Changed

- The policy check lets `lib/telemetry.py`, and no other file, reach the network, and
  only its two endpoints. The secrets check keeps gitleaks' default rules and allows the
  usage data client's public token (`.github/gitleaks.toml`).

## [0.1.0] - 2026-10-06

First full release: 16 tools and the All Magic Tools window for the Favorites tools.
Select Same Family was already in 0.0.1; the other 15 tools are new.

### Added

- **Tools panel**, always on the ribbon:
  - **All Magic Tools**: a searchable window for the ten Favorites tools, grouped by what
    they are for, with a description of each one before you run it. Star the ones you want
    on the ribbon.
  - **Navigation:** Next Sheet, Parent Sheet, Previous Sheet.
  - **Selection:** Select Same Type, Selection Manager, and Select Same Family (from 0.0.1).
- **Favorites panel**, showing only the tools you starred, grouped as in All Magic Tools:
  - **Actions:** Rename Families.
  - **Analysis:** Inspect Model Overrides, Inspect View Overrides, Inspect Element Graphics.
  - **Annotate:** Cloud Manager.
  - **Navigation:** Find Room.
  - **Sheets:** Print Set Manager.
  - **Views, Templates & Filters:** View Template Manager, Goodbye Filter, Create Type Filter.
- **Stars:** star or unstar any of the ten Favorites tools in All Magic Tools and the
  Favorites panel follows. Five tools start starred: View Template Manager, Goodbye Filter,
  Rename Families, Inspect Element Graphics and Print Set Manager. Stars are saved per
  Windows user. The six Tools-panel tools are always on the ribbon and cannot be starred.
- **Repository checks** on every pull request: compile (Python 2.7 syntax and undefined
  names), policy (no network, processes, dynamic code, native loading, registry or hidden
  payloads; only allowed file types), structure (each tool has its script, four icons, a
  title and a tooltip, and is listed in its `bundle.yaml`; each Favorites tool is also
  listed in `lib/groups.json`), secrets (gitleaks) and language (English only).
- **Release ZIP with a manifest:** each release attaches `magic-tools.zip`, which holds one
  `magic-tools.extension/` folder, and `release-manifest.json`, which lists the SHA-256 of
  every file in the ZIP.

### Changed

- New installs go in `magic-tools.extension` (0.0.1 used `MagicTools.extension`); an
  existing git install keeps its folder and updates in place. Keep only one copy, wherever
  pyRevit loads extensions from: update or delete an older one before installing a new one,
  or every tool shows twice.

[Unreleased]: https://github.com/slantis/magic-tools/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/slantis/magic-tools/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/slantis/magic-tools/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/slantis/magic-tools/compare/v0.0.1...v0.1.0
