#!/usr/bin/env python3
"""Host-side shim giving apple/container guests scoped access to Xcode tooling.

Xcode and simctl are macOS-only, so a Linux container can never run them
directly. This listener accepts a fixed set of verbs from the container subnet
and maps each one to an argv-list subprocess call. There is no shell, no verb
that takes a free-form command, and no way to inject extra arguments: every
argument is either a literal, a validated UDID/bundle-id, or a path resolved
inside an allowlisted project root.

Two kinds of verb:

  * Built-in simctl verbs (list, boot, install, screenshot, logs, ...) are
    hardcoded here and work for any project.
  * Project verbs (build, run, test, ...) come from the CONFIG file's per-project
    `commands` block, as argv templates with a fixed placeholder vocabulary.
  * Direct verbs (sim_create, sim_delete, sim_list, pull, status) are several
    fixed host steps behind one request, so they are functions here rather than
    one argv.

A project that declares a `watch` block also gets the watch verbs (watch_build,
watch_boot, watch_install, ...): the Apple Watch app is built by target for the
watch simulator and driven on the watch paired with the iPhone simulator the
request targets, under that simulator's own claim.

A project that declares a `device` block can also install, launch, terminate and
pull a file from one physical iPhone (device_* verbs, `xcrun devicectl`): the
config names the single device and bundle id they may touch, and `pull` is
limited to one directory of the app's data container.

A request carrying "platform": "android" is served from the project's `android`
block instead: the same verbs (boot, install, screenshot, logs, ...) mapped onto
the Android SDK's `emulator` and `adb`, under a claim of their own, so an
emulator run never contends with the iOS simulator.

A project command's output goes to a log inside the caller's checkout
(.mac-sim/logs/<command>.log) while it runs, so the guest can follow a build
that takes minutes instead of waiting on a silent request.

The config is read from the host filesystem ONLY, never from the bind-mounted
workspace. That distinction is the security boundary: anything inside the
container can write to the workspace, so a workspace-resident adapter would let
the guest define its own host commands and defeat the allowlist entirely.

Run via `cc-sim-up`. Logs every accepted and rejected call.
"""

import fcntl
import glob
import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import quote, urlsplit

CONFIG_PATH = os.environ.get(
    "MAC_SIM_CONFIG",
    os.path.expanduser("~/.config/cc-container/mac-sim.json"),
)
TOKEN_PATH = os.environ.get(
    "MAC_SIM_TOKEN_FILE",
    os.path.expanduser("~/.local/state/cc-container/mac-sim.token"),
)
# Where the per-project claim on the shared simulator is recorded. One Mac has
# one simulator, one bundle id and one bundler port, so several checkouts of the
# same project cannot drive it at once; see the Claim class.
LOCK_DIR = Path(os.environ.get(
    "MAC_SIM_LOCK_DIR",
    os.path.expanduser("~/.local/state/cc-container/mac-sim-locks"),
))

# The registry of simulators this shim created, one file per project. It is what
# makes `sim delete` safe to expose: only a device recorded here can be deleted.
SIM_DIR = Path(os.environ.get(
    "MAC_SIM_SIM_DIR",
    os.path.expanduser("~/.local/state/cc-container/mac-sim-sims"),
))

# Builds of the shared simulator's app that private simulators install from, one
# directory per project: <id>/ holds the .app and manifest.json, `current` names
# the newest. With a build here, the shared simulator can stay shut down.
APP_CACHE_DIR = Path(os.environ.get(
    "MAC_SIM_APP_CACHE_DIR",
    os.path.expanduser("~/.local/state/cc-container/mac-sim-apps"),
))
# The previous build stays one save longer, so a `sim create` that read `current`
# just before a save still finds its bundle.
APP_CACHE_KEEP = 2

# Live output of project commands, relative to the caller's checkout: inside the
# bind mount, so the guest reads it while the command is still running.
LOG_SUBDIR = ".mac-sim/logs"
MAX_LOG_REPLY = 200_000

MAX_BODY = 64 * 1024
MAX_PULL_BYTES = 1024 ** 3
DEFAULT_TIMEOUT = 300

UDID_RE = re.compile(r"^[0-9A-Fa-f-]{36}$")
BUNDLE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SCHEME_RE = re.compile(r"^[A-Za-z0-9._ -]{1,128}$")
DEVICE_RE = re.compile(r"^[A-Za-z0-9._()' -]{1,64}$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
URL_RE = re.compile(r"^[A-Za-z0-9+.-]{1,32}://[^\s]{0,512}$")
# What `browse` hands to the Mac's default browser: a printable https URL with
# no credentials or port, under a prefix the host config allows. The browser runs
# outside the container's network policy and with the user's sign-ins, so an open
# list would let any guest agent send data anywhere by "opening" a link.
WEB_URL_RE = re.compile(r"^https://[\x21-\x7e]{1,2048}$")
DEFAULT_BROWSE_ALLOW = ["https://github.com/", "https://app.notion.com/",
                        "https://www.notion.so/"]
LAST_RE = re.compile(r"^\d{1,4}[smh]$")
SIM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}([_-][A-Za-z0-9]{2,8}){0,2}$")
EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9._ -]{1,64}$")
AVD_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

DEVICE_VERBS = {"device_list", "device_install", "device_launch", "device_terminate",
                "device_pull"}
# The device identifier devicectl is handed: a UDID, ECID, serial or CoreDevice
# UUID from the host config, never a name, and never something an option parser
# could read as a flag.
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$")
# What `device_pull` may read: a relative path of plain segments in the app's
# data container, under the configured prefix.
DEVICE_PULL_RE = re.compile(r"^[A-Za-z0-9._ -]+(/[A-Za-z0-9._ -]+)*$")
DEFAULT_DEVICE_PULL_PREFIX = "Library/Diagnostics/"

WATCH_VERBS = {"watch_build", "watch_boot", "watch_install", "watch_launch",
               "watch_terminate", "watch_screenshot", "watch_logs"}

BUILTIN_VERBS = [
    "list", "booted", "runtimes", "boot", "shutdown", "open_simulator_ui",
    "install", "uninstall", "launch", "terminate", "screenshot", "openurl",
    "appearance", "logs", "xcodebuild", "project_command", "lock",
    "sim_create", "sim_delete", "sim_list", "pull", "status", "browse", "memory",
    "app_cache_save", "app_cache_status",
] + sorted(WATCH_VERBS) + sorted(DEVICE_VERBS)

# Where `watch_build` puts its products, relative to the checkout.
WATCH_BUILD_DIR = ".mac-sim-build/watch"
# An argument handed to the watch app at launch (-GymlogFixture name): a flag or
# a plain token, never anything a shell or a URL would give meaning to.
LAUNCH_ARG_RE = re.compile(r"^[A-Za-z0-9._:=-][A-Za-z0-9._:=/-]{0,127}$")
MAX_LAUNCH_ARGS = 16

# Verbs made of several host steps; see the "Direct verbs" section.
DIRECT_VERBS = {"sim_create", "sim_delete", "sim_list", "pull", "status",
                "app_cache_save", "app_cache_status"}

# Request fields an older shim would silently ignore, turning "act on my private
# simulator" into "act on the shared one". The guest checks this list before
# sending such a field, so a stale shim is reported instead of obeyed wrongly.
FEATURES = ["appearance", "locale", "pull", "sim", "progress", "status", "android",
            "android_devices", "browse", "json", "watch", "device", "sim_type", "memory",
            "app_cache"]

# The shape of the "data" object that status, lock, sim_list, memory and android list
# reply with, for `mac-sim <verb> --json`. Adding a field keeps the version;
# renaming, removing or changing the meaning of one bumps it, and the change is
# recorded in docs/MAC-SIM.md.
JSON_SCHEMA_VERSION = 1

# Verbs that need no project context: they describe the Mac, not a checkout.
GLOBAL_VERBS = {"list", "booted", "runtimes", "open_simulator_ui", "browse", "device_list"}

# Verbs that mutate what the simulator is running, so two checkouts doing them
# concurrently produce nonsense. Read-only observation (screenshot, logs) is
# deliberately absent: blocking it would break debugging for no safety gain.
EXCLUSIVE_BUILTINS = {"boot", "shutdown", "install", "uninstall", "launch",
                      "terminate", "openurl", "appearance", "xcodebuild",
                      "watch_build", "watch_boot", "watch_install", "watch_launch",
                      "watch_terminate", "device_install", "device_launch",
                      "device_terminate"}

# What a "platform": "android" request may ask for. The direct ones are several
# adb steps (or binary output) behind one request; none of them changes state.
ANDROID_VERBS = {"list", "boot", "shutdown", "install", "uninstall", "launch",
                 "terminate", "screenshot", "openurl", "appearance", "logs",
                 "status", "lock", "project_command"}
ANDROID_DIRECT = {"list", "screenshot", "logs", "status"}

# Per-command TMPDIRs (`"private_tmp": true`), outside every checkout so a
# bundler's file crawler never sees them.
TMP_DIR = Path(os.environ.get(
    "MAC_SIM_TMP_DIR",
    os.path.expanduser("~/.local/state/cc-container/mac-sim-tmp"),
))


class Rejected(Exception):
    """Request failed validation; never reaches a subprocess."""


class Busy(Rejected):
    """Another checkout holds the simulator. Distinct so it answers 409, not
    400: nothing about the request was wrong, it just arrived at a bad time."""


class HostFailure(Exception):
    """A host step of a direct verb exited non-zero. The request was valid, so
    this is reported like any failed command: exit code plus the tool's output."""

    def __init__(self, argv, code, output):
        super().__init__(f"{shlex.join(argv)} failed (exit {code}): {output}")
        self.argv = argv
        self.code = code
        self.output = output


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
class Config:
    def __init__(self, raw):
        listen = raw.get("listen", {})
        self.host = listen.get("host", "192.168.64.1")
        self.port = int(listen.get("port", 8890))
        self.clients = ip_network(listen.get("allow", "192.168.64.0/24"))
        lock = raw.get("lock", {})
        self.lock_enabled = bool(lock.get("enabled", True))
        # How long a finished command keeps its claim. Long enough to cover a
        # build-verify-screenshot round in one checkout; short enough that a
        # forgotten session does not block the next day's work.
        self.lock_ttl = int(lock.get("ttl", 4 * 3600))
        self.browse_allow = parse_browse_allow(raw.get("browse", {}))
        self.projects = {}
        for entry in raw.get("projects", []):
            name = entry.get("name")
            if not name or not NAME_RE.match(name):
                raise ValueError(f"project name missing or invalid: {name!r}")
            root = Path(os.path.expanduser(entry["root"])).resolve()
            if not root.exists():
                raise ValueError(f"project root does not exist: {root}")
            entry["root"] = root
            # cc-container mounts the project at its host path, so by default a
            # guest path IS the host path. Set guest_mount only for a container
            # that mounts the project somewhere else.
            entry.setdefault("guest_mount", str(root))
            entry.setdefault("defaults", {})
            entry.setdefault("commands", {})
            entry.setdefault("pull", {})
            sims = entry.get("simulators")
            if sims is not None and not isinstance(sims, dict):
                raise ValueError(f"project {name}: 'simulators' must be an object")
            if entry.get("android") is not None:
                entry["android"] = parse_android(name, entry["android"], entry["defaults"])
            if entry.get("watch") is not None:
                entry["watch"] = parse_watch(name, entry["watch"], entry["commands"])
            check_watch_commands(name, entry.get("watch"), entry["commands"])
            if entry.get("device") is not None:
                entry["device"] = parse_device(name, entry["device"], entry["defaults"])
            self.projects[name] = entry
        if not self.projects:
            raise ValueError("config declares no projects")

    @property
    def roots(self):
        return [p["root"] for p in self.projects.values()]

    def project(self, name, workdir=None):
        """The named project, or with no name the only one, or the one whose
        root holds the caller's checkout (`workdir`, a guest path), so a guest
        working in one project need not pass -p once several are configured."""
        if name is None:
            if len(self.projects) == 1:
                return next(iter(self.projects.values()))
            owner = self.project_of(workdir)
            if owner is None:
                raise Rejected(
                    f"field 'project' is required: workdir {workdir!r} is under none of "
                    f"the configured roots ({', '.join(str(r) for r in self.roots)})")
            return owner
        if name not in self.projects:
            raise Rejected(f"unknown project: {name!r}")
        return self.projects[name]

    def project_of(self, workdir):
        """The project whose root (or guest mount) holds `workdir`, the deepest
        one when roots nest; None for no workdir or one outside every root."""
        if not isinstance(workdir, str) or not workdir:
            return None
        path = Path(workdir)
        held = [p for p in self.projects.values()
                if any(path == r or r in path.parents
                       for r in (p["root"], Path(p["guest_mount"])))]
        return max(held, key=lambda p: len(p["root"].parts), default=None)


EMULATOR_KEYS = {"avd", "console_port", "port", "emulator_args"}
ANDROID_KEYS = {"sdk", "package", "java_home", "port", "emulator_args", "boot_timeout",
                "devices", "default", "avd", "console_port"}


def reject_unknown_keys(where, raw, allowed):
    """Refuse a key the shim does not read (`//` comments aside), so a typo such
    as `serial` for `console_port` fails at startup instead of being ignored."""
    unknown = sorted(k for k in raw if k not in allowed and not k.startswith("//"))
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(map(repr, unknown))}; "
                         f"allowed: {', '.join(sorted(allowed))}")


def parse_emulator(name, dname, raw, base):
    """One emulator: its AVD and the console port that fixes its adb serial."""
    avd = raw.get("avd")
    if not isinstance(avd, str) or not AVD_RE.match(avd):
        raise ValueError(f"project {name}: android device {dname!r}: avd missing or "
                         f"invalid: {avd!r}")
    console = int(raw.get("console_port", 5554))
    # The emulator takes the console port and the next one, and adb only scans
    # even ports in this range; anything else gets a serial adb never lists.
    if console % 2 or not 5554 <= console <= 5682:
        raise ValueError(f"project {name}: android device {dname!r}: console_port must "
                         f"be an even number in 5554..5682, got {console}")
    args = raw.get("emulator_args", base["emulator_args"])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError(f"project {name}: android device {dname!r}: emulator_args "
                         f"must be a list of strings")
    return {"avd": avd, "console_port": console, "serial": f"emulator-{console}",
            "port": raw.get("port", base["port"]), "emulator_args": args}


def parse_android(name, raw, defaults):
    """Validate a project's `android` block once, at startup, so a typo in the
    SDK path is a refusal to start rather than a confusing failure mid-request.

    Either one emulator (`avd`, `console_port` at the top level) or several in
    `devices`, keyed by a name the caller picks with `-d`; the shared fields
    (sdk, java_home, package, port, emulator_args) are their defaults."""
    if not isinstance(raw, dict):
        raise ValueError(f"project {name}: 'android' must be an object")
    reject_unknown_keys(f"project {name}: android", raw, ANDROID_KEYS)
    sdk = Path(os.path.expanduser(raw.get("sdk", "") or "")).resolve()
    if not raw.get("sdk") or not (sdk / "platform-tools" / "adb").exists():
        raise ValueError(f"project {name}: android.sdk must be an Android SDK root "
                         f"containing platform-tools/adb, got {raw.get('sdk')!r}")
    package = raw.get("package", defaults.get("bundle_id"))
    if not isinstance(package, str) or not BUNDLE_RE.match(package):
        raise ValueError(f"project {name}: android.package missing or invalid: {package!r}")
    java_home = raw.get("java_home")
    if java_home and not Path(os.path.expanduser(java_home)).is_dir():
        raise ValueError(f"project {name}: android.java_home does not exist: {java_home}")
    base = {"port": raw.get("port"), "emulator_args": raw.get("emulator_args", [])}
    if "devices" in raw:
        if not isinstance(raw["devices"], dict) or not raw["devices"]:
            raise ValueError(f"project {name}: android.devices must be a non-empty object")
        devices = {}
        for dname, draw in raw["devices"].items():
            if not SIM_NAME_RE.match(dname):
                raise ValueError(f"project {name}: android device name invalid: {dname!r}")
            if not isinstance(draw, dict):
                raise ValueError(f"project {name}: android device {dname!r} must be an object")
            reject_unknown_keys(f"project {name}: android device {dname!r}", draw, EMULATOR_KEYS)
            devices[dname] = parse_emulator(name, dname, draw, base)
        default = raw.get("default", next(iter(devices)))
        if default not in devices:
            raise ValueError(f"project {name}: android.default {default!r} is not one of "
                             f"{sorted(devices)}")
    else:
        default = "default"
        devices = {default: parse_emulator(name, default, raw, base)}
    serials = [d["serial"] for d in devices.values()]
    if len(set(serials)) != len(serials):
        raise ValueError(f"project {name}: two android devices share a console_port")
    return {
        "sdk": sdk, "package": package,
        "java_home": os.path.expanduser(java_home) if java_home else None,
        "boot_timeout": int(raw.get("boot_timeout", 600)),
        "devices": devices, "default": default,
    }


