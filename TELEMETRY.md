# Usage data

Magic Tools can send a little usage data to its maintainers, so they can count how many
times it is installed and see which tools people use. **It is off until you say yes.**
This page says exactly what is sent, when, where it goes, and how to turn it off. The code
that does it is [`lib/telemetry.py`](lib/telemetry.py) (the data and the sending) and
[`lib/usage.py`](lib/usage.py) (the question and the switch).

## You are asked once

The first time you click any Magic Tools button, before the tool runs, a window asks
whether you want to share usage data. Nothing is asked while Revit loads.

- **Share usage data**: Magic Tools starts sending the data below.
- **Don't share**, or closing the window: nothing is sent, and you are not asked again.

Before you answer, nothing is recorded, queued or sent. Your answer is saved for your
Windows user and applies to every tool.

## What is sent

| Field | Example | What it is |
|---|---|---|
| `install_id` | `3f2bfb06-b182-4b0a-82eb-b35a52ca7cb6` | A random ID made on your computer when you say yes. It is not derived from anything about you or your computer. |
| `addin_version` | `0.1.0` | The Magic Tools version. |
| `revit_version` | `2025` | The Revit version. |
| `pyrevit_version` | `5.0.1.25181+1416` | The pyRevit version. |
| `channel` | `git` | How Magic Tools was installed: `git` (a clone) or `zip` (a release ZIP). |
| `event_id` | `c83883a4-983b-42bf-b8b0-44d1f6fbc0bd` | A random ID for each event, so an event sent twice is counted once. |
| `event_type` | `tool_run` | `install`, `heartbeat` or `tool_run` (see below). |
| `occurred_at` | `2026-10-06T16:05:00Z` | When it happened, in UTC, to the second. |
| `tool` | `Select Same Family` | Only on `tool_run`: the name of the tool you ran. |
| `result` | `ok` | Only on `tool_run`: `ok`, or `error` if the tool failed. |
| `schema_version` | `1` | The version of this format. |

The installation is registered with `install_id`, `channel` and the three versions. Each
event then carries the fields above. Opening Revit and running one tool sends something
like this:

```json
[
  {
    "event_id": "c83883a4-983b-42bf-b8b0-44d1f6fbc0bd",
    "install_id": "3f2bfb06-b182-4b0a-82eb-b35a52ca7cb6",
    "event_type": "heartbeat",
    "occurred_at": "2026-10-06T16:01:00Z",
    "addin_version": "0.1.0",
    "channel": "git",
    "revit_version": "2025",
    "pyrevit_version": "5.0.1.25181+1416",
    "schema_version": 1
  },
  {
    "event_id": "3076efdc-06a5-44b4-b52c-9ba4d9b73d13",
    "install_id": "3f2bfb06-b182-4b0a-82eb-b35a52ca7cb6",
    "event_type": "tool_run",
    "tool": "Select Same Family",
    "result": "ok",
    "occurred_at": "2026-10-06T16:05:00Z",
    "addin_version": "0.1.0",
    "channel": "git",
    "revit_version": "2025",
    "pyrevit_version": "5.0.1.25181+1416",
    "schema_version": 1
  }
]
```

## What is never sent

- Your name, your Windows user name, your email or your computer's name.
- The names of your files, models, projects, views or sheets, and file paths.
- Anything inside your models.
- Anything you type in a tool.

## Pseudonymous, not anonymous

The install ID is random, but it stays the same from one Revit session to the next, so the
events of one installation can be linked to each other. And, as with any request on the
internet, the server sees the IP address your computer sends it from. That makes the data
pseudonymous rather than anonymous. Turning usage data off deletes the install ID, so
anything sent after you turn it on again cannot be linked to what was sent before.

## When it is sent

- **Registration**, right after you say yes, and again when the Magic Tools, Revit or
  pyRevit version or the install channel changes.
- **`install`**, once, after the first registration the server accepts.
- **`heartbeat`**, at most once per day (UTC), when Revit loads Magic Tools.
- **`tool_run`**, each time you run a tool, when it finishes. A tool with its own window
  that stays open counts once, when the window opens. A tool you start from All Magic Tools
  counts as that tool, not as All Magic Tools too.

Events wait in a queue on your computer and are sent in the background, so Revit never
waits for the network. If the server cannot be reached, they are sent later, with
growing waits between tries (from 30 seconds up to an hour). The queue keeps at most 1000
events and drops the oldest first.

## Where it goes

Over HTTPS to two webhooks on a server the maintainers run (`n8n.srv1888016.hstgr.cloud`),
which store it in their database. It is kept by the maintainers to count installs and tool
use. `lib/telemetry.py` is the only file of Magic Tools allowed to reach the network, and
only those two addresses: the repository checks reject any other.

## Turning it off

- **In Revit:** open **All Magic Tools** and clear **Share usage data** at the bottom of
  the window. Sending stops, and the queue and the install ID are deleted from your
  computer. Tick it again to start over with a new install ID.
- **For one computer or a whole office:** set an environment variable (for example with
  Group Policy, or in Windows' environment variable settings) and restart Revit. Either of
  these turns usage data off and wins over the answer to the question; while it is set,
  nothing is asked or sent, and the switch in All Magic Tools is greyed out:
  - `DO_NOT_TRACK=1` (any value other than `0`, `false`, `no` or `off`), the common switch
    many tools respect.
  - `MAGIC_TOOLS_TELEMETRY=0` (or `off`, `false`, `no`), for Magic Tools only.

## On your computer

Your answer, the install ID and the queue are kept per Windows user in
`%APPDATA%\pyRevit\magic-tools-telemetry\`: `state.json` and `queue.json`, both plain
text you can read. Deleting the folder resets everything, and the next click of a tool
asks again.
