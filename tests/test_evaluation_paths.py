"""Offline evaluation: fake credentials, HTTP responses and Windows registry only."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_nexus import nexus


COMMAND = r'Software\Classes\nxm\shell\open\command'


class FakeKey:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeRegistry:
    HKEY_CURRENT_USER = object()
    REG_SZ = 1

    def __init__(self, previous=None):
        self.values = {} if previous is None else {COMMAND: {'': previous}}

    def OpenKey(self, root, path):
        assert root is self.HKEY_CURRENT_USER
        if path not in self.values:
            raise FileNotFoundError(path)
        return FakeKey(path)

    def CreateKey(self, root, path):
        assert root is self.HKEY_CURRENT_USER
        self.values.setdefault(path, {})
        return FakeKey(path)

    def QueryValueEx(self, key, name):
        return self.values[key.path][name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        assert kind == self.REG_SZ
        self.values[key.path][name] = value

    def DeleteKey(self, root, path):
        assert root is self.HKEY_CURRENT_USER
        if path not in self.values:
            raise FileNotFoundError(path)
        del self.values[path]


class EvaluationPathTests(unittest.TestCase):
    def setUp(self):
        nexus._RATE_STATE.clear()

    def test_key_lookup_precedence_with_fake_values_only(self):
        with TemporaryDirectory() as tmp, patch.object(nexus, 'config_dir', return_value=Path(tmp)), patch.dict(os.environ, {'NEXUS_API_KEY': ' TEST-ENV '}, clear=True):
            (Path(tmp) / 'apikey').write_text('TEST-FILE\n')
            self.assertEqual(nexus.api_key(), 'TEST-ENV')
            del os.environ['NEXUS_API_KEY']
            self.assertEqual(nexus.api_key(), 'TEST-FILE')
            (Path(tmp) / 'apikey').unlink()
            self.assertEqual(nexus.api_key(required=False), '')
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                nexus.api_key()

    def test_whoami_does_not_print_mock_key_in_response(self):
        result = {'name': 'Tester', 'user_id': 42, 'is_premium': True, 'key': 'TEST-SECRET'}
        with patch.object(nexus, 'v1', return_value=(result, {'X-RL-Daily-Remaining': '10'})) as rest, contextlib.redirect_stdout(io.StringIO()) as output:
            nexus.cmd_whoami(SimpleNamespace())
        rest.assert_called_once_with('/users/validate.json')
        self.assertIn('premium: True', output.getvalue())
        self.assertIn('X-RL-Daily-Remaining=10', output.getvalue())
        self.assertNotIn('TEST-SECRET', output.getvalue())

    def test_updated_filters_user_mods(self):
        rows = [{'mod_id': i, 'latest_file_update': 0, 'latest_mod_activity': 0} for i in (111, 222)]
        with patch.object(nexus, 'resolve_game', return_value=(1, 'game', 'Game')), patch.object(nexus, 'v1', return_value=(rows, {})) as rest, contextlib.redirect_stdout(io.StringIO()) as output:
            nexus.cmd_updated(SimpleNamespace(game='game', period='1w', mod_ids=['222']))
        rest.assert_called_once_with('/games/game/mods/updated.json', {'period': '1w'})
        self.assertIn('222', output.getvalue())
        self.assertNotIn('111', output.getvalue())

    def test_md5_uses_local_digest_with_mock_rest(self):
        with TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'test.zip'
            archive.write_bytes(b'offline-test')
            with patch.object(nexus, 'resolve_game', return_value=(1, 'game', 'Game')), patch.object(nexus, 'v1', return_value=([], {})) as rest, contextlib.redirect_stdout(io.StringIO()):
                nexus.cmd_md5(SimpleNamespace(game='game', file=str(archive)))
        digest = hashlib.md5(b'offline-test').hexdigest()
        rest.assert_called_once_with('/games/game/mods/md5_search/' + digest + '.json')

    def test_empty_mirror_response_never_calls_cdn(self):
        with patch.object(nexus, 'v1', return_value=([], {})), patch.object(nexus.urllib.request, 'urlopen') as cdn, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            nexus.do_download('game', 1, 2)
        cdn.assert_not_called()

    def test_incomplete_download_preserves_existing_file_and_removes_partial(self):
        payload = io.BytesIO(b'short')
        payload.headers = {'Content-Length': '10'}
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / 'mod.zip'
            target.write_bytes(b'existing-file')
            with patch.object(nexus, 'config_dir', return_value=Path(tmp)), patch.object(nexus, 'v1', return_value=([{'URI': 'https://example.test/mod.zip'}], {})), patch.object(nexus.urllib.request, 'urlopen', return_value=payload), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                nexus.do_download('game', 1, 2, out=tmp)
            self.assertEqual(target.read_bytes(), b'existing-file')
            self.assertFalse((Path(tmp) / 'mod.zip.part').exists())
            self.assertFalse((Path(tmp) / 'downloads.log').exists())

    def test_handler_dispatch_does_not_print_nxm_token(self):
        args = SimpleNamespace(url='nxm://game/mods/1/files/2?key=TEST-SECRET&expires=42', out='test-output')
        with patch.object(nexus, 'do_download') as download, patch.object(nexus.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as output:
            nexus.cmd_handle_nxm(args)
        download.assert_called_once_with('game', 1, 2, 'TEST-SECRET', '42', 'test-output')
        self.assertNotIn('TEST-SECRET', output.getvalue())

    def test_handler_error_waits_for_acknowledgment_without_downloading(self):
        with patch.object(nexus, 'do_download') as download, patch('builtins.input', return_value='') as prompt, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            nexus.cmd_handle_nxm(SimpleNamespace(url='nxm://game/collections/1?key=TEST', out=None))
        download.assert_not_called()
        prompt.assert_called_once()

    def test_mock_windows_registration_backup_repeated_registration_and_restore(self):
        previous = '"C:\\Program Files\\Vortex\\Vortex.exe" "%1"'
        registry = FakeRegistry(previous)
        with TemporaryDirectory() as tmp, patch.object(nexus, '_reg', return_value=registry), patch.object(nexus, 'config_dir', return_value=Path(tmp)), patch.object(nexus.sys, 'executable', 'C:\\Program Files\\Python\\python.exe'), contextlib.redirect_stdout(io.StringIO()):
            nexus.cmd_register_nxm(SimpleNamespace())
            backup = Path(tmp) / 'nxm_handler_backup.json'
            self.assertEqual(json.loads(backup.read_text())['command'], previous)
            command = registry.values[COMMAND]['']
            self.assertTrue(command.startswith('"C:\\Program Files\\Python\\python.exe" "'))
            self.assertTrue(command.endswith('" handle-nxm "%1"'))
            self.assertEqual(registry.values[r'Software\Classes\nxm']['URL Protocol'], '')
            nexus.cmd_register_nxm(SimpleNamespace())
            self.assertEqual(json.loads(backup.read_text())['command'], previous)
            nexus.cmd_unregister_nxm(SimpleNamespace())
            self.assertEqual(registry.values[COMMAND][''], previous)
            self.assertFalse(backup.exists())

    def test_mock_windows_registration_without_previous_handler_then_cleanup(self):
        registry = FakeRegistry()
        with TemporaryDirectory() as tmp, patch.object(nexus, '_reg', return_value=registry), patch.object(nexus, 'config_dir', return_value=Path(tmp)), contextlib.redirect_stdout(io.StringIO()):
            nexus.cmd_register_nxm(SimpleNamespace())
            self.assertFalse((Path(tmp) / 'nxm_handler_backup.json').exists())
            nexus.cmd_unregister_nxm(SimpleNamespace())
            self.assertEqual(registry.values, {})

    @unittest.skipUnless(os.name != 'nt', 'Linux-only rejection check')
    def test_linux_rejects_real_windows_registry_commands(self):
        with contextlib.redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit):
            nexus._reg()
        self.assertIn('Windows-only', output.getvalue())


if __name__ == '__main__':
    unittest.main()
