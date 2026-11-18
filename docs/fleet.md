# Several Sparks: `dgx-fan-fleet`

`tools/dgx-fan-fleet` sets the [fan profile](usage.md#choose-a-profile) of several
Sparks at once and reads them back, from one of them. It never touches a fan: it
writes one word into each Spark's `/var/lib/dgx-fan-control/profile` and reads
each Spark's `/run/dgx-fan-control/status.json`; the service on each Spark does
the rest within two seconds. If this tool or ssh is down, every Spark keeps the
profile it has.

## Set up

On every Spark: the driver, the service and the profile directory, as in
[installation](installation.md#profiles-and-the-status-file), with the directory
owned by the account that will run this tool. On the Spark you run it from:

```sh
sudo install -m 0755 tools/dgx-fan-fleet /usr/local/bin/dgx-fan-fleet
export DGX_FAN_NODES="spark2 spark3 spark4"   # the other Sparks, as ssh targets
```

The other Sparks are reached over ssh with `BatchMode`: key login from this
Spark must already work, `$HOME/.ssh` must exist (it holds the shared
connections), and on every Spark the profile directory belongs to the ssh login
user, who needs `python3` and `logger`. All Sparks are reached at once, each
attempt bounded. Without `DGX_FAN_NODES` the tool
works on the local Spark only.

## Commands

| Command | What it does |
| --- | --- |
| `dgx-fan-fleet status` | each Spark's profile, state, floor, both fans' RPM, hottest zone, age of the reading |
| `dgx-fan-fleet set PROFILE` | write the profile on every Spark (exit 0: all took it; 1: some did not, each named) |
| `dgx-fan-fleet check PROFILE` | exit 0 only if every Spark runs it, with fresh readings and no guard or error: for scripts, e.g. before a benchmark |
| `dgx-fan-fleet pin PROFILE --why TEXT` | set it and refuse every change (exit 3) until `unpin`; a pin that does not reach every Spark is not kept |
| `dgx-fan-fleet unpin` | allow changes again |
| `dgx-fan-fleet clock set LIMIT` | the GPU clock limit: 2400, 2200, 2000 or 1800 MHz (see below) |

`PROFILE` is one of `stock`, `medium`, `performance`, `max`, or `hold-<RPM>` with
any floor of the service's table (2700, 3600, ... 13500); anything else exits 2
and nothing is written. A reading
older than 10 seconds is shown as stale. A Spark whose service is not running is
read from the kernel directly (cooling device, hwmon and thermal zones, all
world-readable) and shown as stale.

## The GPU clock limit (optional)

The tool does not set clocks itself. `DGX_FAN_CLOCK_TOOL` names a command run as
`TOOL LIMIT [targets...]` that sets the limit on this Spark and each target, and
prints one line a Spark: `<name>: GPU clock ...` when it worked or
`<name>: FAILED: <reason>` (`<name>` is `hostname -s` for this Spark and the
target as given for the others). `DGX_FAN_CLOCK_STATE` names the file on each
Spark in which that command records `{"limit", "at", "ok", "error"}` as JSON;
`status` reports it. Only the four limits above are accepted: never `unlocked`.
Without a clock tool, `clock set` exits 2 and `status` reports no clock.

## For programs (`--json`)

`status --json` prints one object:

```json
{"at": 1700000000.0, "pinned": null, "mixed": false, "profile": "performance",
 "clock": "2200", "clock_mixed": false,
 "nodes": [{"rank": 0, "host": "spark1", "ok": true, "error": null, "stale": false, "age_s": 0.4,
            "status": {"v": 1, "at": 1700000000.0, "profile": "performance", "requested": "performance",
                       "profile_error": null, "state": 0, "floor_rpm": 0, "hottest_c": 44.8, "filtered_c": 44.9,
                       "rpm": [2700, 4050], "zero_rpm_guard": false, "driver": "0.1.4"},
            "gpu_clock": {"limit": "2200", "at": 1700000000.0, "ok": true, "error": null}}]}
```

`profile` is the profile every Spark runs (null when they differ or one does not
answer; `mixed` says which). A Spark whose service is not running is read from
the kernel and its `status` carries `"fallback": true`. `set --json PROFILE` prints
`{"profile", "pinned", "nodes": [{"rank", "host", "ok", "error"}]}` and `clock set --json
LIMIT` the same with `gpu_clock`; the exit codes are those of the plain commands
(0 all, 1 some, 2 refused, 3 pinned). A profile or a limit outside the lists is
refused before anything runs on any Spark.
