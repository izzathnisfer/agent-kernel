#!/usr/bin/env python3
"""
Publish Integration Status data for the public dashboard (kernel.yaala.ai/status).

Runs as the final job of a test workflow on the develop branch. It reads the
current run's job/step conclusions from the GitHub Actions API, joins them with
the dashboard metadata in the test config YAMLs, rolls the previously published
snapshot into history, and writes:

    <data-dir>/status/<workflow-key>.json    - latest tile statuses
    <data-dir>/history/<workflow-key>.jsonl  - one compact line per superseded run

The caller (the workflow's publish-status job) pushes <data-dir> to the orphan
`status-data` branch. See docs/specs/integration-status.md.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

from dashboard_config import resolve_dashboard_entries

HISTORY_RETENTION_RUNS = 50

WORKFLOWS = {
    'test': {
        'config': '.github/test-config.yaml',
        'tiers': ['e2e'],
        'include_deployment_base': False,
        'expected_cadence_hours': None,  # runs on every develop push
    },
    'integration-test': {
        'config': '.github/integration-test-config.yaml',
        'tiers': ['nightly'],
        'include_deployment_base': True,
        'expected_cadence_hours': 48,
    },
    'integration-test-weekly': {
        'config': '.github/integration-test-config.yaml',
        'tiers': ['weekly'],
        'include_deployment_base': True,
        'expected_cadence_hours': 192,
    },
}

# Jobs in test-reusable.yaml that aren't matrix tests but deserve tiles
SYNTHETIC_JOBS = {
    'test': [
        {
            'job_suffix': 'unit-tests',
            'path': 'ak-py',
            'type': 'unit',
            'category': 'Core & Frameworks',
            'label': 'ak-py unit tests',
            'description': 'ak-py library unit test suite',
        },
        {
            'job_suffix': 'script-tests',
            'path': 'scripts',
            'type': 'scripts',
            'category': 'Core & Frameworks',
            'label': 'Utility script tests',
            'description': 'Repository maintenance script tests',
        },
    ],
}

CONCLUSION_TO_STATUS = {
    'success': 'pass',
    'failure': 'fail',
    'timed_out': 'fail',
    'cancelled': 'skipped',
    'skipped': 'skipped',
    'neutral': 'skipped',
}


def tile_key(category: str, label: str) -> str:
    return f"{category}|{label}"


def load_tests(workflow_key: str, repo_root: Path) -> tuple[list, list]:
    """Return (matrix tests, deployment_base tests) for a workflow."""
    meta = WORKFLOWS[workflow_key]
    with open(repo_root / meta['config']) as f:
        config = yaml.safe_load(f)

    tests = []
    for tier in meta['tiers']:
        tests.extend(config.get(tier, {}).get('tests') or [])

    base_tests = []
    if meta['include_deployment_base']:
        base_tests = config.get('deployment_base') or []

    return tests, base_tests


def fetch_run_jobs(repo: str, run_id: str, token: str) -> list:
    """Fetch all jobs of a workflow run (paginated).

    filter=latest returns each job's most recent attempt, so when a failed
    matrix job is re-run (which also re-runs this publisher, as a dependent
    job) the corrected conclusion is published.
    """
    jobs = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs"
            f"?filter=latest&per_page=100&page={page}"
        )
        request = urllib.request.Request(url, headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        })
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
        batch = payload.get('jobs', [])
        jobs.extend(batch)
        if len(batch) < 100:
            return jobs
        page += 1


def find_matrix_job(path: str, jobs: list) -> dict | None:
    """Find the matrix job for a test path. Job names look like
    'run-tests (0, api, examples/api/slack, deploy)' or
    'run-tests / e2e-tests (0, cli, examples/cli/openai, deploy)'."""
    pattern = re.compile(r'[(, ]' + re.escape(path) + r'[,)]')
    for job in jobs:
        if pattern.search(job.get('name', '')):
            return job
    return None


def job_status(job: dict | None) -> str:
    if job is None:
        return 'unknown'
    if job.get('status') != 'completed':
        return 'unknown'
    return CONCLUSION_TO_STATUS.get(job.get('conclusion'), 'unknown')


def base_deployment_status(workflow_key: str, jobs: list) -> tuple[str, dict | None]:
    """Status of the deploy-openai base job.

    In the weekly workflow the test step runs with continue-on-error, so the job
    can be green while the test failed. The follow-up 'Report openai test outcome'
    step only runs when the test failed, which makes it a reliable signal:
    skipped -> pass, ran -> fail.
    """
    job = next((j for j in jobs if j.get('name') == 'deploy-openai'), None)
    if job is None:
        return 'unknown', None

    status = job_status(job)
    if status != 'pass':
        return status, job

    if workflow_key == 'integration-test-weekly':
        for step in job.get('steps', []):
            if step.get('name') == 'Report openai test outcome':
                if step.get('conclusion') == 'skipped':
                    return 'pass', job
                return 'fail', job
        # Fallback: the test step's own conclusion
        for step in job.get('steps', []):
            if step.get('name', '').startswith('Test '):
                return CONCLUSION_TO_STATUS.get(step.get('conclusion'), 'unknown'), job

    return status, job


def build_results(workflow_key: str, tests: list, base_tests: list, jobs: list) -> list:
    """Join config entries with job conclusions, fanning out one result per
    dashboard entry (tile)."""
    results = []

    def add(test: dict, status: str, job: dict | None):
        for entry in resolve_dashboard_entries(test):
            results.append({
                'path': test['path'],
                'type': test['type'],
                'category': entry['category'],
                'label': entry['label'],
                'description': entry['description'],
                'status': status,
                'job_url': job.get('html_url') if job else None,
            })

    for test in base_tests:
        status, job = base_deployment_status(workflow_key, jobs)
        add(test, status, job)

    for test in tests:
        job = find_matrix_job(test['path'], jobs)
        add(test, job_status(job), job)

    for synthetic in SYNTHETIC_JOBS.get(workflow_key, []):
        job = next(
            (j for j in jobs if j.get('name', '').endswith(synthetic['job_suffix'])),
            None,
        )
        results.append({
            'path': synthetic['path'],
            'type': synthetic['type'],
            'category': synthetic['category'],
            'label': synthetic['label'],
            'description': synthetic['description'],
            'status': job_status(job),
            'job_url': job.get('html_url') if job else None,
        })

    return results


def compact_history_line(status_doc: dict) -> dict:
    """Compress a status document into one history line."""
    return {
        'run_id': status_doc.get('run_id'),
        'run_url': status_doc.get('run_url'),
        'commit': status_doc.get('commit'),
        'completed_at': status_doc.get('completed_at'),
        'results': {
            tile_key(r['category'], r['label']): r['status']
            for r in status_doc.get('results', [])
        },
    }


def roll_history(previous_doc: dict | None, history_lines: list[str],
                 current_run_id) -> list[str]:
    """Append the superseded snapshot to history. Idempotent on run_id; trims to
    the most recent HISTORY_RETENTION_RUNS entries."""
    lines = [line for line in history_lines if line.strip()]

    if previous_doc is None:
        return lines[-HISTORY_RETENTION_RUNS:]

    previous_run_id = previous_doc.get('run_id')
    if previous_run_id == current_run_id:
        # Re-publish of the same run: the snapshot is being replaced, not superseded
        return lines[-HISTORY_RETENTION_RUNS:]

    seen_run_ids = set()
    for line in lines:
        try:
            seen_run_ids.add(json.loads(line).get('run_id'))
        except json.JSONDecodeError:
            continue

    if previous_run_id not in seen_run_ids:
        lines.append(json.dumps(compact_history_line(previous_doc), separators=(',', ':')))

    return lines[-HISTORY_RETENTION_RUNS:]


DATA_BRANCH_README = """# status-data

