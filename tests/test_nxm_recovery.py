"""Registry ownership/recovery tests; never imports or calls real winreg."""
import contextlib
import copy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_nexus import nexus
from test_evaluation_paths import FakeRegistry, COMMAND


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(TemporaryDirectory()))
        self.reg = FakeRegistry('"previous.exe" "%1"')
        self.stack.enter_context(patch.object(nexus, '_reg', return_value=self.reg))
        self.stack.enter_context(patch.object(nexus, 'config_dir', return_value=self.tmp))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.backup = self.tmp / 'nxm_handler_backup.json'
        self.args = SimpleNamespace()

    def test_restore_original_metadata_types_binary_and_unrelated_subkeys(self):
        self.reg.values[nexus.NXM_ROOT] = {'': b'\x00\xff', 'URL Protocol': 'original', 'Other': 'keep'}
        self.reg.types[nexus.NXM_ROOT, ''] = 3
        self.reg.types[nexus.NXM_ROOT, 'URL Protocol'] = 2
        self.reg.values[nexus.NXM_ROOT + r'\DefaultIcon'] = {'': 'old.ico'}
        original = copy.deepcopy(self.reg.values)
        nexus.cmd_register_nxm(self.args)
        nexus.cmd_unregister_nxm(self.args)
        self.assertEqual(self.reg.values, original)
        self.assertEqual(self.reg.types[nexus.NXM_ROOT, ''], 3)
        self.assertEqual(self.reg.types[nexus.NXM_ROOT, 'URL Protocol'], 2)

    def test_owner_change_blocks_register_and_unregister_keeps_backup(self):
        nexus.cmd_register_nxm(self.args)
        original_backup = self.backup.read_bytes()
        self.reg.values[COMMAND][''] = 'another-app handle-nxm "%1"'
        changed = copy.deepcopy(self.reg.values)
        for action in (nexus.cmd_register_nxm, nexus.cmd_unregister_nxm):
            with self.assertRaises(SystemExit):
                action(self.args)
            self.assertEqual(self.reg.values, changed)
            self.assertEqual(self.backup.read_bytes(), original_backup)

    def test_changed_managed_metadata_blocks_restore(self):
        nexus.cmd_register_nxm(self.args)
        self.reg.values[nexus.NXM_ROOT]['URL Protocol'] = 'changed-by-other-app'
        changed = copy.deepcopy(self.reg.values)
        with self.assertRaises(SystemExit):
            nexus.cmd_unregister_nxm(self.args)
        self.assertEqual(self.reg.values, changed)
        self.assertTrue(self.backup.exists())

    def test_unrelated_added_metadata_survives_unregister(self):
        self.reg.values.clear()
        nexus.cmd_register_nxm(self.args)
        self.reg.values[COMMAND]['foreign'] = 'keep'
        nexus.cmd_unregister_nxm(self.args)
        self.assertEqual(self.reg.values[COMMAND], {'foreign': 'keep'})

    def test_legacy_and_corrupt_backup_fail_without_registry_changes(self):
        before = copy.deepcopy(self.reg.values)
        for content in ('{"command":"old.exe"}', '{', '[]', '{"schema":2}'):
            self.backup.write_text(content)
            for action in (nexus.cmd_register_nxm, nexus.cmd_unregister_nxm):
                with self.assertRaises(SystemExit):
                    action(self.args)
                self.assertEqual(self.reg.values, before)
                self.assertEqual(self.backup.read_text(), content)

    def test_missing_backup_never_deletes_foreign_handler(self):
        before = copy.deepcopy(self.reg.values)
        with self.assertRaises(SystemExit):
            nexus.cmd_unregister_nxm(self.args)
        self.assertEqual(self.reg.values, before)

    def test_second_unregister_is_safe(self):
        before = copy.deepcopy(self.reg.values)
        nexus.cmd_register_nxm(self.args)
        nexus.cmd_unregister_nxm(self.args)
        with self.assertRaises(SystemExit):
            nexus.cmd_unregister_nxm(self.args)
        self.assertEqual(self.reg.values, before)

    def test_backup_write_failure_does_not_touch_registry(self):
        before = copy.deepcopy(self.reg.values)
        with patch.object(Path, 'open', side_effect=PermissionError('mock')), self.assertRaises(SystemExit):
            nexus.cmd_register_nxm(self.args)
        self.assertEqual(self.reg.values, before)

    def test_registry_read_denial_does_not_mean_absent_handler(self):
        before = copy.deepcopy(self.reg.values)
        with patch.object(self.reg, 'OpenKey', side_effect=PermissionError('mock')), self.assertRaises(SystemExit):
            nexus.cmd_register_nxm(self.args)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.reg.values, before)

    def test_failed_install_rolls_back_and_removes_new_empty_keys(self):
        self.reg.values.clear()
        original_write = self.reg.SetValueEx
        def fail_command(key, name, reserved, kind, value):
            if key.path == COMMAND:
                raise PermissionError('mock')
            return original_write(key, name, reserved, kind, value)
        with patch.object(self.reg, 'SetValueEx', side_effect=fail_command), self.assertRaises(SystemExit):
            nexus.cmd_register_nxm(self.args)
        self.assertEqual(self.reg.values, {})
        self.assertFalse(self.backup.exists())

    def test_failed_restore_rolls_back_keeps_backup_and_can_retry(self):
        nexus.cmd_register_nxm(self.args)
        installed = copy.deepcopy(self.reg.values)
        original_write = self.reg.SetValueEx
        def fail_previous(key, name, reserved, kind, value):
            if key.path == COMMAND and value == '"previous.exe" "%1"':
                raise PermissionError('mock')
            return original_write(key, name, reserved, kind, value)
        with patch.object(self.reg, 'SetValueEx', side_effect=fail_previous), self.assertRaises(SystemExit):
            nexus.cmd_unregister_nxm(self.args)
        self.assertTrue(self.backup.exists())
        self.assertEqual(self.reg.values, installed)
        nexus.cmd_unregister_nxm(self.args)
        self.assertEqual(self.reg.values[COMMAND][''], '"previous.exe" "%1"')

    def test_backup_cleanup_failure_allows_safe_retry(self):
        original = copy.deepcopy(self.reg.values)
        nexus.cmd_register_nxm(self.args)
        with patch.object(Path, 'unlink', side_effect=PermissionError('mock')), self.assertRaises(SystemExit):
            nexus.cmd_unregister_nxm(self.args)
        self.assertEqual(self.reg.values, original)
        self.assertTrue(self.backup.exists())
        nexus.cmd_unregister_nxm(self.args)
        self.assertEqual(self.reg.values, original)
        self.assertFalse(self.backup.exists())

    def test_untrusted_backup_key_path_is_rejected(self):
        nexus.cmd_register_nxm(self.args)
        record = json.loads(self.backup.read_text())
        record['created'] = [r'Software\OtherApp']
        self.backup.write_text(json.dumps(record))
        original = copy.deepcopy(self.reg.values)
        with self.assertRaises(SystemExit):
            nexus.cmd_unregister_nxm(self.args)
        self.assertEqual(self.reg.values, original)

    def test_corrupt_backup_value_type_is_rejected_before_restore(self):
        nexus.cmd_register_nxm(self.args)
        record = json.loads(self.backup.read_text())
        record['before'][0] = {'type': 1, 'binary': False, 'value': 123}
        self.backup.write_text(json.dumps(record))
        original = copy.deepcopy(self.reg.values)
        with self.assertRaises(SystemExit):
            nexus.cmd_unregister_nxm(self.args)
        self.assertEqual(self.reg.values, original)

    def test_owner_changes_during_install_are_not_rolled_over(self):
        original_write = self.reg.SetValueEx
        def change_owner(key, name, reserved, kind, value):
            original_write(key, name, reserved, kind, value)
            if key.path == nexus.NXM_ROOT and name == '':
                self.reg.values[COMMAND][''] = 'new-owner.exe'
        with patch.object(self.reg, 'SetValueEx', side_effect=change_owner), self.assertRaises(SystemExit):
            nexus.cmd_register_nxm(self.args)
        self.assertEqual(self.reg.values[COMMAND][''], 'new-owner.exe')
        self.assertTrue(self.backup.exists())
