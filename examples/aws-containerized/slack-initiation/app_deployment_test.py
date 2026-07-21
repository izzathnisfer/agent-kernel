"""
Live deployment smoke test for the slack-initiation containerized example.

Runs only against an already-deployed stack, using AK_TEST_ENDPOINT and
SLACK_SIGNING_SECRET — both supplied by the weekly integration test workflow
(see .github/integration-test-config.yaml and
.github/workflows/integration-test-weekly.yaml, which runs
.github/scripts/run_single_test.py's deploy -> test -> destroy cycle). Skipped
when those aren't set, so a plain local `uv run pytest` here still runs
app_test.py's local unit tests unaffected.

Unlike app_test.py (which exercises the request handler / output consumer
in-process against fakes), this test sends a real, signed HTTP request to the
deployed API Gateway → ALB → ECS REST service — the only way to catch a broken
deployment (e.g. a missing dependency in the image, or a missing
gateway_endpoints route for /slack/events), since a local-only test imports
the same already-working source tree and can never see packaging or routing
problems in the actually deployed stack.
"""

import hashlib
import hmac
import json
import os
import time

import httpx
import pytest

AK_TEST_ENDPOINT = os.getenv("AK_TEST_ENDPOINT")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

# AK_TEST_ENDPOINT is the agent_invoke_url Terraform output —
# {stage}/api/v1/chat. The containerized module mounts every custom
# gateway_endpoints path under the same {api_base_path}/{api_version} prefix,
# so the Slack webhook lives at {stage}/api/v1/slack/events: swap the trailing
# agent endpoint for the webhook path (main.tf's default agent_endpoint is "chat").
SLACK_EVENTS_URL = f"{AK_TEST_ENDPOINT.removesuffix('/chat')}/slack/events" if AK_TEST_ENDPOINT else None

pytestmark = pytest.mark.skipif(
    not AK_TEST_ENDPOINT or not SLACK_SIGNING_SECRET,
    reason="requires a deployed endpoint (AK_TEST_ENDPOINT) and SLACK_SIGNING_SECRET — set by the weekly integration test workflow",
)


def _sign(body: bytes) -> dict:
    """Signs a request body the way Slack does, using the same signing secret
    Terraform gave the deployed REST service (deploy/main.tf's SLACK_SIGNING_SECRET
    environment variable, sourced from the same CI secret as this test)."""
    timestamp = str(int(time.time()))
    base = b"v0:" + timestamp.encode() + b":" + body
    signature = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": signature,
    }


def test_slack_events_endpoint_accepts_url_verification():
    """
    Round-trips Slack's url_verification handshake against the live endpoint.
    A 200 with the challenge echoed back proves the deployed ECS service
    actually imported and ran its code end to end (API Gateway route,
    overwrite_path rewrite to /slack/events, Bolt signature verification,
    JSON handling) — cheap, side-effect-free, and would fail for a missing
    gateway_endpoints route or a broken container image.
    """
    challenge = "ci-smoke-test-challenge"
    body = json.dumps({"type": "url_verification", "challenge": challenge}).encode()

    resp = httpx.post(SLACK_EVENTS_URL, content=body, headers=_sign(body), timeout=30.0)

    assert resp.status_code == 200
    assert resp.json()["challenge"] == challenge


def test_slack_events_endpoint_rejects_unsigned_request():
    """Confirms signature verification is actually active on the deployed service."""
    body = json.dumps({"type": "url_verification", "challenge": "x"}).encode()

    resp = httpx.post(
        SLACK_EVENTS_URL,
        content=body,
        headers={"content-type": "application/json"},
        timeout=30.0,
    )

    assert resp.status_code == 401
