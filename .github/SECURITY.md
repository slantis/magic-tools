# Security policy

## Reporting a vulnerability

Please do not open a public issue. Report it privately through
[GitHub's private vulnerability reporting](https://github.com/slantis/magic-tools/security/advisories/new).
Include what you found, how to reproduce it, and the Magic Tools version.

We will reply as soon as we can and keep you posted on the fix.

## Supported versions

Only the latest release gets security fixes.

## What the add-in does on the network

Only `lib/telemetry.py` reaches the network, and only after the user opts in to usage
data: see [TELEMETRY.md](../TELEMETRY.md). The checks on every pull request reject network,
process and dynamic code anywhere else in the extension.
