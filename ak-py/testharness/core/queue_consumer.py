import asyncio
import inspect
import logging
import time
from abc import abstractmethod
from typing import Any, Dict, List

from agentkernel.deployment.common import QueueConsumer, ThreadRunner

from .queue_handler import LocalQueueHandler


class LocalQueueConsumer(QueueConsumer):
    """
    Base class for local services that consume the SQLite-backed queue.

    Mirrors ECSSQSConsumer for the local backend: extend this class, implement
    process_message and on_permanent_failure, then call run() as the process
    entry-point. Unlike ECSSQSConsumer, storage access is delegated entirely to
    LocalQueueHandler — this class only knows retry/failure orchestration.

    Contract for on_permanent_failure implementations: must be internally
    defensive (catch their own exceptions). If on_permanent_failure raises, the
    message is NOT deleted and will re-enter the permanent-failure path on the
    next visibility-timeout cycle.
    """

    max_receive_count: int = 3  # overridden by classes that inherit this
    num_consumers: int = 1  # overridden by classes that inherit this
    batch_size: int = 10
    visibility_timeout: float = 30
    # LocalQueue has no SQS-style long-poll wait, so an empty poll returns instantly —
    # this keeps the loop from busy-spinning at 100% CPU while the queue is idle.
    empty_poll_sleep: float = 0.1
    _log = logging.getLogger("ak.local.queueconsumer")

    @classmethod
    @abstractmethod
    def get_queue_name(cls) -> str:
        """
        Return the queue name to poll ('input' or 'output').

        LocalQueueConsumer's equivalent of ECSSQSConsumer.get_queue_url() — the one thing
        each subclass still has to declare.
        """
        raise NotImplementedError

    @classmethod
    def poll(cls) -> List[Dict[str, Any]]:
        return LocalQueueHandler.receive_messages(
            queue_name=cls.get_queue_name(),
            batch_size=cls.batch_size,
            visibility_timeout=cls.visibility_timeout,
        )

    @classmethod
    @abstractmethod
    def process_message(cls, record: Dict[str, Any]) -> None:
        """Process one local queue message. record is shaped like LocalQueue.receive()'s output."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def on_permanent_failure(cls, record: Dict[str, Any]) -> None:
        """
        Called when a message's ApproximateReceiveCount exceeds max_receive_count.
        The message is deleted from the queue immediately after this returns.

        Implementations MUST catch their own exceptions. If this method raises,
        the message is not deleted and will loop back to this path indefinitely.
        """
        raise NotImplementedError

    @classmethod
    def delete_message(cls, record: Dict[str, Any]) -> None:
        LocalQueueHandler.delete_message(cls.get_queue_name(), record["MessageId"])

    @classmethod
    def _process_single(cls, msg: dict) -> None:
        message_id = msg.get("MessageId", "<unknown>")
        receive_count = int(msg.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
        cls._log.debug(f"Processing message {message_id} (receive_count={receive_count})")
        try:
            if receive_count > cls.max_receive_count:
                cls._log.warning(f"Message {message_id} exceeded max_receive_count ({receive_count} > {cls.max_receive_count})")
                cls.on_permanent_failure(msg)
                cls.delete_message(msg)
                return

            underlying_fn = getattr(cls.process_message, "__func__", cls.process_message)
            if inspect.iscoroutinefunction(underlying_fn):
                asyncio.run(cls.process_message(msg))
            else:
                cls.process_message(msg)

            cls.delete_message(msg)
            cls._log.debug(f"Processed and deleted message {message_id}")

        except Exception:
            cls._log.exception(f"Failed to process message {message_id} — leaving in queue for visibility-timeout retry")
            # Do NOT delete — visibility timeout returns it for retry

    @classmethod
    def _consumer_loop(cls) -> None:
        while not ThreadRunner.shutdown_event.is_set():
            try:
                messages = cls.poll()
            except Exception:
                cls._log.exception("Unexpected error in poll loop — retrying in 5 s")
                time.sleep(5)
                continue

            if messages:
                cls._log.debug(f"Processing batch of {len(messages)} message(s)")
                for msg in messages:
                    cls._process_single(msg)
            else:
                time.sleep(cls.empty_poll_sleep)

    @classmethod
    def run(cls) -> None:
        """
        Block forever, polling the queue. Call as the process entry-point.

        Starts `num_consumers` long-lived threads, each independently
        polling and processing messages in a loop.
        """
        queue_name = cls.get_queue_name()
        num_consumers = cls.num_consumers
        if num_consumers < 1:
            raise ValueError(f"{cls.__name__}: num_consumers must be >= 1, got {num_consumers}")
        cls._log.info(f"{cls.__name__} starting — queue: {queue_name}, consumers: {num_consumers}")

        ThreadRunner.run(
            tasks=[
                ThreadRunner.Task(
                    execution_function=cls._consumer_loop,
                    thread_name=f"local-queue-consumer-{queue_name}-{i}",
                    stop_all_on_failure=True,
                    graceful=True,
                )
                for i in range(num_consumers)
            ],
            max_workers=num_consumers,
        )
