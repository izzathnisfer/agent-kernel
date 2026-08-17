import pytest
import pytest_asyncio
from agentkernel.test import CLIClient, Test

pytestmark = pytest.mark.asyncio(loop_scope="session")  # uses a single session for all tests


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def test_client():
    client = CLIClient("demo.py")
    await client.start()
    try:
        yield client
    finally:
        await client.stop()


@pytest.mark.order(1)
async def test_workspace_profile_persists(test_client):
    await test_client.send("In the workspace, write a file marker.txt containing exactly: kept")
    await test_client.send("Read marker.txt from the workspace and reply with only its contents.")
    Test.compare(test_client.last_agent_response, ["kept"])


@pytest.mark.order(2)
async def test_scratch_profile_is_isolated(test_client):
    # marker.txt lives in the persistent workspace profile, not in a fresh scratch sandbox.
    await test_client.send(
        "Using the scratch profile (a fresh throwaway sandbox), check whether marker.txt exists. "
        "Reply with exactly 'present' if it exists, or 'absent' if it does not."
    )
    Test.compare(test_client.last_agent_response, ["absent"])
