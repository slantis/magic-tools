## What changes

## Tools touched

## Tested in

- Revit version(s):
- pyRevit version:

## Checklist

- [ ] Tested by hand in Revit (the checks cannot open Revit).
- [ ] Adds no network access, no processes and no usage data fields (only
      `lib/telemetry.py` sends usage data, and `TELEMETRY.md` lists every field).
- [ ] A new tool: its `script.py` runs inside `with usage.tool_run(__file__) as run:`,
      its folder name is added to `KNOWN_TOOLS` in
      `.github/scripts/tests/test_telemetry.py`, and a maintainer has added that name to
      the usage data server's tool whitelist (see "ADDING A TOOL" in `lib/telemetry.py`).
- [ ] Code, comments and UI text are in English.
