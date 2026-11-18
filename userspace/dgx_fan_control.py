#!/usr/bin/env python3
"""Smooth userland policy for the DGX Spark additive fan-floor device."""

from __future__ import annotations

import argparse
import errno
import logging
import json
import math
import os
import signal
import stat
import sys
import time
from threading import Event
from pathlib import Path

COOLING_DEVICE_TYPE = "dgx_ec_fan_floor"
MAX_STATE = 12
POLL_SECONDS = 2.0
FILTER_ALPHA = 0.35
DOWN_HYSTERESIS_C = 4.0
UP_STATES_PER_POLL = 2
DOWN_STATES_PER_POLL = 1
TRANSPORT_FAILURE_LIMIT = 3
RETRY_INITIAL_SECONDS = 2.0
CONTROLLER_UNAVAILABLE_EXIT = 69
RETRYABLE_ERRNOS = frozenset((
    errno.EBUSY, errno.EAGAIN, errno.EINTR, errno.EIO,
    errno.ETIMEDOUT, errno.EREMOTEIO,
))

# The curve becomes deliberately aggressive well below NVIDIA's stock 80 C
# second step. Firmware still takes the maximum of its own demand and this
# additive request.
PERFORMANCE_CURVE = (
    (50.0, 3),   # 4,500 common RPM floor
    (55.0, 5),   # 6,300
    (60.0, 8),   # 9,000
    (65.0, 10),  # 11,250 (fan 0 saturates at 9,000)
    (70.0, 12),  # 13,500 (both fans at their maxima)
)

# Named profiles: one small file names the profile the daemon runs, a status file reports what it does, and a
# zero-RPM guard forces the maximum floor if a fan stops while the box is warm.
# The profile file is untrusted input: it is never followed through a symlink, must be a regular file of at most
# PROFILE_FILE_MAX bytes and must name a profile of the table below; anything else means Performance, with the reason
# in the status file. No profile can request less than stock: the floor is additive.
DRIVER_VERSION = "0.1.4"
PROFILE_FILE = Path("/var/lib/dgx-fan-control/profile")
STATUS_FILE = Path("/run/dgx-fan-control/status.json")
PROFILE_FILE_MAX = 64
DEFAULT_PROFILE = "performance"
# Common RPM floor of each cooling state (driver table, kernel/dgx_ec_fan_control.c).
FLOOR_RPM = (0, 2700, 3600, 4500, 5400, 6300, 7200, 8100, 9000, 10125, 11250, 12375, 13500)
MEDIUM_CURVE = (
    (55.0, 2),   # 3,600
    (60.0, 4),   # 5,400
    (65.0, 6),   # 7,200
    (70.0, 8),   # 9,000
    (75.0, 10),  # 11,250
    (80.0, 12),  # 13,500
)
CURVES = {"performance": PERFORMANCE_CURVE, "medium": MEDIUM_CURVE}
FIXED_PROFILES = {"stock": 0, "max": MAX_STATE}
ZERO_RPM_PASSES = 3
ZERO_RPM_MIN_TEMP_C = 40.0
HWMON_NAME = "dgx_ec_fan"

LOG = logging.getLogger("dgx-fan-control")


def find_cooling_device(thermal_root: Path) -> Path:
    matches = []
    for candidate in sorted(thermal_root.glob("cooling_device*")):
        try:
            device_type = (candidate / "type").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            continue
        if device_type == COOLING_DEVICE_TYPE:
            matches.append(candidate)
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one {COOLING_DEVICE_TYPE!r} cooling device, found {len(matches)}"
        )
    return matches[0]


def read_hottest_temperature_c(thermal_root: Path) -> float:
    readings = []
    for path in sorted(thermal_root.glob("thermal_zone*/temp")):
        try:
            millidegrees = int(path.read_text(encoding="ascii").strip())
        except (OSError, UnicodeError, ValueError):
            continue
        if 0 < millidegrees <= 200_000:
            readings.append(millidegrees / 1000.0)
    if not readings:
        raise RuntimeError("no valid kernel thermal-zone readings are available")
    return max(readings)


def curve_state(temperature_c: float) -> int:
    if not math.isfinite(temperature_c) or temperature_c <= 0:
        raise ValueError("temperature must be finite and positive")
    state = 0
    for threshold_c, threshold_state in PERFORMANCE_CURVE:
        if temperature_c < threshold_c:
            break
        state = threshold_state
    return state