def parse_watch(name, raw, commands):
    """Validate a project's `watch` block once, at startup, so a typo fails the
    shim's start and not the first watch verb an agent sends."""
    if not isinstance(raw, dict):
        raise ValueError(f"project {name}: 'watch' must be an object")
    known = {"device", "runtime", "project", "target", "product", "bundle_id",
             "configuration", "stamp", "rebuild"}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"project {name}: unknown watch keys {sorted(unknown)}; "
                         f"expected a subset of {sorted(known)}")
    for key, pattern in (("device", DEVICE_RE), ("target", SCHEME_RE),
                         ("product", SCHEME_RE), ("bundle_id", BUNDLE_RE)):
        if key in raw and not (isinstance(raw[key], str) and pattern.match(raw[key])):
            raise ValueError(f"project {name}: watch.{key} is invalid: {raw[key]!r}")
    for key in ("project", "target", "bundle_id"):
        if key not in raw:
            raise ValueError(f"project {name}: watch.{key} is required")
    if raw.get("configuration", "Debug") not in ("Debug", "Release"):
        raise ValueError(f"project {name}: watch.configuration must be Debug or Release")
    if "runtime" in raw and not (isinstance(raw["runtime"], str) and
                                 re.match(r"^[A-Za-z0-9._-]{1,128}$", raw["runtime"])):
        raise ValueError(f"project {name}: watch.runtime is invalid: {raw['runtime']!r}")
    rebuild = raw.get("rebuild")
    if rebuild is not None:
        command = rebuild.get("command") if isinstance(rebuild, dict) else None
        if command not in commands:
            raise ValueError(f"project {name}: watch.rebuild.command {command!r} is not "
                             f"a declared command ({sorted(commands) or 'none'})")
        variant = rebuild.get("variant")
        if variant is not None and variant not in commands[command].get("variants", {}):
            raise ValueError(f"project {name}: watch.rebuild.variant {variant!r} is not "
                             f"a variant of {command!r}")
        if "watch" not in commands[command]:
            raise ValueError(f"project {name}: watch.rebuild.command {command!r} has no "
                             f"\"watch\" block, so it cannot take --watch")
        if "stamp" not in raw:
            raise ValueError(f"project {name}: watch.rebuild needs watch.stamp")
        if commands[command].get("watch_stamp") != "write":
            raise ValueError(f"project {name}: watch.rebuild.command {command!r} must "
                             f"declare \"watch_stamp\": \"write\", or the stamp never "
                             f"changes and every run would regenerate")
    return raw


def parse_device(name, raw, defaults):
    """Validate a project's `device` block once, at startup."""
    if not isinstance(raw, dict):
        raise ValueError(f"project {name}: 'device' must be an object")
    unknown = set(raw) - {"id", "bundle_id", "pull_prefix"}
    if unknown:
        raise ValueError(f"project {name}: unknown device keys {sorted(unknown)}; "
                         f"expected a subset of ['bundle_id', 'id', 'pull_prefix']")
    if not (isinstance(raw.get("id"), str) and DEVICE_ID_RE.match(raw["id"])):
        raise ValueError(f"project {name}: device.id must be the device's UDID, ECID, "
                         f"serial or CoreDevice UUID (`mac-sim device list` shows them), "
                         f"got {raw.get('id')!r}")
    bundle = raw.get("bundle_id", defaults.get("bundle_id"))
    if not (isinstance(bundle, str) and BUNDLE_RE.match(bundle)):
        raise ValueError(f"project {name}: device.bundle_id (or defaults.bundle_id) "
                         f"is required and must be a bundle id, got {bundle!r}")
    prefix = raw.get("pull_prefix", DEFAULT_DEVICE_PULL_PREFIX)
    if not (isinstance(prefix, str) and prefix.endswith("/")
            and DEVICE_PULL_RE.match(prefix.rstrip("/")) and ".." not in prefix.split("/")):
        raise ValueError(f"project {name}: device.pull_prefix must be a relative "
                         f"directory ending in '/', got {prefix!r}")
    return {"id": raw["id"], "bundle_id": bundle, "pull_prefix": prefix}


def check_watch_commands(name, watch, commands):
    """The per-command watch keys, checked whether or not the project has a
    `watch` block: a stamp key with nowhere to point must fail at startup."""
    for cname, spec in commands.items():
        if "watch" in spec and not isinstance(spec["watch"], dict):
            raise ValueError(f"project {name}: command {cname!r}: 'watch' must be an object")
        if spec.get("watch_stamp", "write") not in ("write", "check"):
            raise ValueError(f"project {name}: command {cname!r}: watch_stamp must be "
                             f"\"write\" or \"check\"")
        if "watch_stamp" in spec and "stamp" not in (watch or {}):
            raise ValueError(f"project {name}: command {cname!r} declares watch_stamp "
                             f"but the project's watch block has no stamp")
        if "watch_stamp" in spec and "watch" not in spec:
            raise ValueError(f"project {name}: command {cname!r} declares watch_stamp "
                             f"but no \"watch\" block, so it cannot take --watch")


def parse_browse_allow(raw):
    """The URL prefixes `browse` may open. Each is an https origin followed by a
    path ending in "/", so a prefix can never match a longer host name
    (https://github.com.evil.example)."""
    allow = raw.get("allow", DEFAULT_BROWSE_ALLOW)
    if not isinstance(allow, list):
        raise ValueError(f"browse.allow must be a list of URL prefixes, got {allow!r}")
    for prefix in allow:
        parts = urlsplit(prefix) if isinstance(prefix, str) else None
        if (not parts or parts.scheme != "https" or not parts.hostname
                or parts.username or parts.port or parts.query or parts.fragment
                or not parts.path.endswith("/")):
            raise ValueError(f"browse.allow entry must look like https://host/path/, "
                             f"got {prefix!r}")
    return allow


def browse_url(body, config):
    if not config.browse_allow:
        raise Rejected("browse is turned off: browse.allow is [] in the host config")
    url = body.get("url")
    if not isinstance(url, str) or not WEB_URL_RE.match(url):
        raise Rejected(f"browse requires a printable https:// URL, got {url!r}")
    parts = urlsplit(url)
    if parts.username or parts.password or parts.port:
        raise Rejected(f"browse refuses a URL with credentials or a port: {url!r}")
    if not any(url.startswith(p) and parts.hostname == urlsplit(p).hostname
               for p in config.browse_allow):
        raise Rejected(f"browse only opens URLs under {', '.join(config.browse_allow)} "
                       f"(browse.allow in the host config), got {url!r}")
    return url


def load_config(path):
    try:
        with open(path) as fh:
            return Config(json.load(fh))
    except FileNotFoundError:
        sys.exit(f"mac-sim-shim: no config at {path}\n"
                 f"Copy config/mac-sim.example.json there and declare a project root.")
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        sys.exit(f"mac-sim-shim: invalid config {path}: {exc}")


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------
def safe_path(raw, project, must_exist=False, base=None):
    """Resolve a client-supplied path, refusing anything outside the project root.

    Resolution happens before the containment check, so a symlink inside the
    project pointing at /etc/hosts is refused rather than followed.

    A RELATIVE path resolves against `base` -- the caller's checkout -- not the
    host process cwd. That is what makes ".maestro" mean this worktree's flows
    rather than the main checkout's.
    """
    if not isinstance(raw, str) or not raw:
        raise Rejected("path must be a non-empty string")
    mount = project["guest_mount"]
    if raw == mount or raw.startswith(mount + "/"):
        raw = str(project["root"]) + raw[len(mount):]
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = Path(base or project["root"]) / p
    p = p.resolve()
    root = project["root"]
    if not (p == root or root in p.parents):
        raise Rejected(f"path outside allowed roots: {p}")
    if must_exist and not p.exists():
        raise Rejected(f"path does not exist: {p}")
    return p


def resolve_workdir(body, project):
    """The checkout the caller is working in: the project root, or a git
    worktree beneath it.

    Without this every command ran in the project root, so a session started
    with `ccx --worktree <name>` built and tested the MAIN checkout while
    reporting success for its own branch -- a silent wrong answer, which is
    worse than a refusal. Worktrees created in the guest live under
    <root>/.claude/worktrees/, inside the bind mount and therefore inside the
    allowlisted root, so honouring the caller adds no host reach: safe_path
    still refuses anything outside.
    """
    raw = body.get("workdir")
    if raw is None:
        return project["root"]
    base = safe_path(raw, project, must_exist=True)
    if not base.is_dir():
        raise Rejected(f"workdir is not a directory: {base}")
    # A checkout root, not any directory inside one: command specs declare their
    # cwd relative to a checkout ("apps/mobile"), so a base one level off would
    # resolve to a path that does not exist, or worse, to one that does.
    if base != project["root"] and not (base / ".git").exists():
        raise Rejected(
            f"workdir {base} is not a checkout root (no .git entry). "
            f"Run mac-sim from inside a checkout, or pass -w <checkout root>.")
    return base


def field(body, name, pattern, required=True, default=None):
    val = body.get(name, default)
    if val is None:
        if required:
            raise Rejected(f"missing required field: {name}")
        return None
    if not isinstance(val, str) or not pattern.match(val):
        raise Rejected(f"invalid value for {name}: {val!r}")
    return val


def resolve_device(body, project):
    """Accept a UDID or a device name; always return a UDID."""
    if "udid" in body:
        return field(body, "udid", UDID_RE)
    name = field(body, "device", DEVICE_RE, required=False,
                 default=project["defaults"].get("device"))
    if not name:
        raise Rejected("no device given and the project declares no default device")
    matches = available_named(name)
    if not matches:
        if name != project["defaults"].get("device"):
            raise Rejected(f"no available simulator named {name!r}")
        # The shared simulator is the project's, so it is recreated rather than
        # refused: a wiped device set (Xcode, a cleanup tool, `simctl delete
        # all`) would otherwise stop every build until someone made it by hand.
        with _SHARED_SIM_LOCK:
            matches = available_named(name) or [recreate_shared_device(name)]
    # Prefer an already-booted match so repeated calls stay on one device.
    booted = [d for d in matches if d.get("state") == "Booted"]
    return (booted or matches)[-1]["udid"]


_SHARED_SIM_LOCK = threading.Lock()


def available_named(name):
    devices = json.loads(run_host(
        ["xcrun", "simctl", "list", "devices", "available", "--json"], 60)).get("devices", {})
    return [d for runtime in devices.values() for d in runtime
            if d.get("name") == name and d.get("isAvailable")]


def recreate_shared_device(name):
    """Create the project's default simulator, which is gone, as the device type
    of the same name on the newest iOS runtime that runs it. Its app is not
    installed: the project's build installs it."""
    types = json.loads(run_host(["xcrun", "simctl", "list", "devicetypes", "--json"], 60))
    wanted = next((t for t in types.get("devicetypes", []) if t.get("name") == name), None)
    if wanted is None:
        raise Rejected(
            f"no available simulator named {name!r}, and it cannot be recreated: "
            f"{name!r} is not a simulator device type on this Mac. Create it by hand "
            f"(`xcrun simctl create {shlex.quote(name)} <type> <runtime>`) or change "
            f"defaults.device.")
    runtimes = json.loads(run_host(["xcrun", "simctl", "list", "runtimes", "--json"], 60))
    usable = [r for r in runtimes.get("runtimes", [])
              if r.get("platform") == "iOS" and r.get("isAvailable")
              and wanted["identifier"] in [d.get("identifier") for d in r.get("supportedDeviceTypes", [])]]
    if not usable:
        raise Rejected(
            f"no available simulator named {name!r}, and no installed iOS runtime runs "
            f"that device type, so it cannot be recreated. Install one in Xcode "
            f"(Settings > Components).")
    runtime = max(usable, key=lambda r: [int(part) for part in re.findall(r"\d+", r.get("version", "0"))])
    log(f"the shared simulator {name!r} is gone: recreating it on {runtime['name']}")
    udid = run_host(["xcrun", "simctl", "create", name,
                     wanted["identifier"], runtime["identifier"]], 120)
    if not UDID_RE.match(udid):
        raise HostFailure(["xcrun", "simctl", "create"], 1, f"expected a UDID, got {udid!r}")
    return {"udid": udid, "name": name, "state": "Shutdown", "isAvailable": True}


def app_bundle_id(app_path):
    """Read CFBundleIdentifier so `install` can be followed by `launch`."""
    info = app_path / "Info.plist"
    if not info.exists():
        raise Rejected(f"no Info.plist in {app_path}")
    with open(info, "rb") as fh:
        return plistlib.load(fh).get("CFBundleIdentifier")


def port_listeners(port):
    out = subprocess.run(["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
                         capture_output=True, text=True)
    return out.stdout.split()


def kill_port(port):
    """Free a TCP port without a shell. Used before builds whose bundler flags
    are inlined at build time, where a stale server would serve the wrong thing.

    Listeners only: a plain `lsof -ti :PORT` also names every CLIENT of that
    port, which with several simulators means the app running on somebody
    else's device."""
    killed = []
    for pid in port_listeners(port):
        try:
            os.kill(int(pid), signal.SIGKILL)
            killed.append(pid)
        except (ProcessLookupError, PermissionError, ValueError):
            pass
    if killed:
        log(f"freed port {port} (killed {', '.join(killed)})")


def app_running(udid, bundle):
    """Whether the app has a live process on that simulator."""
    out = subprocess.run(["xcrun", "simctl", "spawn", udid, "launchctl", "list"],
                         capture_output=True, text=True, timeout=30)
    return f"UIKitApplication:{bundle}[" in out.stdout


def app_executable(udid, bundle):
    """The app's process name, which is what the unified log is filtered by."""
    try:
        app = run_host(["xcrun", "simctl", "get_app_container", udid, bundle, "app"], 60)
    except HostFailure as exc:
        raise Rejected(
            f"{bundle} is not installed on that simulator, so its logs cannot be "
            f"told apart from the system's. Install it first, or pass --all for "
            f"the unfiltered log. [{exc}]")
    with open(Path(app) / "Info.plist", "rb") as fh:
        name = plistlib.load(fh).get("CFBundleExecutable")
    if not isinstance(name, str) or not EXECUTABLE_RE.match(name):
        raise Rejected(f"{bundle} has an unusable CFBundleExecutable: {name!r}")
    return name


# --------------------------------------------------------------------------
# Command logs: live output the guest can follow
# --------------------------------------------------------------------------
def settle_after(name, settle):
    after = settle.get("after", 30)
    if not isinstance(after, int) or after < 1:
        raise Rejected(f"settle of {name!r}: \"after\" must be a positive whole number of seconds")
    return after


def command_log_path(project, base, name, sim=None):
    stem = f"{name}@{sim['name']}" if sim else name
    return safe_path(f"{LOG_SUBDIR}/{stem}.log", project, base=base)


def open_command_log(path):
    """A fresh log for one run of a command.

    The directory is writable from the guest, so the file is replaced rather
    than truncated and never opened through a symlink: safe_path already
    refused a path that resolves outside the project, and O_EXCL | O_NOFOLLOW
    refuses a link planted at the final component after that check.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o644), "wb")


KEPT_LOGS = 20


def keep_numbered_copy(path):
    """Copy a finished command's log to <stem>-<UTC time>.log beside it, keeping
    the last KEPT_LOGS per stem. The next run of the command replaces the log
    itself, so without the copies only the last run's output survives (a full
    suite claimed across several runs left one run's evidence)."""
    path = Path(path)
    # The directory is guest-writable: never follow a link planted at the log's name.
    try:
        src_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    with os.fdopen(src_fd, "rb") as src:
        if not stat.S_ISREG(os.fstat(src_fd).st_mode):
            return
        # Microseconds: two runs of one command can finish within a second.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        copy = path.with_name(f"{path.stem}-{stamp}.log")
        with os.fdopen(os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                               0o644), "wb") as dst:
            shutil.copyfileobj(src, dst)
    kept = sorted(path.parent.glob(f"{glob.escape(path.stem)}-2*T*Z.log"))
    for old in kept[:-KEPT_LOGS]:
        old.unlink(missing_ok=True)


def read_log_tail(path, limit=MAX_LOG_REPLY):
    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        fh.seek(max(0, size - limit))
        return fh.read().decode(errors="replace")


def kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# --------------------------------------------------------------------------
# Project commands: declarative argv templates from the host-side config
# --------------------------------------------------------------------------
def render(template, values):
    """Substitute {placeholder} tokens in one argv element.

    Substitution is whole-token or embedded, but the result is always a single
    argv element: it is never re-split, so a value containing a space cannot
    become two arguments, and no shell metacharacter has any meaning.
    """
    def sub(match):
        key = match.group(1)
        if key not in values or values[key] is None:
            raise Rejected(f"template references unknown placeholder {{{key}}}")
        return str(values[key])
    rendered = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, template)
    # A leading ~/ is expanded so a config can name a tool by its home-relative
    # path (~/.maestro/bin/maestro). Only the leading form, so a tilde anywhere
    # else stays the literal the config author wrote.
    if rendered.startswith("~/"):
        rendered = os.path.expanduser(rendered)
    return rendered


def merge_variant(spec, variant_name):
    """Overlay a named variant on the base command spec."""
    variants = spec.get("variants", {})
    if variant_name is None:
        return dict(spec)
    if variant_name not in variants:
        raise Rejected(f"unknown variant {variant_name!r}; "
                       f"declared: {sorted(variants) or 'none'}")
    return overlay_spec(spec, variants[variant_name])


def overlay_spec(spec, overlay):
    """`spec` with `overlay` on top: its env merged in, every other key replaced."""
    merged = dict(spec)
    for key, value in overlay.items():
        if key == "env":
            merged["env"] = {**spec.get("env", {}), **value}
        else:
            merged[key] = value
    return merged


def merge_watch(name, spec, requested):
    """Apply the `--watch` modifier. It combines with a variant, and only a
    command that declares a "watch" block takes it: a bundler or a test runner
    has no native project to put a watch target in."""
    if requested is None or requested is False:
        return spec
    if requested is not True:
        raise Rejected("'watch' must be true")
    if "watch" not in spec:
        raise Rejected(f"command {name!r} does not take --watch; its config has no "
                       f"\"watch\" block")
    return overlay_spec(spec, spec["watch"])


def project_command(body, project, base, sim=None):
    name = field(body, "command", NAME_RE)
    commands = project["commands"]
    if name not in commands:
        raise Rejected(f"project {project['name']!r} declares no command {name!r}; "
                       f"available: {sorted(commands) or 'none'}")
    spec = merge_watch(name, merge_variant(commands[name], body.get("variant")),
                       body.get("watch"))
    # The config decides a command's platform, never the caller: it picks the
    # claim, the device and the bundler port the command runs against.
    platform = spec.get("platform", "ios")
    if platform not in ("ios", "android", "device"):
        raise Rejected(f"command {name!r} declares an unknown platform {platform!r}")
    if body.get("platform") not in (None, platform):
        raise Rejected(f"command {name!r} is an {platform} command; "
                       f"run it without the {body['platform']} prefix")
    android = android_config(project, body) if platform == "android" else None
    if android and sim:
        raise Rejected(f"command {name!r} runs on the Android emulator; drop --sim")
    device = device_config(project) if platform == "device" else None
    if device and sim:
        raise Rejected(f"command {name!r} runs on the physical device; drop --sim")
    if device and spec.get("ready_app"):
        raise Rejected(f"command {name!r}: ready_app watches a simulator; a device "
                       f"command is done when it exits")

    defaults = project["defaults"]
    values = {
        # {root} is the checkout being acted on, which is the project root
        # unless the caller is in a worktree. {project_root} is always the
        # literal root, for the rare command that must reach the main checkout.
        "root": str(base),
        "project_root": str(project["root"]),
        "device": field(body, "device", DEVICE_RE, required=False,
                        default=defaults.get("device")),
        "bundle_id": field(body, "bundle_id", BUNDLE_RE, required=False,
                           default=defaults.get("bundle_id")),
        "scheme": field(body, "scheme", SCHEME_RE, required=False,
                        default=defaults.get("scheme")),
        # {port} is the bundler port of the simulator being targeted: a private
        # simulator's own allocation, otherwise the project's shared default.
        # {sim} is the private simulator's name, empty for the shared one.
        "port": sim["port"] if sim else defaults.get("port"),
        "sim": sim["name"] if sim else "",
    }
    if android:
        values.update({"serial": android["serial"], "avd": android["avd"],
                       "package": android["package"], "port": android["port"],
                       "android_home": str(android["sdk"]),
                       "android_device": android["name"]})
    # Only resolve a UDID when the template actually asks for one: it costs a
    # simctl call and would fail on a machine with no matching simulator.
    templates = spec.get("argv", []) + [part for extra in spec.get("optional_argv", [])
                                        for part in extra.get("argv", [])]
    if device:
        values["device_id"] = device["id"]
        values["bundle_id"] = values["bundle_id"] or device["bundle_id"]
    if platform == "ios" and any("{udid}" in part for part in templates):
        values["udid"] = resolve_device(body, project)

    # Declared parameters, each validated by its own regex. Anything the caller
    # sends that the command did not declare is rejected, not ignored.
    supplied = body.get("args", {})
    if not isinstance(supplied, dict):
        raise Rejected("'args' must be an object")
    declared = spec.get("params", {})
    unknown = set(supplied) - set(declared)
    if unknown:
        raise Rejected(f"undeclared args for {name!r}: {', '.join(sorted(unknown))}")
    for pname, pspec in declared.items():
        raw = supplied.get(pname, pspec.get("default"))
        # An omitted optional parameter is empty, and an empty value is never
        # validated. Without this, a pattern like ^[A-Za-z0-9_,-]{1,64}$ rejects
        # the command's own default of "", making the parameter impossible to
        # omit -- and a "path": true parameter would fail on the empty path.
        if raw is None or raw == "":
            if pspec.get("required"):
                raise Rejected(f"missing required arg: {pname}")
            values[pname] = ""
            continue
        if pspec.get("path"):
            values[pname] = str(safe_path(raw, project, base=base,
                                          must_exist=pspec.get("must_exist", False)))
            continue
        pattern = re.compile(pspec.get("pattern", r"^[A-Za-z0-9._/-]{1,256}$"))
        if not isinstance(raw, str) or not pattern.match(raw):
            raise Rejected(f"invalid value for arg {pname}: {raw!r}")
        values[pname] = raw

    argv = [render(part, values) for part in spec.get("argv", [])]
    if not argv:
        raise Rejected(f"command {name!r} has an empty argv")
    # Optional trailing argv appended only when a param is non-empty, so an
    # unset optional flag does not leave a stray empty argument behind.
    for extra in spec.get("optional_argv", []):
        if values.get(extra.get("when")):
            argv += [render(part, values) for part in extra["argv"]]

    cwd = base
    if spec.get("cwd"):
        cwd = safe_path(spec["cwd"], project, base=base, must_exist=True)

    env = android_env(android) if android else dict(os.environ)
    for prefix in spec.get("path_prepend", []):
        env["PATH"] = f"{os.path.expanduser(prefix)}:{env.get('PATH', '')}"
    env.update({k: str(v) for k, v in spec.get("env", {}).items()})
    if spec.get("private_tmp"):
        # A bundler that caches under TMPDIR keys that cache by file content, not
        # by the env flags it inlines, so a build variant sharing the default
        # TMPDIR can silently ship another variant's bundle. One directory per
        # project, command and variant keeps them apart and keeps reuse.
        tmp = TMP_DIR / project["name"] / "-".join(filter(None, [name, body.get("variant")]))
        tmp.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(tmp)

    # Project commands hold the claim by default: most of them build, install,
    # or drive the simulator. Declare "exclusive": false for the read-only ones.
    extra = {"cwd": str(cwd), "env": env, "_exclusive": bool(spec.get("exclusive", True)),
             "_platform": platform}
    # Rendered like argv, so a command can free and probe "{port}" and stay
    # correct on a private simulator.
    if spec.get("kill_port"):
        rendered = render(str(spec["kill_port"]), values)
        if not rendered.isdigit():
            raise Rejected(f"kill_port of {name!r} rendered to {rendered!r}, not a port")
        extra["kill_port"] = int(rendered)
    # A command that holds the claim runs one at a time, so it can own one log
    # file. A non-exclusive one may overlap itself; it is short, and replies with
    # its output as before.
    if spec.get("log"):
        extra["log"] = render(spec["log"], values)
    elif extra["_exclusive"] or spec.get("detach"):
        # Named like a private simulator's, so two emulators running one command
        # never share a log: <command>@<device>.log, except on the default one.
        suffix = sim
        if android and android["name"] != android["default"]:
            suffix = {"name": android["name"]}
        extra["log"] = str(command_log_path(project, base, name, suffix))
    if spec.get("settle"):
        settle = spec["settle"]
        if not isinstance(settle, dict) or not settle.get("match"):
            raise Rejected(f"settle of {name!r} needs a \"match\" pattern")
        try:
            re.compile(settle["match"], re.M)
            re.compile(settle.get("fail_match") or "x", re.M)
            re.compile(settle.get("pass_match") or "x", re.M)
        except re.error as exc:
            raise Rejected(f"settle of {name!r}: bad pattern: {exc}")
        if spec.get("detach") or spec.get("exclusive", True) is False:
            raise Rejected(f"settle of {name!r} needs a logged, non-detached command")
        extra["settle"] = {"match": settle["match"], "fail_match": settle.get("fail_match"),
                           "pass_match": settle.get("pass_match"),
                           "after": settle_after(name, settle)}
    if spec.get("detach"):
        extra["detach"] = True
        extra["ready_url"] = render(spec["ready_url"], values) if spec.get("ready_url") else None
        extra["ready_match"] = spec.get("ready_match", "")
        if spec.get("ready_app") and android:
            extra["ready_app"] = {"adb": adb_base(android), "package": android["package"]}
        elif spec.get("ready_app"):
            # Ready only once the app is running on the device: a build that
            # also serves the bundle never exits, so its exit cannot be the signal.
            if not values["bundle_id"]:
                raise Rejected(f"ready_app of {name!r} needs a bundle_id")
            extra["ready_app"] = {"udid": values.get("udid") or resolve_device(body, project),
                                  "bundle_id": values["bundle_id"]}
        # What `mac-sim status` reports about the server this leaves running.
        extra["server"] = {"command": name, "variant": body.get("variant"),
                           "port": values["port"]}
    return argv, int(spec.get("timeout", DEFAULT_TIMEOUT)), extra


# --------------------------------------------------------------------------
# Built-in verbs
# --------------------------------------------------------------------------
def build_command(verb, body, config):
    """Resolve a request to (argv, timeout, extra, project).

    Every project-scoped verb is resolved from the caller's checkout rather than
    the project root, and is tagged with the claim it needs. `project` is None
    for the global verbs, otherwise the config entry the lock (if any) belongs
    to -- callers need it to translate a host path back to the guest's own view.
    """
    if verb == "browse":
        return ["open", browse_url(body, config)], 30, {}, None
    if verb in GLOBAL_VERBS:
        argv, timeout, extra = dispatch(verb, body, None, None)
        return argv, timeout, extra, None
    project = config.project(body.get("project"), body.get("workdir"))
    base = resolve_workdir(body, project)
    sim = resolve_sim(body, project)
    if sim:
        body = {**body, "udid": sim["udid"]}
    if body.get("platform") == "android" and verb != "project_command":
        if sim:
            raise Rejected("--sim names an iOS simulator; it does not apply to Android")
        argv, timeout, extra = android_dispatch(verb, body, project, base)
    else:
        argv, timeout, extra = dispatch(verb, body, project, base, sim)
    # A project command's platform comes from its spec; a built-in's from the request.
    platform = extra.pop("_platform", body.get("platform"))
    name = lock_name(project, sim, platform, body)
    extra["workdir"] = str(base)
    if "log" in extra:
        extra["guest_log"] = to_guest_path(project, extra["log"])
    if "server" in extra:
        extra["server"].update({"name": name, "workdir": str(base),
                                "guest_workdir": to_guest_path(project, base)})
    if extra.pop("_exclusive", verb in EXCLUSIVE_BUILTINS) and config.lock_enabled:
        extra["_lock"] = name
    return argv, timeout, extra, project


def simctl_log_argv(body, udid, default_bundle):
    """`log show` on one simulator, bounded: a finite dump, never a stream.
    Returns the argv and how many trailing lines to keep."""
    lines = body.get("lines", 200)
    if not isinstance(lines, int) or not 1 <= lines <= 5000:
        raise Rejected("lines must be an int in 1..5000")
    argv = ["xcrun", "simctl", "spawn", udid,
            "log", "show", "--style", "compact", "--last",
            field(body, "last", LAST_RE, required=False, default="2m")]
    if "predicate" in body:
        pred = body["predicate"]
        if not isinstance(pred, str) or len(pred) > 512 or '"' in pred:
            raise Rejected("invalid predicate")
        argv += ["--predicate", pred]
    elif not body.get("all"):
        # The simulator's own daemons write nearly all of the unified log, so
        # the unfiltered dump buries the app. Scope to its process by default.
        bundle = field(body, "bundle_id", BUNDLE_RE, required=False, default=default_bundle)
        if not bundle:
            raise Rejected("no bundle_id to scope the log to; pass --all for the "
                           "unfiltered log, or --predicate")
        argv += ["--predicate", f'process == "{app_executable(udid, bundle)}"']
    return argv, lines


def dispatch(verb, body, project, base, sim=None):
    if verb == "list":
        return ["xcrun", "simctl", "list", "devices", "available", "--json"], 60, {}
    if verb == "booted":
        return ["xcrun", "simctl", "list", "devices", "booted", "--json"], 60, {}
    if verb == "runtimes":
        return ["xcrun", "simctl", "list", "runtimes", "--json"], 60, {}
    if verb == "open_simulator_ui":
        return ["open", "-a", "Simulator"], 60, {}
    if verb == "boot":
        return ["xcrun", "simctl", "boot", resolve_device(body, project)], 180, {}
    if verb == "shutdown":
        return ["xcrun", "simctl", "shutdown", resolve_device(body, project)], 120, {}

    if verb == "install":
        app = safe_path(body.get("app"), project, base=base, must_exist=True)
        if app.suffix != ".app":
            raise Rejected("install requires a .app bundle")
        return (["xcrun", "simctl", "install", resolve_device(body, project), str(app)],
                300, {"bundle_id": app_bundle_id(app)})

    if verb in ("uninstall", "launch", "terminate"):
        bundle = field(body, "bundle_id", BUNDLE_RE, required=False,
                       default=project["defaults"].get("bundle_id"))
        if not bundle:
            raise Rejected("no bundle_id given and the project declares no default")
        udid = resolve_device(body, project)
        locale = field(body, "locale", LOCALE_RE, required=False)
        if locale is None:
            return ["xcrun", "simctl", verb, udid, bundle], 120, {}
        if verb != "launch":
            raise Rejected("locale applies to launch only")
        # Launch arguments reach only a fresh process: launching an app that is
        # already running just foregrounds it in its old language.
        argv = ["xcrun", "simctl", "launch", "--terminate-running-process", udid, bundle,
                "-AppleLanguages", f"({locale})"]
        if re.search(r"[_-]", locale):
            argv += ["-AppleLocale", locale.replace("-", "_")]
        return argv, 120, {}

    if verb == "screenshot":
        # Written inside the bind mount so the guest can read it back.
        out = safe_path(body.get("out"), project, base=base)
        # Resolve the device first: a bad device name must not leave stray dirs.
        udid = resolve_device(body, project)
        out.parent.mkdir(parents=True, exist_ok=True)
        return (["xcrun", "simctl", "io", udid, "screenshot", str(out)],
                120, {"path": str(out)})

    if verb == "openurl":
        url = body.get("url")
        if not isinstance(url, str) or not URL_RE.match(url):
            raise Rejected("openurl requires a scheme:// URL")
        return ["xcrun", "simctl", "openurl", resolve_device(body, project), url], 120, {}

    if verb == "appearance":
        udid = resolve_device(body, project)
        mode = body.get("mode")
        if mode is None:
            # Reading the current appearance changes nothing, so it needs no claim.
            return ["xcrun", "simctl", "ui", udid, "appearance"], 60, {"_exclusive": False}
        if mode not in ("light", "dark"):
            raise Rejected(f"appearance mode must be 'light' or 'dark', got {mode!r}")
        return ["xcrun", "simctl", "ui", udid, "appearance", mode], 60, {}

    if verb == "logs":
        argv, lines = simctl_log_argv(body, resolve_device(body, project),
                                      project["defaults"].get("bundle_id"))
        return argv, 180, {"tail": lines}

    if verb == "xcodebuild":
        defaults = project["defaults"]
        workspace = safe_path(body.get("workspace") or defaults.get("workspace", ""),
                              project, base=base, must_exist=True)
        scheme = field(body, "scheme", SCHEME_RE, required=False,
                       default=defaults.get("scheme"))
        if not scheme:
            raise Rejected("no scheme given and the project declares no default")
        config_name = field(body, "configuration", re.compile(r"^(Debug|Release)$"),
                            required=False, default="Debug")
        derived = safe_path(body.get("derived_data", ".mac-sim-build"),
                            project, base=base)
        return (["xcodebuild", "-workspace", str(workspace), "-scheme", scheme,
                 "-configuration", config_name, "-destination",
                 f"id={resolve_device(body, project)}",
                 "-derivedDataPath", str(derived), "build"],
                int(body.get("timeout", 2400)) if isinstance(body.get("timeout"), int) else 2400,
                {})

    if verb == "project_command":
        return project_command(body, project, base, sim)

    if verb in WATCH_VERBS:
        return watch_dispatch(verb, body, project, base, sim)

    if verb in DEVICE_VERBS:
        return device_dispatch(verb, body, project, base, sim)

    raise Rejected(f"unknown verb: {verb!r}")


