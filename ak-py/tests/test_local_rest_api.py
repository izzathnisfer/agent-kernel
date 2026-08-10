from unittest.mock import Mock, patch

import pytest

from testharness.core.api.rest_api import LocalQueueRequestHandler, LocalRestAPI
from testharness.core.queue_handler import LocalQueueHandler
from testharness.core.response_store import LocalResponseStore


@pytest.fixture
def config(tmp_path):
    cfg = Mock()
    cfg.execution.queues.output.url = str(tmp_path / "queue.db")
    cfg.api.max_file_size = 10_000_000
    return cfg


class TestLocalQueueRequestHandler:
    def test_get_queue_handler_returns_local_queue_handler(self, config):
        with (
            patch("agentkernel.deployment.common.rest_handler.AKConfig.get", return_value=config),
            patch("agentkernel.api.handler.Config.get", return_value=config),
        ):
            handler = LocalQueueRequestHandler()

        assert handler.get_queue_handler() is LocalQueueHandler

    def test_get_response_store_returns_local_response_store(self, config):
        with (
            patch("agentkernel.deployment.common.rest_handler.AKConfig.get", return_value=config),
            patch("agentkernel.api.handler.Config.get", return_value=config),
        ):
            handler = LocalQueueRequestHandler()
            store = handler.get_response_store()

        assert isinstance(store, LocalResponseStore)

    def test_get_response_store_is_cached(self, config):
        with (
            patch("agentkernel.deployment.common.rest_handler.AKConfig.get", return_value=config),
            patch("agentkernel.api.handler.Config.get", return_value=config),
        ):
            handler = LocalQueueRequestHandler()
            first = handler.get_response_store()
            second = handler.get_response_store()

        assert first is second


class TestLocalRestAPI:
    def test_get_default_handlers_returns_local_queue_request_handler(self, config):
        with (
            patch("agentkernel.deployment.common.rest_handler.AKConfig.get", return_value=config),
            patch("agentkernel.api.handler.Config.get", return_value=config),
        ):
            [handler] = LocalRestAPI.get_default_handlers()

        assert isinstance(handler, LocalQueueRequestHandler)
