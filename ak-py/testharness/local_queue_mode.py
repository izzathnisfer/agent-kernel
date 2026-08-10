from __future__ import annotations

import logging
import socket
import threading
import time
from typing import List, Optional

import uvicorn

from agentkernel.core.config import AKConfig

from .core.api.rest_api import LocalRestAPI
from .local_agent_runner import LocalAgentRunner
from .local_output_consumer import LocalOutputConsumer


class LocalQueueMode:
    """
    Starts the REST API, agent runner, and output consumer as daemon threads in the
    current process — a single-process convenience wrapper around the same local queue
    mode topology LocalIOHandler/LocalAgentRunner/LocalOutputConsumer use across two
    processes, for fast iteration and pytest (no bash script, no second process).

    Deliberately does NOT reuse LocalIOHandler.run()/LocalQueueConsumer.run(): those
    drive ThreadRunner.run(), whose shutdown_event is a process-wide singleton — setting
    it (the natural-looking way to stop this wrapper) makes every ThreadRunner.run() call
    in the *whole process* call os._exit(1) once its own tasks drain, which would kill the
    entire pytest run, not just this wrapper's threads. Instead, this class owns a private
    threading.Event and drives its own poll loops plus a stoppable uvicorn.Server directly.

    One poll thread per role (not execution.queues.*.no_of_consumers many) — enough for
    local test-mode throughput; use the two-process LocalIOHandler/LocalAgentRunner entry
    points instead when consumer concurrency itself is what's under test.

    Usage::

        with LocalQueueMode():
            httpx.post(f"http://{host}:{port}/api/v1/chat", json={...})
    """

    _log = logging.getLogger("ak.local.queuemode")

    def __init__(self, startup_timeout: float = 10.0, poll_interval: float = 0.05):
        self._startup_timeout = startup_timeout
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._threads: List[threading.Thread] = []
        self._server: Optional[uvicorn.Server] = None

    def __enter__(self) -> "LocalQueueMode":
        self._stop_event.clear()
        config = AKConfig.get()

        self._server = self._build_server(config)
        server_thread = threading.Thread(target=self._server.run, name="local-rest-api", daemon=True)
        agent_thread = threading.Thread(target=lambda: self._poll_loop(LocalAgentRunner), name="local-agent-runner", daemon=True)
        output_thread = threading.Thread(target=lambda: self._poll_loop(LocalOutputConsumer), name="local-output-consumer", daemon=True)

        self._threads = [server_thread, agent_thread, output_thread]
        for thread in self._threads:
            thread.start()

        self._wait_for_port(config.api.host, config.api.port)
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop_event.set()
        if self._server is not None:
            self._server.should_exit = True
        for thread in self._threads:
            thread.join(timeout=5)

    def _poll_loop(self, consumer) -> None:
        while not self._stop_event.is_set():
            try:
                messages = consumer.poll()
            except Exception:
                self._log.exception(f"{consumer.__name__}: unexpected error in poll loop")
                time.sleep(1)
                continue

            if messages:
                for msg in messages:
                    consumer._process_single(msg)
            else:
                time.sleep(self._poll_interval)

    @staticmethod
    def _build_server(config) -> uvicorn.Server:
        handler = LocalRestAPI.get_default_handlers()[0]
        app = LocalRestAPI._create_app(routers=[handler.get_router()])
        return uvicorn.Server(uvicorn.Config(app=app, host=config.api.host, port=config.api.port, reload=False))

    def _wait_for_port(self, host: str, port: int) -> None:
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.05)
        raise TimeoutError(f"LocalQueueMode: REST API did not start listening on {host}:{port} within {self._startup_timeout}s")
