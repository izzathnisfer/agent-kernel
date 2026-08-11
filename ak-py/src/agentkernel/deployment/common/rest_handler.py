import asyncio
import logging
import uuid
from abc import abstractmethod
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ...api.handler import AgentRESTRequestHandler, BearerIdentityMixin
from ...core.config import AKConfig
from ...core.model import BaseRunRequest, ExecutionMode
from ...core.thread import Authoriser
from ...core.util.factory import AKConfigError
from ...scheduler import SchedulerConflictError, SchedulerFactory, SchedulerPermissionError, ScheduleValidationError
from .queue_handler import QueueHandler
from .response_store import ResponseStore


class RestHandler(BearerIdentityMixin, AgentRESTRequestHandler):
    """Queue-aware REST handler; adds queue-based enqueue/poll chat routes when an input queue is configured.

    A chat body carrying a ``schedule`` block is registered to run later instead of being
    enqueued for execution. That branch lives in this provider-agnostic base because it
    contains nothing AWS-specific, so any queue-mode target inheriting it gets the create path.
    """

    # Poll route reuses the chat path (GET vs the enqueue POST).
    CHAT_POLL_PATH = AgentRESTRequestHandler.CHAT_PATH

    def __init__(self, logger_name: str = "ak.deployment.queue_handler", authoriser: Optional[Authoriser] = None):
        """
        :param logger_name: Deployment-specific logger name.
        :param authoriser: Resolves the owner of a scheduled task. Required when
            ``scheduler.enabled``, so a deployment that cannot establish ownership fails at
            startup rather than at the first create.
        :raises AKConfigError: Scheduling is enabled but no Authoriser was supplied.
        """
        super().__init__()
        # Override base logger with the deployment-specific one.
        self._log = logging.getLogger(logger_name)
        self._config = AKConfig.get()
        self._authoriser = authoriser
        SchedulerFactory.validate_config()
        if SchedulerFactory.enabled() and authoriser is None:
            raise AKConfigError("scheduler.enabled requires an Authoriser on the chat route — every scheduled task must have an authenticated owner")
        self._schedule_service = SchedulerFactory.service()

    @abstractmethod
    def get_response_store(self) -> ResponseStore:
        """Return the ResponseStore implementation used to poll for responses."""
        pass

    @abstractmethod
    def get_queue_handler(self) -> QueueHandler:
        """Return the QueueHandler implementation used to enqueue requests."""
        pass

    def _is_queue_mode(self) -> bool:
        """True when an input queue is configured (enqueue mode); False for direct mode."""
        return self._config.execution.queues.input.url is not None

    async def enqueue_and_wait(self, body: BaseRunRequest, request: Request = None):
        """Enqueue request; REST_SYNC waits for the response, REST_ASYNC returns request_id immediately.

        A body carrying a ``schedule`` block is registered instead: nothing is enqueued, and
        the first message on the input queue appears when the timer fires.

        :param body: The chat body, optionally carrying a ``schedule`` block.
        :param request: The incoming request, used to resolve the scheduled task's owner.
        """
        try:
            # Checked before session_id, which a scheduled create legitimately omits because
            # the service derives one.
            if body.schedule is not None:
                return await self._create_scheduled_task(body, request)

            if not body.session_id:
                raise HTTPException(status_code=400, detail="session_id is required")
            if not body.prompt:
                raise HTTPException(status_code=400, detail="prompt is required")

            # Unique request_id, distinct from session_id.
            request_id = str(uuid.uuid4())

            self._log.info(f"[REQUEST START] session_id={body.session_id}, request_id={request_id}, agent={body.agent}, prompt={body.prompt[:50]}")

            # Offload the sync send so it doesn't block the event loop.
            queue_result = await asyncio.to_thread(
                self.get_queue_handler().send_message_to_input_queue,
                message_body=body.model_dump(),
                attributes={"message_group_id": body.session_id, "message_deduplication_id": request_id},
                request_id=request_id,  # This becomes a custom message attribute
            )

            self._log.info(f"[ENQUEUED] MessageId={queue_result.get('MessageId')}, request_id={request_id}")

            if self._config.execution.mode == ExecutionMode.REST_SYNC:
                # Wait for the response in the response store.
                self._log.info(f"[WAITING] Polling response store for request_id={request_id}")

                response = await self.get_response_store().get_message_with_retry(request_id, True, async_mode=True)

                if not response:
                    raise HTTPException(
                        status_code=504,
                        detail={
                            "error": f"No response received for request_id: {request_id}",
                            "session_id": body.session_id,
                            "request_id": request_id,
                        },
                    )

                self._log.info(f"[RESPONSE FOUND] request_id={request_id}, response_keys={list(response.keys())}")

                return response.get("body", response)

            elif self._config.execution.mode == ExecutionMode.REST_ASYNC:
                # Return request_id for later polling.
                return {"status": "ACCEPTED", "request_id": request_id, "session_id": body.session_id}

            else:
                raise HTTPException(status_code=500, detail=f"Unsupported execution mode: {self._config.execution.mode}")

        except HTTPException:
            raise
        except Exception as e:
            self._log.error(f"Error processing request: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail={"error": str(e), "session_id": body.session_id if body else None})

    async def _create_scheduled_task(self, body: BaseRunRequest, request: Optional[Request]) -> JSONResponse:
        """Register a chat body to run later and acknowledge the registration.

        The acknowledgement is returned directly in both REST modes. There is no run to wait
        for, so the REST_SYNC response-store wait is skipped and run outcomes are read from
        ``GET /api/v1/schedule/{scheduled_task_id}`` rather than by polling.

        :param body: The chat body carrying the ``schedule`` block.
        :param request: The incoming request, used to resolve the owner.
        :return: A 201 carrying the acknowledgement.
        :raises HTTPException: 400 when scheduling is disabled or the schedule is invalid,
            401 when the caller is unauthenticated, 403/409 on an ownership or state conflict.
        """
        if self._schedule_service is None:
            raise HTTPException(status_code=400, detail="Scheduling is not enabled for this deployment")

        owner_id = self._resolve_user(request)
        try:
            ack = self._schedule_service.create(
                spec=body.schedule,
                prompt=body.prompt,
                agent=body.agent,
                owner_id=owner_id,
            )
        except ScheduleValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except SchedulerPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except SchedulerConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))

        self._log.info(f"[SCHEDULED] scheduled_task_id={ack.scheduled_task_id}, owner_id={owner_id}")
        return JSONResponse(status_code=201, content=ack.model_dump(mode="json", exclude_none=True))

    async def poll_response(self, request_id: Optional[str] = None, session_id: Optional[str] = None):
        """
        Poll for response (REST_ASYNC mode only).

        :param request_id: Specific request to poll for (query parameter)
        :param session_id: Optional session identifier (query parameter, used for logging/errors)
        """
        try:
            if self._config.execution.mode != ExecutionMode.REST_ASYNC:
                raise HTTPException(status_code=404, detail="GET endpoint only available in REST_ASYNC mode")

            if not request_id:
                raise HTTPException(status_code=400, detail={"error": "request_id is required", "session_id": session_id})

            self._log.info(f"Polling for response: request_id={request_id}, session_id={session_id}")

            response = await self.get_response_store().get_message_with_retry(request_id=request_id, get_and_delete=True, async_mode=True)

            if not response:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error": "NOT_FOUND",
                        "message": f"No response message found for request_id '{request_id}'. The message may be unavailable. Please try again.",
                        "request_id": request_id,
                        "session_id": session_id,
                    },
                )

            return response.get("body", response)

        except HTTPException:
            raise
        except Exception as e:
            self._log.error(f"Error polling response: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail={"error": str(e), "session_id": session_id})

    def get_router(self) -> APIRouter:
        """Return the APIRouter: inherited direct-mode routes, or agents plus queue-based chat routes in queue mode."""
        if not self._is_queue_mode():
            return super().get_router()

        router = APIRouter()
        router.add_api_route(self.AGENTS_PATH, self.list_agents, methods=["GET"])
        router.add_api_route(self.CHAT_PATH, self.enqueue_and_wait, methods=["POST"])
        router.add_api_route(self.CHAT_POLL_PATH, self.poll_response, methods=["GET"])
        return router
