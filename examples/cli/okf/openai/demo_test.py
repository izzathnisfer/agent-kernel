"""End-to-end demo test — requires a live model key.

Drives the real CLI (``demo.py``) through the full three-role flow over the
committed ``sample_source/`` (four original synthetic detective stories), using the
``Test`` harness which starts the agents as a subprocess and switches between
them with ``!select <name>``:

1. **Curator** syncs ``sample_source/`` into the bundle. This is setup only —
   the sync mechanics are already unit-tested offline in ``test_okf.py``, so we
   only sanity-check that the run happened, not its correctness.
2. **Consumer** answers a question grounded in the freshly synced content.
3. **Producer** writes a new concept, then the **consumer re-reads** it, proving
   a write is visible end-to-end (all three agents share one in-process bundle).

``sample_bundle/`` is cleaned before the run so the curator always repopulates a
fresh bundle. Skipped when no OpenAI key is present, matching how the other CLI
examples behave in CI.
"""

import os
import shutil
from pathlib import Path

import pytest
import pytest_asyncio
from agentkernel.test import Test

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set"),
]

BUNDLE_DIR = Path(__file__).parent / "sample_bundle"


@pytest.fixture(scope="session", autouse=True)
def clean_bundle():
    """Start every run from an empty bundle so the curator repopulates it fresh.

    ``FileSystemStorage`` / the curator sync recreate the directory on demand, so
    removing it outright is safe.
    """
    shutil.rmtree(BUNDLE_DIR, ignore_errors=True)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def test_client():
    test = Test("demo.py")
    await test.start()
    try:
        yield test
    finally:
        await test.stop()


@pytest.mark.order(1)
async def test_curator_syncs_source(test_client):
    # Setup step: build the bundle from the source. Curator correctness is covered
    # by the offline sync tests in test_okf.py, so this only confirms the sync ran.
    await test_client.send("!select curator")
    # Single-line message: the Test harness pairs one send() with one CLI turn,
    # and the CLI reads stdin one line at a time (skipping blank lines), so a
    # multi-line prompt would desync request/response for the rest of the run.
    #
    # Ask for the VERBATIM mirror (sync_source) rather than the curator's
    # categorized import, so this setup step stays deterministic — sync_source()
    # mirrors the source layout under synced/ and its correctness is unit-tested
    # offline in test_okf.py. Assert against that output, not the model's phrasing.
    await test_client.send("Do a verbatim import of the source folder by calling sync_source() — no reorganizing.")
    response = (test_client.last_agent_response or "").lower()
    assert "sync" in response
    assert (BUNDLE_DIR / "synced").is_dir()
    assert any((BUNDLE_DIR / "synced").rglob("*.md"))


@pytest.mark.order(2)
async def test_consumer_answers_from_bundle(test_client):
    await test_client.send("!select consumer")
    await test_client.send(
        "In the story 'The Lighthouse Cipher', what is the name of the thief Inspector "
        "Merrow refers to as 'the Magpie'? Answer in one short sentence."
    )
    response = (test_client.last_agent_response or "").lower()
    assert "coral deveraux" in response


@pytest.mark.order(3)
async def test_producer_writes_then_consumer_reads(test_client):
    # Producer writes a new concept with a distinctive, checkable fact.
    await test_client.send("!select producer")
    await test_client.send(
        "Create a concept at summaries/lighthouse_cipher.md summarizing 'The Lighthouse "
        "Cipher'. Use type Summary and a title, and make sure the summary mentions "
        "Coral Deveraux. Then log the change."
    )
    producer_response = (test_client.last_agent_response or "").lower()
    assert "wrote" in producer_response or "summaries/lighthouse_cipher.md" in producer_response

    # The consumer re-reads the just-written doc: proves the producer's write is
    # visible end-to-end (the agents share one in-process, write-through bundle).
    await test_client.send("!select consumer")
    await test_client.send("Read summaries/lighthouse_cipher.md and tell me which thief it is about.")
    consumer_response = (test_client.last_agent_response or "").lower()
    assert "coral deveraux" in consumer_response
