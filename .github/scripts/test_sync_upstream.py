"""Offline Git integration tests. All repositories/refs live in temporary directories."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('sync', Path(__file__).with_name('sync_upstream.py'))
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(os.chdir, self.previous)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Fixture upstream')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.write('app.js', 'original\n')
        self.write('delete.js', 'delete me\n')
        self.write('vercel.json', '{"version":2}\n')
        self.write('package.json', '{"type":"module","engines":{"node":"24"}}\n')
        self.write('.github/workflows/untrusted.yml', 'never import\n')
        self.write('.pr_agent.toml', 'upstream config\n')
        self.baseline = self.commit('upstream baseline')
        self.git('branch', 'upstream')
        self.git('rm', '-qr', '.github', '.pr_agent.toml')
        self.write('.github/workflows/sync-upstream.yml', 'owner workflow\n')
        self.write(sync.STATE, json.dumps(dict(upstream_repository=sync.UPSTREAM,
                                              upstream_branch='main', upstream_sha=self.baseline)))
        self.head = self.commit('owner controls')
        self.git('update-ref', 'refs/remotes/origin/main', self.head)

    def git(self, *args):
        return subprocess.check_output(['git', *args], stderr=subprocess.PIPE).decode().strip()

    def write(self, path, text):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text)

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    def upstream_change(self, mutate):
        self.git('checkout', '-q', 'upstream')
        mutate()
        tip = self.commit('upstream update')
        self.git('checkout', '-q', 'main')
        return tip

    def test_noop_idempotent_no_commit_no_worktree_change(self):
        first = sync.plan('upstream')
        self.assertEqual(first, sync.plan('upstream'))
        self.assertEqual(first['mode'], 'noop')
        self.assertEqual(sync.finalize(first), '')
        self.assertEqual(first['tree'], self.git('rev-parse', 'HEAD^{tree}'))
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_change_delete_modes_controls_and_owner_attribution(self):
        def mutate():
            self.write('app.js', 'new upstream\n')
            Path('delete.js').unlink()
            self.write('added.js', 'new file\n')
            self.write('.github/workflows/evil.yml', 'do not import\n')
            self.write('.pr_agent.toml', 'changed upstream control\n')
            Path('app.js').chmod(0o755)
        tip = self.upstream_change(mutate)
        saved = sync.plan(tip)
        self.assertTrue(saved['source_changed'])
        commit = sync.finalize(saved)
        self.assertEqual(self.git('show', commit + ':app.js'), 'new upstream')
        self.assertNotIn('delete.js', sync.entries(commit))
        self.assertIn('added.js', sync.entries(commit))
        self.assertEqual(sync.entries(commit)['app.js'][0], '100755')
        for path in sync.entries(commit):
            if sync.protected(path) and path != sync.STATE:
                self.assertEqual(sync.entries(commit)[path], sync.entries(self.head)[path])
        self.assertNotIn('.pr_agent.toml', sync.entries(commit))
        self.assertNotIn('.github/workflows/evil.yml', sync.entries(commit))
        self.assertEqual(sync.read_json(commit, sync.STATE)['upstream_sha'], tip)
        identity = self.git('show', '-s', '--format=%an|%ae|%cn|%ce|%P|%B', commit)
        self.assertIn('Jerrychenshen|37981711+Jerrychenshen@users.noreply.github.com|Jerrychenshen|37981711+Jerrychenshen@users.noreply.github.com', identity)
        self.assertIn(self.head, identity)
        self.assertIn(sync.UPSTREAM + '/commit/' + tip, identity)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.head)
        self.assertEqual(self.git('config', 'user.name'), 'Fixture upstream')
        # Adopt candidate to exercise the next run, without writing any external ref.
        self.git('read-tree', '--reset', '-u', commit)
        self.git('update-ref', 'refs/heads/main', commit)
        self.git('update-ref', 'refs/remotes/origin/main', commit)
        self.assertEqual(sync.plan(tip)['mode'], 'noop')

    def test_customization_is_rejected_even_without_upstream_update(self):
        self.write('app.js', 'owner custom edit\n')
        custom = self.commit('custom')
        self.git('update-ref', 'refs/remotes/origin/main', custom)
        with self.assertRaisesRegex(ValueError, 'Custom source'):
            sync.plan('upstream')

    def test_dirty_and_untracked_are_rejected(self):
        self.write('generated.lock', 'test output\n')
        with self.assertRaisesRegex(ValueError, 'clean'):
            sync.plan('upstream')

    def test_runtime_and_vercel_changes_are_rejected(self):
        tip = self.upstream_change(lambda: self.write('vercel.json', '{"version":3}'))
        with self.assertRaisesRegex(ValueError, 'manual review'):
            sync.plan(tip)

    def test_package_runtime_migration_is_rejected(self):
        tip = self.upstream_change(lambda: self.write('package.json', '{"engines":{"node":"26"}}'))
        with self.assertRaisesRegex(ValueError, 'runtime'):
            sync.plan(tip)

    def test_dependency_only_update_is_allowed(self):
        tip = self.upstream_change(lambda: self.write('package.json', json.dumps(
            {'type': 'module', 'engines': {'node': '24'}, 'dependencies': {'example': '^1.0.0'}})))
        self.assertTrue(sync.plan(tip)['source_changed'])

    def test_history_rewrite_is_rejected(self):
        unrelated = self.git('commit-tree', self.baseline + '^{tree}', '-m', 'unrelated root')
        with self.assertRaisesRegex(ValueError, 'merge-base'):
            sync.plan(unrelated)

    def test_concurrent_or_wrong_base_is_rejected(self):
        self.git('update-ref', 'refs/remotes/origin/main', self.baseline)
        with self.assertRaisesRegex(ValueError, 'concurrent or unsafe base'):
            sync.plan('upstream')

    def test_empty_verification_commit_is_explicit(self):
        saved = sync.plan('upstream', True)
        self.assertEqual(saved['mode'], 'verify')
        commit = sync.finalize(saved)
        self.assertEqual(self.git('rev-parse', commit + '^{tree}'), self.git('rev-parse', 'HEAD^{tree}'))
        self.assertIn('No application source changes', self.git('show', '-s', '--format=%B', commit))

    def test_controls_only_upstream_update_advances_state_without_npm(self):
        tip = self.upstream_change(lambda: self.write('.github/extra.yml', 'excluded'))
        saved = sync.plan(tip)
        self.assertEqual(saved['mode'], 'update')
        self.assertFalse(saved['source_changed'])
        self.assertEqual(sync.read_json(sync.finalize(saved), sync.STATE)['upstream_sha'], tip)

    def test_tampered_plan_is_rejected(self):
        saved = sync.plan('upstream', True)
        saved['tree'] = self.git('rev-parse', self.baseline + '^{tree}')
        with self.assertRaisesRegex(ValueError, 'tampered'):
            sync.finalize(saved)

    def test_context_restrictions(self):
        good = dict(GITHUB_REPOSITORY=sync.REPOSITORY, GITHUB_REF='refs/heads/main',
                    GITHUB_EVENT_NAME='workflow_dispatch')
        with patch.dict(os.environ, good):
            sync.check_context()
            for key, value in [('GITHUB_REPOSITORY', 'other/repo'),
                               ('GITHUB_REF', 'refs/heads/dev'), ('GITHUB_EVENT_NAME', 'push')]:
                with patch.dict(os.environ, {key: value}):
                    with self.assertRaisesRegex(ValueError, 'Only scheduled/manual'):
                        sync.check_context()


if __name__ == '__main__':
    unittest.main()
