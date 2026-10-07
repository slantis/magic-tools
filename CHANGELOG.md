# Changelog

All notable changes to Magic Tools are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
