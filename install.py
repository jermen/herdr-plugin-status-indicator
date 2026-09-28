#!/usr/bin/env python3
"""Preview/install the sidebar with a checked backup and explicit Radar handover."""
import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

START = "# >>> status-indicator sidebar block"
END = "# <<< status-indicator sidebar block"
RADAR_START = "# >>> herdr-radar sidebar block"
RADAR_END = "# <<< herdr-radar sidebar block"
ROOT = Path(__file__).resolve().parent
PLUGIN = tomllib.loads((ROOT / "herdr-plugin.toml").read_text())["id"]


def remove_block(text, start, end):
    if text.count(start) != text.count(end) or text.count(start) > 1:
        raise ValueError("ambiguous managed block")
    if start not in text:
        return text
    pattern = re.compile(r"(?m)^" + re.escape(start) + r"\n[\s\S]*?^" + re.escape(end) + r"(?:\n|$)")
    result, count = pattern.subn("", text)
    if count != 1:
        raise ValueError("invalid managed block")
    return result


def sidebar():
    # One source for the local installer and a separately configured SSH client.
    return (ROOT / "client-sidebar.toml").read_text()


def candidate(text, replace_radar=False):
    tomllib.loads(text)
    text = remove_block(text, START, END)
    if RADAR_START in text:
        if not replace_radar:
            raise ValueError("Radar owns the sidebar; use --replace-radar for the handover")
        text = remove_block(text, RADAR_START, RADAR_END)
    # Refuse any unowned agent configuration, including dotted/quoted tables.
    doc = tomllib.loads(text)
    if "agents" in doc.get("ui", {}).get("sidebar", {}):
        raise ValueError("user-owned ui.sidebar.agents table needs an explicit merge")
    result = text.rstrip() + "\n\n" + sidebar()
    tomllib.loads(result)
    return result


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=20).stdout


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def atomic(path, text):
    fd, name = tempfile.mkstemp(prefix=path.name + ".status-indicator.", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temp, path.stat().st_mode & 0o777)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def config_path():
    if os.environ.get("HERDR_CONFIG_PATH"):
        return Path(os.environ["HERDR_CONFIG_PATH"]).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return (base / "herdr/config.toml").resolve()


def display_config(config_path, mode):
    path = config_path.parent / "plugins/config" / PLUGIN / "config.json"
    before = path.read_text() if path.exists() else None
    config = json.loads(before if before is not None else (ROOT / "config.example.json").read_text())
    if not isinstance(config, dict):
        raise ValueError("plugin config must be a JSON object")
    if before is not None and config.get("mode") == mode:
        return path, before, before
    config["mode"] = mode
    return path, before, json.dumps(config, indent=2) + "\n"


def restore_display_config(path, previous):
    if previous is None:
        path.unlink()
    else:
        atomic(path, previous)


def font_directory():
    if sys.platform == "darwin":
        return Path.home() / "Library/Fonts"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Microsoft/Windows/Fonts"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "fonts"


def install_font():
    directory = font_directory()
    directory.mkdir(parents=True, exist_ok=True)
    font = directory / "HerdrAgentIconsMax-Regular.ttf"
    if not font.exists():
        shutil.copyfile(ROOT / "assets/HerdrAgentIconsMax-Regular.ttf", font)
    if sys.platform == "win32":
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
            winreg.SetValueEx(key, "Herdr Agent Icons Max (TrueType)", 0, winreg.REG_SZ, str(font))
    elif shutil.which("fc-cache"):
        run("fc-cache", "-f", str(directory))
    return font


def client_backup(path, before, after):
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    backup = state / "herdr-status-indicator/client-backups" / str(time.time_ns())
    backup.mkdir(parents=True, mode=0o700)
    (backup / "config.toml").write_text(before)
    (backup / "restore.json").write_text(json.dumps({"mode": "client", "config_path": str(path),
        "config_existed": path.exists(), "installed_hash": digest(after)}))
    if (backup / "config.toml").read_text() != before:
        raise ValueError("backup verification failed")
    return backup


