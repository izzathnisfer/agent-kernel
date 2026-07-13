#!/usr/bin/env python3
"""Tests for publish_integration_status.py and dashboard_config.py."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import publish_integration_status as pub
from dashboard_config import resolve_dashboard_entries, validate_dashboard_block


def job(name, conclusion='success', steps=None, status='completed'):
    return {
        'name': name,
        'status': status,
        'conclusion': conclusion,
        'html_url': f'https://github.com/x/y/actions/runs/1/job/{abs(hash(name)) % 10000}',
        'steps': steps or [],
    }


class TestDashboardConfig(unittest.TestCase):
    def test_defaults_when_omitted(self):
        entries = resolve_dashboard_entries({'type': 'api', 'path': 'examples/api/multimodal/redis'})
        self.assertEqual(entries, [{
            'category': 'Core & Examples',
            'label': 'multimodal / redis',
            'description': None,
        }])

    def test_hidden(self):
        self.assertEqual(resolve_dashboard_entries({'path': 'x', 'dashboard': 'hidden'}), [])

    def test_single_mapping(self):
        entries = resolve_dashboard_entries({
            'type': 'api', 'path': 'examples/api/slack',
            'dashboard': {'category': 'Messaging Integrations', 'label': 'Slack'},
        })
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['category'], 'Messaging Integrations')
        self.assertEqual(entries[0]['label'], 'Slack')

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
            'dashboard': {'category': 'A', 'label': 'x', 'description': 'd'},
        }), [])


class TestJobMatching(unittest.TestCase):
    def test_matches_matrix_job(self):
        jobs = [job('run-tests (0, api, examples/api/slack, deploy)')]
        self.assertIsNotNone(pub.find_matrix_job('examples/api/slack', jobs))

    def test_matches_reusable_workflow_prefix(self):
        jobs = [job('run-tests / e2e-tests (3, cli, examples/cli/openai, deploy)')]
        self.assertIsNotNone(pub.find_matrix_job('examples/cli/openai', jobs))

    def test_no_prefix_false_positive(self):
        jobs = [job('run-tests (0, api, examples/api/slack-extended, deploy)')]
        self.assertIsNone(pub.find_matrix_job('examples/api/slack', jobs))

    def test_status_mapping(self):
        self.assertEqual(pub.job_status(job('j', 'success')), 'pass')
        self.assertEqual(pub.job_status(job('j', 'failure')), 'fail')
        self.assertEqual(pub.job_status(job('j', 'cancelled')), 'skipped')
        self.assertEqual(pub.job_status(job('j', None, status='in_progress')), 'unknown')
        self.assertEqual(pub.job_status(None), 'unknown')


class TestBaseDeploymentStatus(unittest.TestCase):
    def test_weekly_pass_when_report_step_skipped(self):
        jobs = [job('deploy-openai', 'success', steps=[
            {'name': 'Test examples/aws-serverless/openai', 'conclusion': 'success'},
            {'name': 'Report openai test outcome', 'conclusion': 'skipped'},
        ])]
        status, _ = pub.base_deployment_status('integration-test-weekly', jobs)
        self.assertEqual(status, 'pass')

    def test_weekly_fail_when_report_step_ran(self):
        # continue-on-error hides the test failure at job level; the report step
        # running is the failure signal
        jobs = [job('deploy-openai', 'success', steps=[
            {'name': 'Test examples/aws-serverless/openai', 'conclusion': 'failure'},
            {'name': 'Report openai test outcome', 'conclusion': 'success'},
        ])]
        status, _ = pub.base_deployment_status('integration-test-weekly', jobs)
        self.assertEqual(status, 'fail')

    def test_deploy_failure(self):
        jobs = [job('deploy-openai', 'failure')]
        status, _ = pub.base_deployment_status('integration-test-weekly', jobs)
        self.assertEqual(status, 'fail')

    def test_nightly_uses_job_conclusion(self):
        jobs = [job('deploy-openai', 'success')]
        status, _ = pub.base_deployment_status('integration-test', jobs)
        self.assertEqual(status, 'pass')

    def test_missing_job(self):
        status, matched = pub.base_deployment_status('integration-test-weekly', [])
        self.assertEqual(status, 'unknown')
        self.assertIsNone(matched)


class TestBuildResults(unittest.TestCase):
    def test_fan_out_shares_status_and_job(self):
        tests = [{
            'type': 'azure-serverless', 'path': 'examples/memory/cosmos',
            'dashboard': [
                {'category': 'Agent Memory / Knowledge', 'label': 'Cosmos DB memory'},
                {'category': 'Azure Serverless', 'label': 'OpenAI + Cosmos memory'},
            ],
        }]
        jobs = [job('run-tests (0, azure-serverless, examples/memory/cosmos, deploy)', 'failure')]
        results = pub.build_results('integration-test-weekly', tests, [], jobs)
        self.assertEqual(len(results), 2)
        self.assertEqual({r['status'] for r in results}, {'fail'})
        self.assertEqual(len({r['job_url'] for r in results}), 1)

    def test_unmatched_test_is_unknown(self):
        tests = [{'type': 'api', 'path': 'examples/api/new-thing'}]
        results = pub.build_results('integration-test', tests, [], [])
        self.assertEqual(results[0]['status'], 'unknown')
        self.assertIsNone(results[0]['job_url'])

    def test_hidden_test_emits_nothing(self):
        tests = [{'type': 'api', 'path': 'examples/api/x', 'dashboard': 'hidden'}]
        self.assertEqual(pub.build_results('integration-test', tests, [], []), [])

    def test_synthetic_jobs_for_test_workflow(self):
        jobs = [
            job('run-tests / unit-tests', 'success'),
            job('run-tests / script-tests', 'failure'),
        ]
        results = pub.build_results('test', [], [], jobs)
        by_label = {r['label']: r['status'] for r in results}
        self.assertEqual(by_label['ak-py unit tests'], 'pass')
        self.assertEqual(by_label['Utility script tests'], 'fail')


class TestHistory(unittest.TestCase):
    def doc(self, run_id):
        return {
            'run_id': run_id,
            'run_url': f'https://github.com/x/y/actions/runs/{run_id}',
            'commit': 'abc12345',
            'completed_at': '2026-07-01T00:00:00Z',
            'results': [
                {'category': 'A', 'label': 'x', 'status': 'pass'},
            ],
        }

    def test_appends_superseded_snapshot(self):
        lines = pub.roll_history(self.doc(1), [], current_run_id=2)
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry['run_id'], 1)
        self.assertEqual(entry['results'], {'A|x': 'pass'})

    def test_no_previous_snapshot(self):
        self.assertEqual(pub.roll_history(None, [], current_run_id=2), [])

    def test_idempotent_on_run_id(self):
        first = pub.roll_history(self.doc(1), [], current_run_id=2)
        second = pub.roll_history(self.doc(1), first, current_run_id=2)
        self.assertEqual(first, second)

    def test_same_run_republish_not_recorded(self):
        self.assertEqual(pub.roll_history(self.doc(2), [], current_run_id=2), [])

    def test_trims_to_retention(self):
        lines = [
            json.dumps({'run_id': i, 'results': {}})
            for i in range(pub.HISTORY_RETENTION_RUNS + 10)
        ]
        rolled = pub.roll_history(self.doc(999), lines, current_run_id=1000)
        self.assertEqual(len(rolled), pub.HISTORY_RETENTION_RUNS)
        self.assertEqual(json.loads(rolled[-1])['run_id'], 999)


class TestPublishEndToEnd(unittest.TestCase):
    def run_publish(self, data_dir, run_id, jobs):
        env = {
            'GITHUB_REPOSITORY': 'yaalalabs/agent-kernel',
            'GITHUB_RUN_ID': str(run_id),
            'GITHUB_TOKEN': 'test-token',
            'GITHUB_SHA': 'abcdef1234567890',
            'GITHUB_WORKFLOW': 'Nightly Integration Tests',
        }
        repo_root = Path(__file__).resolve().parents[2]
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(pub, 'fetch_run_jobs', return_value=jobs):
            return pub.publish('integration-test', Path(data_dir), repo_root)

    def test_publish_writes_status_and_rolls_history(self):
        jobs = [
            job('deploy-openai', 'success'),
            job('run-tests (0, api, examples/api/slack, deploy)', 'success'),
            job('run-tests (1, api, examples/api/telegram, deploy)', 'failure'),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            doc1 = self.run_publish(tmp, 100, jobs)
            status_file = Path(tmp) / 'status' / 'integration-test.json'
            history_file = Path(tmp) / 'history' / 'integration-test.jsonl'

            self.assertTrue(status_file.exists())
            self.assertTrue((Path(tmp) / 'README.md').exists())
            self.assertEqual(history_file.read_text(), '')

            written = json.loads(status_file.read_text())
            self.assertEqual(written['run_id'], 100)
            statuses = {r['label']: r['status'] for r in written['results']}
            self.assertEqual(statuses['Slack (nightly)'], 'pass')
            self.assertEqual(statuses['Telegram (nightly)'], 'fail')
            self.assertEqual(statuses['OpenAI agent (base deployment)'], 'pass')
            # every configured nightly test appears, even without a matching job
            self.assertGreaterEqual(len(written['results']), 9)

            # second publish for a new run rolls run 100 into history
            self.run_publish(tmp, 101, jobs)
            history = [json.loads(l) for l in history_file.read_text().splitlines()]
            self.assertEqual([h['run_id'] for h in history], [100])
            self.assertEqual(history[0]['results']['Messaging Integrations|Slack (nightly)'], 'pass')
            self.assertEqual(json.loads(status_file.read_text())['run_id'], 101)


if __name__ == '__main__':
    unittest.main(verbosity=2)
