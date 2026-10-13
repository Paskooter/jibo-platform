"""A delivery-hint migration may change only its exact saved prompt section."""
import hashlib
import importlib.util
import pathlib
import sqlite3
import tempfile
import unittest
import os
import subprocess
import sys

spec = importlib.util.spec_from_file_location('refresh_platform_hint', pathlib.Path(__file__).resolve().parents[1] / 'tools/refresh_platform_hint.py')
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


class PromptRefreshTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(self.conn.close)
        self.conn.executescript('CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, system_prompt_hash TEXT, system_prompt TEXT);'
            'CREATE TABLE system_prompts(hash TEXT PRIMARY KEY, prompt TEXT);'
            'CREATE TABLE messages(session_id TEXT, content TEXT);')
        self.old = 'Identity and preferences\nOLD DELIVERY HINT\nOther instructions'
        self.old_hash = hashlib.sha256(self.old.encode()).hexdigest()
        self.conn.execute('INSERT INTO system_prompts VALUES (?,?)', (self.old_hash, self.old))
        self.conn.executemany('INSERT INTO sessions VALUES (?,?,?,NULL)', [('jibo', 'jibo', self.old_hash), ('other', 'discord', self.old_hash)])
        self.conn.execute('INSERT INTO messages VALUES (?,?)', ('jibo', 'Original conversation'))

    def test_preview_is_read_only_and_apply_retains_history_and_other_prompts(self):
        report = refresh.refresh(self.conn, 'jibo', 'OLD DELIVERY HINT', 'NEW DELIVERY HINT')
        self.assertTrue(report['changed'])
        self.assertFalse(report['applied'])
        self.assertEqual(refresh.prepare(self.conn, 'jibo', 'OLD DELIVERY HINT', 'NEW DELIVERY HINT')[1], self.old)
        with tempfile.TemporaryDirectory() as directory:
            backup = pathlib.Path(directory) / 'before.json'
            applied = refresh.refresh(self.conn, 'jibo', 'OLD DELIVERY HINT', 'NEW DELIVERY HINT', backup=backup)
            self.assertTrue(applied['applied'])
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        updated = refresh.prepare(self.conn, 'jibo', 'OLD DELIVERY HINT', 'NEW DELIVERY HINT')[1]
        self.assertEqual(updated, self.old.replace('OLD DELIVERY HINT', 'NEW DELIVERY HINT'))
        self.assertEqual(self.conn.execute('SELECT system_prompt_hash FROM sessions WHERE id="other"').fetchone()[0], self.old_hash)
        self.assertEqual(self.conn.execute('SELECT content FROM messages').fetchall(), [('Original conversation',)])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM system_prompts').fetchone()[0], 2)

    def test_unknown_prompt_and_other_platform_are_rejected(self):
        for session, old in [('other', 'OLD DELIVERY HINT'), ('jibo', 'MISSING')]:
            with self.assertRaises(ValueError):
                refresh.refresh(self.conn, session, old, 'NEW DELIVERY HINT')

    def test_failed_backup_cannot_change_the_session(self):
        with tempfile.TemporaryDirectory() as directory:
            backup = pathlib.Path(directory) / 'before.json'
            backup.write_text('existing backup')
            with self.assertRaises(FileExistsError):
                refresh.refresh(self.conn, 'jibo', 'OLD DELIVERY HINT', 'NEW DELIVERY HINT', backup=backup)
            self.assertEqual(backup.read_text(), 'existing backup')
        self.assertEqual(self.conn.execute('SELECT system_prompt_hash FROM sessions WHERE id="jibo"').fetchone()[0], self.old_hash)

    def test_cli_preview_uses_the_selected_profile_home(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            profile = base / 'profile'
            profile.mkdir()
            conn = sqlite3.connect(profile / 'state.db')
            self.conn.backup(conn)
            conn.close()
            previous, current = base / 'previous.py', base / 'current.py'
            previous.write_text('ctx.register_platform(name="jibo", platform_hint="OLD DELIVERY HINT")')
            current.write_text('ctx.register_platform(name="jibo", platform_hint="NEW DELIVERY HINT")')
            env = {'PATH': os.environ['PATH'], 'HOME': str(base / 'empty-home'),
                   'HERMES_HOME': str(profile)}
            result = subprocess.run([sys.executable, refresh.__file__, '--session', 'jibo',
                                     '--previous-plugin', str(previous), '--current-plugin', str(current)],
                                    env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn('"applied": false', result.stdout)
            self.assertEqual(self.conn.execute('SELECT prompt FROM system_prompts').fetchone()[0], self.old)

    def test_hint_is_parsed_without_executing_the_plugin(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = pathlib.Path(directory) / '__init__.py'
            plugin.write_text('raise Exception("must not execute")\nctx.register_platform(name="jibo", platform_hint=("a" "b"))')
            self.assertEqual(refresh.platform_hint(plugin), 'ab')
