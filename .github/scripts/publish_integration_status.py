#!/usr/bin/env python3
"""
Publish Integration Status data for the public dashboard (kernel.yaala.ai/status).

Distributed publisher: each test job runs this script as its final step
(if: always()) to publish its OWN tile statuses the moment its test finishes,
instead of a central job collecting every result at the end of the run. This
also means re-running a single failed test republishes just that tile.

The script updates two files on the orphan `status-data` branch:

    status/<workflow-key>.json    - one entry per tile, with per-tile run metadata
    history/<workflow-key>.jsonl  - one line per superseded tile status

Concurrent matrix jobs publish at the same time, so the push is an atomic
compare-and-swap: the new state is committed as a single orphan commit built
on the fetched branch tip and pushed with --force-with-lease pinned to that
tip's SHA. If another job pushed in between, the push is rejected and the
whole fetch-patch-push cycle retries. The branch history therefore stays at
exactly one commit.

Usage (from a repository checkout with actions/checkout credentials):
    publish_integration_status.py --workflow test --path examples/cli/openai --outcome success
    publish_integration_status.py --workflow test --synthetic unit-tests --outcome failure

See docs/specs/integration-status.md.
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

import yaml

STATUS_BRANCH = 'status-data'
HISTORY_RETENTION_PER_TILE = 15
PUSH_ATTEMPTS = 10

WORKFLOWS = {
    'test': {
        'config': '.github/test-config.yaml',
        'tiers': ['e2e'],
        'include_deployment_base': False,
        'workflow_name': 'Test',
        'expected_cadence_hours': None,  # runs on every push to the source branch
    },
    'integration-test': {
        'config': '.github/integration-test-config.yaml',
        'tiers': ['nightly'],
        'include_deployment_base': True,
        'workflow_name': 'Nightly Integration Tests',
        'expected_cadence_hours': 48,
    },
    'integration-test-weekly': {
        'config': '.github/integration-test-config.yaml',
        'tiers': ['weekly'],
        'include_deployment_base': True,
        'workflow_name': 'Weekly Integration Tests',
        'expected_cadence_hours': 192,
    },
}

# Non-matrix jobs that publish their own tiles via --synthetic
SYNTHETIC_TILES = {
    'unit-tests': {
        'path': 'ak-py',
        'type': 'unit',
        'category': 'Core & Frameworks',
        'label': 'ak-py unit tests',
        'description': 'ak-py library unit test suite',
        'logo': None,
    },
    'script-tests': {
        'path': 'scripts',
        'type': 'scripts',
        'category': 'Core & Frameworks',
        'label': 'Utility script tests',
        'description': 'Repository maintenance script tests',
        'logo': None,
    },
}

# job.status / steps.<id>.outcome values -> tile status
OUTCOME_TO_STATUS = {
    'success': 'pass',
    'failure': 'fail',
    'cancelled': 'skipped',
    'skipped': 'skipped',
}


def tile_key(category: str, label: str) -> str:
    return f"{category}|{label}"


def resolve_tiles(workflow_key: str, path: str | None, synthetic: str | None,
                  repo_root: str = '.') -> list[dict]:
    """Resolve the dashboard tiles this invocation publishes."""
    if synthetic:
        tile = SYNTHETIC_TILES[synthetic]
        return [dict(tile)]

    meta = WORKFLOWS[workflow_key]
    with open(os.path.join(repo_root, meta['config'])) as f:
        config = yaml.safe_load(f)

    candidates = []
    if meta['include_deployment_base']:
        candidates.extend(config.get('deployment_base') or [])
    for tier in meta['tiers']:
        candidates.extend(config.get(tier, {}).get('tests') or [])

    test = next((t for t in candidates if t.get('path') == path), None)
    if test is None:
        raise SystemExit(f"❌ No config entry with path '{path}' for workflow '{workflow_key}'")

    # Imported lazily so the pure-function tests don't need the module path set up
    from dashboard_config import resolve_dashboard_entries

    return [
        {
            'path': test['path'],
            'type': test['type'],
            'category': entry['category'],
            'label': entry['label'],
            'description': entry['description'],
            'logo': entry['logo'],
        }
        for entry in resolve_dashboard_entries(test)
    ]


def apply_update(status_doc: dict | None, history_lines: list[str],
                 tiles: list[dict], status: str, run_meta: dict,
                 workflow_key: str) -> tuple[dict, list[str]]:
    """Merge this job's tile statuses into the status document.

    A tile superseded by a NEW run rolls its previous entry into history
    (per-item roll-over). Republishing under the same run_id (a re-run)
    replaces the entry in place without a history event.
    """
    meta = WORKFLOWS[workflow_key]
    doc = status_doc or {
        'workflow': workflow_key,
        'workflow_name': meta['workflow_name'],
        'expected_cadence_hours': meta['expected_cadence_hours'],
        'results': [],
    }
    doc['branch'] = run_meta['branch']
    doc['commit'] = run_meta['commit']
    doc['updated_at'] = run_meta['completed_at']

    lines = [line for line in history_lines if line.strip()]
    seen_events = set()
    for line in lines:
        try:
            event = json.loads(line)
            seen_events.add((event.get('key'), event.get('run_id')))
        except json.JSONDecodeError:
            continue

    results = {tile_key(r['category'], r['label']): r for r in doc.get('results', [])}

    for tile in tiles:
        key = tile_key(tile['category'], tile['label'])
        previous = results.get(key)
        if previous and previous.get('run_id') != run_meta['run_id']:
            event_id = (key, previous.get('run_id'))
            if event_id not in seen_events:
                lines.append(json.dumps({
                    'key': key,
                    'status': previous.get('status'),
                    'run_id': previous.get('run_id'),
                    'run_url': previous.get('run_url'),
                    'completed_at': previous.get('completed_at'),
                }, separators=(',', ':')))
                seen_events.add(event_id)
        results[key] = {
            **tile,
            'status': status,
            'run_id': run_meta['run_id'],
            'run_url': run_meta['run_url'],
            'job_url': run_meta['job_url'],
            'completed_at': run_meta['completed_at'],
        }

    doc['results'] = [results[key] for key in sorted(results)]

    # Trim history to the most recent events per tile
    per_tile: dict = {}
    for line in lines:
        try:
            key = json.loads(line).get('key')
        except json.JSONDecodeError:
            continue
        per_tile.setdefault(key, []).append(line)
    trimmed = []
    for line in lines:
        try:
            key = json.loads(line).get('key')
        except json.JSONDecodeError:
            continue
        if line in per_tile[key][-HISTORY_RETENTION_PER_TILE:]:
            trimmed.append(line)

    return doc, trimmed


# ─── Own-job discovery (best effort, for the tile's job link) ────────────────

def fetch_run_jobs(repo: str, run_id: str, token: str) -> list:
    """Fetch all jobs of the current run. filter=latest returns each job's most
    recent attempt, so re-runs link to the attempt that produced the status."""
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


def find_own_job(jobs: list, path: str | None, synthetic: str | None,
                 github_job: str) -> dict | None:
    """Locate this job in the run's job list. Matrix job names embed the test
    path, e.g. 'run-tests (0, api, examples/api/slack, deploy)'; other jobs are
    matched by their job id (GITHUB_JOB), e.g. 'deploy-openai' or
    'run-tests / unit-tests'."""
    if synthetic:
        return next((j for j in jobs if j.get('name', '').endswith(synthetic)), None)
    if path:
        pattern = re.compile(r'[(, ]' + re.escape(path) + r'[,)]')
        job = next((j for j in jobs if pattern.search(j.get('name', ''))), None)
        if job:
            return job
    return next(
        (j for j in jobs
         if j.get('name') == github_job or j.get('name', '').endswith(f'/ {github_job}')),
        None,
    )


# ─── Git plumbing: atomic compare-and-swap on the status-data branch ─────────

def git(*args: str, check: bool = True, env: dict | None = None) -> str:
    merged_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        ['git', *args], capture_output=True, text=True, env=merged_env,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def fetch_branch_tip() -> str | None:
    """Fetch the status-data branch tip; None when the branch doesn't exist yet."""
    result = subprocess.run(
        ['git', 'fetch', 'origin', STATUS_BRANCH],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return git('rev-parse', 'FETCH_HEAD')


def read_branch_file(tip: str | None, path: str) -> str | None:
    if tip is None:
        return None
    result = subprocess.run(
        ['git', 'cat-file', 'blob', f'{tip}:{path}'],
        capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else None


DATA_BRANCH_README = """# status-data

Machine-generated data for the public Integration Status page
(https://kernel.yaala.ai/status). Do not edit by hand.

- `status/<workflow>.json` - latest tile statuses per workflow
- `history/<workflow>.jsonl` - one line per superseded tile status

Published by the test jobs of the Test, Nightly Integration Tests, and
Weekly Integration Tests workflows. See docs/specs/integration-status.md
for the format.
"""


def commit_files(tip: str | None, files: dict[str, str], message: str) -> str:
    """Build a single orphan commit containing the branch's previous tree plus
    `files`, without touching the working tree."""
    index_file = os.path.join(os.environ.get('RUNNER_TEMP', '/tmp'),
                              f'status-index-{os.getpid()}')
    env = {'GIT_INDEX_FILE': index_file}
    if tip:
        git('read-tree', tip, env=env)
    else:
        git('read-tree', '--empty', env=env)
        files = {'README.md': DATA_BRANCH_README, **files}

    for path, content in files.items():
        blob = subprocess.run(
            ['git', 'hash-object', '-w', '--stdin'],
            input=content, capture_output=True, text=True, check=True,
        ).stdout.strip()
        git('update-index', '--add', '--cacheinfo', f'100644,{blob},{path}', env=env)

    tree = git('write-tree', env=env)
    os.unlink(index_file)

    commit_env = {
        'GIT_AUTHOR_NAME': 'agent-kernel-ci[bot]',
        'GIT_AUTHOR_EMAIL': 'agent-kernel-ci[bot]@users.noreply.github.com',
        'GIT_COMMITTER_NAME': 'agent-kernel-ci[bot]',
        'GIT_COMMITTER_EMAIL': 'agent-kernel-ci[bot]@users.noreply.github.com',
    }
    # No parent: the branch always holds exactly one commit
    return git('commit-tree', tree, '-m', message, env=commit_env)


def push_cas(commit: str, expected_tip: str | None) -> bool:
    """Push atomically: succeeds only if the remote branch still points at
    expected_tip (or doesn't exist when expected_tip is None)."""
    lease = f'--force-with-lease=refs/heads/{STATUS_BRANCH}:{expected_tip or ""}'
    result = subprocess.run(
        ['git', 'push', 'origin', f'{commit}:refs/heads/{STATUS_BRANCH}', lease],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ℹ️  Push rejected (concurrent publish), retrying: {result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'unknown'}")
    return result.returncode == 0


def publish(workflow_key: str, path: str | None, synthetic: str | None,
            outcome: str) -> None:
    repo = os.environ['GITHUB_REPOSITORY']
    run_id = os.environ['GITHUB_RUN_ID']
    token = os.environ['GITHUB_TOKEN']

    status = OUTCOME_TO_STATUS.get(outcome, 'unknown')
    tiles = resolve_tiles(workflow_key, path, synthetic)
    if not tiles:
        print(f"ℹ️  '{path or synthetic}' is hidden from the dashboard, nothing to publish")
        return

    job_url = None
    try:
        jobs = fetch_run_jobs(repo, run_id, token)
        job = find_own_job(jobs, path, synthetic, os.environ.get('GITHUB_JOB', ''))
        job_url = job.get('html_url') if job else None
    except Exception as error:  # job link is best effort
        print(f"⚠️  Could not resolve job URL: {error}")

    run_meta = {
        'run_id': int(run_id),
        'run_url': f"https://github.com/{repo}/actions/runs/{run_id}",
        'job_url': job_url,
        'branch': os.environ.get('GITHUB_REF_NAME', ''),
        'commit': os.environ.get('GITHUB_SHA', '')[:8],
        'completed_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }

    status_path = f'status/{workflow_key}.json'
    history_path = f'history/{workflow_key}.jsonl'

    for attempt in range(1, PUSH_ATTEMPTS + 1):
        tip = fetch_branch_tip()

        status_doc = None
        raw_status = read_branch_file(tip, status_path)
        if raw_status:
            try:
                status_doc = json.loads(raw_status)
            except json.JSONDecodeError:
                print(f"⚠️  Ignoring unparseable {status_path}")

        raw_history = read_branch_file(tip, history_path) or ''
        doc, history = apply_update(
            status_doc, raw_history.splitlines(), tiles, status, run_meta, workflow_key,
        )

        commit = commit_files(tip, {
            status_path: json.dumps(doc, indent=2) + '\n',
            history_path: '\n'.join(history) + ('\n' if history else ''),
        }, f"status: {workflow_key} {path or synthetic} run {run_id}")

        if push_cas(commit, tip):
            labels = ', '.join(f"{t['category']} / {t['label']}" for t in tiles)
            print(f"✅ Published '{status}' for {labels} (attempt {attempt})")
            return

        time.sleep(random.uniform(1, 4))

    raise SystemExit(f"❌ Could not publish after {PUSH_ATTEMPTS} attempts")


def main():
    parser = argparse.ArgumentParser(description='Publish one job\'s Integration Status tiles')
    parser.add_argument('--workflow', choices=sorted(WORKFLOWS), required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--path', help='Test path from the config file (matrix jobs)')
    group.add_argument('--synthetic', choices=sorted(SYNTHETIC_TILES),
                       help='Non-matrix tile to publish (unit-tests, script-tests)')
    parser.add_argument('--outcome', required=True,
                        help="job.status or a step outcome: success/failure/cancelled/skipped")
    args = parser.parse_args()

    publish(args.workflow, args.path, args.synthetic, args.outcome)


if __name__ == '__main__':
    main()
