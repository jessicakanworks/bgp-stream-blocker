# tvfilter

Time-windowed, BGP-based IP blocking for live sports streams.

`tvfilter` is an [ExaBGP](https://github.com/Exa-Networks/exabgp) process script. While a match is being played (according to an iCal fixture calendar), it fetches a list of IP addresses from a remote source and announces each one as a `/32` (IPv4) or `/128` (IPv6) BGP route. When the match ends, the routes are withdrawn. Your routers use those routes to divert or discard traffic toward the listed addresses, which are typically illegal streaming servers.

---

## Table of contents

- [How it works](#how-it-works)
- [Features](#features)
- [Repository layout](#repository-layout)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [ExaBGP setup](#exabgp-setup)
- [IP list format](#ip-list-format)
- [Safety filters](#safety-filters)
- [Calendar behaviour](#calendar-behaviour)
- [Files and logging](#files-and-logging)
- [Testing](#testing)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## How it works

```
 ┌─────────────┐   every 30 s   ┌──────────────────────┐
 │ iCal feed   │───────────────▶│                      │
 │ (fixtures)  │  match live?   │                      │
 └─────────────┘                │                      │   announce / withdraw
                                │      tvfilter.py     │──────────────────────┐
 ┌─────────────┐   only during  │                      │      (stdout)        │
 │ S3 / HTTP   │───────────────▶│  fetch → filter →    │                      ▼
 │ IP list     │   a match      │  diff → emit         │               ┌────────────┐   BGP    ┌─────────┐
 └─────────────┘                └──────────────────────┘               │  ExaBGP    │─────────▶│ Routers │
                                                                       └────────────┘          └─────────┘
```

Each cycle (every 30 seconds):

1. **Check the calendar.** The `.ics` file is refreshed at most once per hour. If the current time falls inside any `VEVENT`, a block is active.
2. **Fetch the IP list** (only if a block is active). The list is downloaded from the configured endpoint. If no block is active, the desired list is empty.
3. **Filter** the list through the [safety filters](#safety-filters).
4. **Diff** against the routes currently announced, using a sorted merge.
5. **Emit** `announce route ...` and `withdraw route ...` commands on stdout for ExaBGP to turn into BGP updates.

Because the desired list is empty outside match windows, everything is automatically withdrawn when the match ends.

## Features

- Blocking is limited to the duration of matches, driven by any iCal feed.
- Only host routes (`/32`, `/128`) are accepted, so a bad list cannot blackhole a whole network.
- Private, reserved, loopback, link-local, multicast, and unspecified addresses are rejected.
- Your own ISP prefixes are protected by an explicit deny list (`TVFILTER_LOCALISP`).
- A hard cap on the number of prefixes (`TVFILTER_MAXPREFIXES`).
- Incremental updates: only changes are announced or withdrawn, not the whole table.
- Configuration lives in a `.env` file, so credentials stay out of the source.
- Every version of the downloaded list is archived on disk, with a timestamped symlink for each change.
- If the list cannot be fetched, the routes already announced are kept and the fetch is retried after 30 seconds.
- Both downloads have a timeout, and a broken calendar, an unusable event, or a failed archive write is logged and survived rather than killing the process.

## Repository layout

| File | Purpose |
|---|---|
| `tvfilter.py` | The ExaBGP process script. |
| `.env.example` | Annotated configuration template. Copy to `.env`. |
| `exabgp.conf` | Working ExaBGP example, with both address families enabled. |
| `.gitignore` | Keeps the real `.env` and runtime state out of version control. |

## Requirements

- Python 3.6 or newer
- [ExaBGP](https://github.com/Exa-Networks/exabgp) (this script is run as one of its API processes)
- Python packages: `requests`, `icalendar`
- A BGP-speaking router configured to act on the announced routes (see [ExaBGP setup](#exabgp-setup))

```bash
pip install requests icalendar
```

No package is needed to read `.env` — the script parses it itself.

## Installation

```bash
# 1. Create the working directory
sudo mkdir -p /opt/tvfilter

# 2. Install the script
sudo cp tvfilter.py /opt/tvfilter/tvfilter.py
sudo chmod +x /opt/tvfilter/tvfilter.py

# 3. Configure it
sudo cp .env.example /opt/tvfilter/.env
sudo nano /opt/tvfilter/.env          # at minimum: the list source and TVFILTER_LOCALISP

# 4. Make sure the ExaBGP user can write to the directory, and only it can read .env
sudo chown -R exabgp: /opt/tvfilter
sudo chmod 600 /opt/tvfilter/.env
```

The working directory holds the cached calendar and the archived IP lists (see [Files and logging](#files-and-logging)).

## Configuration

Settings come from environment variables. At startup `tvfilter.py` reads a `.env` file sitting next to it; anything already set in the real environment wins, so systemd or ExaBGP can override the file. Every variable is optional and falls back to a default in the script.

```bash
cp .env.example .env
chmod 600 .env          # it may hold credentials
```

| Variable | Default | Description |
|---|---|---|
| `TVFILTER_ENDPOINT` | `https://s3.us-west-2.amazonaws.com` | Base URL of the IP list source. |
| `TVFILTER_RESOURCE` | `/somes3ressource` | Path of the list object. |
| `TVFILTER_ACCESS_KEY_ID` | *(empty)* | Public key identifier (`AKIA…`), sent in the `Authorization` header. |
| `TVFILTER_SECRET_ACCESS_KEY` | *(empty)* | Secret used to sign the request. **Leave both empty for a public object** — the request is then sent unsigned. |
| `TVFILTER_CALENDAR` | a public EPL feed (see below) | URL of the iCal feed. Set to an empty value to drop the calendar and block **permanently**. |
| `TVFILTER_LOCALISP` | documentation ranges | Comma-separated list of **your own** customer and infrastructure prefixes. Any listed address overlapping one of these is dropped. **Replace these before deploying.** |
| `TVFILTER_MAXPREFIXES` | `2000` | Maximum number of prefixes to announce. |
| `TVFILTER_WORKDIR` | `/opt/tvfilter` | Where the cached calendar and the archived lists are kept. Must be writable by the ExaBGP user. |
| `TVFILTER_CALPATH` | `$TVFILTER_WORKDIR/calendar.ics` | Override only to put the calendar somewhere else. |
| `TVFILTER_TIMEOUT` | `20` | HTTP timeout in seconds, for both downloads. |
| `TVFILTER_ENV` | `<script directory>/.env` | Path to the `.env` file itself. |
| `DEBUG` | unset | If set to any value, log level is `DEBUG` instead of `INFO`. |

The default `TVFILTER_CALENDAR` is a public English Premier League fixture feed on `ics.ecal.com`, so out of the box the script contacts a third party once an hour. Point it at your own feed if that is not what you want.

### Credentials

`TVFILTER_ACCESS_KEY_ID` is the public identifier and `TVFILTER_SECRET_ACCESS_KEY` is the secret. When the secret is empty the request is sent unsigned, which is what you want for a public object. When it is set, the request is signed with AWS Signature Version 2 — read [Known limitations](#known-limitations) before relying on that.

Keep `.env` out of version control; the shipped `.gitignore` already excludes it.

### `.env` format

One `KEY=VALUE` per line. Blank lines and lines starting with `#` are ignored, surrounding whitespace is trimmed, and a single pair of matching quotes around a value is stripped. There is no variable interpolation, no multi-line value, and no `export` prefix — the file is read by the script, not sourced by a shell.

## ExaBGP setup

The repository ships a working example in `exabgp.conf`; use it as your starting point rather than the excerpt below. The essentials:

```
process tvfilter {
    run python3 /opt/tvfilter/tvfilter.py;
    encoder text;
}

neighbor 100.64.0.1 {
    router-id 100.64.0.2;
    local-address 100.64.0.2;
    local-as 64512;
    peer-as 64513;

    family {
        ipv4 unicast;
        ipv6 unicast;
    }

    api {
        processes [ tvfilter ];
    }
}
```

The `family` block is not optional. Without `ipv6 unicast`, the `/128` announcements below are silently never sent.

The script announces routes like this:

```
announce route 45.83.64.10/32 next-hop self
announce route 2a0b:f4c0:16c::5/128 next-hop ::1
```

Make sure your routers know what to do with them. Common approaches:

- Import the routes into a policy that sets a discard or sinkhole next-hop.
- Match on a dedicated BGP community and apply a blackhole or redirect policy.
- Restrict acceptance to `/32` and `/128` routes from the ExaBGP session only.

Always apply a max-prefix limit and a prefix filter on the router side as well. The script's own safety filters should be a second line of defence, not the only one.

## IP list format

The remote resource is a plain text file with one address or network per line:

```
45.83.64.10
45.83.64.11/32
2a0b:f4c0:16c::5/128
```

Blank lines are ignored, surrounding whitespace is trimmed, and both LF and CRLF line endings work. Invalid lines are logged and skipped. Duplicates are removed and the result is sorted. There is no comment syntax: every non-blank line must be an address or a network.

> **Do not build a test list out of documentation ranges.** Python's `ipaddress` module reports `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, `2001:db8::/32` (RFC 5737 / RFC 3849) and `198.18.0.0/15` (RFC 2544) as *private*, so the [safety filters](#safety-filters) drop every one of them and you will get an empty announcement set with a wall of warnings. Use real public addresses in test lists. The addresses in this README are illustrative only.

## Safety filters

Entries are discarded, with a warning in the log, if they:

| Rule | Rejected because |
|---|---|
| Not a valid IP or network | Parse error (logged at `ERROR`) |
| Multicast | Not a unicast destination |
| Private | RFC 1918 / ULA space, **plus the documentation and benchmarking ranges** |
| Unspecified | `0.0.0.0` or `::` |
| Reserved | Reserved address space |
| Loopback | `127.0.0.0/8`, `::1` |
| Link-local | `169.254.0.0/16`, `fe80::/10` |
| IPv4 prefix shorter than `/32` | Only host routes allowed |
| IPv6 prefix shorter than `/128` | Only host routes allowed |
| Overlaps `TVFILTER_LOCALISP` | Protects your own address space |

After filtering, if more than `TVFILTER_MAXPREFIXES` entries remain, the list is truncated to that number. The kept entries are the first ones in sorted (string) order, so the truncation is arbitrary. Treat the cap as a safety net, not a priority mechanism.

## Calendar behaviour

- The calendar is downloaded when the cached copy is older than one hour. A non-200 response is treated as a failure and the cache is left alone.
- A block is active when the current time is between an event's start and end.
- When `TVFILTER_CALENDAR` is empty, the block is **always** active.
- When no block is active, the log shows `No blocking operation at the moment` and all routes are withdrawn.
- The summary of the active event is logged as `Blocage en cours: <summary>`.
- Events the parser cannot use — all-day events, events with a `DURATION` instead of a `DTEND`, anything missing `DTSTART` or `DTEND` — are skipped with a warning. Other events in the same feed are still honoured.
- A calendar that cannot be downloaded or parsed means no block, so everything is withdrawn.

Any iCal feed works, for example a league fixture calendar or one you maintain yourself.

## Files and logging

### Files in `$TVFILTER_WORKDIR`

| File | Purpose |
|---|---|
| `calendar.ics` | Cached copy of the fixture calendar |
| `file-<sha1>.txt` | Archived copy of each distinct IP list, named by content hash |
| `file-<YYYYMMDDHHMMSS>.txt` | Symlink created each time the list changes, pointing to the matching archive |

Archives and symlinks share the `file-*.txt` pattern; the timestamped names are the symlinks. Nothing here is cleaned up automatically — set up a cron job to prune old entries.

If the directory is missing or not writable, archiving is skipped with a `could not archive the list` warning and announcements continue normally.

### Logging

- Logs go to **stderr** and are normally captured by ExaBGP's log.
- **stdout is reserved for ExaBGP commands.** Never print anything else to stdout.
- Format: `>>> <time> :: tvfilter :: <LEVEL> :: <message>`

## Testing

Run the script by hand to see what it would announce, without a BGP session:

```bash
DEBUG=1 /opt/tvfilter/tvfilter.py
```

Announcements appear on stdout and logs on stderr.

To exercise it outside a match window, point it at a local list over a throwaway `.env`:

```bash
printf '45.83.64.10\n45.83.64.11\n2a0b:f4c0:16c::5\n' > /tmp/list/list.txt
( cd /tmp/list && python3 -m http.server 8080 & )

cat > /tmp/tvfilter.env <<'EOF'
TVFILTER_ENDPOINT=http://127.0.0.1:8080
TVFILTER_RESOURCE=/list.txt
TVFILTER_CALENDAR=
TVFILTER_WORKDIR=/tmp/tvfilter
TVFILTER_LOCALISP=203.0.113.0/24
EOF

mkdir -p /tmp/tvfilter
TVFILTER_ENV=/tmp/tvfilter.env DEBUG=1 ./tvfilter.py
```

An empty `TVFILTER_CALENDAR` makes the block permanent, so you see announcements immediately. Edit `list.txt` and watch the next cycle emit only the difference.

Before going live, verify on your router that:

1. The announced `/32` routes are accepted and your policy is applied.
2. Your own prefixes and key infrastructure (DNS, mail, monitoring) can never be matched.
3. Withdrawals remove the routes when the match ends.

## Known limitations

- **Authentication uses AWS Signature V2**, which buckets created after mid-2020 do not accept. Use a public object, or switch to SigV4 or `boto3` for private buckets.
- **Timezone handling.** Event times are converted using the server's local timezone. Run the server in UTC, or verify the match windows against known fixtures.
- **Calendar download failure** results in all routes being withdrawn, even if a valid cached calendar exists.
- **Archived lists accumulate** indefinitely.
- **Next-hop values** (`self` for IPv4, `::1` for IPv6) are fixed in the code. Adjust `announce()` to match your routers' policy.
- **The `.env` parser is deliberately minimal** — no interpolation, no multi-line values, no `export`. See [`.env` format](#env-format).
- **No persistent state across restarts.** After a restart the script re-announces the current list from an empty table.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Nothing is ever announced | No active event in the calendar, calendar download failing, or all list entries rejected by the filters (check the warnings). |
| Every entry is rejected as `private address` | The list is built from documentation ranges. See [IP list format](#ip-list-format). |
| `request returned an error 403` | Wrong credentials, `TVFILTER_SECRET_ACCESS_KEY` set for an object that is public, or a bucket that requires SigV4. |
| `request returned an error 404` | Wrong `TVFILTER_ENDPOINT` or `TVFILTER_RESOURCE`. |
| Changes in `.env` have no effect | The variable is already set in the real environment (it wins), `TVFILTER_ENV` points elsewhere, or the line is malformed. |
| IPv4 works but IPv6 never arrives | `ipv6 unicast` is missing from the neighbor's `family` block. |
| `could not archive the list` | `TVFILTER_WORKDIR` does not exist or is not writable by the ExaBGP user. |
| Routes announced but no effect | Router policy or community mapping is missing, or the session is not established. |
| ExaBGP shows garbage commands | Something wrote to stdout. Keep all diagnostics on the logger (stderr). |
| Blocks start or end at the wrong time | Server timezone issue (see [Known limitations](#known-limitations)). |
| `attempt to add a GVA prefix` | The address overlaps `TVFILTER_LOCALISP`. Intended protection, or a sign the list is too broad. |
| `skipping unusable calendar event` | An all-day or `DURATION`-based event in the feed. Harmless unless it was the event you wanted. |

## License

Add your license here (for example, MIT, Apache-2.0, or "Proprietary, internal use only").
