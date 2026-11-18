#!/usr/bin/env bash
# dgx-fan-fleet without any real ssh: ssh, logger and mv stood in, four "Sparks" (this one and three ssh targets), each
# with its own directory under DGX_FAN_DIR (profile, status.json; the pin lives in this Spark's).  Checks: set
# refuses what is not a profile (exit 2, nothing run) and what is pinned (exit 3, nothing run); set writes a temporary
# file and renames it, logs through logger, names each Spark; a Spark that is down is named and the others still set
# (exit 1); status merges the four status files, marks a stale one and a mixed set, and reads the kernel directly
# when a daemon is not answering; check fails unless all four run the profile; pin and unpin; nothing secret in the output.
#   bash tests/test-dgx-fan-fleet.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
F="$HERE/../tools/dgx-fan-fleet"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin"
cat > "$T/bin/ssh" <<'EOS'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$LOGDIR/ssh.argv"
while (( $# )); do case $1 in -o) shift 2;; -n) shift;; *) break;; esac; done
target=$1; shift
[[ " ${FAIL_SSH:-} " == *" $target "* ]] && { echo "ssh: connect to host $target: Connection refused" >&2; exit 255; }
FAKE_HOST=$target exec bash -c "$*"
EOS
cat > "$T/bin/logger" <<'EOS'
#!/usr/bin/env bash
printf '%s %s\n' "${FAKE_HOST:-here}" "$*" >> "$LOGDIR/logger.argv"
EOS
cat > "$T/bin/mv" <<'EOS'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$LOGDIR/mv.argv"
exec /bin/mv "$@"
EOS
chmod +x "$T/bin/"*
fails=0
ok()  { echo "ok:   $1"; }
bad() { echo "FAIL: $1"; fails=$((fails + 1)); }
ME=$(hostname -s)
SPARKS=("$ME" w1 w2 w3)
now() { python3 -c 'import time; print(time.time())'; }
# one status.json for a Spark: label profile [age_s] [extra python dict updates]
mkstatus() {
  local label=$1 profile=$2 age=${3:-1} extra=${4:-\{\}}
  mkdir -p "$T/st/$label"
  python3 - "$T/st/$label/status.json" "$profile" "$age" "$extra" <<'EOP'
import json, sys, time
path, profile, age, extra = sys.argv[1], sys.argv[2], float(sys.argv[3]), json.loads(sys.argv[4])
d = {"v": 1, "at": time.time() - age, "profile": profile, "requested": profile, "profile_error": None, "state": 5,
     "floor_rpm": 6300, "hottest_c": 57.4, "filtered_c": 57.1, "rpm": [3620, 5400], "zero_rpm_guard": False, "driver": "0.1.4"}
d.update(extra)
open(path, "w").write(json.dumps(d) + "\n")
EOP
}
reset() { rm -rf "$T/log" "$T/st" "$T/gc"; mkdir -p "$T/log" "$T/st"; touch "$T/log/clock.argv"; touch "$T/log/ssh.argv" "$T/log/logger.argv" "$T/log/mv.argv"; }
run() {
  LOGDIR="$T/log" PATH="$T/bin:$PATH" DGX_FAN_SSH="$T/bin/ssh" DGX_FAN_DIR="$T/st" DGX_FAN_NODES="w1 w2 w3" \
    DGX_FAN_CLOCK_TOOL="$T/bin/gpu-clock" DGX_FAN_CLOCK_STATE_DIR="$T/gc" DGX_FAN_THERMAL_ROOT="$T/thermal" DGX_FAN_HWMON_ROOT="$T/hwmon" DGX_FAN_TEST_SECRET=hunter2 \
    bash "$F" "$@" > "$T/out" 2> "$T/err"
  echo $? > "$T/rc"
}
rc() { cat "$T/rc"; }
profile_of() { cat "$T/st/$1/profile" 2>/dev/null; }
jq_() { python3 -c 'import json, sys
d = json.load(open(sys.argv[1]))
print(eval(sys.argv[2], {"d": d}))' "$T/out" "$1"; }

# a clock tool stood in: logs its arguments, answers one line a Spark in its own words; FAIL_CLOCK names a Spark that fails
cat > "$T/bin/gpu-clock" <<'EOS'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$LOGDIR/clock.argv"
limit=$1; shift
rc=0
for l in "$(hostname -s)" "$@"; do
  if [[ " ${FAIL_CLOCK:-} " == *" $l "* ]]; then echo "$l: FAILED: nvidia-smi -lgc 0,$limit said: Not Supported"; rc=1
  else echo "$l: GPU clock $limit (applied)"; fi
done
exit $rc
EOS
chmod +x "$T/bin/gpu-clock"
# a Spark's clock record, as a clock tool writes it
mkclock() { mkdir -p "$T/gc/$1"; printf '{"limit": "%s", "at": %s, "ok": true, "error": null}\n' "$2" "$(now)" > "$T/gc/$1/state"; }

# a fake kernel for the fallback
mkdir -p "$T/thermal/thermal_zone0" "$T/thermal/cooling_device0" "$T/hwmon/hwmon2"
echo 61000 > "$T/thermal/thermal_zone0/temp"; echo dgx_ec_fan_floor > "$T/thermal/cooling_device0/type"
echo 4 > "$T/thermal/cooling_device0/cur_state"; echo 12 > "$T/thermal/cooling_device0/max_state"
echo dgx_ec_fan > "$T/hwmon/hwmon2/name"; echo 5400 > "$T/hwmon/hwmon2/fan1_input"; echo 7000 > "$T/hwmon/hwmon2/fan2_input"

reset; run
[[ $(rc) == 2 ]] && ok "no command: usage, exit 2" || bad "no command: rc $(rc)"

for p in quiet hold-5000 "" Max "max;x" "../max" hold-; do
  reset; run set "$p"
  [[ $(rc) == 2 && ! -s $T/log/ssh.argv && ! -e $T/st/$ME/profile ]] || { bad "set '$p': rc $(rc), $(cat "$T/log/ssh.argv")"; continue; }
done
reset; run set quiet
grep -q 'stock' "$T/err" && ok "an unknown profile: exit 2, the profiles named, nothing run" || bad "unknown: $(cat "$T/err")"

reset; run set medium
[[ $(rc) == 0 ]] && ok "set medium on four Sparks: exit 0" || bad "set: rc $(rc): $(cat "$T/out") $(cat "$T/err")"
n=0; for s in "${SPARKS[@]}"; do [[ $(profile_of "$s") == medium ]] && n=$((n + 1)); done
[[ $n == 4 ]] && ok "each Spark's profile file says medium" || bad "files: $n of 4"
[[ $(grep -c 'profile.tmp\|profile.new' "$T/log/mv.argv") == 4 && -z $(ls "$T"/st/*/profile.* 2>/dev/null) ]] &&
  ok "written to a temporary file and renamed (no leftover)" || bad "mv: $(cat "$T/log/mv.argv"); $(ls "$T"/st/*)"
[[ $(grep -c '^[^ ]* -t dgx-fan-fleet' "$T/log/logger.argv") -ge 4 ]] && ok "logged through logger -t dgx-fan-fleet on each Spark" || bad "logger: $(cat "$T/log/logger.argv")"
[[ $(grep -c . "$T/log/ssh.argv") == 3 ]] && ok "three ssh calls, this Spark without ssh" || bad "ssh: $(cat "$T/log/ssh.argv")"
for s in "${SPARKS[@]}"; do grep -q "^$s: " "$T/out" || bad "no line for $s: $(cat "$T/out")"; done
[[ $(grep -c . "$T/out") == 4 ]] && ok "one line a Spark" || bad "lines: $(cat "$T/out")"

reset; run set --json hold-6300
if [[ $(rc) == 0 ]] && [[ $(jq_ 'd["profile"]') == hold-6300 && $(jq_ 'len(d["nodes"])') == 4 && $(jq_ '[n["rank"] for n in d["nodes"]]') == "[0, 1, 2, 3]" && $(jq_ 'all(n["ok"] for n in d["nodes"])') == True ]]; then
  ok "set --json: profile, four nodes by rank, each ok"; else bad "json: rc $(rc): $(cat "$T/out")"; fi

# a Spark that is down: named, the others still set
reset; FAIL_SSH=w2 run set max
[[ $(rc) == 1 && $(profile_of w1) == max && $(profile_of w3) == max && $(profile_of "$ME") == max && -z $(profile_of w2) ]] &&
  grep -q '^w2: FAILED' "$T/out" && ok "a Spark down: named, exit 1, the other three set" || bad "down: rc $(rc): $(cat "$T/out")"
reset; FAIL_SSH=w2 run set --json max
[[ $(jq_ '[(n["rank"], n["ok"]) for n in d["nodes"]]') == "[(0, True), (1, True), (2, False), (3, True)]" && $(jq_ 'd["nodes"][2]["error"] is not None') == True ]] &&
  ok "set --json names the failed rank with an error" || bad "json down: $(cat "$T/out")"

# the pin: set refuses, nothing is run
reset; mkdir -p "$T/st/$ME"; printf 'timing window 3\n' > "$T/st/$ME/pin"
run set max
[[ $(rc) == 3 && ! -s $T/log/ssh.argv && -z $(profile_of "$ME") ]] && grep -q 'timing window 3' "$T/err" &&
  ok "pinned: set refused (exit 3), the reason shown, nothing run" || bad "pinned: rc $(rc): $(cat "$T/err")"
run set --json max
[[ $(rc) == 3 && $(jq_ 'd["pinned"]') == "timing window 3" ]] && ok "pinned, --json: exit 3 and the reason in the answer" || bad "pinned json: rc $(rc): $(cat "$T/out")"

# status
reset
mkstatus "$ME" medium; mkstatus w1 medium; mkstatus w2 medium; mkstatus w3 medium
run status --json
if [[ $(rc) == 0 && $(jq_ 'len(d["nodes"])') == 4 && $(jq_ 'd["mixed"]') == False && $(jq_ 'd["profile"]') == medium && $(jq_ 'd["pinned"]') == None &&
      $(jq_ 'd["nodes"][1]["status"]["rpm"]') == "[3620, 5400]" && $(jq_ 'any(n["stale"] for n in d["nodes"])') == False ]]; then
  ok "status --json: four nodes merged, none stale, not mixed, the profile named"; else bad "status: rc $(rc): $(cat "$T/out") $(cat "$T/err")"; fi
[[ $(jq_ 'all(0 <= n["age_s"] < 30 for n in d["nodes"])') == True ]] && ok "age of each reading, from the Spark's own clock" || bad "ages: $(cat "$T/out")"

mkstatus w2 performance
run status --json
[[ $(jq_ 'd["mixed"]') == True && $(jq_ 'd["profile"]') == None ]] && ok "one Spark on another profile: mixed" || bad "mixed: $(cat "$T/out")"

mkstatus w2 medium 100
run status --json
[[ $(jq_ 'd["nodes"][2]["stale"]') == True && $(jq_ 'd["nodes"][1]["stale"]') == False ]] &&
  ok "a reading older than 10 s: marked stale" || bad "stale: $(cat "$T/out")"

reset; mkstatus "$ME" medium; mkstatus w1 medium; mkstatus w3 medium
mkdir -p "$T/st/w2"; printf 'medium\n' > "$T/st/w2/profile"
run status --json
if [[ $(jq_ 'd["nodes"][2]["stale"]') == True && $(jq_ 'd["nodes"][2]["status"]["fallback"]') == True &&
      $(jq_ 'd["nodes"][2]["status"]["state"]') == 4 && $(jq_ 'd["nodes"][2]["status"]["rpm"]') == "[5400, 7000]" &&
      $(jq_ 'd["nodes"][2]["status"]["hottest_c"]') == 61.0 && $(jq_ 'd["nodes"][2]["status"]["requested"]') == medium ]]; then
  ok "no status file (a daemon not answering): read from the kernel directly, marked stale and as a fallback"
else bad "fallback: $(cat "$T/out")"; fi

reset; FAIL_SSH=w3 run status --json
[[ $(jq_ 'd["nodes"][3]["ok"]') == False && $(jq_ 'd["nodes"][3]["error"] is not None') == True ]] && ok "a Spark ssh cannot reach: ok false with an error" || bad "unreachable: $(cat "$T/out")"

reset; for s in "${SPARKS[@]}"; do mkstatus "$s" medium; done; mkdir -p "$T/st/$ME"; printf 'window 7\n' > "$T/st/$ME/pin"
run status --json
[[ $(jq_ 'd["pinned"]') == "window 7" ]] && ok "status carries the pin's reason" || bad "pin in status: $(cat "$T/out")"
run status
[[ $(rc) == 0 && $(grep -c . "$T/out") -ge 4 ]] && grep -q 'medium' "$T/out" && ok "status as text: a line a Spark" || bad "status text: $(cat "$T/out")"

# check
reset; for s in "${SPARKS[@]}"; do mkstatus "$s" medium; done
run check medium
[[ $(rc) == 0 ]] && ok "check medium: all four run it, exit 0" || bad "check ok: rc $(rc): $(cat "$T/out") $(cat "$T/err")"
run check max
[[ $(rc) != 0 ]] && ok "check max while they run medium: fails" || bad "check wrong profile"
mkstatus w1 performance; run check medium
[[ $(rc) != 0 ]] && grep -q '^w1' "$T/out" && ok "check on a mixed set: fails" || bad "check mixed: rc $(rc)"
mkstatus w1 medium 100; run check medium
[[ $(rc) != 0 ]] && ok "check with a stale Spark: fails" || bad "check stale"
mkstatus w1 medium 1 '{"zero_rpm_guard": true}'; run check medium
[[ $(rc) != 0 ]] && ok "check with a zero-RPM guard set: fails" || bad "check guard"
mkstatus w1 medium 1 '{"profile_error": "unknown profile name"}'; run check medium
[[ $(rc) != 0 ]] && ok "check with a profile error: fails" || bad "check error"
run check quiet
[[ $(rc) == 2 ]] && ok "check of an unknown profile: exit 2" || bad "check unknown: rc $(rc)"

# pin / unpin
reset; run pin max --why "timing window 9"
[[ $(rc) == 0 && $(cat "$T/st/$ME/pin") == "timing window 9" && $(profile_of w3) == max ]] && ok "pin: all four set, the reason kept in the pin file" || bad "pin: rc $(rc): $(cat "$T/out") $(cat "$T/err")"
run set medium
[[ $(rc) == 3 && $(profile_of w3) == max ]] && ok "after a pin, set is refused" || bad "set after pin: rc $(rc)"
run unpin
[[ $(rc) == 0 && ! -e $T/st/$ME/pin ]] && ok "unpin removes it" || bad "unpin: rc $(rc)"
run set medium
[[ $(rc) == 0 && $(profile_of w3) == medium ]] && ok "set works again" || bad "set after unpin: rc $(rc)"
reset; run pin max
[[ $(rc) == 2 && ! -e $T/st/$ME/pin ]] && ok "pin without --why: refused (exit 2)" || bad "pin no why: rc $(rc)"
reset; FAIL_SSH=w1 run pin max --why "w"
[[ $(rc) == 1 && ! -e $T/st/$ME/pin ]] && ok "pin with a Spark down: not pinned (a pin that does not hold is worse than none)" || bad "pin partial: rc $(rc)"

# the GPU clock: the same path
reset; run clock set 2200
[[ $(rc) == 0 && $(cat "$T/log/clock.argv") == "2200 w1 w2 w3" ]] && ok "clock set 2200: the clock tool run once for this Spark and the three targets" || bad "clock: rc $(rc): $(cat "$T/log/clock.argv") $(cat "$T/out") $(cat "$T/err")"
[[ $(grep -c 'GPU clock 2200' "$T/out") == 4 ]] && ok "clock set: a line a Spark" || bad "clock lines: $(cat "$T/out")"
for c in 2400 2200 2000 1800; do reset; run clock set "$c"; [[ $(rc) == 0 && $(cat "$T/log/clock.argv") == "$c w1 w2 w3" ]] || bad "clock $c: rc $(rc)"; done
for c in unlocked 2300 "" abc "2200;x" 0 -1 2400.0; do
  reset; run clock set "$c"
  [[ $(rc) == 2 && ! -s $T/log/clock.argv && ! -s $T/log/ssh.argv ]] || bad "clock '$c': rc $(rc) ran: $(cat "$T/log/clock.argv")"
done
grep -q '2400 2200 2000 1800' "$T/err" && ok "a limit outside the four (unlocked too): refused (exit 2), the four named, nothing run" || bad "clock refusal text: $(cat "$T/err")"
reset; run clock set --json 2000
if [[ $(rc) == 0 && $(jq_ 'd["gpu_clock"]') == 2000 && $(jq_ '[n["rank"] for n in d["nodes"]]') == "[0, 1, 2, 3]" && $(jq_ 'all(n["ok"] for n in d["nodes"])') == True ]]; then
  ok "clock set --json: the limit, four nodes by rank, each ok"; else bad "clock json: rc $(rc): $(cat "$T/out")"; fi
reset; FAIL_CLOCK=w2 run clock set --json 2200
[[ $(rc) == 1 && $(jq_ '[n["ok"] for n in d["nodes"]]') == "[True, True, False, True]" && $(jq_ '"Not Supported" in d["nodes"][2]["error"]') == True ]] &&
  ok "a Spark that refuses: named with its words, exit 1, the others set" || bad "clock fail: rc $(rc): $(cat "$T/out")"
reset; mkdir -p "$T/st/$ME"; printf 'window 2\n' > "$T/st/$ME/pin"
run clock set --json 2200
[[ $(rc) == 3 && ! -s $T/log/clock.argv && $(jq_ 'd["pinned"]') == "window 2" ]] && ok "pinned: the clock is refused too (exit 3), nothing run" || bad "clock pinned: rc $(rc): $(cat "$T/out")"
reset; run clock
[[ $(rc) == 2 ]] && ok "clock without a subcommand: usage" || bad "clock bare: rc $(rc)"

reset; for s in "${SPARKS[@]}"; do mkstatus "$s" medium; mkclock "$s" 2200; done
run status --json
if [[ $(jq_ 'd["nodes"][2]["gpu_clock"]["limit"]') == 2200 && $(jq_ 'd["nodes"][2]["gpu_clock"]["ok"]') == True && $(jq_ 'd["clock"]') == 2200 && $(jq_ 'd["clock_mixed"]') == False ]]; then
  ok "status --json: each Spark's clock record, the common limit"; else bad "status clock: $(cat "$T/out")"; fi
mkclock w3 2000; run status --json
[[ $(jq_ 'd["clock_mixed"]') == True && $(jq_ 'd["clock"]') == None ]] && ok "one Spark on another limit: clock_mixed" || bad "clock mixed: $(cat "$T/out")"
rm -f "$T/gc/w1/state"; run status --json
[[ $(jq_ 'd["nodes"][1]["gpu_clock"]') == None && $(jq_ 'd["nodes"][1]["ok"]') == True ]] && ok "a Spark with no clock record: gpu_clock null, still ok" || bad "no record: $(cat "$T/out")"
run status
grep -q 'clock' "$T/out" && ok "status as text names the clocks" || bad "status text clock: $(cat "$T/out")"

# without a clock tool: clock set refuses, status reports no clock
reset; LOGDIR="$T/log" PATH="$T/bin:$PATH" DGX_FAN_SSH="$T/bin/ssh" DGX_FAN_DIR="$T/st" DGX_FAN_NODES="w1 w2 w3" \
  DGX_FAN_THERMAL_ROOT="$T/thermal" DGX_FAN_HWMON_ROOT="$T/hwmon" bash "$F" clock set 2200 > "$T/out" 2> "$T/err"; echo $? > "$T/rc"
[[ $(rc) == 2 && ! -s $T/log/clock.argv && ! -s $T/log/ssh.argv ]] && grep -q 'DGX_FAN_CLOCK_TOOL' "$T/err" \
  && ok "no DGX_FAN_CLOCK_TOOL: clock set refused (exit 2), nothing run, the setting named" || bad "no clock tool: rc $(rc) $(cat "$T/err")"
reset; for s in "${SPARKS[@]}"; do mkstatus "$s" medium; done
LOGDIR="$T/log" PATH="$T/bin:$PATH" DGX_FAN_SSH="$T/bin/ssh" DGX_FAN_DIR="$T/st" DGX_FAN_NODES="w1 w2 w3" \
  DGX_FAN_THERMAL_ROOT="$T/thermal" DGX_FAN_HWMON_ROOT="$T/hwmon" bash "$F" status --json > "$T/out" 2> "$T/err"; echo $? > "$T/rc"
[[ $(rc) == 0 && $(jq_ '[n.get("gpu_clock") for n in d["nodes"]]') == "[None, None, None, None]" ]] \
  && ok "no clock state file: status reports no clock and still answers" || bad "status without clock: rc $(rc) $(cat "$T/out")"

# without DGX_FAN_NODES: this Spark only, no ssh
reset; mkstatus "$ME" medium
LOGDIR="$T/log" PATH="$T/bin:$PATH" DGX_FAN_SSH="$T/bin/ssh" DGX_FAN_DIR="$T/st" \
  DGX_FAN_THERMAL_ROOT="$T/thermal" DGX_FAN_HWMON_ROOT="$T/hwmon" bash "$F" set max > "$T/out" 2> "$T/err"; echo $? > "$T/rc"
[[ $(rc) == 0 && $(profile_of "$ME") == max && ! -s $T/log/ssh.argv ]] && ok "no DGX_FAN_NODES: this Spark only, no ssh" || bad "no nodes: rc $(rc) ssh: $(cat "$T/log/ssh.argv")"

# nothing secret
reset; for s in "${SPARKS[@]}"; do mkstatus "$s" medium; done
run set medium; cat "$T/out" "$T/err" > "$T/all"; run status --json; cat "$T/out" "$T/err" >> "$T/all"
! grep -q hunter2 "$T/all" && ! grep -qi 'token\|api[-_]key\|secret' "$T/all" && ok "no secret in any output" || bad "secret in output"

echo "test-dgx-fan-fleet: $fails failed"
exit $((fails > 0))