def next_state(current: int, filtered_temperature_c: float) -> int:
    """Rate-limit the curve, with prompt increases and slow hysteretic falls."""
    if not 0 <= current <= MAX_STATE:
        raise ValueError("current cooling state is outside the driver contract")
    rising_target = curve_state(filtered_temperature_c)
    if rising_target > current:
        return min(rising_target, current + UP_STATES_PER_POLL)
    falling_target = curve_state(filtered_temperature_c + DOWN_HYSTERESIS_C)
    if falling_target < current:
        return max(falling_target, current - DOWN_STATES_PER_POLL)
    return current


def valid_profiles() -> frozenset:
    names = set(FIXED_PROFILES) | set(CURVES)
    names.update(f"hold-{rpm}" for rpm in FLOOR_RPM[1:])
    return frozenset(names)


def hold_state(profile: str) -> int | None:
    """The state a hold-<RPM> profile keeps as its floor, None for any other profile."""
    if not profile.startswith("hold-"):
        return None
    return FLOOR_RPM.index(int(profile[5:]))


def _curve_state_of(curve: tuple, temperature_c: float) -> int:
    state = 0
    for threshold_c, threshold_state in curve:
        if temperature_c < threshold_c:
            break
        state = threshold_state
    return state


def profile_target(profile: str, temperature_c: float) -> int:
    """The state a profile wants at a (filtered) temperature, before any rate limit."""
    if profile not in valid_profiles():
        raise ValueError(f"unknown fan profile {profile!r}")
    if profile in FIXED_PROFILES:
        return FIXED_PROFILES[profile]
    if not math.isfinite(temperature_c) or temperature_c <= 0:
        raise ValueError("temperature must be finite and positive")
    held = hold_state(profile)
    if held is not None:
        return max(held, _curve_state_of(PERFORMANCE_CURVE, temperature_c))
    return _curve_state_of(CURVES[profile], temperature_c)


def next_profile_state(profile: str, current: int, filtered_temperature_c: float) -> int:
    """Rate-limit a profile's target like next_state: up 2 a pass at most, down 1 and only when the target
    4 C hotter is lower.  stock and max are written at once (leaving max is the slow fall)."""
    if not 0 <= current <= MAX_STATE:
        raise ValueError("current cooling state is outside the driver contract")
    if profile == "stock":
        return 0
    if profile == "max":
        return MAX_STATE
    rising = profile_target(profile, filtered_temperature_c)
    if rising > current:
        return min(rising, current + UP_STATES_PER_POLL)
    falling = profile_target(profile, filtered_temperature_c + DOWN_HYSTERESIS_C)
    if falling < current:
        return max(falling, current - DOWN_STATES_PER_POLL)
    return current


def read_profile_file(path: Path) -> tuple[str, str | None, str]:
    """(profile in force, error or None, what the file said).  Never raises."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return DEFAULT_PROFILE, "profile file missing", ""
    except OSError as exc:
        return DEFAULT_PROFILE, f"profile file not readable: {exc.strerror or exc}", ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return DEFAULT_PROFILE, "profile file is not a regular file", ""
        raw = os.read(fd, PROFILE_FILE_MAX + 1)
    except OSError as exc:
        return DEFAULT_PROFILE, f"profile file not readable: {exc.strerror or exc}", ""
    finally:
        os.close(fd)
    if len(raw) > PROFILE_FILE_MAX:
        return DEFAULT_PROFILE, f"profile file over {PROFILE_FILE_MAX} bytes", ""
    try:
        text = raw.decode("ascii").strip()
    except UnicodeError:
        return DEFAULT_PROFILE, "profile file is not text", ""
    if text in valid_profiles():
        return text, None, text
    return DEFAULT_PROFILE, "unknown profile name", text[:32]


def read_fan_rpm(hwmon_root: Path) -> list:
    """[fan1, fan2] from the driver's hwmon device; None for a reading that cannot be had."""
    rpm: list = [None, None]
    try:
        devices = sorted(hwmon_root.glob("hwmon*"))
    except OSError:
        return rpm
    for device in devices:
        try:
            if (device / "name").read_text(encoding="ascii").strip() != HWMON_NAME:
                continue
        except (OSError, UnicodeError):
            continue
        for index in (0, 1):
            try:
                rpm[index] = int((device / f"fan{index + 1}_input").read_text(encoding="ascii").strip())
            except (OSError, UnicodeError, ValueError):
                pass
        break
    return rpm