# --------------------------------------------------------------------------
# Apple Watch
# --------------------------------------------------------------------------
# A watch app is built by target for the watch simulator (no scheme, no pods) and
# runs on the watch paired with the iPhone simulator the request targets, so it
# shares that simulator's claim: `--sim mine watch launch` and `--sim mine
# launch` are one device pair, and two checkouts never drive it at once.
#
# The `watch` block of the project config names the project, the target and the
# bundle id; a project without it has no watch verbs at all.
def watch_config(project):
    cfg = project.get("watch")
    if cfg is None:
        raise Rejected(
            f"project {project['name']!r} declares no \"watch\" block in the host "
            f"config, so it has no watch verbs. See docs/MAC-SIM.md.")
    return cfg


def watch_app_path(cfg, project, base):
    """Where `watch_build` leaves the app."""
    product = cfg.get("product", cfg["target"])
    configuration = cfg.get("configuration", "Debug")
    return safe_path(f"{WATCH_BUILD_DIR}/{configuration}-watchsimulator/{product}.app",
                     project, base=base)


def paired_watches(phone_udid):
    pairs = json.loads(run_host(["xcrun", "simctl", "list", "pairs", "--json"], 60))
    return [pair["watch"] for pair in pairs.get("pairs", {}).values()
            if pair.get("phone", {}).get("udid") == phone_udid]


def resolve_watch(body, project):
    """The UDID of the watch paired with the iPhone simulator the request targets."""
    watch_config(project)
    phone = resolve_device(body, project)
    watches = paired_watches(phone)
    if not watches:
        raise Rejected(
            f"the iPhone simulator {phone} has no paired watch. `mac-sim sim create "
            f"<name> --with-watch` makes a private pair; for the shared simulator, "
            f"pair one by hand (`xcrun simctl pair <watch> {phone}`).")
    if len(watches) > 1:
        booted = [w for w in watches if w.get("state") == "Booted"]
        if len(booted) != 1:
            raise Rejected(
                f"the iPhone simulator {phone} is paired with {len(watches)} watches "
                f"({', '.join(w.get('name', '?') + ' ' + w['udid'] for w in watches)}) "
                f"and not exactly one is booted; unpair the extra ones with "
                f"`xcrun simctl unpair`.")
        return booted[0]["udid"]
    return watches[0]["udid"]


def watch_dispatch(verb, body, project, base, sim=None):
    cfg = watch_config(project)

    if verb == "watch_build":
        xcodeproj = safe_path(cfg["project"], project, base=base, must_exist=True)
        if xcodeproj.suffix != ".xcodeproj":
            raise Rejected(f"watch.project must be an .xcodeproj, got {xcodeproj}")
        out = safe_path(WATCH_BUILD_DIR, project, base=base)
        # By target, not scheme: the generated project may carry no watch scheme,
        # and the watch target needs nothing from the workspace's pods.
        argv = ["xcodebuild", "-project", str(xcodeproj), "-target", cfg["target"],
                "-sdk", "watchsimulator",
                "-configuration", cfg.get("configuration", "Debug"),
                f"SYMROOT={out}", f"OBJROOT={out}/obj", "build"]
        return argv, 2400, {
            "cwd": str(base), "app": str(watch_app_path(cfg, project, base)),
            "log": str(command_log_path(project, base, "watch-build", sim))}

    udid = resolve_watch(body, project)
    bundle = field(body, "bundle_id", BUNDLE_RE, required=False, default=cfg["bundle_id"])

    if verb == "watch_boot":
        return ["xcrun", "simctl", "boot", udid], 180, {}

    if verb == "watch_install":
        raw = body.get("app")
        app = safe_path(raw, project, base=base, must_exist=True) if raw \
            else watch_app_path(cfg, project, base)
        if not app.exists():
            raise Rejected(f"no watch app at {app}; run `mac-sim watch build` first")
        if app.suffix != ".app":
            raise Rejected("watch install requires a .app bundle")
        return (["xcrun", "simctl", "install", udid, str(app)], 300,
                {"bundle_id": app_bundle_id(app)})

    if verb == "watch_launch":
        args = body.get("args", [])
        if not isinstance(args, list) or len(args) > MAX_LAUNCH_ARGS or not all(
                isinstance(a, str) and LAUNCH_ARG_RE.match(a) for a in args):
            raise Rejected(f"launch args must be at most {MAX_LAUNCH_ARGS} plain tokens "
                           f"({LAUNCH_ARG_RE.pattern}), got {args!r}")
        # Arguments reach only a fresh process: launching a running app just
        # foregrounds it with the arguments it already had.
        return (["xcrun", "simctl", "launch", "--terminate-running-process", udid, bundle]
                + args, 120, {})

    if verb == "watch_terminate":
        return ["xcrun", "simctl", "terminate", udid, bundle], 120, {}

    if verb == "watch_screenshot":
        out = safe_path(body.get("out"), project, base=base)
        out.parent.mkdir(parents=True, exist_ok=True)
        return (["xcrun", "simctl", "io", udid, "screenshot", str(out)], 120,
                {"path": str(out)})

    if verb == "watch_logs":
        argv, lines = simctl_log_argv({**body, "bundle_id": bundle}, udid, bundle)
        return argv, 180, {"tail": lines}

    raise Rejected(f"unknown watch verb: {verb!r}")


# --------------------------------------------------------------------------
# A physical iPhone
# --------------------------------------------------------------------------
# One device, named in the host config, and nothing a caller can redirect: the
# verbs take no device argument, so a guest cannot point them at another phone.
# They hold a claim of their own (<project>@device), apart from every simulator's.
def device_config(project):
    cfg = project.get("device")
    if cfg is None:
        raise Rejected(
            f"project {project['name']!r} declares no \"device\" block in the host "
            f"config, so it has no physical device. See docs/MAC-SIM.md.")
    return cfg


def devicectl_json(argv, key):
    """Run a devicectl query that writes JSON, and return `result[key]`. A shape
    other than the expected one is an error that shows what came back."""
    with tempfile.TemporaryDirectory(prefix="mac-sim-device-") as tmp:
        out = Path(tmp) / "out.json"
        try:
            run_host(argv + ["--json-output", str(out)], 60)
        except HostFailure as exc:
            raise Rejected(f"{exc}")
        raw = out.read_text()
    try:
        items = json.loads(raw)["result"][key]
    except (ValueError, KeyError, TypeError):
        items = None
    if not isinstance(items, list):
        raise Rejected(f"devicectl's answer has no result.{key} list, so this shim "
                       f"cannot read it: {raw[:500]}")
    return items


def device_pid(device_id, bundle):
    """The pid of the app's process on the device. devicectl terminates by pid and
    lists processes by executable path, so the app's bundle path is the join."""
    base = ["xcrun", "devicectl", "device", "info"]
    apps = devicectl_json(base + ["apps", "--device", device_id, "--bundle-id", bundle], "apps")
    bundles = [a for a in apps if a.get("bundleIdentifier") == bundle and a.get("url")]
    if not bundles:
        raise Rejected(f"{bundle} is not installed on the device, so there is nothing to "
                       f"terminate. Install it first (`mac-sim device install`).")
    app_url = bundles[0]["url"].rstrip("/") + "/"
    procs = devicectl_json(base + ["processes", "--device", device_id], "runningProcesses")
    pids = [p["processIdentifier"] for p in procs
            if str(p.get("executable", "")).startswith(app_url)
            and isinstance(p.get("processIdentifier"), int)]
    if not pids:
        raise Rejected(f"{bundle} is not running on the device.")
    return pids[0]


def device_dispatch(verb, body, project, base, sim=None):
    if verb == "device_list":
        return ["xcrun", "devicectl", "list", "devices"], 60, {}
    cfg = device_config(project)
    if sim:
        raise Rejected("--sim names a simulator; it does not apply to the physical device")
    device_id = cfg["id"]
    bundle = field(body, "bundle_id", BUNDLE_RE, required=False, default=cfg["bundle_id"])
    # The claim is the device's, not the shared simulator's.
    extra = {"_platform": "device"}

    if verb == "device_install":
        app = safe_path(body.get("app"), project, base=base, must_exist=True)
        if app.suffix != ".app":
            raise Rejected("device install requires a .app bundle")
        return (["xcrun", "devicectl", "device", "install", "app", "--device", device_id,
                 str(app)], 600, {**extra, "bundle_id": app_bundle_id(app)})

    if verb == "device_launch":
        args = body.get("args", [])
        if not isinstance(args, list) or len(args) > MAX_LAUNCH_ARGS or not all(
                isinstance(a, str) and LAUNCH_ARG_RE.match(a) for a in args):
            raise Rejected(f"launch args must be at most {MAX_LAUNCH_ARGS} plain tokens "
                           f"({LAUNCH_ARG_RE.pattern}), got {args!r}")
        return (["xcrun", "devicectl", "device", "process", "launch", "--device", device_id,
                 "--terminate-existing", bundle] + args, 120, extra)

    if verb == "device_terminate":
        # A tool's terminate is a kill from outside, not a user swiping the app
        # away: iOS treats the two differently (background refresh, for one).
        pid = device_pid(device_id, bundle)
        return (["xcrun", "devicectl", "device", "process", "terminate", "--device",
                 device_id, "--pid", str(pid)], 120, extra)

    if verb == "device_pull":
        raw = body.get("path")
        if not (isinstance(raw, str) and DEVICE_PULL_RE.match(raw)
                and ".." not in raw.split("/")):
            raise Rejected(f"invalid device path {raw!r}: plain relative segments only")
        if not raw.startswith(cfg["pull_prefix"]) or raw == cfg["pull_prefix"]:
            raise Rejected(f"device pull is limited to files under {cfg['pull_prefix']} "
                           f"in the app's data container, got {raw!r}")
        out = safe_path(body.get("out"), project, base=base)
        out.parent.mkdir(parents=True, exist_ok=True)
        return (["xcrun", "devicectl", "device", "copy", "from", "--device", device_id,
                 "--domain-type", "appDataContainer", "--domain-identifier", bundle,
                 "--source", raw, "--destination", str(out)], 300,
                {**extra, "path": str(out)})

    raise Rejected(f"unknown device verb: {verb!r}")


# --------------------------------------------------------------------------
# Private simulators
# --------------------------------------------------------------------------
# The shared simulator serialises every checkout behind one claim. A private
# simulator removes that for the common case, a change that is JS only: it gets a
# copy of the app already installed on the shared device, its own bundler port,
# and its own claim, so two worktrees verify at the same time.
#
# Everything project-specific is declared in the project's "simulators" block
# (which preferences to copy, which defaults point the app at its bundler); the
# steps themselves are fixed here. A project without that block has no private
# simulators at all, so the verbs stay inert until the host owner opts in.
_SIMS_LOCK = threading.Lock()


def _sims_path(project_name):
    return SIM_DIR / f"{project_name}.json"


def read_sims(project_name):
    try:
        with open(_sims_path(project_name)) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def write_sims(project_name, sims):
    SIM_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _sims_path(project_name).with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(sims, fh, indent=2)
    tmp.replace(_sims_path(project_name))


def lock_name(project, sim=None, platform=None, body=None):
    """One claim per device: the shared simulator's is the project name, an
    Android emulator's is <project>@android[-<name>] (see android_config)."""
    if platform == "android":
        return android_config(project, body)["lock"]
    if platform == "device":
        return f"{project['name']}@device"
    return f"{project['name']}@{sim['name']}" if sim else project["name"]


def resolve_sim(body, project):
    """The private simulator a request targets, or None for the shared one."""
    name = field(body, "sim", SIM_NAME_RE, required=False)
    if name is None:
        return None
    if "device" in body or "udid" in body:
        raise Rejected("'sim' names the device already; do not also send device/udid")
    entry = read_sims(project["name"]).get(name)
    if not entry or entry.get("state") != "ready":
        raise Rejected(
            f"project {project['name']!r} has no private simulator {name!r}"
            + (" (it is still being created)" if entry else "")
            + f". `mac-sim sim list` shows them; `mac-sim sim create {name}` makes one.")
    return {"name": name, **entry}