def apply_client(path, before, after):
    """Configure the UI computer without loading the daemon or contacting Herdr."""
    if path.is_symlink():
        raise ValueError("use the resolved config path, not a symlink")
    exists = path.exists()
    backup = client_backup(path, before, after) if before != after else None
    font = install_font()
    if path.exists() != exists or (path.read_text() if exists else "") != before:
        raise ValueError("Herdr config changed during installation")
    if before != after:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic(path, after)
    print("Client sidebar installed: " + str(path))
    print("Icon font: " + str(font))
    if backup:
        print("Backup: " + str(backup))
    print("No plugin daemon was installed or started on this computer.")
    print("In Herdr's global menu, select 'reload config'.")
    print("If icons show as boxes, configure your terminal's fallback font:")
    print('  Ghostty: font-codepoint-map = U+E1A0-U+E1B6="Herdr Agent Icons Max"')
    print('           font-codepoint-map = U+E1C0-U+E1C5="Herdr Agent Icons Max"')
    print('  Kitty: symbol_map U+E1A0-U+E1B6,U+E1C0-U+E1C5 Herdr Agent Icons Max')
    print("Other terminals: map those codepoint ranges to Herdr Agent Icons Max, then reopen the terminal if needed.")


def apply(config_path, before, after, replace_radar, display_mode="quota"):
    from indicator import call

    plugin_config, plugin_before, plugin_after = display_config(config_path, display_mode)
    endpoint = os.environ.get("HERDR_SOCKET_PATH")
    if not endpoint:
        raise ValueError("run inside Herdr")
    call(endpoint, "session.snapshot")
    plugins = run("herdr", "plugin", "list")
    radar_enabled = bool(re.search(r"^- hhdebb\.herdr-radar .* enabled ", plugins, re.M))
    if radar_enabled and not replace_radar:
        raise ValueError("disable Radar via --replace-radar before installing")
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    backup = state_home / "herdr-status-indicator/backups" / str(time.time_ns())
    backup.mkdir(parents=True, mode=0o700)
    (backup / "config.toml").write_text(before)
    (backup / "plugins.txt").write_text(plugins)
    if plugin_before is not None:
        (backup / "plugin-config.json").write_text(plugin_before)
        if (backup / "plugin-config.json").read_text() != plugin_before:
            raise ValueError("plugin config backup verification failed")
    (backup / "restore.json").write_text(json.dumps({"config_path": str(config_path), "installed_hash": digest(after),
        "radar_enabled": radar_enabled, "plugin_config_path": str(plugin_config),
        "plugin_config_existed": plugin_before is not None, "plugin_config_installed_hash": digest(plugin_after)}))
    if (backup / "config.toml").read_text() != before:
        raise ValueError("backup verification failed")
    plugin_written = False
    try:
        if radar_enabled:
            run("herdr", "plugin", "action", "invoke", "state-stop", "--plugin", "hhdebb.herdr-radar")
            run("herdr", "plugin", "disable", "hhdebb.herdr-radar")
            call(endpoint, "agent.view.clear", {"source": "plugin:hhdebb.herdr-radar"})
        if config_path.read_text() != before:
            raise ValueError("Herdr config changed during installation")
        install_font()
        # Stop using the old settings before changing them. Write the config
        # before linking/enabling so plugin initialization sees the chosen mode.
        run(sys.executable, str(ROOT / "indicator.py"), "stop")
        if (plugin_config.read_text() if plugin_config.exists() else None) != plugin_before:
            raise ValueError("plugin config changed during installation")
        if config_path.read_text() != before:
            raise ValueError("Herdr config changed during installation")
        if plugin_before != plugin_after:
            plugin_config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic(plugin_config, plugin_after)
            plugin_written = True
        run("herdr", "plugin", "link", str(ROOT))
        run("herdr", "plugin", "enable", PLUGIN)
        atomic(config_path, after)
        run("herdr", "server", "reload-config")
        run("herdr", "plugin", "action", "invoke", "refresh", "--plugin", PLUGIN)
        run("herdr", "plugin", "action", "invoke", "start", "--plugin", PLUGIN)
    except Exception:
        for command in ((sys.executable, str(ROOT / "indicator.py"), "stop"),
                        (sys.executable, str(ROOT / "indicator.py"), "clear"),
                        ("herdr", "plugin", "disable", PLUGIN)):
            try:
                run(*command)
            except (OSError, subprocess.SubprocessError):
                pass
        # Restore only our exact write, preserving concurrent user edits.
        if plugin_written and plugin_config.exists() and plugin_config.read_text() == plugin_after:
            restore_display_config(plugin_config, plugin_before)
        if config_path.read_text() == after:
            atomic(config_path, before)
            run("herdr", "server", "reload-config")
        if radar_enabled:
            run("herdr", "plugin", "enable", "hhdebb.herdr-radar")
            run("herdr", "plugin", "action", "invoke", "state-start", "--plugin", "hhdebb.herdr-radar")
        raise
    print("Installed " + PLUGIN + "; display mode: " + display_mode + "; backup: " + str(backup))
    print("Server metadata is ready. The sidebar is rendered by the Herdr UI client's local config.")
    print("For SSH connections, run ./install.sh from a complete checkout on the UI computer to install its sidebar and font.")
    print("In the Herdr UI, open the global menu and select 'reload config'; the server reload command does not reload the client.")