def write_status(path: Path, doc: dict) -> None:
    """Replace the status file atomically, readable by everyone (the unit's UMask is 0077)."""
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="ascii")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


class Controller:
    """One pass of the policy at a time (step), so the passes can be tested without a clock or a signal."""

    def __init__(self, thermal_root: Path, profile_file: Path = PROFILE_FILE, status_file: Path = STATUS_FILE,
                 hwmon_root: Path = Path("/sys/class/hwmon"), clock=time.time) -> None:
        self.thermal_root = thermal_root
        self.cooling_device = find_cooling_device(thermal_root)
        self.profile_file = profile_file
        self.status_file = status_file
        self.hwmon_root = hwmon_root
        self.clock = clock
        self.filtered_temperature_c: float | None = None
        self.profile = DEFAULT_PROFILE
        self.profile_error: str | None = None
        self.zero_passes = 0
        self.guard = False
        self._logged_error: str | None = None

    def reset_filter(self) -> None:
        self.filtered_temperature_c = None

    def _read_profile(self) -> str:
        profile, error, requested = read_profile_file(self.profile_file)
        if error != self._logged_error:
            if error:
                LOG.warning("%s (%r): using %s", error, requested, DEFAULT_PROFILE)
            self._logged_error = error
        if profile != self.profile:
            LOG.info("profile %s -> %s (file)", self.profile, profile)
            self.profile = profile
        self.profile_error = error
        return requested or (profile if error is None else "")

    def _guard(self, rpm: list, hottest: float) -> None:
        readings = [value for value in rpm if value is not None]
        if readings and min(readings) == 0 and hottest > ZERO_RPM_MIN_TEMP_C:
            self.zero_passes += 1
            if self.zero_passes >= ZERO_RPM_PASSES and not self.guard:
                LOG.error("a fan reads 0 RPM while the box is warm: forcing the maximum floor")
                self.guard = True
        elif readings and min(readings) > 0:
            if self.guard:
                LOG.info("fan RPM above zero again: zero-RPM guard released")
            self.zero_passes = 0
            self.guard = False
        elif not readings or min(readings) == 0:
            # unreadable, or zero on a cold box: neither counts as a fault, neither clears a standing guard
            if not self.guard:
                self.zero_passes = 0

    def step(self) -> None:
        requested = self._read_profile()
        current = read_state(self.cooling_device)
        rpm = read_fan_rpm(self.hwmon_root)
        try:
            hottest = read_hottest_temperature_c(self.thermal_root)
            if self.filtered_temperature_c is None:
                self.filtered_temperature_c = hottest
            else:
                self.filtered_temperature_c += FILTER_ALPHA * (hottest - self.filtered_temperature_c)
            self._guard(rpm, hottest)
            desired = next_profile_state(self.profile, current, self.filtered_temperature_c)
        except (OSError, RuntimeError, ValueError):
            LOG.exception("temperature observation failed; requesting maximum cooling")
            hottest = math.nan
            self.filtered_temperature_c = None
            desired = 0 if self.profile == "stock" else MAX_STATE
        if self.guard:
            desired = MAX_STATE
        if desired != current:
            write_state(self.cooling_device, desired)
            LOG.info(
                "cooling state %d -> %d (profile=%s hottest=%.1fC filtered=%s)",
                current, desired, self.profile, hottest,
                "unavailable" if self.filtered_temperature_c is None else f"{self.filtered_temperature_c:.1f}C",
            )
        self._publish(requested, desired, hottest, rpm)

    def _publish(self, requested: str, state: int, hottest: float, rpm: list) -> None:
        def number(value: float | None):
            return None if value is None or not math.isfinite(value) else round(value, 2)

        doc = {
            "v": 1,
            "at": self.clock(),
            "profile": self.profile,
            "requested": requested,
            "profile_error": self.profile_error,
            "state": state,
            "floor_rpm": FLOOR_RPM[state],
            "hottest_c": number(hottest),
            "filtered_c": number(self.filtered_temperature_c),
            "rpm": rpm,
            "zero_rpm_guard": self.guard,
            "driver": DRIVER_VERSION,
        }
        try:
            write_status(self.status_file, doc)
        except OSError as exc:
            LOG.warning("status file not written: %s", exc)


