#!/usr/bin/env python3
"""Two-line Herdr agent status display; Python standard library only."""
import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

PLUGIN = "jermen.status-indicator"
SOURCE = "plugin:" + PLUGIN
STATES = ("working", "blocked", "done", "idle", "unknown")
DETAIL_TOKENS = [f"si_{metric}_{level}" for metric in ("h", "w", "ctx")
                 for level in ("normal", "yellow", "red")] + ["si_usage", "si_notes",
                 "si_h_stale", "si_w_stale", "si_usage_stale"]
TOKENS = ["si_" + state for state in STATES] + ["si_detail", *DETAIL_TOKENS]
# Herdr Radar 1.3.5 / herdr-icon-agent-ui glyph map; see THIRD_PARTY_NOTICES.md.
VENDORS = "claude codex opencode omp cline mastracode kimi kilo maki pi hermes cursor copilot deepseek gemini gpt qwen grok agy kiro amp devin qodercli".split()
LOGOS = {name: chr(0xE1A0 + n) for n, name in enumerate(VENDORS)}
MARKS = {"done": "\ue1c0", "blocked": "\ue1c1", "idle": "\ue1c2", "unknown": "\ue1c3"}
TEXT_MARKS = {"done": "✓", "blocked": "?", "idle": "○", "unknown": "◌"}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def clean(value):
    """Keep metadata as one printable line, including untrusted terminal titles."""
    value = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", str(value or ""))
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    return re.sub(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]", " ", value).strip()[:240]


def stamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if math.isfinite(value) else 0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0


