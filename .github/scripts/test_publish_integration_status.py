#!/usr/bin/env python3
"""Tests for publish_integration_status.py and dashboard_config.py."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import publish_integration_status as pub
from dashboard_config import resolve_dashboard_entries, validate_dashboard_block

REPO_ROOT = str(Path(__file__).resolve().parents[2])

RUN_META_1 = {
    'run_id': 100,
    'run_url': 'https://github.com/x/y/actions/runs/100',
    'job_url': 'https://github.com/x/y/actions/runs/100/job/1',
    'branch': 'develop',
    'commit': 'abc12345',
    'completed_at': '2026-07-10T00:00:00Z',
}

RUN_META_2 = {**RUN_META_1, 'run_id': 101,
              'run_url': 'https://github.com/x/y/actions/runs/101',
              'completed_at': '2026-07-11T00:00:00Z'}

TILE = {
    'path': 'examples/api/slack',
    'type': 'api',
    'category': 'Messaging Integrations',
    'label': 'Slack (nightly)',
    'description': None,
}


class TestDashboardConfig(unittest.TestCase):
    def test_defaults_when_omitted(self):
        entries = resolve_dashboard_entries({'type': 'api', 'path': 'examples/api/multimodal/redis'})
        self.assertEqual(entries, [])

    def test_entry_without_category_is_omitted(self):
        entries = resolve_dashboard_entries({
            'type': 'api', 'path': 'examples/api/slack',
            'dashboard': {'label': 'Slack'},
        })
        self.assertEqual(entries, [])

    def test_hidden(self):
        self.assertEqual(resolve_dashboard_entries({'path': 'x', 'dashboard': 'hidden'}), [])

    def test_multi_mapping_fans_out(self):
        entries = resolve_dashboard_entries({
            'type': 'azure-serverless', 'path': 'examples/memory/cosmos',
            'dashboard': [
                {'category': 'Agent Memory / Knowledge', 'label': 'Cosmos DB memory'},
                {'category': 'Azure Serverless', 'label': 'OpenAI + Cosmos memory'},
            ],
        })
        self.assertEqual(len(entries), 2)

    def test_validate_rejects_bad_shapes(self):
        self.assertTrue(validate_dashboard_block({'path': 'p', 'dashboard': 42}))
        self.assertTrue(validate_dashboard_block({'path': 'p', 'dashboard': []}))
        self.assertTrue(validate_dashboard_block({'path': 'p', 'dashboard': {'category': ''}}))
        self.assertTrue(validate_dashboard_block({'path': 'p', 'dashboard': {'categry': 'typo'}}))
        self.assertTrue(validate_dashboard_block({
            'path': 'p', 'type': 'api',
            'dashboard': [{'category': 'A', 'label': 'x'}, {'category': 'A', 'label': 'y'}],
        }))

    def test_validate_accepts_valid_shapes(self):
        self.assertEqual(validate_dashboard_block({'path': 'p'}), [])
        self.assertEqual(validate_dashboard_block({'path': 'p', 'dashboard': 'hidden'}), [])
        self.assertEqual(validate_dashboard_block({
            'path': 'p',
            'dashboard': {'category': 'A', 'label': 'x', 'description': 'd', 'logo': 'x.svg'},
        }), [])

    def test_validate_rejects_bad_logo(self):
        self.assertTrue(validate_dashboard_block({
            'path': 'p', 'dashboard': {'category': 'A', 'label': 'x', 'logo': ''},
        }))

    def test_logo_passes_through(self):
        entries = resolve_dashboard_entries({
            'type': 'api', 'path': 'p',
            'dashboard': {'category': 'A', 'label': 'x', 'logo': 'custom.svg'},
        })
        self.assertEqual(entries[0]['logo'], 'custom.svg')


class TestResolveTiles(unittest.TestCase):
    def test_matrix_test_from_config(self):
        tiles = pub.resolve_tiles('integration-test', 'examples/api/slack', None, REPO_ROOT)
        self.assertEqual(len(tiles), 1)
        self.assertEqual(tiles[0]['category'], 'Messaging Integrations')
        self.assertEqual(tiles[0]['label'], 'Slack (nightly)')

    def test_multi_tile_test(self):
        tiles = pub.resolve_tiles('integration-test-weekly', 'examples/memory/cosmos', None, REPO_ROOT)
        self.assertEqual(len(tiles), 2)

    def test_deployment_base_resolves(self):
        tiles = pub.resolve_tiles('integration-test-weekly', 'examples/aws-serverless/openai', None, REPO_ROOT)
        self.assertEqual(tiles[0]['label'], 'OpenAI agent (base deployment)')

    def test_synthetic(self):
        tiles = pub.resolve_tiles('test', None, 'unit-tests', REPO_ROOT)
        self.assertEqual(tiles[0]['label'], 'ak-py unit tests')

    def test_unknown_path_exits(self):
        with self.assertRaises(SystemExit):
            pub.resolve_tiles('test', 'examples/does/not/exist', None, REPO_ROOT)


class TestFindOwnJob(unittest.TestCase):
    def job(self, name):
        return {'name': name, 'html_url': f'https://x/{name}'}

    def test_matrix_job_by_path(self):
        jobs = [self.job('run-tests (0, api, examples/api/slack, deploy)')]
        self.assertIsNotNone(pub.find_own_job(jobs, 'examples/api/slack', None, 'run-tests'))

    def test_no_prefix_false_positive(self):
        jobs = [self.job('run-tests (0, api, examples/api/slack-extended, deploy)')]
        self.assertIsNone(pub.find_own_job(jobs, 'examples/api/slack', None, 'other'))

    def test_synthetic_by_suffix(self):
        jobs = [self.job('run-tests / unit-tests')]
        self.assertIsNotNone(pub.find_own_job(jobs, None, 'unit-tests', 'unit-tests'))

    def test_fallback_to_github_job(self):
        jobs = [self.job('deploy-openai')]
        found = pub.find_own_job(jobs, 'examples/aws-serverless/openai', None, 'deploy-openai')
        self.assertEqual(found['name'], 'deploy-openai')


class TestApplyUpdate(unittest.TestCase):
    def test_insert_into_empty_doc(self):
        doc, history = pub.apply_update(None, [], [TILE], 'pass', RUN_META_1, 'integration-test')
        self.assertEqual(len(doc['results']), 1)
        entry = doc['results'][0]
        self.assertEqual(entry['status'], 'pass')
        self.assertEqual(entry['run_id'], 100)
        self.assertEqual(doc['branch'], 'develop')
        self.assertEqual(history, [])

    def test_same_run_republish_replaces_without_history(self):
        doc, _ = pub.apply_update(None, [], [TILE], 'fail', RUN_META_1, 'integration-test')
        doc, history = pub.apply_update(doc, [], [TILE], 'pass', RUN_META_1, 'integration-test')
        self.assertEqual(doc['results'][0]['status'], 'pass')
        self.assertEqual(history, [])

    def test_new_run_rolls_previous_into_history(self):
        doc, history = pub.apply_update(None, [], [TILE], 'fail', RUN_META_1, 'integration-test')
        doc, history = pub.apply_update(doc, history, [TILE], 'pass', RUN_META_2, 'integration-test')
        self.assertEqual(doc['results'][0]['status'], 'pass')
        self.assertEqual(len(history), 1)
        event = json.loads(history[0])
        self.assertEqual(event['run_id'], 100)
        self.assertEqual(event['status'], 'fail')
        self.assertEqual(event['key'], 'Messaging Integrations|Slack (nightly)')

    def test_history_roll_is_idempotent(self):
        doc, history = pub.apply_update(None, [], [TILE], 'fail', RUN_META_1, 'integration-test')
        doc2, history2 = pub.apply_update(doc, history, [TILE], 'pass', RUN_META_2, 'integration-test')
        # Re-publishing run 101 again must not duplicate run 100's event
        _, history3 = pub.apply_update(doc2, history2, [TILE], 'pass', RUN_META_2, 'integration-test')
        self.assertEqual(history2, history3)

    def test_other_tiles_untouched(self):
        other = {**TILE, 'label': 'Telegram (nightly)', 'path': 'examples/api/telegram'}
        doc, _ = pub.apply_update(None, [], [other], 'pass', RUN_META_1, 'integration-test')
        doc, _ = pub.apply_update(doc, [], [TILE], 'fail', RUN_META_1, 'integration-test')
        labels = {r['label']: r['status'] for r in doc['results']}
        self.assertEqual(labels['Telegram (nightly)'], 'pass')
        self.assertEqual(labels['Slack (nightly)'], 'fail')

    def test_history_trimmed_per_tile(self):
        doc, history = None, []
        for run in range(pub.HISTORY_RETENTION_PER_TILE + 5):
            meta = {**RUN_META_1, 'run_id': run,
                    'completed_at': f'2026-07-{(run % 28) + 1:02d}T00:00:00Z'}
            doc, history = pub.apply_update(doc, history, [TILE], 'pass', meta, 'integration-test')
        self.assertEqual(len(history), pub.HISTORY_RETENTION_PER_TILE)

    def test_multi_tile_updates_share_status(self):
        tiles = pub.resolve_tiles('integration-test-weekly', 'examples/memory/cosmos', None, REPO_ROOT)
        doc, _ = pub.apply_update(None, [], tiles, 'fail', RUN_META_1, 'integration-test-weekly')
        self.assertEqual([r['status'] for r in doc['results']], ['fail', 'fail'])


class TestPublishEndToEnd(unittest.TestCase):
    """Full publish flow against a local bare 'origin', including the CAS push."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.origin = base / 'origin.git'
        self.clone = base / 'clone'
        subprocess.run(['git', 'init', '--bare', '-q', str(self.origin)], check=True)
        subprocess.run(['git', 'init', '-q', str(self.clone)], check=True)
        subprocess.run(['git', '-C', str(self.clone), 'remote', 'add', 'origin', str(self.origin)], check=True)
        # The script resolves config files relative to the checkout; copy them in
        github_dir = self.clone / '.github'
        github_dir.mkdir()
        for name in ('integration-test-config.yaml', 'test-config.yaml'):
            (github_dir / name).write_text((Path(REPO_ROOT) / '.github' / name).read_text())
        self.cwd = os.getcwd()
        os.chdir(self.clone)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def run_publish(self, run_id, path, outcome):
        env = {
            'GITHUB_REPOSITORY': 'yaalalabs/agent-kernel',
            'GITHUB_RUN_ID': str(run_id),
            'GITHUB_TOKEN': 'test-token',
            'GITHUB_SHA': 'abcdef1234567890',
            'GITHUB_REF_NAME': 'develop',
            'GITHUB_JOB': 'run-tests',
            'RUNNER_TEMP': self.tmp.name,
        }
        jobs = [{'name': f'run-tests (0, api, {path}, deploy)',
                 'html_url': f'https://github.com/x/y/actions/runs/{run_id}/job/7'}]
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(pub, 'fetch_run_jobs', return_value=jobs):
            pub.publish('integration-test', path, None, outcome)

    def branch_file(self, path):
        result = subprocess.run(
            ['git', '-C', str(self.origin), 'show', f'{pub.STATUS_BRANCH}:{path}'],
            capture_output=True, text=True,
        )
        return result.stdout if result.returncode == 0 else None

    def test_bootstrap_publish_and_update(self):
        # First publish bootstraps the branch
        self.run_publish(100, 'examples/api/slack', 'failure')
        doc = json.loads(self.branch_file('status/integration-test.json'))
        self.assertEqual(doc['results'][0]['status'], 'fail')
        self.assertIsNotNone(self.branch_file('README.md'))

        # A different tile publishes without clobbering the first
        self.run_publish(100, 'examples/api/telegram', 'success')
        doc = json.loads(self.branch_file('status/integration-test.json'))
        statuses = {r['label']: r['status'] for r in doc['results']}
        self.assertEqual(statuses['Slack (nightly)'], 'fail')
        self.assertEqual(statuses['Telegram (nightly)'], 'pass')

        # Re-run of the failed test in the SAME run corrects in place, no history
        self.run_publish(100, 'examples/api/slack', 'success')
        doc = json.loads(self.branch_file('status/integration-test.json'))
        self.assertEqual({r['label']: r['status'] for r in doc['results']}['Slack (nightly)'], 'pass')
        self.assertEqual(self.branch_file('history/integration-test.jsonl'), '')

        # A NEW run rolls the superseded status into history
        self.run_publish(101, 'examples/api/slack', 'success')
        history = [json.loads(l) for l in self.branch_file('history/integration-test.jsonl').splitlines()]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['run_id'], 100)

        # Branch keeps a single-commit history despite four publishes
        count = subprocess.run(
            ['git', '-C', str(self.origin), 'rev-list', '--count', pub.STATUS_BRANCH],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(count, '1')

    def test_cas_retry_on_concurrent_push(self):
        self.run_publish(100, 'examples/api/slack', 'success')

        # Simulate a concurrent publisher landing between fetch and push:
        # the first push attempt is rejected, the retry must succeed and
        # preserve the concurrent update.
        original_fetch = pub.fetch_branch_tip
        calls = {'n': 0}

        def racy_fetch():
            tip = original_fetch()
            if calls['n'] == 0:
                calls['n'] += 1
                run_concurrent = pub.publish
                env = dict(os.environ)
                jobs = [{'name': 'run-tests (1, api, examples/api/gmail, deploy)',
                         'html_url': 'https://x/j'}]
                with mock.patch.object(pub, 'fetch_run_jobs', return_value=jobs), \
                        mock.patch.object(pub, 'fetch_branch_tip', original_fetch):
                    run_concurrent('integration-test', 'examples/api/gmail', None, 'success')
                os.environ.update(env)
            return tip

        with mock.patch.object(pub, 'fetch_branch_tip', side_effect=racy_fetch), \
                mock.patch.object(pub.time, 'sleep'):
            self.run_publish(100, 'examples/api/telegram', 'failure')

        doc = json.loads(self.branch_file('status/integration-test.json'))
        labels = {r['label']: r['status'] for r in doc['results']}
        self.assertEqual(labels['Gmail (nightly)'], 'pass')       # concurrent update kept
        self.assertEqual(labels['Telegram (nightly)'], 'fail')    # retried update landed
        self.assertEqual(labels['Slack (nightly)'], 'pass')


if __name__ == '__main__':
    unittest.main(verbosity=2)
