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

The config is read from the host filesystem ONLY, never from the bind-mounted
workspace. That distinction is the security boundary: anything inside the
container can write to the workspace, so a workspace-resident adapter would let
the guest define its own host commands and defeat the allowlist entirely.

Run via `cc-sim-up`. Logs every accepted and rejected call.
"""

import fcntl
import json
import os
import plistlib
import re
import shlex
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address, ip_network
from pathlib import Path

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

MAX_BODY = 64 * 1024
DEFAULT_TIMEOUT = 300

UDID_RE = re.compile(r"^[0-9A-Fa-f-]{36}$")
BUNDLE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SCHEME_RE = re.compile(r"^[A-Za-z0-9._ -]{1,128}$")
DEVICE_RE = re.compile(r"^[A-Za-z0-9._()' -]{1,64}$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
URL_RE = re.compile(r"^[A-Za-z0-9+.-]{1,32}://[^\s]{0,512}$")
LAST_RE = re.compile(r"^\d{1,4}[smh]$")

BUILTIN_VERBS = [
    "list", "booted", "runtimes", "boot", "shutdown", "open_simulator_ui",
    "install", "uninstall", "launch", "terminate", "screenshot", "openurl",
    "logs", "xcodebuild", "project_command", "lock",
]

# Verbs that need no project context: they describe the Mac, not a checkout.
GLOBAL_VERBS = {"list", "booted", "runtimes", "open_simulator_ui"}

# Verbs that mutate what the simulator is running, so two checkouts doing them
# concurrently produce nonsense. Read-only observation (screenshot, logs) is
# deliberately absent: blocking it would break debugging for no safety gain.
EXCLUSIVE_BUILTINS = {"boot", "shutdown", "install", "uninstall", "launch",
                      "terminate", "openurl", "xcodebuild"}


class Rejected(Exception):
    """Request failed validation; never reaches a subprocess."""


class Busy(Rejected):
    """Another checkout holds the simulator. Distinct so it answers 409, not
    400: nothing about the request was wrong, it just arrived at a bad time."""


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
        self.projects = {}
        for entry in raw.get("projects", []):
            name = entry.get("name")
            if not name or not NAME_RE.match(name):
                raise ValueError(f"project name missing or invalid: {name!r}")
            root = Path(os.path.expanduser(entry["root"])).resolve()
            if not root.exists():
                raise ValueError(f"project root does not exist: {root}")
            entry["root"] = root
            entry.setdefault("guest_mount", "/workspace")
            entry.setdefault("defaults", {})
            entry.setdefault("commands", {})
            self.projects[name] = entry
        if not self.projects:
            raise ValueError("config declares no projects")

    @property
    def roots(self):
        return [p["root"] for p in self.projects.values()]

    def project(self, name):
        if name is None:
            if len(self.projects) == 1:
                return next(iter(self.projects.values()))
            raise Rejected("field 'project' is required when several are configured")
        if name not in self.projects:
            raise Rejected(f"unknown project: {name!r}")
        return self.projects[name]


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
    out = subprocess.run(
        ["xcrun", "simctl", "list", "devices", "available", "--json"],
        capture_output=True, text=True, timeout=60,
    )
    devices = json.loads(out.stdout).get("devices", {})
    matches = [d for runtime in devices.values() for d in runtime
               if d.get("name") == name and d.get("isAvailable")]
    if not matches:
        raise Rejected(f"no available simulator named {name!r}")
    # Prefer an already-booted match so repeated calls stay on one device.
    booted = [d for d in matches if d.get("state") == "Booted"]
    return (booted or matches)[-1]["udid"]


def app_bundle_id(app_path):
    """Read CFBundleIdentifier so `install` can be followed by `launch`."""
    info = app_path / "Info.plist"
    if not info.exists():
        raise Rejected(f"no Info.plist in {app_path}")
    with open(info, "rb") as fh:
        return plistlib.load(fh).get("CFBundleIdentifier")


def kill_port(port):
    """Free a TCP port without a shell. Used before builds whose bundler flags
    are inlined at build time, where a stale server would serve the wrong thing."""
    out = subprocess.run(["lsof", "-ti", f":{int(port)}"],
                         capture_output=True, text=True)
    killed = []
    for pid in out.stdout.split():
        try:
            os.kill(int(pid), signal.SIGKILL)
            killed.append(pid)
        except (ProcessLookupError, PermissionError, ValueError):
            pass
    if killed:
        log(f"freed port {port} (killed {', '.join(killed)})")


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
    merged = dict(spec)
    overlay = variants[variant_name]
    for key, value in overlay.items():
        if key == "env":
            merged["env"] = {**spec.get("env", {}), **value}
        else:
            merged[key] = value
    return merged


def project_command(body, project, base):
    name = field(body, "command", NAME_RE)
    commands = project["commands"]
    if name not in commands:
        raise Rejected(f"project {project['name']!r} declares no command {name!r}; "
                       f"available: {sorted(commands) or 'none'}")
    spec = merge_variant(commands[name], body.get("variant"))

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
    }
    # Only resolve a UDID when the template actually asks for one: it costs a
    # simctl call and would fail on a machine with no matching simulator.
    if any("{udid}" in part for part in spec.get("argv", [])):
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

    env = dict(os.environ)
    for prefix in spec.get("path_prepend", []):
        env["PATH"] = f"{os.path.expanduser(prefix)}:{env.get('PATH', '')}"
    env.update({k: str(v) for k, v in spec.get("env", {}).items()})

    # Project commands hold the claim by default: most of them build, install,
    # or drive the simulator. Declare "exclusive": false for the read-only ones.
    extra = {"cwd": str(cwd), "env": env, "_exclusive": bool(spec.get("exclusive", True))}
    if spec.get("kill_port"):
        extra["kill_port"] = int(spec["kill_port"])
    if spec.get("detach"):
        extra["detach"] = True
        extra["ready_url"] = spec.get("ready_url")
        extra["ready_match"] = spec.get("ready_match", "")
        extra["log"] = spec.get("log", f"/tmp/mac-sim-{name}.log")
    return argv, int(spec.get("timeout", DEFAULT_TIMEOUT)), extra


# --------------------------------------------------------------------------
# Built-in verbs
# --------------------------------------------------------------------------
def build_command(verb, body, config):
    """Resolve a request to (argv, timeout, extra).

    Every project-scoped verb is resolved from the caller's checkout rather than
    the project root, and is tagged with the claim it needs.
    """
    if verb in GLOBAL_VERBS:
        return dispatch(verb, body, None, None)
    project = config.project(body.get("project"))
    base = resolve_workdir(body, project)
    argv, timeout, extra = dispatch(verb, body, project, base)
    extra["workdir"] = str(base)
    if extra.pop("_exclusive", verb in EXCLUSIVE_BUILTINS) and config.lock_enabled:
        extra["_lock"] = project["name"]
    return argv, timeout, extra


def dispatch(verb, body, project, base):
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
        return ["xcrun", "simctl", verb, resolve_device(body, project), bundle], 120, {}

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

    if verb == "logs":
        # Bounded: a finite dump, never an unbounded stream.
        lines = body.get("lines", 200)
        if not isinstance(lines, int) or not 1 <= lines <= 5000:
            raise Rejected("lines must be an int in 1..5000")
        argv = ["xcrun", "simctl", "spawn", resolve_device(body, project),
                "log", "show", "--style", "compact", "--last",
                field(body, "last", LAST_RE, required=False, default="2m")]
        if "predicate" in body:
            pred = body["predicate"]
            if not isinstance(pred, str) or len(pred) > 512 or '"' in pred:
                raise Rejected("invalid predicate")
            argv += ["--predicate", pred]
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
        return project_command(body, project, base)

    raise Rejected(f"unknown verb: {verb!r}")


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
def _owner_path(name):
    return LOCK_DIR / f"{name}.owner.json"


def read_owner(name):
    try:
        with open(_owner_path(name)) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def write_owner(name, workdir, verb):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _owner_path(name).with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump({"workdir": str(workdir), "verb": verb, "at": time.time()}, fh)
    tmp.replace(_owner_path(name))  # atomic: a reader never sees a half-write


def clear_owner(name):
    try:
        _owner_path(name).unlink()
    except FileNotFoundError:
        pass


def describe_owner(name, ttl):
    owner = read_owner(name)
    if not owner:
        return f"{name}: simulator free"
    age = int(time.time() - float(owner.get("at", 0)))
    stale = " (expired)" if age >= ttl else ""
    return (f"{name}: simulator held by {owner.get('workdir')}{stale}\n"
            f"  last command: {owner.get('verb')}, {age // 60}m {age % 60}s ago")


class Claim:
    def __init__(self, name, workdir, verb, force, ttl):
        self.name, self.workdir = name, str(workdir)
        self.verb, self.force, self.ttl = verb, force, ttl
        self.fh = None

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
                       f"({owner.get('verb', '?')} from {owner.get('workdir', '?')}). "
                       f"Wait for it to finish.")
        owner = read_owner(self.name)
        if (owner and not self.force
                and owner.get("workdir") != self.workdir
                and time.time() - float(owner.get("at", 0)) < self.ttl):
            self.release(record=False)
            age = int(time.time() - float(owner["at"]))
            raise Busy(
                f"{self.name}: the simulator is held by {owner['workdir']}\n"
                f"  (last command {owner.get('verb')!r}, {age // 60}m ago)\n"
                f"Running from {self.workdir} would overwrite that build: same "
                f"bundle id, same device, same bundler port.\n"
                f"Take it over with -f, or hand it back with `mac-sim release`.")
        write_owner(self.name, self.workdir, self.verb)
        return self

    def release(self, record=True):
        if self.fh is None:
            return
        if record:
            # Refresh on the way out so the TTL measures idleness, not the age
            # of the first command in a session.
            write_owner(self.name, self.workdir, self.verb)
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
            "lock_ttl": self.config.lock_ttl,
            "projects": {
                name: {
                    "root": str(p["root"]),
                    "owner": read_owner(name),
                    "guest_mount": p["guest_mount"],
                    "defaults": p["defaults"],
                    "commands": {
                        cname: {
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
            if verb == "lock":
                return self._lock_verb(body)
            argv, timeout, extra = build_command(verb, body, self.config)
            lock_name = extra.pop("_lock", None)
            if lock_name:
                claim = Claim(lock_name, extra.get("workdir"),
                              body.get("command") or verb,
                              bool(body.get("force")), self.config.lock_ttl)
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
            self._execute(verb, body, argv, timeout, extra)
        finally:
            if claim:
                claim.release()

    def _lock_verb(self, body):
        """Inspect or hand back the claim. Never runs a subprocess."""
        project = self.config.project(body.get("project"))
        name = project["name"]
        if body.get("action") == "release":
            clear_owner(name)
            log(f"RELEASE {name}")
            return self._reply(200, {"verb": "lock", "exit_code": 0,
                                     "stdout": f"{name}: simulator released",
                                     "stderr": ""})
        return self._reply(200, {"verb": "lock", "exit_code": 0,
                                 "stdout": describe_owner(name, self.config.lock_ttl),
                                 "stderr": ""})

    def _execute(self, verb, body, argv, timeout, extra):
        cwd = extra.pop("cwd", None)
        tail = extra.pop("tail", None)
        env = extra.pop("env", None)
        port = extra.pop("kill_port", None)
        if port:
            kill_port(port)
        detach = extra.pop("detach", False)
        ready_url = extra.pop("ready_url", None)
        ready_match = extra.pop("ready_match", "")
        logfile = extra.pop("log", None)
        label = body.get("command") or verb
        log(f"RUN {label}: {shlex.join(argv)}" + (f" (cwd={cwd})" if cwd else ""))

        if detach:
            # A server must outlive this request, so it runs in its own session
            # with output to a log the caller can tail.
            try:
                with open(logfile, "wb") as fh:
                    subprocess.Popen(argv, cwd=cwd, env=env, stdout=fh,
                                     stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as exc:
                log(f"FAIL {label}: {type(exc).__name__}: {exc}")
                return self._reply(502, {
                    "error": f"could not start {argv[0]!r} on the host: {exc}"})
            ready, deadline = not ready_url, time.time() + timeout
            while ready_url and time.time() < deadline:
                probe = subprocess.run(["curl", "-s", "-m", "2", ready_url],
                                       capture_output=True, text=True)
                if ready_match in probe.stdout and probe.stdout:
                    ready = True
                    break
                time.sleep(1)
            log(f"DONE {label}: ready={ready}")
            return self._reply(200, {
                "verb": verb, "exit_code": 0 if ready else 1,
                "stdout": "ready" if ready else "",
                "stderr": "" if ready else
                          f"did not report ready within {timeout}s; see {logfile} on the host",
                "log": logfile, **extra})

        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout, cwd=cwd, env=env)
        except subprocess.TimeoutExpired:
            return self._reply(504, {"error": f"{label} timed out after {timeout}s"})
        except OSError as exc:
            # A missing or unexecutable binary must come back as an error the
            # caller can read, not as a dead connection.
            log(f"FAIL {label}: {type(exc).__name__}: {exc}")
            return self._reply(502, {
                "error": f"could not execute {argv[0]!r} on the host: {exc}"})

        stdout = proc.stdout
        if tail:
            stdout = "\n".join(stdout.splitlines()[-tail:])
        # Builds are noisy; keep the tail, which is where failures appear.
        if len(stdout) > 200_000:
            stdout = stdout[-200_000:]
        log(f"DONE {label}: exit={proc.returncode}")
        self._reply(200, {"verb": verb, "exit_code": proc.returncode,
                          "stdout": stdout, "stderr": proc.stderr[-40_000:], **extra})


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
    # second line of defence, not the first.
    log(f"listening on {config.host}:{config.port}, clients={config.clients}")
    for name, project in config.projects.items():
        log(f"  project {name}: {project['root']} "
            f"[{', '.join(sorted(project['commands'])) or 'no project commands'}]")
    ThreadingHTTPServer((config.host, config.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