def percent(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 100:
        return f"{value:.0f} %"
    return "--"


def money(value):
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and amount >= 0:
            return f"${amount:.2f}"
    except InvalidOperation:
        pass
    return "--"


def quota_label(window):
    name, minutes = window.get("name"), window.get("window_minutes")
    if name == "five_hour" or minutes == 300:
        return "h"
    if name == "seven_day" or minutes == 10080:
        return "w"
    if minutes == 1440:
        return "d"
    if isinstance(minutes, (int, float)) and minutes > 0:
        return f"{minutes / 60:g}h"
    return clean(name) or "quota"


def subagents(pane):
    # Optional integration contract: a count, never guessed from sibling panes.
    value = pane.get("tokens", {}).get("subagents_running")
    if isinstance(value, (str, int)) and re.fullmatch(r"[0-9]{1,5}", str(value)):
        n = int(value)
        return f"{n} subagent" + ("s" if n != 1 else "")
    return None


def detail_parts(pane, snapshot, config, now):
    """Ordered (field, text, raw percentage) values for plain and styled layouts."""
    children = subagents(pane)
    suffix = [("notes", children, None)] if children else []
    if not snapshot or snapshot.get("schema_version") != 1:
        return [("usage", "usage unavailable", None)] + suffix
    age = now - stamp(snapshot.get("generated_at"))
    if age > config.get("snapshot_max_age", 120) or age < -60:
        return [("usage", "usage stale", None)] + suffix
    binding = next((p for p in snapshot.get("panes", []) if p.get("pane_id") == pane["pane_id"]), None)
    identity = pane.get("agent_session") or {}
    key = f"{pane['agent']}:{identity.get('value')}" if identity.get("kind") == "id" else None
    if not binding or not key or binding.get("session_key") != key:
        return [("usage", "usage unavailable", None)] + suffix
    session = next((s for s in snapshot.get("sessions", []) if s.get("key") == key), None)
    if not session or not session.get("active"):
        return [("usage", "usage unavailable", None)] + suffix
    current = session.get("current") or {}
    limits = current.get("limits") or []
    mode = config.get("modes", {}).get(pane["agent"], config.get("mode", "quota"))
    parts = []
    collection = binding.get("collection_status")
    collection_note = {
        "transcript_missing": "transcript missing",
        "transcript_ambiguous": "multiple transcripts",
        "transcript_error": "transcript read error",
    }.get(collection, "usage " + clean(collection or "unavailable"))
    note_shown = False
    if mode == "quota" or (mode == "auto" and limits):
        windows = {quota_label(w): w for w in limits if isinstance(w, dict)}
        observed = current.get("metric_observed_at", {}).get("limits")
        stale = limits and (not observed or now - stamp(observed) > config.get("quota_max_age", 900))
        other_stale = False
        # Preserve both requested columns even if a provider reports only one.
        for label in dict.fromkeys(["h", "w", *windows]):
            w = windows.get(label, {})
            expired = w.get("resets_at") is not None and stamp(w["resets_at"]) <= now
            value = None if expired else w.get("used_percent")
            field = label if label in ("h", "w") else "usage"
            parts.append((field, label + " " + percent(value), value))
            if stale and percent(value) != "--":
                if field in ("h", "w"):
                    parts.append((field + "_stale", "*", None))
                else:
                    other_stale = True
        # Additional provider windows share one sidebar field and freshness mark.
        if other_stale:
            parts.append(("usage_stale", "*", None))
    else:
        has_spending = False
        for label, period in (("d", "today"), ("w", "last_7_days"), ("m", "last_31_days")):
            data = session.get("periods", {}).get(period, {})
            metric = data.get("billed_spend") if mode == "billed" else data.get("estimated_spend")
            value = "--"
            if isinstance(metric, dict) and metric.get("currency") == "USD":
                if mode == "billed" or data.get("usage_records_observed", 0) > 0:
                    value = money(metric.get("amount"))
                    if value != "--" and mode != "billed":
                        value = "~" + value
                    if metric.get("coverage") == "partial":
                        value += "*"
            has_spending = has_spending or value not in ("--", "--*")
            parts.append(("usage", label + " " + value, None))
        if not has_spending and collection != "ok":
            # Put the reason in the first visible field instead of three empty
            # amounts pushing the diagnostic beyond a narrow sidebar's edge.
            parts = [("usage", collection_note, None)]
            note_shown = True
        elif mode == "billed":
            parts.append(("usage", "reported", None))
    context = current.get("context") or {}
    if context or mode == "quota":
        value = context.get("used_percent")
        parts.append(("ctx", "ctx " + percent(value), value))
    if collection != "ok" and not note_shown:
        parts.append(("notes", collection_note, None))
    return parts + suffix


def details(pane, snapshot, config, now):
    return " / ".join(text for _, text, _ in detail_parts(pane, snapshot, config, now))


def detail_tokens(parts):
    tokens = dict.fromkeys(DETAIL_TOKENS)
    # Keep the complete plain line for clients that have not updated their layout.
    tokens["si_detail"] = " / ".join(text for _, text, _ in parts)
    for field, text, value in parts:
        key = "si_" + field
        if field in ("h", "w", "ctx"):
            level = "normal"
            if percent(value) != "--":
                level = "red" if value > 80 else "yellow" if value > 40 else "normal"
            key += "_" + level
        tokens[key] = " / ".join(filter(None, (tokens[key], text)))
    return tokens


class Renderer:
    def __init__(self, config):
        self.config = config
        self.history = {}

    def render(self, snapshot, usage, now):
        result = {}
        for pane in snapshot.get("panes", []):
            if not pane.get("agent"):
                continue
            pid = pane["pane_id"]
            identity = (pane.get("agent_session") or {}).get("value") or pane.get("terminal_id")
            previous = self.history.get(pid, {})
            if previous.get("identity") != identity:
                previous = {}
            state = pane.get("agent_status", "unknown")
            state = state if state in STATES else "unknown"
            # A completion/question must survive Herdr's subsequent idle report.
            if state == "idle" and previous.get("state") == "blocked":
                state = "blocked"
            if state == "idle" and previous.get("state") == "done" and not pane.get("focused"):
                state = "done"
            title = clean(pane.get("label") or pane.get("terminal_title_stripped"))
            title = title or previous.get("title") or clean(pane["agent"])
            self.history[pid] = {"identity": identity, "state": state, "title": title}
            font = self.config.get("icons", "text") == "font"
            logo = LOGOS.get(pane["agent"], "◇") if font else clean(pane["agent"])
            mark = SPINNER[int(now * 2) % len(SPINNER)] if state == "working" else (MARKS if font else TEXT_MARKS)[state]
            tokens = dict.fromkeys(TOKENS)
            tokens["si_" + state] = f"{logo} {mark} {title}"
            tokens.update(detail_tokens(detail_parts(pane, usage, self.config, now)))
            result[pid] = tokens
        self.history = {pid: h for pid, h in self.history.items() if pid in result}
        return result


def call(endpoint, method, params=None):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(4)
        sock.connect(endpoint)
        sock.sendall((json.dumps({"id": "status-indicator", "method": method, "params": params or {}}) + "\n").encode())
        with sock.makefile("rb") as stream:
            line = stream.readline(8 * 1024 * 1024 + 1)
    if len(line) > 8 * 1024 * 1024 or not line.endswith(b"\n"):
        raise ValueError("invalid Herdr response")
    reply = json.loads(line)
    if reply.get("error"):
        raise ValueError("Herdr request failed: " + str(reply["error"].get("code", "unknown")))
    return reply["result"]


def read_json(path):
    try:
        with Path(path).open() as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


class SnapshotReader:
    """Read a collector publication once, not once per animation frame."""
    def __init__(self, path):
        self.path, self.identity, self.value = Path(path), None, None

    def read(self):
        try:
            stat = self.path.stat()
            identity = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if identity != self.identity:
                self.value = read_json(self.path) if stat.st_size <= 64 * 1024 * 1024 else None
                self.identity = identity
            return self.value
        except OSError:
            self.identity, self.value = None, None
            return None


def settings(args):
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    herdr = Path(os.environ.get("HERDR_CONFIG_PATH", config_home / "herdr/config.toml"))
    endpoint = args.socket or os.environ.get("HERDR_SOCKET_PATH")
    if not endpoint:
        raise ValueError("HERDR_SOCKET_PATH or --socket is required")
    directory = Path(os.environ.get("HERDR_PLUGIN_CONFIG_DIR", herdr.parent / "plugins/config" / PLUGIN))
    config = read_json(args.config or directory / "config.json") or {}
    if config.get("mode", "quota") not in ("auto", "quota", "estimated", "billed"):
        raise ValueError("invalid display mode")
    for mode in config.get("modes", {}).values():
        if mode not in ("auto", "quota", "estimated", "billed"):
            raise ValueError("invalid agent display mode")
    if config.get("icons", "text") not in ("text", "font"):
        raise ValueError("icons must be text or font")
    for name, default in (("snapshot_max_age", 120), ("quota_max_age", 900)):
        if not isinstance(config.get(name, default), (float, int)) or not 30 <= config.get(name, default) <= 86400:
            raise ValueError("invalid freshness threshold")
    scope = hashlib.sha256(os.path.abspath(endpoint).encode()).hexdigest()[:16]
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    state = Path(config.get("state_dir", state_home / "herdr-status-indicator")) / scope
    usage_config = read_json(herdr.parent / "plugins/config/jermen.agent-usage/config.json") or {}
    usage_root = Path(usage_config.get("state_dir", state_home / "herdr-agent-usage"))
    usage = Path(config.get("usage_snapshot", usage_root / scope / "snapshot.json"))
    return config, endpoint, state, usage


def report_tokens(endpoint, pid, tokens):
    # Herdr accepts at most 16 token patches per request, including clears.
    items = list(tokens.items())
    for start in range(0, len(items), 16):
        call(endpoint, "pane.report_metadata", {"pane_id": pid, "source": SOURCE,
                                                "tokens": dict(items[start:start + 16])})


def publish(endpoint, rows, snapshot):
    panes = {p["pane_id"]: p for p in snapshot["panes"]}
    for pid, tokens in rows.items():
        current = panes[pid].get("tokens", {})
        changed = {k: v for k, v in tokens.items() if current.get(k) != v}
        report_tokens(endpoint, pid, changed)
    # Clear only this plugin's fields when an agent exits its pane.
    for pid, pane in panes.items():
        if pid not in rows and any(k in pane.get("tokens", {}) for k in TOKENS):
            report_tokens(endpoint, pid, {k: None for k in TOKENS if k in pane.get("tokens", {})})


def watch(config, endpoint, state, usage):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "watch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        (state / "pid").write_text(str(os.getpid()))
        running = True

        def stop(_sig, _frame):
            nonlocal running
            running = False

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        original = os.stat(endpoint)
        renderer = Renderer(config)
        reader = SnapshotReader(usage)
        try:
            while running:
                begin = time.monotonic()
                try:
                    current = os.stat(endpoint)
                    if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
                        break
                    snapshot = call(endpoint, "session.snapshot")["snapshot"]
                    publish(endpoint, renderer.render(snapshot, reader.read(), time.time()), snapshot)
                    (state / "health.json").write_text(json.dumps({"pid": os.getpid(), "updated_at": time.time(), "agents": len(renderer.history)}))
                except FileNotFoundError:
                    break
                except (OSError, ValueError, KeyError, TypeError):
                    print("status-indicator: refresh_failed", file=sys.stderr, flush=True)
                time.sleep(max(0, 0.5 - (time.monotonic() - begin)))
        finally:
            (state / "pid").unlink(missing_ok=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preview", "refresh", "start", "watch", "stop", "clear", "status"))
    parser.add_argument("--socket")
    parser.add_argument("--config")
    args = parser.parse_args()
    config, endpoint, state, usage = settings(args)
    if args.command == "status":
        health = read_json(state / "health.json") or {}
        health["healthy"] = time.time() - health.get("updated_at", 0) < 10
        print(json.dumps(health))
        return
    if args.command == "watch":
        watch(config, endpoint, state, usage)
        return
    if args.command in ("start", "stop"):
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (state / "watch.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                alive = False
            except BlockingIOError:
                alive = True
            if args.command == "stop":
                if alive:
                    # The held lock authenticates the owner; no unlocked stale PID kill.
                    pid = int((state / "pid").read_text())
                    os.kill(pid, signal.SIGTERM)
                    deadline = time.monotonic() + 6
                    while time.monotonic() < deadline:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            time.sleep(0.1)
                    else:
                        raise ValueError("watcher did not stop")
                return
            if alive:
                return
        command = [sys.executable, str(Path(__file__).resolve()), "watch", "--socket", endpoint]
        if args.config:
            command += ["--config", str(Path(args.config).resolve())]
        with (state / "watch.log").open("a") as output:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True, close_fds=True)
        return
    snapshot = call(endpoint, "session.snapshot")["snapshot"]
    rows = Renderer(config).render(snapshot, read_json(usage), time.time())
    if args.command == "preview":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif args.command == "clear":
        publish(endpoint, {}, snapshot)
    else:
        publish(endpoint, rows, snapshot)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("status-indicator: " + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