def run_host(argv, timeout=120, binary=False, env=None):
    log(f"RUN {shlex.join(argv)}")
    try:
        proc = subprocess.run(argv, capture_output=True, text=not binary,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise HostFailure(argv, 124, f"timed out after {timeout}s")
    if proc.returncode != 0:
        err = proc.stderr or proc.stdout
        if binary:
            err = err.decode(errors="replace")
        raise HostFailure(argv, proc.returncode, err.strip())
    return proc.stdout if binary else proc.stdout.strip()


def simctl_devices():
    """Every simulator on the Mac, keyed by UDID, with its runtime attached."""
    listing = json.loads(run_host(["xcrun", "simctl", "list", "devices", "--json"], 60))
    return {d["udid"]: {**d, "runtime": runtime}
            for runtime, devices in listing.get("devices", {}).items() for d in devices}


def simulators_config(project):
    cfg = project.get("simulators")
    if cfg is None:
        raise Rejected(
            f"project {project['name']!r} declares no \"simulators\" block in the host "
            f"config, so it has only the shared simulator. See docs/MAC-SIM.md.")
    return cfg


# A freshly booted iOS 26 simulator refuses its first install for several
# seconds after `bootstatus -b` reports the boot finished ("Failed to set icon
# resources promise ... Failed to create promise", IXErrorDomain code 2); the
# same install a few seconds later succeeds. Only that refusal is retried.
INSTALL_NOT_READY = "Failed to create promise"
INSTALL_ATTEMPTS = 6
INSTALL_RETRY_DELAY = 5


def install_when_ready(udid, app):
    for attempt in range(1, INSTALL_ATTEMPTS + 1):
        try:
            return run_host(["xcrun", "simctl", "install", udid, app], 300)
        except HostFailure as exc:
            if INSTALL_NOT_READY not in exc.output:
                raise
            if attempt == INSTALL_ATTEMPTS:
                raise HostFailure(exc.argv, exc.code,
                                  f"{exc.output}\n(the simulator still refused the install "
                                  f"after {INSTALL_ATTEMPTS} attempts {INSTALL_RETRY_DELAY}s apart)")
            log(f"install on {udid}: simulator not ready to install yet "
                f"(attempt {attempt}/{INSTALL_ATTEMPTS}), retrying in {INSTALL_RETRY_DELAY}s")
            time.sleep(INSTALL_RETRY_DELAY)


def patched_app_copy(app, keys, workdir):
    """A copy of `app` under `workdir` with `keys` set in its Info.plist, ad-hoc
    re-signed so the edit does not break the seal installd checks. The shared
    simulator's own bundle is never touched."""
    copy = Path(workdir) / Path(app).name
    run_host(["ditto", app, str(copy)], 300)
    info = copy / "Info.plist"
    raw = info.read_bytes()
    fmt = plistlib.FMT_BINARY if raw.startswith(b"bplist") else plistlib.FMT_XML
    plist = plistlib.loads(raw)
    plist.update(keys)
    info.write_bytes(plistlib.dumps(plist, fmt=fmt))
    run_host(["codesign", "--force", "--sign", "-",
              "--preserve-metadata=entitlements,flags", str(copy)], 120)
    return str(copy)


def sim_device_type(name, runtime_id):
    """The simctl device type named `name` that the shared simulator's runtime
    can run (an iPad for a tablet layout), as its identifier."""
    types = json.loads(run_host(["xcrun", "simctl", "list", "devicetypes", "--json"], 60))
    runtimes = json.loads(run_host(["xcrun", "simctl", "list", "runtimes", "--json"], 60))
    runtime = next((r for r in runtimes.get("runtimes", []) if r.get("identifier") == runtime_id), {})
    supported = {d.get("identifier"): d.get("name") for d in runtime.get("supportedDeviceTypes", [])}
    wanted = next((t for t in types.get("devicetypes", []) if t.get("name") == name), None)
    if wanted is None or wanted["identifier"] not in supported:
        known = sorted(n for n in supported.values() if n and ("iPad" in n or "iPhone" in n))
        raise Rejected(f"{name!r} is not a simulator device type the shared simulator's "
                       f"runtime ({runtime.get('name', runtime_id)}) runs; it runs: "
                       f"{', '.join(known) or 'none'}")
    return wanted["identifier"]


def create_paired_watch(device_name, phone_udid, cfg):
    """Create a watch simulator of the configured model on the newest watchOS
    runtime installed, pair it with the iPhone and boot it. Returns its UDID."""
    types = json.loads(run_host(["xcrun", "simctl", "list", "devicetypes", "--json"], 60))
    wanted = next((t for t in types.get("devicetypes", []) if t.get("name") == cfg["device"]), None)
    if wanted is None:
        known = sorted(t["name"] for t in types.get("devicetypes", [])
                       if "Watch" in t.get("name", ""))
        raise Rejected(f"watch.device {cfg['device']!r} is not a simulator device type "
                       f"on this Mac; watch models: {', '.join(known) or 'none'}")
    runtimes = json.loads(run_host(["xcrun", "simctl", "list", "runtimes", "--json"], 60))
    pinned = cfg.get("runtime")
    usable = [r for r in runtimes.get("runtimes", [])
              if r.get("platform") == "watchOS" and r.get("isAvailable")
              and wanted["identifier"] in [d.get("identifier") for d in r.get("supportedDeviceTypes", [])]
              and (pinned is None or pinned in (r.get("identifier"), r.get("version"), r.get("name")))]
    if not usable:
        raise Rejected(
            f"no available watchOS runtime supports {cfg['device']!r}"
            + (f" and matches watch.runtime {pinned!r}" if pinned else "")
            + ". Install one in Xcode (Settings > Components).")
    runtime = max(usable, key=lambda r: [int(part) for part in re.findall(r"\d+", r.get("version", "0"))])
    watch_udid = run_host(["xcrun", "simctl", "create", device_name,
                           wanted["identifier"], runtime["identifier"]], 120)
    if not UDID_RE.match(watch_udid):
        raise HostFailure(["xcrun", "simctl", "create"], 1,
                          f"expected a UDID, got {watch_udid!r}")
    try:
        run_host(["xcrun", "simctl", "pair", watch_udid, phone_udid], 120)
        run_host(["xcrun", "simctl", "boot", watch_udid], 180)
        run_host(["xcrun", "simctl", "bootstatus", watch_udid, "-b"], 600)
    except BaseException:
        subprocess.run(["xcrun", "simctl", "delete", watch_udid], capture_output=True)
        raise
    return watch_udid


def _app_cache_dir(project_name):
    return APP_CACHE_DIR / project_name


def cached_app(project_name):
    """The project's current cached build as (id, manifest, .app path), or None
    when nothing was saved."""
    pdir = _app_cache_dir(project_name)
    try:
        build = (pdir / "current").read_text().strip()
    except FileNotFoundError:
        return None
    manifest_file = pdir / build / "manifest.json"
    try:
        manifest = json.loads(manifest_file.read_text())
    except FileNotFoundError:
        raise HostFailure(["app_cache"], 1,
                          f"{pdir / 'current'} names build {build!r}, which has no "
                          f"{manifest_file}; save a build again (`mac-sim app-cache save`)")
    return build, manifest, pdir / build / manifest["app"]


def require_cached_app(project_name):
    found = cached_app(project_name)
    if found is None:
        raise Rejected(
            f"no cached build of {project_name}'s app; build it on the shared simulator "
            f"(`mac-sim run`), then `mac-sim app-cache save` in the same checkout")
    return found


def manifest_data(manifest):
    """A manifest as the JSON answers carry it: its checkout as the guest sees it."""
    data = {k: v for k, v in manifest.items() if k != "guest_workdir"}
    data["workdir"] = manifest.get("guest_workdir") or manifest["workdir"]
    return data


def app_cache_save(body, project, base):
    """Copy the app installed on the shared simulator into the project's cache and
    make it current. Runs under the shared simulator's claim (see _direct), so no
    other checkout can install over the build between its run and this copy."""
    pname = project["name"]
    bundle = project["defaults"].get("bundle_id")
    if not bundle:
        raise Rejected(f"project {pname!r} sets no defaults.bundle_id, so there is no app to cache")
    source_udid = resolve_device({}, project)
    source = simctl_devices()[source_udid]
    if source["state"] != "Booted":
        raise Rejected(f"the shared simulator {source['name']!r} is not booted, so its "
                       f"installed app cannot be read. Build it there first (`mac-sim run`).")
    try:
        app = Path(run_host(["xcrun", "simctl", "get_app_container",
                             source_udid, bundle, "app"], 60))
    except HostFailure as exc:
        raise Rejected(f"{bundle} is not installed on the shared simulator "
                       f"{source['name']!r}; build it there first (`mac-sim run`). [{exc}]")
    build = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"
    manifest = {"id": build, "bundle_id": bundle, "app": app.name, "at": time.time(),
                "workdir": str(base), "guest_workdir": to_guest_path(project, base),
                "device": source["name"], "runtime": source["runtime"]}
    pdir = _app_cache_dir(pname)
    pdir.mkdir(parents=True, exist_ok=True)
    # Written beside its final place and renamed in, so a half copy is never current.
    tmp = pdir / f".tmp-{build}"
    try:
        run_host(["ditto", str(app), str(tmp / app.name)], 300)
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))
        tmp.rename(pdir / build)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    current = pdir / "current.tmp"
    current.write_text(build)
    current.replace(pdir / "current")
    builds = sorted(p.name for p in pdir.iterdir() if p.is_dir() and not p.name.startswith("."))
    for old in builds[:-APP_CACHE_KEEP]:
        shutil.rmtree(pdir / old)
    return (f"cached {bundle} from {source['name']!r} as build {build}; private "
            f"simulators install it from now on",
            {"data": {"project": pname, "current": build, "manifest": manifest_data(manifest)}})


def app_cache_status(body, project, base):
    pname = project["name"]
    found = cached_app(pname)
    source = simctl_devices()[resolve_device({}, project)]
    pdir = _app_cache_dir(pname)
    builds = (sorted(p.name for p in pdir.iterdir() if p.is_dir() and not p.name.startswith("."))
              if pdir.is_dir() else [])
    data = {"project": pname, "current": found[0] if found else None,
            "manifest": manifest_data(found[1]) if found else None, "builds": builds,
            "base": {"name": source["name"], "udid": source["udid"], "state": source["state"]}}
    if found:
        m = found[1]
        text = (f"{pname}: current build {found[0]} ({m['bundle_id']}, from "
                f"{m.get('guest_workdir') or m['workdir']}, "
                f"{int(time.time() - float(m['at'])) // 60}m old)")
    else:
        text = f"{pname}: no cached build (`mac-sim run`, then `mac-sim app-cache save`)"
    return f"{text}\n  shared simulator {source['name']!r}: {source['state']}", {"data": data}


def sim_create(body, project, base):
    cfg = simulators_config(project)
    pname = project["name"]
    name = field(body, "name", SIM_NAME_RE)
    if name == "android":
        raise Rejected("'android' names the Android emulator's claim; pick another name")
    with_watch = body.get("with_watch", False)
    if with_watch not in (True, False):
        raise Rejected("'with_watch' must be true or false")
    watch_cfg = watch_config(project) if with_watch else None
    # Another model than the shared simulator's (an iPad beside the iPhone), on
    # the same runtime, so the cached build of its app runs on it unchanged.
    device_type = field(body, "device_type", DEVICE_RE, required=False)
    if device_type and with_watch and "iPhone" not in device_type:
        raise Rejected(f"a watch pairs only with an iPhone, not {device_type!r}; "
                       f"drop --with-watch or --type")
    limit = int(cfg.get("max", 3))
    port_base = int(cfg.get("port_base", 8090))

    # Reserve the name and the port under the mutex, then do the slow work
    # outside it: a boot takes the better part of a minute.
    with _SIMS_LOCK:
        sims = read_sims(pname)
        if name in sims:
            raise Rejected(f"simulator {name!r} already exists; target it with "
                           f"--sim {name}, or `mac-sim sim delete {name}` first")
        if len(sims) >= limit:
            raise Rejected(
                f"{pname} already has {len(sims)} private simulators "
                f"({', '.join(sorted(sims))}), the configured maximum. Each booted "
                f"one costs a few GB of RAM; delete one first.")
        taken = {entry.get("port") for entry in sims.values()}
        port = next((p for p in range(port_base, port_base + 200)
                     if p not in taken and not port_listeners(p)), None)
        if port is None:
            raise Rejected(f"no free bundler port in {port_base}..{port_base + 199}")
        sims[name] = {"state": "creating", "port": port, "workdir": str(base),
                      "guest_workdir": to_guest_path(project, base), "created": time.time()}
        write_sims(pname, sims)

    udid, watch_udid, notes = None, None, []
    try:
        source_udid = resolve_device({}, project)
        source = simctl_devices()[source_udid]
        app, build = None, None
        if cfg.get("clone_app", True):
            # From the app cache, never the shared simulator itself, which may be
            # shut down and may since hold another checkout's build.
            build, _, cached = require_cached_app(pname)
            app = str(cached)

        type_id = (sim_device_type(device_type, source["runtime"]) if device_type
                   else source["deviceTypeIdentifier"])
        udid = run_host(["xcrun", "simctl", "create", f"{pname}-{name}",
                         type_id, source["runtime"]], 120)
        if not UDID_RE.match(udid):
            raise HostFailure(["xcrun", "simctl", "create"], 1,
                              f"expected a UDID, got {udid!r}")

        # Copied while the new device is still shut down, so the system reads
        # them on its first boot.
        for pref in cfg.get("copy_prefs", []):
            src = Path(source["dataPath"]) / "Library" / "Preferences" / pref
            if not src.is_file():
                notes.append(f"note: the shared simulator has no {pref}; not copied")
                continue
            dst_dir = Path(simctl_devices()[udid]["dataPath"]) / "Library" / "Preferences"
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst_dir / pref)

        run_host(["xcrun", "simctl", "boot", udid], 180)
        run_host(["xcrun", "simctl", "bootstatus", udid, "-b"], 600)
        if app:
            # Values the app reads from its own bundle, so they hold on every
            # launch: Expo's debug app rewrites its jsLocation from Info.plist
            # `RCTMetroPort` at start, which no defaults domain overrides.
            keys = {key: render(str(template), {"port": port, "sim": name})
                    for key, template in cfg.get("info_plist", {}).items()}
            if keys:
                with tempfile.TemporaryDirectory(prefix="mac-sim-app-") as workdir:
                    install_when_ready(udid, patched_app_copy(app, keys, workdir))
            else:
                install_when_ready(udid, app)
        # The global domain, not the app's: an app-state reset wipes the app's own
        # defaults and would silently send it back to the shared bundler port.
        for key, template in cfg.get("global_defaults", {}).items():
            run_host(["xcrun", "simctl", "spawn", udid, "defaults", "write", "-g",
                      key, render(str(template), {"port": port, "sim": name})], 60)
        if with_watch:
            watch_udid = create_paired_watch(f"{pname}-{name}-watch", udid, watch_cfg)
    except BaseException:
        # The watch first: a device is paired to the phone until one of them goes.
        for leftover in (watch_udid, udid):
            if leftover and UDID_RE.match(leftover):
                subprocess.run(["xcrun", "simctl", "delete", leftover], capture_output=True)
        with _SIMS_LOCK:
            sims = read_sims(pname)
            sims.pop(name, None)
            write_sims(pname, sims)
        raise

    with _SIMS_LOCK:
        sims = read_sims(pname)
        sims[name].update({"state": "ready", "udid": udid})
        if device_type:
            sims[name]["device_type"] = device_type
        if watch_udid:
            sims[name]["watch_udid"] = watch_udid
        write_sims(pname, sims)
    lines = [f"created simulator {name!r} ({pname}-{name})",
             f"  udid: {udid}",
             f"  device: {device_type or source['name']}",
             f"  bundler port: {port}",
             f"  app: {f'build {build} from the app cache' if app else 'none installed'}"]
    if watch_udid:
        lines.append(f"  watch: {watch_udid} (paired, booted; shares this claim)")
    lines.append(f"Target it with --sim {name} on any command.")
    return "\n".join(lines + notes), {"sim": name, "udid": udid, "port": port,
                                      "watch_udid": watch_udid, "app_build": build}


def sim_delete(body, project, base):
    simulators_config(project)
    pname = project["name"]
    name = field(body, "name", SIM_NAME_RE)
    entry = read_sims(pname).get(name)
    if not entry:
        raise Rejected(f"{pname} has no private simulator {name!r}; only simulators "
                       f"created with `mac-sim sim create` can be deleted")
    kill_port(entry["port"])  # its bundler, which serves nothing once the device is gone
    # The watch first: a device is paired to the phone until one of them goes.
    devices = simctl_devices()
    for udid in (entry.get("watch_udid"), entry.get("udid")):
        device = devices.get(udid) if udid else None
        if device:
            if device["state"] != "Shutdown":
                run_host(["xcrun", "simctl", "shutdown", udid], 120)
            run_host(["xcrun", "simctl", "delete", udid], 120)
    with _SIMS_LOCK:
        sims = read_sims(pname)
        sims.pop(name, None)
        write_sims(pname, sims)
    return f"deleted simulator {name!r}", {"sim": name}


def sim_list(body, project, base):
    cfg = simulators_config(project)
    pname = project["name"]
    sims = read_sims(pname)
    limit = int(cfg.get("max", 3))
    data = {"project": pname, "max": limit, "simulators": []}
    if not sims:
        return (f"{pname}: no private simulators (max {limit})",
                {"simulators": {}, "data": data})
    devices = simctl_devices()
    lines = [f"{pname}: {len(sims)} of {limit} private simulators"]
    for name, entry in sorted(sims.items()):
        device = devices.get(entry.get("udid"))
        if entry.get("state") != "ready":
            state = kind = "creating"
        elif device is None:
            state = f"GONE (deleted outside mac-sim; `mac-sim sim delete {name}` clears it)"
            kind = "gone"
        else:
            state = kind = device["state"]
        age = int(time.time() - float(entry.get("created", 0)))
        lock = f"{pname}@{name}"
        data["simulators"].append({
            "name": name, "state": kind, "udid": entry.get("udid"),
            "port": entry.get("port"), "watch_udid": entry.get("watch_udid"),
            "device_type": entry.get("device_type"),
            "workdir": entry.get("guest_workdir") or entry.get("workdir"),
            "created": entry.get("created"), "age": age,
            "claim": owner_data(lock, Handler.config.lock_ttl)})
        lines.append(f"  {name:<16} {state:<10} port {entry.get('port')}  "
                     f"{entry.get('udid', '-')}"
                     f"{'  +watch ' + entry['watch_udid'] if entry.get('watch_udid') else ''}  from "
                     f"{entry.get('guest_workdir') or entry.get('workdir')}, "
                     f"{age // 3600}h {age % 3600 // 60}m old")
    return "\n".join(lines), {"simulators": sims, "data": data}


