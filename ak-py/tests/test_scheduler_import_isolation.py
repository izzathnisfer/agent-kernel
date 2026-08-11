"""The scheduler enablement check must fire on start, not on import.

``agentkernel.aws`` re-exports every AWS deployment class, so an entry point importing one
of them imports all of them — including the authorizer Lambda, which is deliberately given
no ``AK_SCHEDULER__*`` environment. A check in a class body would fail that import.
"""

import importlib
from unittest.mock import patch

import pytest
from conftest_scheduler import enable_scheduler_config, reset_scheduler_config

from agentkernel.core.util.factory import AKConfigError
from agentkernel.deployment.aws.containerized import akoutputconsumer
from agentkernel.deployment.aws.containerized.akoutputconsumer import ECSOutputConsumer
from agentkernel.deployment.aws.containerized.core import ECSSQSConsumer
from agentkernel.deployment.aws.serverless import akresponsehandler
from agentkernel.deployment.aws.serverless.akresponsehandler import ResponseHandler


@pytest.fixture(autouse=True)
def _clean_config():
    yield
    reset_scheduler_config()


@pytest.mark.parametrize("module", [akoutputconsumer, akresponsehandler])
def test_importing_a_deployment_module_does_not_validate_scheduler_config(module):
    """Re-executes the module body under config that validate_config() would reject."""
    enable_scheduler_config(group_name="")
    importlib.reload(module)  # must not raise


def test_output_consumer_run_validates_before_polling():
    enable_scheduler_config(group_name="")
    with patch.object(ECSSQSConsumer, "run") as base_run:
        with pytest.raises(AKConfigError, match="group_name"):
            ECSOutputConsumer.run()
    base_run.assert_not_called()


def test_output_consumer_run_polls_when_the_wiring_is_present():
    enable_scheduler_config()
    with patch.object(ECSSQSConsumer, "run") as base_run:
        ECSOutputConsumer.run()
    base_run.assert_called_once()


def test_response_handler_validates_before_processing():
    enable_scheduler_config(group_name="")
    with pytest.raises(AKConfigError, match="group_name"):
        ResponseHandler.handle({"Records": []}, None)


def test_response_handler_processes_when_the_wiring_is_present():
    enable_scheduler_config()
    assert ResponseHandler.handle({"Records": []}, None) == {"batchItemFailures": []}
