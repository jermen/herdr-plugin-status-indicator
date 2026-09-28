import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import install


class ClientInstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="indicator client test ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / "config/herdr/config.toml"
        self.state = self.root / "state"
        self.data = self.root / "data"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # Herdr must never be invoked in client mode, even if the caller inherited
        # a live socket. Fake it with a failing command and an invocation marker.
        herdr = self.bin / "herdr"
        herdr.write_text('#!/bin/sh\ntouch "' + str(self.root / 'herdr-called') + '"\nexit 99\n')
        herdr.chmod(0o755)
        font_cache = self.bin / "fc-cache"
        font_cache.write_text('#!/bin/sh\nexit 0\n')
        font_cache.chmod(0o755)
        self.env = {**os.environ, "XDG_CONFIG_HOME": str(self.root / "config"),
                    "XDG_STATE_HOME": str(self.state), "XDG_DATA_HOME": str(self.data),
                    "HERDR_CONFIG_PATH": str(self.config), "HERDR_SOCKET_PATH": "/must/not/connect.sock",
                    "PATH": str(self.bin) + os.pathsep + os.environ["PATH"], "PYTHON_BIN": sys.executable}

    def invoke(self, *arguments, check=True):
        result = subprocess.run(["bash", str(install.ROOT / "install.sh"), *arguments],
                                cwd=self.root, env=self.env, capture_output=True, text=True, timeout=15)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "herdr-called").exists())
        return result

    def backups(self):
        return sorted(self.state.glob("herdr-status-indicator/client-backups/*"))

    def test_default_installs_client_from_any_cwd_without_daemon(self):
        result = self.invoke()
        self.assertIn("No plugin daemon", result.stdout)
        config = tomllib.loads(self.config.read_text())
        self.assertEqual(len(config["ui"]["sidebar"]["agents"]["rows"]), 2)
        font = self.data / "fonts/HerdrAgentIconsMax-Regular.ttf"
        self.assertEqual(font.read_bytes(), (install.ROOT / "assets/HerdrAgentIconsMax-Regular.ttf").read_bytes())
        self.assertFalse((self.config.parent / "plugins").exists())
        self.assertEqual(len(self.backups()), 1)

    def test_client_install_preserves_settings_and_repeated_install(self):
        self.config.parent.mkdir(parents=True)
        previous = '[keys]\nprefix = "ctrl+a"\n[theme]\nname = "terminal"\n'
        self.config.write_text(previous)
        self.invoke("--client")
        first = self.config.read_bytes()
        self.invoke("--client")
        self.assertEqual(self.config.read_bytes(), first)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual((self.backups()[0] / "config.toml").read_text(), previous)
        self.assertEqual(tomllib.loads(first.decode())["keys"]["prefix"], "ctrl+a")

    def test_dry_run_does_not_create_config_font_or_backup(self):
        result = self.invoke("--client", "--dry-run")
        self.assertIn("$si_ctx_yellow", result.stdout)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.data.exists())
        self.assertFalse(self.state.exists())

    def test_config_path_with_spaces(self):
        path = self.root / "other config.toml"
        self.invoke("--config", str(path))
        self.assertTrue(path.exists())
        self.assertFalse(self.config.exists())

    def test_client_rollback_restores_previous_file(self):
        self.config.parent.mkdir(parents=True)
        previous = '[theme]\nname = "terminal"\n'
        self.config.write_text(previous)
        self.invoke()
        self.invoke("--rollback", str(self.backups()[0]))
        self.assertEqual(self.config.read_text(), previous)

    def test_client_rollback_removes_only_newly_created_config(self):
        self.invoke()
        self.invoke("--rollback", str(self.backups()[0]))
        self.assertFalse(self.config.exists())
        self.assertTrue((self.data / "fonts/HerdrAgentIconsMax-Regular.ttf").exists())

    def test_rollback_refuses_user_edits_since_install(self):
        self.invoke()
        edited = self.config.read_text() + '\n[theme]\nname = "terminal"\n'
        self.config.write_text(edited)
        result = self.invoke("--rollback", str(self.backups()[0]), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_text(), edited)

    def test_server_rollback_refuses_client_backup_before_contacting_herdr(self):
        self.invoke()
        installed = self.config.read_text()
        result = self.invoke("--server", "--rollback", str(self.backups()[0]), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("backup mode differs", result.stderr)
        self.assertEqual(self.config.read_text(), installed)

    def test_server_install_requires_existing_config_before_contacting_herdr(self):
        result = self.invoke("--server", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("server config does not exist", result.stderr)
        self.assertFalse(self.state.exists())

    def test_unknown_sidebar_fails_without_touching_it(self):
        self.config.parent.mkdir(parents=True)
        before = '[ui.sidebar.agents]\nrows = [["agent"]]\n'
        self.config.write_text(before)
        result = self.invoke(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_text(), before)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.data.exists())

    def test_invalid_cli_arguments_do_not_write(self):
        for arguments in [("--config",), ("--client", "--server"), ("--surprise",),
                          ("--dry-run", "--rollback", "/nonexistent"),
                          ("--display-mode", "quota"), ("--server", "--display-mode"),
                          ("--server", "--display-mode", "invalid"),
                          ("--server", "--display-mode", "quota", "--rollback", "/nonexistent")]:
            self.assertEqual(self.invoke(*arguments, check=False).returncode, 2)
        self.assertFalse(self.config.exists())

    def test_server_dry_run_previews_display_mode_without_writes(self):
        plugin = self.config.parent / "plugins/config" / install.PLUGIN / "config.json"
        plugin.parent.mkdir(parents=True)
        previous = '{"mode":"auto","icons":"text"}\n'
        plugin.write_text(previous)
        for arguments, mode in (((), "quota"), (("--display-mode", "estimated"), "estimated")):
            result = self.invoke("--server", "--dry-run", *arguments)
            self.assertIn('"mode": "' + mode + '"', result.stdout)
            self.assertIn(str(plugin), result.stdout)
            self.assertEqual(plugin.read_text(), previous)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.data.exists())
        self.assertFalse(self.state.exists())

    def test_radar_replacement_requires_flag_and_preserves_other_settings(self):
        self.config.parent.mkdir(parents=True)
        before = '[keys]\nprefix = "ctrl+a"\n' + install.RADAR_START + '\n[ui.sidebar.agents]\nrows = [["agent"]]\n' + install.RADAR_END + '\n'
        self.config.write_text(before)
        self.assertNotEqual(self.invoke(check=False).returncode, 0)
        self.invoke("--replace-radar")
        self.assertEqual(tomllib.loads(self.config.read_text())["keys"]["prefix"], "ctrl+a")
        self.assertNotIn(install.RADAR_START, self.config.read_text())


class ServerInstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="indicator server test ")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "config.toml"
        self.before = '[theme]\nname = "terminal"\n'
        self.config.write_text(self.before)
        self.after = install.candidate(self.before)
        self.plugin = self.root / "plugins/config" / install.PLUGIN / "config.json"
        self.plugin.parent.mkdir(parents=True)
        self.previous = '{"mode":"auto","icons":"text","modes":{"kimi":"estimated"},"quota_max_age":300}\n'
        self.plugin.write_text(self.previous)
        for mock in (patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
                                            "HERDR_SOCKET_PATH": "/test.sock"}),
                     patch("indicator.call"), patch.object(install, "install_font"),
                     patch("sys.stdout", new_callable=io.StringIO)):
            mock.start()
            self.addCleanup(mock.stop)
        runner = patch.object(install, "run", return_value="")
        self.run = runner.start()
        self.addCleanup(runner.stop)

    def apply(self, **kwargs):
        install.apply(self.config, self.before, self.after, False, **kwargs)
        return max((self.root / "state/herdr-status-indicator/backups").iterdir())

    def test_upgrade_sets_quota_preserves_other_settings_and_can_roll_back(self):
        backup = self.apply()
        expected = json.loads(self.previous)
        expected["mode"] = "quota"
        self.assertEqual(json.loads(self.plugin.read_text()), expected)
        self.assertEqual((backup / "plugin-config.json").read_text(), self.previous)
        install.rollback(backup)
        self.assertEqual(self.plugin.read_text(), self.previous)
        self.assertEqual(self.config.read_text(), self.before)

    def test_fresh_install_defaults_to_quota_and_rollback_removes_new_config(self):
        self.plugin.unlink()
        backup = self.apply()
        self.assertEqual(json.loads(self.plugin.read_text())["mode"], "quota")
        install.rollback(backup)
        self.assertFalse(self.plugin.exists())

    def test_installed_plugin_is_configured_without_linking(self):
        install.rollback(self.apply())
        self.assertIn(("herdr", "plugin", "link", str(install.ROOT)), [c.args for c in self.run.call_args_list])
        self.run.reset_mock()
        self.apply(link=False)
        commands = [c.args for c in self.run.call_args_list]
        self.assertNotIn("link", [c[2] for c in commands if c[:2] == ("herdr", "plugin")])
        self.assertIn(("herdr", "plugin", "enable", install.PLUGIN), commands)
        self.assertIn(("herdr", "plugin", "action", "invoke", "start", "--plugin", install.PLUGIN), commands)

    def test_explicit_modes_override_installer_default(self):
        for mode in ("auto", "estimated", "billed"):
            backup = self.apply(display_mode=mode)
            self.assertEqual(json.loads(self.plugin.read_text())["mode"], mode)
            install.rollback(backup)

    def test_rollback_refuses_plugin_edits_before_stopping_watcher(self):
        backup = self.apply()
        edited = '{"mode":"billed"}\n'
        self.plugin.write_text(edited)
        self.run.reset_mock()
        with self.assertRaisesRegex(ValueError, "plugin config changed after install"):
            install.rollback(backup)
        self.run.assert_not_called()
        self.assertEqual(self.plugin.read_text(), edited)
        self.assertEqual(self.config.read_text(), self.after)

    def test_failure_restores_plugin_config_unless_concurrently_edited(self):
        for existed, edited in ((True, False), (False, False), (True, True)):
            with self.subTest(existed=existed, edited=edited):
                self.config.write_text(self.before)
                if existed:
                    self.plugin.write_text(self.previous)
                else:
                    self.plugin.unlink(missing_ok=True)
                failed = False
                def fail_reload(*args):
                    nonlocal failed
                    if args == ("herdr", "server", "reload-config") and not failed:
                        failed = True
                        if edited:
                            self.plugin.write_text('{"mode":"billed"}\n')
                        raise subprocess.CalledProcessError(1, args)
                    return ""
                self.run.side_effect = fail_reload
                with self.assertRaises(subprocess.CalledProcessError):
                    self.apply()
                self.assertEqual(self.config.read_text(), self.before)
                if edited:
                    self.assertEqual(self.plugin.read_text(), '{"mode":"billed"}\n')
                elif existed:
                    self.assertEqual(self.plugin.read_text(), self.previous)
                else:
                    self.assertFalse(self.plugin.exists())

    def test_install_preserves_concurrent_plugin_edits(self):
        edited = '{"mode":"billed"}\n'
        def edit_config(*args):
            if args == (sys.executable, str(install.ROOT / "indicator.py"), "stop"):
                self.plugin.write_text(edited)
            return ""
        self.run.side_effect = edit_config
        with self.assertRaisesRegex(ValueError, "plugin config changed during installation"):
            self.apply()
        self.assertEqual(self.plugin.read_text(), edited)
        self.assertEqual(self.config.read_text(), self.before)

    def test_invalid_plugin_config_is_rejected_before_contacting_herdr(self):
        self.plugin.write_text('[]\n')
        with self.assertRaisesRegex(ValueError, "JSON object"):
            self.apply()
        self.run.assert_not_called()
        self.assertEqual(self.config.read_text(), self.before)

    def test_older_server_backups_still_restore_sidebar(self):
        backup = self.apply()
        info = json.loads((backup / "restore.json").read_text())
        info = {key: value for key, value in info.items() if not key.startswith("plugin_config_")}
        (backup / "restore.json").write_text(json.dumps(info))
        installed_plugin = self.plugin.read_text()
        install.rollback(backup)
        self.assertEqual(self.config.read_text(), self.before)
        self.assertEqual(self.plugin.read_text(), installed_plugin)


class PlatformPathTests(unittest.TestCase):
    def test_mac_font_path(self):
        with patch.object(install.sys, "platform", "darwin"), patch.object(install.Path, "home", return_value=Path("/fake/user")):
            self.assertEqual(install.font_directory(), Path("/fake/user/Library/Fonts"))

    def test_windows_config_and_font_paths(self):
        with patch.object(install.sys, "platform", "win32"), patch.dict(os.environ, {"APPDATA": "/fake/roaming", "LOCALAPPDATA": "/fake/local"}, clear=True):
            self.assertEqual(install.config_path(), Path("/fake/roaming/herdr/config.toml"))
            self.assertEqual(install.font_directory(), Path("/fake/local/Microsoft/Windows/Fonts"))


if __name__ == "__main__":
    unittest.main()