def pull(body, project, base):
    """Copy one file out of the app's data container into the workspace, so a
    guest can inspect what the app actually wrote (its database, a settings file)."""
    aliases = project["pull"]
    what = body.get("path")
    rel = aliases.get(what, what) if isinstance(what, str) else None
    if not isinstance(rel, str) or not rel or len(rel) > 512 or os.path.isabs(rel):
        raise Rejected("pull needs a path relative to the app's data container"
                       + (f", or one of: {', '.join(sorted(aliases))}" if aliases else ""))
    bundle = field(body, "bundle_id", BUNDLE_RE, required=False,
                   default=project["defaults"].get("bundle_id"))
    if not bundle:
        raise Rejected("no bundle_id given and the project declares no default")
    sim = resolve_sim(body, project)
    udid = sim["udid"] if sim else resolve_device(body, project)
    out = safe_path(body.get("out"), project, base=base)
    container = Path(run_host(["xcrun", "simctl", "get_app_container",
                               udid, bundle, "data"], 60)).resolve()
    src = (container / rel).resolve()
    if container not in src.parents:
        raise Rejected(f"{rel!r} resolves outside the data container of {bundle}")
    if not src.is_file():
        raise Rejected(f"no file {rel!r} in the data container of {bundle} on that simulator")
    size = src.stat().st_size
    if size > MAX_PULL_BYTES:
        raise Rejected(f"{rel!r} is {size} bytes, over the {MAX_PULL_BYTES} byte limit")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fh:
        is_sqlite = fh.read(16) == b"SQLite format 3\x00"
    if is_sqlite:
        # A live database keeps recent writes in its -wal file, so copying the
        # main file alone yields a stale or torn snapshot. The backup API reads
        # through the WAL and writes one self-contained file.
        out.unlink(missing_ok=True)
        source = sqlite3.connect(f"file:{quote(str(src))}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(str(out))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    else:
        shutil.copyfile(src, out)
    return (f"{out} ({out.stat().st_size} bytes{', sqlite snapshot' if is_sqlite else ''})",
            {"path": str(out)})


# The server a detached command left running (a bundler, usually), recorded so
# `mac-sim status` can say which build profile it carries: the server itself
# cannot report the flags it was started with.
def _server_path(name):
    return LOCK_DIR / f"{name}.server.json"


def write_server(record):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _server_path(record["name"]).with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(record, fh)
    tmp.replace(_server_path(record["name"]))


def read_server(name):
    """The recorded server, only while the port is still held by the process
    group that record describes. Anything else listening there was started some
    other way, and its flags are unknown."""
    try:
        with open(_server_path(name)) as fh:
            record = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    for pid in port_listeners(record["port"]):
        try:
            if os.getpgid(int(pid)) == record["pid"]:
                return record
        except (ProcessLookupError, PermissionError, ValueError):
            pass
    return None


def status(body, project, base):
    """One objective answer to "can I use the app now?": the device, the claim,
    what is executing, the bundler and the app, checked on the host."""
    sim = resolve_sim(body, project)
    name = lock_name(project, sim)
    defaults = project["defaults"]
    udid = sim["udid"] if sim else resolve_device(body, project)
    device = simctl_devices().get(udid)
    if device is None:
        raise Rejected(f"simulator {udid} no longer exists on the Mac")
    booted = device["state"] == "Booted"
    lines = [f"simulator: {device['name']} ({udid}) {device['state']}"]

    owner_text = describe_owner(name, Handler.config.lock_ttl)
    lines.append("claim: " + owner_text.split(": ", 1)[1])
    executing = _executing_info(name)
    if executing and executing.get("log"):
        lines.append(f"  log: {executing['log']}")

    port = sim["port"] if sim else defaults.get("port")
    serving = bundler = None
    if port:
        serving, line = describe_bundler(name, port)
        lines.append(line)
        bundler = bundler_data(name, port, serving)

    bundle = defaults.get("bundle_id")
    running = None
    if bundle:
        running = booted and app_running(udid, bundle)
        lines.append(f"app: {bundle} {'running' if running else 'not running'}")

    blockers = [reason for failed, reason in (
        (not booted, "simulator not booted"),
        (executing is not None, f"{(executing or {}).get('verb')!r} still executing"),
        (serving is False, "no bundler"),
        (running is False, "app not running"),
    ) if failed]
    lines.append("ready: " + ("yes" if not blockers else "no (" + ", ".join(blockers) + ")"))
    data = {
        "platform": "ios", "project": project["name"], "lock": name,
        "ready": not blockers, "blockers": blockers,
        "device": {"name": device["name"], "udid": udid, "state": device["state"],
                   "booted": booted, "sim": sim["name"] if sim else None},
        "claim": owner_data(name, Handler.config.lock_ttl),
        "executing": executing_data(executing),
        "bundler": bundler,
        "app": None if not bundle else {"bundle_id": bundle, "running": bool(running)},
    }
    return "\n".join(lines), {"ready": not blockers, "data": data}


def describe_bundler(name, port):
    """(serving, line): whether something listens on the bundler port, and which
    build profile it carries when mac-sim started it."""
    serving = bool(port_listeners(port))
    server = read_server(name) if serving else None
    if server:
        age = int(time.time() - float(server["at"]))
        profile = server.get("variant") or "default"
        return serving, (f"bundler: running on {port}, started by `{server['command']}` "
                         f"(profile {profile}) from "
                         f"{server.get('guest_workdir') or server['workdir']}, "
                         f"{age // 60}m {age % 60}s ago")
    if serving:
        return serving, (f"bundler: something is listening on {port}, not started "
                         f"through mac-sim, so its build profile is unknown")
    return serving, f"bundler: nothing listening on {port}"


def bundler_data(name, port, serving):
    """The bundler line as data. `variant` is null both for the default profile
    and when the profile is unknown; `started_by_mac_sim` tells them apart."""
    server = read_server(name) if serving else None
    data = {"port": port, "serving": bool(serving), "started_by_mac_sim": bool(server),
            "command": None, "variant": None, "workdir": None, "started": None}
    if server:
        data.update({"command": server.get("command"), "variant": server.get("variant"),
                     "workdir": server.get("guest_workdir") or server.get("workdir"),
                     "started": server.get("at")})
    return data


def host_memory():
    """The Mac's memory, for a tool in the guest that shows it beside the guest's
    own (a dashboard): total, and what a new process could get without swapping,
    which is free, inactive (reclaimable cache) and speculative pages. Read-only
    and claim-free: it describes the Mac, not a checkout."""
    total = int(run_host(["sysctl", "-n", "hw.memsize"], 10))
    out = run_host(["vm_stat"], 10)
    page = re.search(r"page size of (\d+) bytes", out)
    counts = [re.search(rf"^Pages {name}:\s+(\d+)", out, re.M)
              for name in ("free", "inactive", "speculative")]
    if not page or not all(counts):
        raise HostFailure(["vm_stat"], 1, f"no page size or free, inactive and speculative "
                                          f"pages in its output:\n{out}")
    available = sum(int(c.group(1)) for c in counts) * int(page.group(1))
    gb = 1024 ** 3
    return (f"memory: {available / gb:.1f} GB available of {total / gb:.0f} GB",
            {"total": total, "available": available})


DIRECT = {"sim_create": sim_create, "sim_delete": sim_delete,
          "sim_list": sim_list, "pull": pull, "status": status,
          "app_cache_save": app_cache_save, "app_cache_status": app_cache_status}


# --------------------------------------------------------------------------
# Android: the same verbs on the SDK's emulator and adb
# --------------------------------------------------------------------------
# Each emulator is named by the `android` block: its AVD, and the console port
# that fixes its adb serial (emulator-<port>). Fixing the port is what keeps this
# off an emulator somebody started on the Mac by hand, which takes 5554 by
# default. Every adb call names that serial, so a second device attached to the
# Mac is never the one acted on. Each emulator has its own claim: the default
# one's is <project>@android, another's <project>@android-<name>.
def android_config(project, body=None):
    """The shared Android settings merged with the emulator the request names
    (`android_device`), or the default one."""
    cfg = project.get("android")
    if cfg is None:
        raise Rejected(
            f"project {project['name']!r} declares no \"android\" block in the host "
            f"config, so it has no emulator. See docs/MAC-SIM.md.")
    dname = (body or {}).get("android_device") or cfg["default"]
    if dname not in cfg["devices"]:
        raise Rejected(f"project {project['name']!r} declares no android device {dname!r}; "
                       f"declared: {', '.join(sorted(cfg['devices']))}")
    lock = f"{project['name']}@android" + ("" if dname == cfg["default"] else f"-{dname}")
    return {**cfg, **cfg["devices"][dname], "name": dname, "lock": lock}


def adb_base(cfg):
    return [str(cfg["sdk"] / "platform-tools" / "adb"), "-s", cfg["serial"]]


def android_env(cfg):
    """The environment every Android tool expects: Gradle, Expo and Maestro all
    find the SDK through ANDROID_HOME and a JDK through JAVA_HOME, and none of
    them is set for a process launched outside a login shell."""
    env = dict(os.environ)
    sdk = str(cfg["sdk"])
    env.update({"ANDROID_HOME": sdk, "ANDROID_SDK_ROOT": sdk})
    path = [f"{sdk}/platform-tools", f"{sdk}/emulator"]
    if cfg["java_home"]:
        env["JAVA_HOME"] = cfg["java_home"]
        path.append(f"{cfg['java_home']}/bin")
    env["PATH"] = ":".join(path + [env.get("PATH", "")])
    return env


def adb_state(cfg):
    """The serial's state as `adb devices` reports it ("device", "offline", ...),
    or None when no emulator holds that console port."""
    out = run_host([str(cfg["sdk"] / "platform-tools" / "adb"), "devices"], 30)
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == cfg["serial"]:
            return parts[1]
    return None


def android_booted(adb):
    probe = subprocess.run(adb + ["shell", "getprop", "sys.boot_completed"],
                           capture_output=True, text=True, timeout=30)
    return probe.returncode == 0 and probe.stdout.strip() == "1"


def android_pid(adb, package):
    """The app's pid on the emulator, or None when it is not running."""
    probe = subprocess.run(adb + ["shell", "pidof", "-s", package],
                           capture_output=True, text=True, timeout=30)
    pid = probe.stdout.strip()
    return pid if probe.returncode == 0 and pid.isdigit() else None


def require_emulator(cfg):
    state = adb_state(cfg)
    if state != "device" or not android_booted(adb_base(cfg)):
        raise Rejected(
            f"the emulator {cfg['serial']} ({cfg['avd']}) is "
            + (f"{state}, not booted" if state else "not running")
            + ". `mac-sim android boot` starts it.")


COMPONENT_RE = re.compile(r"^[A-Za-z0-9._$]{1,128}/[A-Za-z0-9._$]{1,256}$")


def launcher_activity(adb, package):
    """The package's launcher activity as package/class, as the device resolves it."""
    try:
        out = run_host(adb + ["shell", "cmd", "package", "resolve-activity", "--brief",
                              "-c", "android.intent.category.LAUNCHER", package], 30)
    except HostFailure as exc:
        raise Rejected(f"cannot resolve the launcher activity of {package}: {exc}")
    component = out.splitlines()[-1].strip() if out else ""
    if not COMPONENT_RE.match(component):
        raise Rejected(f"{package} has no launcher activity on the emulator (is it "
                       f"installed?); the device answered {out!r}")
    return component


def android_dispatch(verb, body, project, base):
    """Resolve an Android built-in to (argv, timeout, extra), like dispatch()."""
    cfg = android_config(project, body)
    adb = adb_base(cfg)
    package = field(body, "bundle_id", BUNDLE_RE, required=False, default=cfg["package"])

    if verb == "boot":
        state = adb_state(cfg)
        if state == "device" and android_booted(adb):
            return ["/bin/echo", "already booted"], 10, {}
        if state is not None:
            raise Rejected(
                f"{cfg['serial']} is {state}: an emulator holds that console port but "
                f"has not finished booting. Wait, or `mac-sim android shutdown` it.")
        argv = [str(cfg["sdk"] / "emulator" / "emulator"), "-avd", cfg["avd"],
                "-port", str(cfg["console_port"])] + cfg["emulator_args"]
        # Detached like a bundler: the emulator must outlive this request, and is
        # ready once Android itself reports the boot complete.
        return argv, cfg["boot_timeout"], {
            "detach": True, "ready_boot": {"adb": adb}, "env": android_env(cfg),
            "log": str(command_log_path(
                project, base, "android-emulator",
                None if cfg["name"] == cfg["default"] else {"name": cfg["name"]}))}

    if verb == "shutdown":
        require_emulator(cfg)
        return adb + ["emu", "kill"], 60, {}

    if verb == "install":
        apk = safe_path(body.get("app"), project, base=base, must_exist=True)
        if apk.suffix != ".apk":
            raise Rejected("android install requires an .apk")
        require_emulator(cfg)
        return adb + ["install", "-r", str(apk)], 600, {}

    if verb == "uninstall":
        return adb + ["uninstall", package], 120, {}

    if verb == "terminate":
        return adb + ["shell", "am", "force-stop", package], 60, {}

    if verb == "launch":
        locale = field(body, "locale", LOCALE_RE, required=False)
        # Started by component: an implicit launcher intent does not resolve
        # through `am start`, which adds the DEFAULT category a launcher activity
        # does not declare, and `monkey` exits 251 on an emulator with no keys.
        launch = f"am start -n {launcher_activity(adb, package)}"
        if locale is None:
            return adb + ["shell", launch], 60, {}
        # A per-app locale (Android 13+) reaches only a fresh process, so the app
        # is stopped first. Both values matched strict patterns above, so this
        # line has nothing the device shell could read as syntax.
        tag = locale.replace("_", "-")
        return adb + ["shell", f"cmd locale set-app-locales {package} --locales {tag} "
                               f"&& am force-stop {package} && {launch}"], 60, {}

    if verb == "openurl":
        url = body.get("url")
        if not isinstance(url, str) or not URL_RE.match(url):
            raise Rejected("openurl requires a scheme:// URL")
        # adb joins its arguments into one line for the DEVICE shell, so the URL
        # is quoted for that shell: an & in a query string must not end the command.
        return adb + ["shell", "am", "start", "-a", "android.intent.action.VIEW",
                      "-d", shlex.quote(url), package], 60, {}

    if verb == "appearance":
        mode = body.get("mode")
        if mode is None:
            return adb + ["shell", "cmd", "uimode", "night"], 30, {"_exclusive": False}
        if mode not in ("light", "dark"):
            raise Rejected(f"appearance mode must be 'light' or 'dark', got {mode!r}")
        return adb + ["shell", "cmd", "uimode", "night",
                      "yes" if mode == "dark" else "no"], 30, {}

    raise Rejected(f"verb {verb!r} is not available for android")


def android_list(body, project, base):
    cfg = android_config(project, body)
    avds = run_host([str(cfg["sdk"] / "emulator" / "emulator"), "-list-avds"], 60)
    devices = run_host([str(cfg["sdk"] / "platform-tools" / "adb"), "devices", "-l"], 30)
    attached = []
    # `adb devices -l`: a header, then "<serial> <state> key:value ...".
    for line in devices.splitlines()[1:]:
        serial, _, rest = line.strip().partition(" ")
        if not serial or serial.startswith("*"):
            continue
        state, *props = rest.split()
        attached.append({"serial": serial, "state": state,
                         **{k: v for k, _, v in (p.partition(":") for p in props) if v}})
    data = {
        "project": project["name"], "package": cfg["package"], "default": cfg["default"],
        "emulators": [
            {"name": n, "avd": d["avd"], "serial": d["serial"], "port": d.get("port"),
             "default": n == cfg["default"]}
            for n, d in sorted(cfg["devices"].items())],
        "avds": [a.strip() for a in avds.splitlines() if a.strip()],
        "attached": attached,
    }
    return (f"project emulators: " + ", ".join(
                f"{n} = {d['avd']} as {d['serial']}" for n, d in cfg["devices"].items())
            + f"\n"
            f"AVDs:\n" + "\n".join(f"  {a}" for a in avds.splitlines() if a.strip())
            + f"\n{devices}"), {"data": data}


def android_screenshot(body, project, base):
    cfg = android_config(project, body)
    out = safe_path(body.get("out"), project, base=base)
    require_emulator(cfg)
    png = run_host(adb_base(cfg) + ["exec-out", "screencap", "-p"], 60, binary=True)
    if not png.startswith(b"\x89PNG"):
        raise HostFailure(adb_base(cfg) + ["exec-out", "screencap", "-p"], 1,
                          f"expected a PNG, got {png[:120]!r}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    # The directory is guest-writable: never write through a planted symlink.
    with os.fdopen(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                           0o644), "wb") as fh:
        fh.write(png)
    return str(out), {"path": str(out)}


def android_logs(body, project, base):
    """A bounded logcat dump: the app's own process by default, like iOS logs.
    logcat filters by pid, so the app must be running to scope it."""
    cfg = android_config(project, body)
    lines = body.get("lines", 200)
    if not isinstance(lines, int) or not 1 <= lines <= 5000:
        raise Rejected("lines must be an int in 1..5000")
    require_emulator(cfg)
    adb = adb_base(cfg)
    argv = adb + ["logcat", "-d", "-t", str(lines)]
    if not body.get("all"):
        package = field(body, "bundle_id", BUNDLE_RE, required=False,
                        default=cfg["package"])
        pid = android_pid(adb, package)
        if pid is None:
            raise Rejected(f"{package} is not running on {cfg['serial']}, so its log "
                           f"cannot be told apart by pid. Launch it, or pass --all.")
        argv.append(f"--pid={pid}")
    return run_host(argv, 120), {}


def android_status(body, project, base):
    """`status` for the emulator: the device, the claim, the bundler a debug build
    needs, and the app. A release build embeds its bundle, so only a debuggable
    one is not ready without a bundler."""
    cfg = android_config(project, body)
    name = cfg["lock"]
    adb = adb_base(cfg)
    state = adb_state(cfg)
    booted = state == "device" and android_booted(adb)
    lines = [f"emulator: {cfg['avd']} ({cfg['serial']}) "
             + ("booted" if booted else f"{state}, still booting" if state else "not running")]
    lines.append("claim: " + describe_owner(name, Handler.config.lock_ttl).split(": ", 1)[1])
    executing = _executing_info(name)
    if executing and executing.get("log"):
        lines.append(f"  log: {executing['log']}")

    installed = debuggable = running = None
    if booted:
        dump = subprocess.run(adb + ["shell", "dumpsys", "package", cfg["package"]],
                              capture_output=True, text=True, timeout=30).stdout
        installed = f"Package [{cfg['package']}]" in dump
        flags = next((l for l in dump.splitlines() if "pkgFlags=[" in l), "")
        debuggable = installed and "DEBUGGABLE" in flags
        running = installed and android_pid(adb, cfg["package"]) is not None

    serving = bundler = None
    if cfg["port"]:
        serving, line = describe_bundler(name, cfg["port"])
        lines.append(line)
        bundler = bundler_data(name, cfg["port"], serving)

    if installed is None:
        lines.append(f"app: {cfg['package']} unknown (emulator not booted)")
    elif not installed:
        lines.append(f"app: {cfg['package']} not installed")
    else:
        lines.append(f"app: {cfg['package']} {'running' if running else 'not running'} "
                     f"({'debug build, loads its bundle from the bundler' if debuggable else 'release build, bundle embedded'})")

    blockers = [reason for failed, reason in (
        (not booted, "emulator not booted"),
        (executing is not None, f"{(executing or {}).get('verb')!r} still executing"),
        (bool(debuggable) and serving is False, "debug build but no bundler"),
        (installed is False, "app not installed"),
        (installed and not running, "app not running"),
    ) if failed]
    lines.append("ready: " + ("yes" if not blockers else "no (" + ", ".join(blockers) + ")"))
    data = {
        "platform": "android", "project": project["name"], "lock": name,
        "ready": not blockers, "blockers": blockers,
        "device": {"name": cfg["name"], "avd": cfg["avd"], "serial": cfg["serial"],
                   "state": state or None, "booted": booted},
        "claim": owner_data(name, Handler.config.lock_ttl),
        "executing": executing_data(executing),
        "bundler": bundler,
        "app": {"package": cfg["package"], "installed": installed,
                "running": running, "debuggable": debuggable},
    }
    return "\n".join(lines), {"ready": not blockers, "data": data}


ANDROID_DIRECT_FNS = {"list": android_list, "screenshot": android_screenshot,
                      "logs": android_logs, "status": android_status}


# --------------------------------------------------------------------------
# The claim: which checkout currently owns the simulator
# --------------------------------------------------------------------------
# Targeting the right worktree is necessary but not sufficient. One Mac has one
# simulator, one installed copy of a bundle id, and one bundler on port 8081 --
# and `run` explicitly frees that port before building. So two checkouts running
# concurrently do not merely race: the second silently replaces the first's
# build, and both sessions then verify something neither of them wrote.
#
# Two mechanisms, because there are two failure modes:
#   * a non-blocking flock, held for the command, stops genuine overlap;
#   * a sticky owner record, honoured for `lock.ttl`, stops the slower and more
#     confusing case where checkout B starts after A finished and quietly
#     inherits A's installed app.
# Switching checkouts is legitimate, so this refuses rather than forbids: -f
# takes the claim over, `mac-sim release` hands it back.
#
# The two mechanisms are visible and recoverable to different degrees. The
# owner record is: `mac-sim lock` reads it, `mac-sim release` clears it. The
# flock historically was not: nothing inspected it and nothing but the holder
# finishing (or its process dying) ever released it, so a client that gave up
# on a request already in flight on the host (a killed guest wrapper, a
# force-killed session) left it held with no recovery path short of restarting
# the shim. ACTIVE + kill_active() below close that gap: the currently-running
# subprocess for a lock is tracked, `describe_owner()` reports it even when the
# owner record has gone missing, and `mac-sim reset` kills it -- which lets the
# holder's own thread finish and release the flock the normal way, rather than
# reaching into another thread's file descriptor.
def _owner_path(name):
    return LOCK_DIR / f"{name}.owner.json"


def read_owner(name):
    try:
        with open(_owner_path(name)) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def write_owner(name, workdir, verb, guest_workdir=None):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _owner_path(name).with_suffix(".tmp")
    payload = {"workdir": str(workdir), "verb": verb, "at": time.time()}
    if guest_workdir:
        payload["guest_workdir"] = guest_workdir
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    tmp.replace(_owner_path(name))  # atomic: a reader never sees a half-write


def clear_owner(name):
    try:
        _owner_path(name).unlink()
    except FileNotFoundError:
        pass


def to_guest_path(project, host_path):
    """Best-effort translation of a host-resolved checkout path back to what
    the caller itself sees under its own bind mount, so a refusal or a `lock`
    report names the path the guest can actually recognise (D7)."""
    if project is None or host_path is None:
        return None
    try:
        rel = Path(host_path).resolve().relative_to(project["root"])
    except (ValueError, OSError, RuntimeError):
        return None
    mount = project["guest_mount"]
    return mount if str(rel) == "." else f"{mount}/{rel}"


# Which Claim currently holds each named lock, if any. This is what makes the
# execution lock (the flock below) inspectable and recoverable at all: without
# it, a stuck command is invisible to every guest-side verb, and `-f` cannot
# reach it because it only ever touched the sticky owner record. Guarded by
# _ACTIVE_LOCK because acquire/release run on the HTTP server's worker threads.
_ACTIVE_LOCK = threading.Lock()
ACTIVE = {}  # lock name -> Claim


def describe_owner(name, ttl):
    owner = read_owner(name)
    with _ACTIVE_LOCK:
        claim = ACTIVE.get(name)
    live = None
    if claim is not None:
        elapsed = int(time.time() - (claim.started or time.time()))
        live = (f"  EXECUTING NOW: {claim.verb!r} (pid {claim.pid or 'starting'}), "
                f"{elapsed // 60}m {elapsed % 60}s so far, from "
                f"{claim.guest_workdir or claim.workdir}")
        if claim.log:
            live += f"\n  live output: {claim.log}"
    if not owner:
        out = f"{name}: simulator free"
        if live:
            out += ("\n  (no claim on record, but a command is still running -- "
                     "this is the stuck state; run `mac-sim reset` to recover)\n" + live)
        return out
    age = int(time.time() - float(owner.get("at", 0)))
    stale = " (expired)" if age >= ttl else ""
    out = (f"{name}: simulator held by {owner.get('guest_workdir') or owner.get('workdir')}{stale}\n"
           f"  last command: {owner.get('verb')}, {age // 60}m {age % 60}s ago")
    if live:
        out += "\n" + live
    return out


def _executing_info(name):
    with _ACTIVE_LOCK:
        claim = ACTIVE.get(name)
    if claim is None:
        return None
    return {"verb": claim.verb, "pid": claim.pid,
            "workdir": claim.guest_workdir or claim.workdir,
            "log": claim.log,
            "elapsed": time.time() - (claim.started or time.time())}


def owner_data(name, ttl):
    """The sticky owner record as data, or None when nobody holds the claim."""
    owner = read_owner(name)
    if not owner:
        return None
    age = int(time.time() - float(owner.get("at", 0)))
    return {"workdir": owner.get("guest_workdir") or owner.get("workdir"),
            "verb": owner.get("verb"), "at": owner.get("at"), "age": age,
            "expired": age >= ttl}


def with_schema(data):
    return {"schemaVersion": JSON_SCHEMA_VERSION, **data}


def executing_data(executing):
    if executing is None:
        return None
    return {**executing, "elapsed": int(executing["elapsed"])}


def kill_active(name, grace=3.0, expected_pid=None):
    """Force-stop whatever is currently executing under `name`'s lock, if
    anything. Killing the tracked subprocess (rather than trying to reach into
    another thread's flock directly) lets that thread's own `finally:
    claim.release()` run normally and unlock/close the fd -- the one code path
    already known to release it correctly.

    `expected_pid`, when given, guards against the TOCTOU race where the
    command a caller saw via `mac-sim lock` has already finished and a new,
    unrelated one has started under the same name by the time the reset
    request lands: a mismatch is rejected rather than killing whatever is
    there now. Omitting it keeps the old best-effort "kill whatever is
    current" behaviour, for a caller that has not first checked.
    """
    with _ACTIVE_LOCK:
        claim = ACTIVE.get(name)
    if claim is None:
        return None
    if expected_pid is not None and claim.pid != expected_pid:
        raise Rejected(
            f"{name}: refusing to reset -- the running command is no longer "
            f"pid {expected_pid} (now {claim.pid or 'starting'}, {claim.verb!r}, "
            f"started {int(time.time() - (claim.started or time.time()))}s ago). "
            f"Run `mac-sim lock` again and retry with that pid, or omit --pid "
            f"to reset whatever is current.")
    claim.aborted = True  # tell its own release() not to re-write the owner we're about to clear
    info = {"verb": claim.verb, "pid": claim.pid,
            "workdir": claim.workdir, "guest_workdir": claim.guest_workdir,
            "elapsed": time.time() - (claim.started or time.time())}
    pid = claim.pid
    if pid:
        # killpg, not kill: build/test tools fork children (xcodebuild, node,
        # watchman, maestro's driver) started in their own session (see the
        # Popen calls above), and a lone SIGKILL to the top pid would leave
        # those running -- still touching the simulator or a shared port even
        # after this call claims the lock is free.
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pid = None
        deadline = time.time() + grace
        while pid and time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pid = None
                break
            time.sleep(0.2)
        if pid:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return info


class Claim:
    def __init__(self, name, workdir, verb, force, ttl, guest_workdir=None):
        self.name, self.workdir = name, str(workdir)
        self.guest_workdir = guest_workdir
        self.verb, self.force, self.ttl = verb, force, ttl
        self.fh = None
        self.pid = None       # set once the tracked subprocess actually starts
        self.log = None       # where that subprocess writes, as the guest sees it
        self.started = None
        self.aborted = False  # set by kill_active(): skip the owner rewrite on release

    def acquire(self):
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        self.fh = open(LOCK_DIR / f"{self.name}.lock", "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            owner = read_owner(self.name) or {}
            raise Busy(f"{self.name}: another mac-sim command is running right now "
                       f"({owner.get('verb', '?')} from "
                       f"{owner.get('guest_workdir') or owner.get('workdir', '?')}). "
                       f"Wait for it to finish, or run `mac-sim reset` if you are sure "
                       f"it is stuck.")
        owner = read_owner(self.name)
        if (owner and not self.force
                and owner.get("workdir") != self.workdir
                and time.time() - float(owner.get("at", 0)) < self.ttl):
            self.release(record=False)
            age = int(time.time() - float(owner["at"]))
            raise Busy(
                f"{self.name}: the simulator is held by "
                f"{owner.get('guest_workdir') or owner['workdir']}\n"
                f"  (last command {owner.get('verb')!r}, {age // 60}m ago)\n"
                f"Running from {self.guest_workdir or self.workdir} would overwrite that "
                f"build: same bundle id, same device, same bundler port.\n"
                f"Take it over with -f, or hand it back with `mac-sim release`.")
        write_owner(self.name, self.workdir, self.verb, self.guest_workdir)
        self.started = time.time()
        with _ACTIVE_LOCK:
            ACTIVE[self.name] = self
        return self

    def release(self, record=True):
        with _ACTIVE_LOCK:
            if ACTIVE.get(self.name) is self:
                del ACTIVE[self.name]
        if self.fh is None:
            return
        if record and not self.aborted:
            # Refresh on the way out so the TTL measures idleness, not the age
            # of the first command in a session.
            write_owner(self.name, self.workdir, self.verb, self.guest_workdir)
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()
        self.fh = None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    config = None
    token = None

    def log_message(self, *_):
        pass  # we do our own logging

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self, client):
        if ip_address(client) not in self.config.clients and client != "127.0.0.1":
            log(f"REJECT {client}: not in {self.config.clients}")
            self._reply(403, {"error": "client not allowed"})
            return False
        # Constant-time compare so the token cannot be probed byte by byte.
        import hmac
        supplied = self.headers.get("X-Mac-Sim-Token", "")
        if not hmac.compare_digest(supplied, self.token):
            log(f"REJECT {client}: bad or missing token")
            self._reply(401, {"error": "bad or missing X-Mac-Sim-Token"})
            return False
        return True

    def do_GET(self):
        if self.path != "/health":
            return self._reply(404, {"error": "only POST / and GET /health"})
        if not self._authorised(self.client_address[0]):
            return
        self._reply(200, {
            "ok": True,
            "verbs": BUILTIN_VERBS,
            "features": FEATURES,
            "schemaVersion": JSON_SCHEMA_VERSION,
            "lock_ttl": self.config.lock_ttl,
            "projects": {
                name: {
                    "root": str(p["root"]),
                    "owner": read_owner(name),
                    "executing": _executing_info(name),
                    "guest_mount": p["guest_mount"],
                    "defaults": p["defaults"],
                    "pull": p["pull"],
                    "simulators": None if p.get("simulators") is None else {
                        sname: {**entry, "owner": read_owner(f"{name}@{sname}")}
                        for sname, entry in read_sims(name).items()},
                    "android": None if p.get("android") is None else {
                        "package": p["android"]["package"],
                        "default": p["android"]["default"],
                        "devices": {
                            dname: {"avd": d["avd"], "serial": d["serial"], "port": d["port"],
                                    "owner": read_owner(lock), "executing": _executing_info(lock)}
                            for dname, d in p["android"]["devices"].items()
                            for lock in [android_config(p, {"android_device": dname})["lock"]]}},
                    "device": None if p.get("device") is None else {
                        "bundle_id": p["device"]["bundle_id"],
                        "pull_prefix": p["device"]["pull_prefix"]},
                    "watch": None if p.get("watch") is None else {
                        "stamp": p["watch"].get("stamp"),
                        "rebuild": p["watch"].get("rebuild"),
                        "bundle_id": p["watch"]["bundle_id"]},
                    "commands": {
                        cname: {
                            "platform": c.get("platform", "ios"),
                            "watch": "watch" in c,
                            "watch_stamp": c.get("watch_stamp"),
                            "variants": sorted(c.get("variants", {})),
                            "params": {k: {kk: vv for kk, vv in v.items() if kk != "pattern"}
                                       for k, v in c.get("params", {}).items()},
                        } for cname, c in p["commands"].items()
                    },
                } for name, p in self.config.projects.items()
            },
        })

    def do_POST(self):
        client = self.client_address[0]
        claim = None
        try:
            if not self._authorised(client):
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._reply(413, {"error": "body too large"})
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                return self._reply(400, {"error": "body must be a JSON object"})
            verb = body.get("verb")
            if verb not in BUILTIN_VERBS:
                log(f"REJECT {client}: verb={verb!r}")
                return self._reply(400, {"error": f"verb must be one of {BUILTIN_VERBS}"})
            platform = body.get("platform")
            if platform not in (None, "ios", "android", "device"):
                raise Rejected(f"unknown platform {platform!r}; expected ios, android or device")
            if platform == "device" and verb != "lock":
                raise Rejected("platform 'device' applies to the lock verb only; use "
                               "the device_* verbs")
            if platform == "android" and verb not in ANDROID_VERBS:
                raise Rejected(f"{verb!r} is not available for android; available: "
                               f"{', '.join(sorted(ANDROID_VERBS))}")
            if verb == "lock":
                return self._lock_verb(body)
            if verb == "memory":
                return self._memory()
            if verb in (ANDROID_DIRECT if platform == "android" else DIRECT_VERBS):
                return self._direct(verb, body)
            argv, timeout, extra, project = build_command(verb, body, self.config)
            lock_name = extra.pop("_lock", None)
            if lock_name:
                workdir = extra.get("workdir")
                claim = Claim(lock_name, workdir, body.get("command") or verb,
                              bool(body.get("force")), self.config.lock_ttl,
                              guest_workdir=to_guest_path(project, workdir))
                claim.acquire()
        except Busy as exc:
            log(f"BUSY {client}: {exc}")
            return self._reply(409, {"error": str(exc)})
        except Rejected as exc:
            log(f"REJECT {client}: {exc}")
            return self._reply(400, {"error": str(exc)})
        except Exception as exc:
            log(f"ERROR {client}: {type(exc).__name__}: {exc}")
            return self._reply(400, {"error": f"{type(exc).__name__}: {exc}"})

        try:
            self._execute(verb, body, argv, timeout, extra, claim)
        finally:
            if claim:
                claim.release()

    def _direct(self, verb, body):
        """Run a direct verb. Creating or deleting a private simulator claims
        that simulator, so a second checkout cannot delete one in use."""
        claim, previous_owner, done = None, None, False
        try:
            project = self.config.project(body.get("project"), body.get("workdir"))
            base = resolve_workdir(body, project)
            if verb in ("sim_create", "sim_delete") and self.config.lock_enabled:
                sim_name = field(body, "name", SIM_NAME_RE)
                previous_owner = read_owner(f"{project['name']}@{sim_name}")
                claim = Claim(f"{project['name']}@{sim_name}", base, verb,
                              bool(body.get("force")), self.config.lock_ttl,
                              guest_workdir=to_guest_path(project, base)).acquire()
            elif verb == "app_cache_save" and self.config.lock_enabled:
                # The shared simulator's claim: the build copied is the one this
                # checkout installed there.
                previous_owner = read_owner(project["name"])
                claim = Claim(project["name"], base, verb,
                              bool(body.get("force")), self.config.lock_ttl,
                              guest_workdir=to_guest_path(project, base)).acquire()
            log(f"RUN {verb}: {body.get('name') or body.get('path') or ''}")
            if body.get("platform") == "android":
                if "sim" in body:
                    raise Rejected("--sim names an iOS simulator; it does not apply to Android")
                stdout, extra = ANDROID_DIRECT_FNS[verb](body, project, base)
            else:
                stdout, extra = DIRECT[verb](body, project, base)
            done = True
            if "data" in extra:
                extra["data"] = with_schema(extra["data"])
            log(f"DONE {verb}: exit=0")
            return self._reply(200, {"verb": verb, "exit_code": 0,
                                     "stdout": stdout, "stderr": "", **extra})
        except Busy as exc:
            log(f"BUSY {verb}: {exc}")
            return self._reply(409, {"error": str(exc)})
        except Rejected as exc:
            log(f"REJECT {verb}: {exc}")
            return self._reply(400, {"error": str(exc)})
        except HostFailure as exc:
            log(f"FAIL {verb}: {exc}")
            return self._reply(200, {"verb": verb, "exit_code": exc.code,
                                     "stdout": "", "stderr": str(exc)})
        finally:
            if claim and claim.fh is not None:
                # A created simulator belongs to its creator and a deleted one to
                # nobody; the shared simulator stays its builder's after a save. A
                # request that failed changed nothing, so the record goes back to
                # what it was before this request claimed it.
                claim.aborted = not (done and verb in ("sim_create", "app_cache_save"))
                claim.release()
                if claim.aborted:
                    if previous_owner and not done:
                        write_owner(claim.name, previous_owner["workdir"],
                                    previous_owner.get("verb"),
                                    previous_owner.get("guest_workdir"))
                    else:
                        clear_owner(claim.name)

    def _memory(self):
        try:
            stdout, data = host_memory()
        except HostFailure as exc:
            log(f"FAIL memory: {exc}")
            return self._reply(200, {"verb": "memory", "exit_code": exc.code,
                                     "stdout": "", "stderr": str(exc)})
        return self._reply(200, {"verb": "memory", "exit_code": 0, "stdout": stdout,
                                 "stderr": "", "data": with_schema(data)})

    def _lock_verb(self, body):
        """Inspect, hand back, or force-clear the claim. Never runs a subprocess
        itself -- `reset` stops the tracked subprocess of an in-flight command,
        which is what actually frees the execution lock (see kill_active)."""
        project = self.config.project(body.get("project"), body.get("workdir"))
        if body.get("platform") == "android":
            name = android_config(project, body)["lock"]
        elif body.get("platform") == "device":
            device_config(project)
            name = lock_name(project, platform="device")
        else:
            name = lock_name(project, resolve_sim(body, project))
        action = body.get("action")
        if action in ("release", "reset"):
            killed = kill_active(name, expected_pid=body.get("pid")) if action == "reset" else None
            clear_owner(name)
            log(f"{'RESET' if action == 'reset' else 'RELEASE'} {name}"
                + (f" (killed pid={killed.get('pid')} verb={killed['verb']!r})"
                   if killed and killed.get("pid") else
                   " (nothing was executing)" if action == "reset" else ""))
            msg = f"{name}: simulator released"
            data = {"lock": name, "action": action, "stopped": None}
            if action == "reset" and killed:
                data["stopped"] = {"verb": killed["verb"], "pid": killed.get("pid"),
                                   "elapsed": int(killed["elapsed"]),
                                   "workdir": killed.get("guest_workdir") or killed.get("workdir")}
            if action == "reset":
                if killed:
                    elapsed = int(killed["elapsed"])
                    msg += (f"\n  stopped a stuck {killed['verb']!r} "
                            f"(pid {killed.get('pid') or '?'}, running "
                            f"{elapsed // 60}m {elapsed % 60}s), started from "
                            f"{killed.get('guest_workdir') or killed.get('workdir')}")
                else:
                    msg += "\n  nothing was executing; only the ownership claim was cleared"
            return self._reply(200, {"verb": "lock", "exit_code": 0,
                                     "stdout": msg, "stderr": "", "data": with_schema(data)})
        return self._reply(200, {"verb": "lock", "exit_code": 0,
                                 "stdout": describe_owner(name, self.config.lock_ttl),
                                 "stderr": "", "data": with_schema({
                                     "lock": name, "action": None,
                                     "claim": owner_data(name, self.config.lock_ttl),
                                     "executing": executing_data(_executing_info(name))})})

    def _client_gone(self):
        """Whether the guest closed its connection (a killed `mac-sim` call). The guest
        sends nothing after its request body, so a readable EOF is the only answer."""
        try:
            return self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    def _execute_logged(self, verb, label, argv, timeout, extra, claim, cwd, env,
                        logfile, detach, ready_url, ready_match, ready_app, server,
                        ready_boot=None):
        """Run a project command with its output in a log the guest can follow.

        A plain command is done when it exits. A detached one (a bundler, or a
        build that goes on to serve the bundle) is done when it reports ready,
        and is then left running; exiting before that is its failure.
        """
        shown = extra.get("guest_log") or logfile
        settle = extra.pop("settle", None)
        if ready_app:
            # Stop the installed copy first, so "the app is running" below can
            # only mean this command launched it.
            if "adb" in ready_app:
                subprocess.run(ready_app["adb"] + ["shell", "am", "force-stop",
                                                   ready_app["package"]],
                               capture_output=True, timeout=60)
            else:
                subprocess.run(["xcrun", "simctl", "terminate", ready_app["udid"],
                                ready_app["bundle_id"]], capture_output=True, timeout=60)
        try:
            with open_command_log(logfile) as fh:
                # A new session so a reset or a timeout can kill the whole tree.
                proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=fh,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            log(f"FAIL {label}: {type(exc).__name__}: {exc}")
            return self._reply(502, {
                "error": f"could not start {argv[0]!r} on the host: {exc}"})
        if claim:
            claim.pid, claim.log = proc.pid, shown

        def is_ready():
            if ready_url:
                probe = subprocess.run(["curl", "-s", "-m", "2", ready_url],
                                       capture_output=True, text=True)
                if not probe.stdout or ready_match not in probe.stdout:
                    return False
            if ready_boot and not android_booted(ready_boot["adb"]):
                return False
            if not ready_app:
                return True
            if "adb" in ready_app:
                return android_pid(ready_app["adb"], ready_app["package"]) is not None
            return app_running(ready_app["udid"], ready_app["bundle_id"])

        deadline, code, ready = time.time() + timeout, None, False
        settled_at, settled, gone, last_check, last_size = None, None, False, 0.0, -1
        while time.time() < deadline:
            code = proc.poll()
            if code is not None:
                break
            if detach and is_ready():
                ready = True
                break
            now = time.time()
            if not detach and now - last_check >= 2:
                last_check = now
                # Nobody is waiting for the answer any more (the guest was killed): stop
                # the command rather than leave it driving the device and holding the claim.
                # Only for a settling command (a test run): a build may outlive the guest's
                # 3700 s read timeout (android-build allows 5400 s) and must go on.
                if settle and self._client_gone():
                    gone = True
                    break
                # A command that printed its final summary and then went quiet (maestro's
                # NoSuchFileException in DebugLogStore.finalizeRun) is done. Quiet means the
                # log stopped growing: a match in a log that still grows is a run in progress.
                if settle:
                    try:
                        size = os.stat(logfile).st_size
                        if size != last_size:
                            last_size, settled_at = size, None
                            if re.search(settle["match"], read_log_tail(logfile), re.M):
                                settled_at = now
                    except OSError as exc:
                        # The guest removed the log: keep supervising, just without settling.
                        log(f"WARN {label}: cannot read {logfile} to settle: {exc}")
                        settled_at = None
                if settled_at is not None and now - settled_at >= settle["after"]:
                    tail = read_log_tail(logfile)
                    # A pass needs the summary to say so: a crash with no summary is a failure.
                    failed = bool(settle["fail_match"] and re.search(settle["fail_match"], tail, re.M)) \
                        or bool(settle["pass_match"] and not re.search(settle["pass_match"], tail, re.M))
                    settled = 1 if failed else 0
                    break
            time.sleep(1 if detach else 0.2)
        if gone or settled is not None:
            kill_group(proc.pid)
            proc.wait(timeout=10)
        if gone:
            try:
                keep_numbered_copy(logfile)
            except OSError as exc:
                log(f"WARN {label}: could not keep a numbered copy of {logfile}: {exc}")
            log(f"GONE {label}: the client disconnected; stopped pid {proc.pid}")
            return
        if settled is not None:
            code = settled
            try:
                with os.fdopen(os.open(logfile, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW),
                               "ab") as fh:
                    fh.write(f"\nmac-sim: {label} printed its summary and then stayed quiet for "
                             f"{settle['after']}s; stopped it (exit {code} from the summary)\n"
                             .encode())
            except OSError as exc:
                log(f"WARN {label}: could not note the settle in {logfile}: {exc}")
            log(f"SETTLED {label}: stopped after its summary, exit={code}")

        if ready:
            if server:
                write_server({**server, "pid": proc.pid, "at": time.time(), "log": shown})
            log(f"DONE {label}: ready, left running as pid {proc.pid}")
            return self._reply(200, {"verb": verb, "exit_code": 0, "stdout": "ready",
                                     "stderr": "", "log": shown, **extra})

        timed_out = code is None
        if timed_out:
            kill_group(proc.pid)
            proc.wait(timeout=10)
        output = read_log_tail(logfile)
        try:
            keep_numbered_copy(logfile)
        except OSError as exc:
            log(f"WARN {label}: could not keep a numbered copy of {logfile}: {exc}")
        if claim and claim.aborted:
            log(f"RESET {label}: exit={proc.returncode}")
            return self._reply(200, {
                "verb": verb, "exit_code": proc.returncode or 137, "stdout": output,
                "stdout_is_log": True, "stderr": "killed by `mac-sim reset`",
                "log": shown, **extra})
        if timed_out:
            log(f"TIMEOUT {label} after {timeout}s")
            return self._reply(504, {
                "error": f"{label} " + ("did not report ready" if detach else "did not finish")
                         + f" within {timeout}s and was stopped; its output is in {shown}"})
        if detach:
            log(f"FAIL {label}: exited {code} before ready")
            return self._reply(200, {
                "verb": verb, "exit_code": code or 1, "stdout": output,
                "stdout_is_log": True, "log": shown,
                "stderr": f"{label} exited (code {code}) before it was ready", **extra})
        log(f"DONE {label}: exit={code}")
        self._reply(200, {"verb": verb, "exit_code": code, "stdout": output,
                          "stdout_is_log": True, "stderr": "", "log": shown, **extra})

    def _execute(self, verb, body, argv, timeout, extra, claim=None):
        cwd = extra.pop("cwd", None)
        tail = extra.pop("tail", None)
        env = extra.pop("env", None)
        port = extra.pop("kill_port", None)
        if port:
            kill_port(port)
        detach = extra.pop("detach", False)
        ready_url = extra.pop("ready_url", None)
        ready_match = extra.pop("ready_match", "")
        ready_app = extra.pop("ready_app", None)
        ready_boot = extra.pop("ready_boot", None)
        server = extra.pop("server", None)
        logfile = extra.pop("log", None)
        label = body.get("command") or verb
        log(f"RUN {label}: {shlex.join(argv)}" + (f" (cwd={cwd})" if cwd else ""))

        if logfile:
            return self._execute_logged(verb, label, argv, timeout, extra, claim, cwd, env,
                                        logfile, detach, ready_url, ready_match,
                                        ready_app, server, ready_boot)

        try:
            # A new session so a reset can kill the whole tree (os.killpg below),
            # not just this one pid: build/test tools commonly fork children
            # (xcodebuild, node, watchman, maestro's driver), and SIGKILL to only
            # the top pid can leave those running, still touching the simulator
            # or a shared port, even after reset claims the lock is free.
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, cwd=cwd, env=env, start_new_session=True)
        except OSError as exc:
            # A missing or unexecutable binary must come back as an error the
            # caller can read, not as a dead connection.
            log(f"FAIL {label}: {type(exc).__name__}: {exc}")
            return self._reply(502, {
                "error": f"could not execute {argv[0]!r} on the host: {exc}"})
        # Tracked on the claim so `mac-sim reset` can kill exactly this process
        # (see kill_active) instead of leaving the lock unrecoverable once the
        # caller that started it is gone.
        if claim:
            claim.pid = proc.pid
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Group-kill here too, not just from kill_active: a plain timeout
            # (no reset involved) has exactly the same orphaned-children risk
            # if only the top pid is signalled.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except Exception:
                stdout, stderr = "", ""
            if claim and claim.aborted:
                log(f"RESET {label}: exit={proc.returncode}")
                return self._reply(200, {
                    "verb": verb,
                    "exit_code": proc.returncode if proc.returncode is not None else 137,
                    "stdout": "\n".join(stdout.splitlines()[-tail:]) if tail else stdout,
                    "stderr": f"killed by `mac-sim reset`\n{stderr[-4_000:]}", **extra})
            log(f"TIMEOUT {label} after {timeout}s")
            return self._reply(504, {"error": f"{label} timed out after {timeout}s"})

        if tail:
            stdout = "\n".join(stdout.splitlines()[-tail:])
        # Builds are noisy; keep the tail, which is where failures appear.
        if len(stdout) > 200_000:
            stdout = stdout[-200_000:]
        log(f"DONE {label}: exit={proc.returncode}")
        self._reply(200, {"verb": verb, "exit_code": proc.returncode,
                          "stdout": stdout, "stderr": stderr[-40_000:], **extra})


