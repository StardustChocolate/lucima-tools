"""Offline regression checks for missing runtimes and visible startup errors."""
from pathlib import Path
import os
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from desktop import bootstrap


class WindowsStartupTests(unittest.TestCase):
    def check_windows(self, *, build=19045, net=True, webview=True):
        return (
            patch.object(bootstrap.sys, 'platform', 'win32'),
            patch.object(bootstrap.sys, 'getwindowsversion', return_value=SimpleNamespace(build=build), create=True),
            patch.object(bootstrap, '_net_framework_available', return_value=net),
            patch.object(bootstrap, '_webview2_available', return_value=webview),
        )

    def test_windows10_and_windows11_pass_with_runtimes(self):
        for build in (17763, 19045, 26100):
            with self.subTest(build=build):
                a, b, c, d = self.check_windows(build=build)
                with a, b, c, d:
                    bootstrap.check_environment()

    def test_old_windows_and_missing_components_have_specific_repair_messages(self):
        cases = [({'build':7601}, 'Windows 10'), ({'net':False}, '.NET Framework'),
                 ({'webview':False}, '离线补齐')]
        for options, message in cases:
            a, b, c, d = self.check_windows(**options)
            with a, b, c, d, self.assertRaisesRegex(RuntimeError, message):
                bootstrap.check_environment()

    def test_missing_or_broken_loader_is_treated_as_missing_webview(self):
        with patch.object(bootstrap.ctypes, 'WinDLL', side_effect=OSError('missing DLL'), create=True):
            self.assertFalse(bootstrap._webview2_available())

    def test_installer_runtime_probe_returns_status_without_launching_application(self):
        with patch.object(bootstrap.sys, 'argv', ['bootstrap', '--check-webview2']), \
             patch.object(bootstrap, '_webview2_available', return_value=False), \
             patch.object(bootstrap, '_launch') as launch:
            self.assertEqual(bootstrap.main(), 1)
            launch.assert_not_called()

    def test_launch_failure_is_logged_and_shown_in_a_native_dialog(self):
        messagebox = Mock()
        with TemporaryDirectory() as directory, \
             patch.dict(os.environ, {'ARK_DATA_DIR':directory}), \
             patch.object(bootstrap.sys, 'argv', ['app']), \
             patch.object(bootstrap.sys, 'platform', 'win32'), \
             patch.object(bootstrap, 'check_environment'), \
             patch.object(bootstrap, '_launch', side_effect=ImportError('缺少组件 regression')), \
             patch.object(bootstrap.ctypes, 'windll', SimpleNamespace(user32=SimpleNamespace(MessageBoxW=messagebox)), create=True):
            self.assertEqual(bootstrap.main(), 1)
            self.assertIn('缺少组件 regression', (Path(directory)/'logs/startup.log').read_text())
            self.assertIn('缺少组件 regression', messagebox.call_args.args[1])
            self.assertIn(str(Path(directory)/'logs/startup.log'), messagebox.call_args.args[1])

    def test_system_pythonnet_choice_does_not_override_private_runtime(self):
        launch = Mock()
        with patch.dict(os.environ, {'PYTHONNET_RUNTIME':'coreclr', 'PYTHONNET_NETFX_RUNTIME_CONFIG':'bad.json'}), \
             patch.object(bootstrap.sys, 'platform', 'win32'), \
             patch.dict(bootstrap.sys.modules, {'desktop.run':SimpleNamespace(main=launch)}):
            bootstrap._launch()
            self.assertEqual(os.environ['PYTHONNET_RUNTIME'], 'netfx')
            self.assertNotIn('PYTHONNET_NETFX_RUNTIME_CONFIG', os.environ)
            launch.assert_called_once()


if __name__ == '__main__':
    unittest.main()
