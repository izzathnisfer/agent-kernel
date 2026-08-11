import asyncio
import os
import uuid

import httpx
import pytest
import pytest_asyncio
from agentkernel.test import Test

pytestmark = pytest.mark.asyncio(loop_scope="session")  # uses a single session for all tests

ALICE = "alice-token"
BOB = "bob-token"

# Lambda cold starts make the first calls flaky, so retry transient failures.
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 5

# EventBridge Scheduler's finest granularity is one minute, so a fire cannot be observed
# any sooner than that. Allow three minutes for the fire plus the agent run.
RUN_POLL_TIMEOUT_SECONDS = 180
RUN_POLL_INTERVAL_SECONDS = 10


class APITestClient:
    def __init__(self, url):
        self.url = url
        self.session_id = str(uuid.uuid4())

    @staticmethod
    def _headers(token):
        # Every route sits behind the API Gateway authorizer, so even ordinary chat needs
        # a token in this deployment.
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def send(self, prompt, additional_context=None):
        """Send an ordinary chat request and return the agent's answer."""
        payload = {
            "prompt": prompt,
            "session_id": self.session_id,
            "agent": "assistant",
            "additional_context": additional_context,
        }
        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(self.url, json=payload, headers=self._headers(ALICE))
                    resp.raise_for_status()
                    return resp.json().get("result", "")
            except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
                last_error = e
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
        raise last_error

    async def create_scheduled_task(self, prompt, schedule, token=ALICE, agent="assistant"):
        """Create a scheduled task through the chat endpoint. Returns the raw response."""
        payload = {"prompt": prompt, "agent": agent, "schedule": schedule}
        async with httpx.AsyncClient(timeout=60.0) as client:
            return await client.post(self.url, json=payload, headers=self._headers(token))

    async def schedule_request(self, method, path="", token=ALICE, json_body=None):
        """Call a /api/v1/schedule management route. Returns the raw response."""
        # agent_invoke_url points at /api/v1/chat; the schedule routes are its siblings.
        base = self.url.rsplit("/", 1)[0]
        async with httpx.AsyncClient(timeout=60.0) as client:
            return await client.request(
                method,
                f"{base}/schedule{path}",
                headers=self._headers(token),
                json=json_body,
            )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def http_client():
    endpoint = os.getenv("AK_TEST_ENDPOINT")
    yield APITestClient(endpoint)


@pytest.fixture(scope="session")
def created_task():
    """Carries the scheduled task created in one ordered test into the next."""
    return {}


@pytest.mark.asyncio
@pytest.mark.order(1)
async def test_ordinary_chat_still_works(http_client):
    """Enabling scheduling must not change the normal request path."""
    response = await http_client.send("Who won the 1996 cricket world cup?")
    Test.compare(response, ["Sri Lanka won the 1996 cricket world cup."])


@pytest.mark.asyncio
@pytest.mark.order(2)
async def test_create_requires_authentication(http_client):
    """Every scheduled task needs an owner, so an unauthenticated create is rejected.

    On serverless the API Gateway authorizer rejects the call before it reaches the
    Lambda; the app's own check is the second line of defence.
    """
    resp = await http_client.create_scheduled_task(
        "This should never be scheduled",
        {"cron": "0 9 * * ? *"},
        token=None,
    )
    assert resp.status_code in (401, 403)

    resp = await http_client.create_scheduled_task(
        "This should never be scheduled",
        {"cron": "0 9 * * ? *"},
        token="not-a-real-token",
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
@pytest.mark.order(3)
async def test_create_returns_acknowledgement(http_client, created_task):
    """A chat body with a schedule block registers a task instead of running it."""
    resp = await http_client.create_scheduled_task(
        "Summarise the overnight error log",
        {"cron": "0 9 * * ? *", "mode": "per_run"},
    )
    assert resp.status_code == 201

    ack = resp.json()
    assert ack["status"] == "SCHEDULED"
    assert ack["scheduled_task_id"]
    assert ack["scheduled_task_version"]

    created_task["id"] = ack["scheduled_task_id"]


@pytest.mark.asyncio
@pytest.mark.order(4)
async def test_listing_is_scoped_to_the_owner(http_client, created_task):
    """A caller sees only their own scheduled tasks."""
    task_id = created_task["id"]

    resp = await http_client.schedule_request("GET", token=ALICE)
    assert resp.status_code == 200
    assert task_id in [task["scheduled_task_id"] for task in resp.json()["scheduled_tasks"]]

    resp = await http_client.schedule_request("GET", token=BOB)
    assert resp.status_code == 200
    assert task_id not in [task["scheduled_task_id"] for task in resp.json()["scheduled_tasks"]]


@pytest.mark.asyncio
@pytest.mark.order(5)
async def test_non_owner_cannot_read_or_mutate(http_client, created_task):
    """Ownership is checked on every management route, not just the listing."""
    path = f"/{created_task['id']}"

    assert (await http_client.schedule_request("GET", path, token=BOB)).status_code == 403
    assert (
        await http_client.schedule_request("PUT", path, token=BOB, json_body={"prompt": "hijacked"})
    ).status_code == 403
    assert (await http_client.schedule_request("DELETE", path, token=BOB)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.order(6)
async def test_update_then_delete(http_client, created_task):
    """Update rewrites the definition; delete makes the task unreadable."""
    path = f"/{created_task['id']}"

    resp = await http_client.schedule_request(
        "PUT", path, json_body={"prompt": "Summarise the overnight warnings instead"}
    )
    assert resp.status_code == 200

    resp = await http_client.schedule_request("GET", path)
    assert resp.status_code == 200
    assert resp.json()["message"]["prompt"] == "Summarise the overnight warnings instead"

    assert (await http_client.schedule_request("DELETE", path)).status_code == 200
    assert (await http_client.schedule_request("GET", path)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.order(7)
async def test_a_scheduled_task_actually_runs(http_client):
    """The only test that proves a fire reaches the agent runner.

    A one-minute rate schedule fires, the runner executes it as an ordinary message, and
    the response handler records the outcome back onto the row.
    """
    resp = await http_client.create_scheduled_task(
        "Reply with the single word: tick",
        {"rate": "1 minute", "mode": "per_run"},
    )
    assert resp.status_code == 201
    task_id = resp.json()["scheduled_task_id"]

    try:
        last_run_status = None
        for _ in range(RUN_POLL_TIMEOUT_SECONDS // RUN_POLL_INTERVAL_SECONDS):
            await asyncio.sleep(RUN_POLL_INTERVAL_SECONDS)
            resp = await http_client.schedule_request("GET", f"/{task_id}")
            assert resp.status_code == 200
            last_run_status = resp.json().get("last_run_status")
            if last_run_status is not None:
                break

        assert last_run_status == "COMPLETED", f"scheduled task did not complete a run in {RUN_POLL_TIMEOUT_SECONDS}s"
    finally:
        # A rate schedule fires forever; always tear it down.
        await http_client.schedule_request("DELETE", f"/{task_id}")
