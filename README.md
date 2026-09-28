# Magic Tools

Open source [pyRevit](https://github.com/pyrevitlabs/pyRevit) tools for Autodesk Revit,
made by [/slantis](https://github.com/slantis).

## Tools

| Tool | Panel | What it does |
|---|---|---|
| **Select Same Family** | Selection | Grows the current selection to every element of the same family visible in the active view, whatever its type. Select one window and get every window of that family on the view, in all its sizes. Works for loadable families and for system families (walls, floors, ducts). With nothing selected, it asks you to pick elements first. |

More tools are on the way.

## Install

You need [pyRevit](https://github.com/pyrevitlabs/pyRevit/releases) installed.

### With git (recommended, receives updates)

Using the pyRevit CLI:

```
pyrevit extend ui MagicTools https://github.com/slantis/magic-tools.git --branch=main
```

Then reload pyRevit (or restart Revit). The **Magic-tools** tab appears in the ribbon. To
update later, use pyRevit's **Update** button.

### With a ZIP (no automatic updates)

1. Download `MagicTools.extension.zip` from the latest
   [release](https://github.com/slantis/magic-tools/releases).
2. Unzip it into a folder pyRevit loads custom extensions from, for example
   `%APPDATA%\pyRevit\Extensions`, so you end up with
   `...\Extensions\MagicTools.extension\`.
3. Reload pyRevit.

A ZIP install does not update itself: download a newer release to update.

## What we measure

We count **downloads only**, from GitHub's side: the number of git clones of this
repository and the download count of each release ZIP. A scheduled GitHub Action saves
those numbers once a day. The add-in itself sends nothing.

## License

- Code: [MIT](LICENSE), Copyright (c) 2026 Slantis LLC.
- Icons, documentation and the "Magic Tools" name: [CC BY 4.0](LICENSE-CONTENT).
- DM Sans fonts in `lib/slantisui/fonts/`: [SIL Open Font License 1.1](lib/slantisui/fonts/OFL.txt).