def main():
    if subprocess.run(["xcrun", "simctl", "help"],
                      capture_output=True).returncode != 0:
        sys.exit("mac-sim-shim: xcrun/simctl unavailable; is Xcode installed?")
    config = load_config(CONFIG_PATH)
    try:
        with open(TOKEN_PATH) as fh:
            token = fh.read().strip()
    except OSError as exc:
        sys.exit(f"mac-sim-shim: cannot read the shared token at {TOKEN_PATH}: {exc}\n"
                 f"It is created by cc-sim-up; start the shim that way.")
    if len(token) < 32:
        sys.exit(f"mac-sim-shim: token at {TOKEN_PATH} is too short to be safe")

    Handler.config = config
    Handler.token = token
    # Bind to the bridge address only, not 0.0.0.0: the client-IP allowlist is a
    # second line of defence, not the first. Loopback is for host/bin/mac-sim on
    # the Mac itself: a VPN that routes 192.168.64.0/24 into its tunnel swallows
    # the Mac's own connections to the bridge address, but never loopback ones.
    # Both are bound before serving so either bind failing stops the shim.
    bridge = ThreadingHTTPServer((config.host, config.port), Handler)
    loopback = ThreadingHTTPServer(("127.0.0.1", config.port), Handler)
    log(f"listening on {config.host}:{config.port} and 127.0.0.1:{config.port}, "
        f"clients={config.clients}")
    for name, project in config.projects.items():
        log(f"  project {name}: {project['root']} "
            f"[{', '.join(sorted(project['commands'])) or 'no project commands'}]")
        if project.get("android"):
            android = project["android"]
            for dname, device in android["devices"].items():
                log(f"    android {dname}: {device['avd']} as {device['serial']}"
                    f"{' (default)' if dname == android['default'] else ''}")
    threading.Thread(target=loopback.serve_forever, daemon=True).start()
    bridge.serve_forever()


if __name__ == "__main__":
    main()