Machine-generated data for the public Integration Status page
(https://kernel.yaala.ai/status). Do not edit by hand.

- `status/<workflow>.json` - latest tile statuses per workflow
- `history/<workflow>.jsonl` - one compact line per superseded run

Published by the `publish-status` job of the Test, Nightly Integration Tests,
and Weekly Integration Tests workflows on the develop branch.
See docs/specs/integration-status.md on develop for the format.
"""


def publish(workflow_key: str, data_dir: Path, repo_root: Path) -> dict:
    repo = os.environ['GITHUB_REPOSITORY']
    run_id = os.environ['GITHUB_RUN_ID']
    token = os.environ['GITHUB_TOKEN']

    tests, base_tests = load_tests(workflow_key, repo_root)
    jobs = fetch_run_jobs(repo, run_id, token)
    results = build_results(workflow_key, tests, base_tests, jobs)

    status_doc = {
        'workflow': workflow_key,
        'workflow_name': os.environ.get('GITHUB_WORKFLOW', workflow_key),
        'run_id': int(run_id),
        'run_url': f"https://github.com/{repo}/actions/runs/{run_id}",
        'branch': 'develop',
        'commit': os.environ.get('GITHUB_SHA', '')[:8],
        'completed_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'expected_cadence_hours': WORKFLOWS[workflow_key]['expected_cadence_hours'],
        'results': results,
    }

    status_path = data_dir / 'status' / f'{workflow_key}.json'
    history_path = data_dir / 'history' / f'{workflow_key}.jsonl'
    status_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.parent.mkdir(parents=True, exist_ok=True)

    previous_doc = None
    if status_path.exists():
        try:
            previous_doc = json.loads(status_path.read_text())
        except json.JSONDecodeError:
            print(f"⚠️  Ignoring unparseable previous snapshot: {status_path}")

    history_lines = history_path.read_text().splitlines() if history_path.exists() else []
    new_history = roll_history(previous_doc, history_lines, status_doc['run_id'])
    history_path.write_text('\n'.join(new_history) + ('\n' if new_history else ''))

    status_path.write_text(json.dumps(status_doc, indent=2) + '\n')

    readme = data_dir / 'README.md'
    if not readme.exists():
        readme.write_text(DATA_BRANCH_README)

    return status_doc


def main():
    parser = argparse.ArgumentParser(description='Publish Integration Status data')
    parser.add_argument('--workflow', choices=sorted(WORKFLOWS), required=True,
                        help='Workflow key to publish for')
    parser.add_argument('--data-dir', required=True,
                        help='Checkout of the status-data branch to write into')
    parser.add_argument('--repo-root', default='.',
                        help='Repository root containing the config YAMLs')
    args = parser.parse_args()

    status_doc = publish(args.workflow, Path(args.data_dir), Path(args.repo_root))

    counts = {}
    for result in status_doc['results']:
        counts[result['status']] = counts.get(result['status'], 0) + 1
    summary = ', '.join(f"{count} {status}" for status, count in sorted(counts.items()))
    print(f"✅ Published {len(status_doc['results'])} tiles for '{args.workflow}': {summary}")

    if counts.get('unknown'):
        print("⚠️  Some tiles had no matching job in this run (status 'unknown')")


if __name__ == '__main__':
    main()
