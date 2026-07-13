#!/usr/bin/env python3
"""
Validate test configuration files (.github/integration-test-config.yaml and
.github/test-config.yaml).
Checks for:
- Valid YAML syntax
- Required fields
- Valid paths
- No duplicate tests
- Well-formed `dashboard` blocks (Integration Status page metadata)
- Unique (category, label) tile identity across all config files
"""

import yaml
import sys
from pathlib import Path
from typing import Set

from dashboard_config import resolve_dashboard_entries, validate_dashboard_block

VALID_TYPES = {
    'cli', 'api', 'memory', 'containerized',
    'aws-containerized', 'aws-serverless',
    'azure-containerized', 'azure-serverless',
    'gcp-containerized', 'gcp-serverless',
}

# Tiers expected in each known config file
REQUIRED_TIERS = {
    'integration-test-config.yaml': ['nightly', 'weekly'],
    'test-config.yaml': ['e2e'],
}

DEFAULT_CONFIG_PATHS = [
    '.github/integration-test-config.yaml',
    '.github/test-config.yaml',
]


def validate_tests(tests: list, tier: str, seen_tiles: dict) -> bool:
    """Validate one tier's test list. Returns True when valid."""
    tier_valid = True
    print(f"  ℹ️  Total tests: {len(tests)}")

    seen_paths: Set[str] = set()

    for idx, test in enumerate(tests, 1):
        if not isinstance(test, dict):
            print(f"  ❌ Test {idx} is not a dictionary")
            tier_valid = False
            continue

        # Check required fields
        if 'type' not in test:
            print(f"  ❌ Test {idx} missing 'type' field")
            tier_valid = False
            continue

        if 'path' not in test:
            print(f"  ❌ Test {idx} missing 'path' field")
            tier_valid = False
            continue

        test_type = test['type']
        test_path = test['path']

        # Validate type
        if test_type not in VALID_TYPES:
            print(f"  ❌ Test {idx} has invalid type: {test_type}")
            print(f"     Valid types: {', '.join(sorted(VALID_TYPES))}")
            tier_valid = False

        # Check for duplicates
        if test_path in seen_paths:
            print(f"  ⚠️  Test {idx} is duplicate: {test_path}")
        seen_paths.add(test_path)

        # Validate path exists
        if not Path(test_path).exists():
            print(f"  ⚠️  Test {idx} path does not exist: {test_path}")

        # For AWS tests, check deploy_dir
        if test_type in ['aws-containerized', 'aws-serverless']:
            deploy_dir = test.get('deploy_dir', 'deploy')
            full_deploy_path = Path(test_path) / deploy_dir

            if not full_deploy_path.exists():
                print(f"  ⚠️  Test {idx} deploy directory not found: {full_deploy_path}")

            deploy_script = full_deploy_path / 'deploy.sh'
            if not deploy_script.exists():
                print(f"  ⚠️  Test {idx} deploy.sh not found: {deploy_script}")

        # Validate dashboard block and tile identity uniqueness
        dashboard_errors = validate_dashboard_block(test)
        for error in dashboard_errors:
            print(f"  ❌ Test {idx} dashboard: {error}")
            tier_valid = False

        if not dashboard_errors:
            for entry in resolve_dashboard_entries(test):
                tile_key = (entry['category'], entry['label'])
                if tile_key in seen_tiles:
                    print(
                        f"  ❌ Test {idx} dashboard tile "
                        f"'{entry['category']} / {entry['label']}' already used by "
                        f"{seen_tiles[tile_key]}"
                    )
                    tier_valid = False
                else:
                    seen_tiles[tile_key] = f"{tier}: {test_path}"

    return tier_valid


def validate_config(config_path: str, seen_tiles: dict) -> bool:
    """Validate a single test configuration file."""
    config_file = Path(config_path)

    if not config_file.exists():
        print(f"❌ Configuration file not found: {config_path}")
        return False

    try:
        with open(config_file, 'r') as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"❌ Invalid YAML syntax: {e}")
        return False

    print("✅ Valid YAML syntax")

    # Check required tiers for this file
    required_tiers = REQUIRED_TIERS.get(config_file.name)
    if required_tiers is None:
        required_tiers = [t for t in ('nightly', 'weekly', 'e2e') if t in config]
        if not required_tiers:
            print("❌ No known tiers found (expected nightly/weekly or e2e)")
            return False

    for tier in required_tiers:
        if tier not in config:
            print(f"❌ Missing required tier: {tier}")
            return False

    print(f"✅ All required tiers present ({', '.join(required_tiers)})")

    all_valid = True

    # Validate deployment_base entries (integration config only)
    if config.get('deployment_base'):
        print("\n🔍 Validating deployment_base...")
        base_valid = validate_tests(config['deployment_base'], 'deployment_base', seen_tiles)
        all_valid = all_valid and base_valid
        if base_valid:
            print("  ✅ deployment_base configuration valid")

    # Validate each tier
    for tier in required_tiers:
        print(f"\n🔍 Validating {tier} tier...")

        if 'tests' not in config[tier]:
            print(f"  ❌ Missing 'tests' key in {tier}")
            all_valid = False
            continue

        tests = config[tier]['tests']
        if not isinstance(tests, list):
            print(f"  ❌ 'tests' must be a list in {tier}")
            all_valid = False
            continue

        tier_valid = validate_tests(tests, tier, seen_tiles)
        all_valid = all_valid and tier_valid
        if tier_valid:
            print(f"  ✅ {tier} tier configuration valid")

    return all_valid


def main() -> int:
    config_paths = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATHS

    # Tile identity (category, label) must be unique across ALL config files,
    # since the dashboard merges every workflow's results onto one page.
    seen_tiles: dict = {}
    all_valid = True

    for config_path in config_paths:
        print(f"\n{'=' * 60}")
        print(f"Validating {config_path}")
        print('=' * 60)
        all_valid = validate_config(config_path, seen_tiles) and all_valid

    if all_valid:
        print("\n✅ Configuration file(s) valid!")
    else:
        print("\n❌ Configuration file(s) have errors")

    return 0 if all_valid else 1


if __name__ == '__main__':
    sys.exit(main())
