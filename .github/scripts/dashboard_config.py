#!/usr/bin/env python3
"""
Shared helpers for the Integration Status dashboard configuration.

Test entries in .github/test-config.yaml and .github/integration-test-config.yaml
may carry a `dashboard` block (a mapping, a list of mappings, or the literal
'hidden') that controls how the test appears on https://kernel.yaala.ai/status.
This module resolves that block — including the defaults applied when it is
omitted — so the validator and the status publisher agree on the result.

See docs/specs/integration-status.md.
"""

from typing import Optional

# Section used when a test has no explicit dashboard category
DEFAULT_CATEGORY_BY_TYPE = {
    'cli': 'Core & Examples',
    'api': 'Core & Examples',
    'memory': 'Core & Examples',
    'containerized': 'Core & Examples',
    'aws-serverless': 'AWS Serverless',
    'aws-containerized': 'AWS Containerized',
    'azure-serverless': 'Azure Serverless',
    'azure-containerized': 'Azure Containerized',
    'gcp-serverless': 'GCP Serverless',
    'gcp-containerized': 'GCP Containerized',
}

ALLOWED_DASHBOARD_KEYS = {'category', 'label', 'description', 'logo'}


def default_label(path: str) -> str:
    """Derive a tile label from a test path: last two segments, minus 'examples/'."""
    segments = [s for s in path.split('/') if s and s != 'examples']
    return ' / '.join(segments[-2:]) if segments else path


def default_category(test_type: str) -> str:
    return DEFAULT_CATEGORY_BY_TYPE.get(test_type, 'Other')


def resolve_dashboard_entries(test: dict) -> list[dict]:
    """
    Resolve a test's dashboard tiles.

    Returns a list of {'category', 'label', 'description'} dicts — empty when the
    test is hidden. Assumes the config already passed validation; malformed blocks
    raise ValueError.
    """
    dashboard = test.get('dashboard')

    if dashboard == 'hidden':
        return []

    if dashboard is None:
        return [{
            'category': default_category(test.get('type', '')),
            'label': default_label(test.get('path', '')),
            'description': None,
            'logo': None,
        }]

    if isinstance(dashboard, dict):
        dashboard = [dashboard]

    if not isinstance(dashboard, list):
        raise ValueError(f"Invalid dashboard block for {test.get('path')}: {dashboard!r}")

    entries = []
    for item in dashboard:
        if not isinstance(item, dict):
            raise ValueError(f"Invalid dashboard entry for {test.get('path')}: {item!r}")
        entries.append({
            'category': item.get('category') or default_category(test.get('type', '')),
            'label': item.get('label') or default_label(test.get('path', '')),
            'description': item.get('description'),
            # File under docs/static/img/integrations/, or a site-absolute
            # path starting with '/'. When omitted, the status page's
            # keyword/type fallback picks a logo.
            'logo': item.get('logo'),
        })
    return entries


def validate_dashboard_block(test: dict) -> list[str]:
    """
    Validate a test's `dashboard` block in isolation.

    Returns a list of error messages (empty when valid).
    """
    errors: list[str] = []
    dashboard = test.get('dashboard')
    path = test.get('path', '<unknown path>')

    if dashboard is None or dashboard == 'hidden':
        return errors

    if isinstance(dashboard, dict):
        items = [dashboard]
    elif isinstance(dashboard, list):
        items = dashboard
        if not items:
            errors.append(f"{path}: dashboard list must not be empty (use 'hidden' to opt out)")
    else:
        errors.append(
            f"{path}: dashboard must be a mapping, a list of mappings, or 'hidden' "
            f"(got {type(dashboard).__name__})"
        )
        return errors

    seen_categories: set[str] = set()
    for idx, item in enumerate(items, 1):
        where = f"{path} dashboard entry {idx}"
        if not isinstance(item, dict):
            errors.append(f"{where}: must be a mapping")
            continue

        unknown = set(item.keys()) - ALLOWED_DASHBOARD_KEYS
        if unknown:
            errors.append(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")

        for field in ('category', 'label'):
            value = item.get(field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                errors.append(f"{where}: '{field}' must be a non-empty string")

        description = item.get('description')
        if description is not None and not isinstance(description, str):
            errors.append(f"{where}: 'description' must be a string")

        logo = item.get('logo')
        if logo is not None and (not isinstance(logo, str) or not logo.strip()):
            errors.append(f"{where}: 'logo' must be a non-empty string")

        if isinstance(item, dict):
            category = item.get('category') or default_category(test.get('type', ''))
            if category in seen_categories:
                errors.append(f"{where}: duplicate category '{category}' within the same test")
            seen_categories.add(category)

    return errors