def read_state(cooling_device: Path) -> int:
    state = int((cooling_device / "cur_state").read_text(encoding="ascii").strip())
    maximum = int((cooling_device / "max_state").read_text(encoding="ascii").strip())
    if maximum != MAX_STATE or not 0 <= state <= maximum:
        raise RuntimeError(
            f"unexpected cooling-device contract: state={state} max_state={maximum}"
        )
    return state


def write_state(cooling_device: Path, state: int) -> None:
    if not 0 <= state <= MAX_STATE:
        raise ValueError(f"state must be between 0 and {MAX_STATE}")
    (cooling_device / "cur_state").write_text(f"{state}\n", encoding="ascii")
    observed = read_state(cooling_device)
    if observed != state:
        raise RuntimeError(f"cooling-state readback mismatch: wrote {state}, read {observed}")


def run_daemon(thermal_root: Path, poll_seconds: float, profile_file: Path = PROFILE_FILE,
               status_file: Path = STATUS_FILE, hwmon_root: Path = Path("/sys/class/hwmon")) -> int:
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError("poll interval must be finite and positive")
    controller = Controller(thermal_root, profile_file, status_file, hwmon_root)
    cooling_device = controller.cooling_device
    stop_requested = Event()
    failures = 0
    primary_failure = False

    def request_stop(signum: int, frame: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    LOG.info("controller starting; authenticating the current cooling state")

    try:
        while not stop_requested.is_set():
            try:
                # Suspend, an EC reset, or an uncertain write may change state.
                # The controller reads it on every pass before deciding that no update is needed.
                controller.step()
                if failures:
                    LOG.info("fan communication recovered after %d failed attempts", failures)
                failures = 0
            except OSError as exc:
                failures += 1
                controller.reset_filter()
                if exc.errno not in RETRYABLE_ERRNOS or failures >= TRANSPORT_FAILURE_LIMIT:
                    LOG.error(
                        "fan control unavailable after %d failed attempt(s): %s; "
                        "stopping for operator recovery, current floor is unverified",
                        failures, exc,
                    )
                    raise
                delay = RETRY_INITIAL_SECONDS * 2 ** (failures - 1)
                LOG.warning(
                    "fan communication failed (%d/%d): %s; retrying in %.1fs",
                    failures, TRANSPORT_FAILURE_LIMIT, exc, delay,
                )
                stop_requested.wait(delay)
                continue
            stop_requested.wait(poll_seconds)
    except BaseException:
        primary_failure = True
        raise
    finally:
        try:
            write_state(cooling_device, 0)
        except Exception:
            LOG.exception("automatic restoration failed; current fan floor is unverified")
            if not primary_failure:
                raise
        else:
            LOG.info("automatic NVIDIA fan policy restored")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--thermal-root",
        type=Path,
        default=Path("/sys/class/thermal"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--profile-file", type=Path, default=PROFILE_FILE, help=argparse.SUPPRESS)
    parser.add_argument("--status-file", type=Path, default=STATUS_FILE, help=argparse.SUPPRESS)
    parser.add_argument("--hwmon-root", type=Path, default=Path("/sys/class/hwmon"), help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    daemon = subparsers.add_parser("daemon", help="run the profile named in /var/lib/dgx-fan-control/profile (default: the performance curve)")
    daemon.add_argument("--poll-seconds", type=float, default=POLL_SECONDS)
    set_state = subparsers.add_parser("set-state", help="set one explicit cooling state")
    set_state.add_argument("state", type=int, choices=range(MAX_STATE + 1))
    subparsers.add_parser("automatic", help="restore firmware-automatic policy")
    subparsers.add_parser("status", help="show the cooling state and hottest sensor")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "daemon":
            return run_daemon(args.thermal_root, args.poll_seconds, args.profile_file, args.status_file, args.hwmon_root)
        cooling_device = find_cooling_device(args.thermal_root)
        if args.command == "set-state":
            write_state(cooling_device, args.state)
            return 0
        if args.command == "automatic":
            write_state(cooling_device, 0)
            return 0
        if args.command == "status":
            print(
                f"state={read_state(cooling_device)}/{MAX_STATE} "
                f"hottest={read_hottest_temperature_c(args.thermal_root):.1f}C"
            )
            return 0
    except (OSError, RuntimeError, ValueError) as exc:
        LOG.error("%s", exc)
        return CONTROLLER_UNAVAILABLE_EXIT if args.command == "daemon" else 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