def rollback(backup, client=False):
    info = json.loads((backup / "restore.json").read_text())
    if (info.get("mode") == "client") != client:
        raise ValueError("backup mode differs; select --client or --server to match the backup")
    config = Path(info["config_path"])
    if digest(config.read_text()) != info["installed_hash"]:
        raise ValueError("config changed after install; merge the saved config manually")
    previous = (backup / "config.toml").read_text()
    tomllib.loads(previous)
    if client:
        if info.get("config_existed", True):
            atomic(config, previous)
        else:
            config.unlink()
        print("Restored client config; installed fonts and backups retained. Reload config in the Herdr UI.")
        return
    plugin_config = Path(info["plugin_config_path"]) if "plugin_config_path" in info else None
    if plugin_config:
        if not plugin_config.is_file() or digest(plugin_config.read_text()) != info["plugin_config_installed_hash"]:
            raise ValueError("plugin config changed after install; merge the saved config manually")
        plugin_previous = (backup / "plugin-config.json").read_text() if info["plugin_config_existed"] else None
        if plugin_previous is not None:
            json.loads(plugin_previous)
    run(sys.executable, str(ROOT / "indicator.py"), "stop")
    run(sys.executable, str(ROOT / "indicator.py"), "clear")
    run("herdr", "plugin", "disable", PLUGIN)
    if plugin_config:
        restore_display_config(plugin_config, plugin_previous)
    atomic(config, previous)
    run("herdr", "server", "reload-config")
    if info["radar_enabled"]:
        run("herdr", "plugin", "enable", "hhdebb.herdr-radar")
        run("herdr", "plugin", "action", "invoke", "state-start", "--plugin", "hhdebb.herdr-radar")
    print("Restored sidebar from " + str(backup))
    print("Reload config in the Herdr UI as well. A separately configured SSH client must restore its own sidebar block.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--client", action="store_true", help="Install only this computer's sidebar and icon font")
    parser.add_argument("--config", type=Path, help="Explicit Herdr config file")
    parser.add_argument("--replace-radar", action="store_true")
    parser.add_argument("--rollback", type=Path)
    parser.add_argument("--display-mode", choices=("quota", "auto", "estimated", "billed"),
                        help="Server display mode, including existing configs (default: quota)")
    args = parser.parse_args()
    if args.display_mode and (args.client or args.rollback):
        parser.error("--display-mode requires a server install, not --client or --rollback")
    if args.rollback:
        rollback(args.rollback, args.client)
        return
    path = args.config.expanduser().resolve() if args.config else config_path()
    if args.apply and not args.client and not path.is_file():
        raise ValueError("Herdr server config does not exist; start Herdr before installing")
    before = path.read_text() if path.exists() else ""
    after = candidate(before, args.replace_radar)
    if args.apply:
        if args.client:
            apply_client(path, before, after)
        else:
            os.environ["HERDR_CONFIG_PATH"] = str(path)
            apply(path, before, after, args.replace_radar, args.display_mode or "quota")
    else:
        print("".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile=str(path), tofile="status-indicator candidate")), end="")
        if not args.client:
            plugin_path, plugin_before, plugin_after = display_config(path, args.display_mode or "quota")
            print("".join(difflib.unified_diff((plugin_before or "").splitlines(True), plugin_after.splitlines(True),
                          fromfile=str(plugin_path), tofile="status-indicator display config candidate")), end="")


if __name__ == "__main__":
    main()
