"""REST request handler exposing the scheduled-task management endpoints.

These routes query and manage **already-created** scheduled tasks. Creation is the chat
endpoint's job — there is no new creation endpoint.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..auth import Authoriser
from ..core.model import ScheduleSpec
from ..core.util.factory import AKConfigError
from ..scheduler import ScheduledTaskService, SchedulerError, SchedulerFactory, http_status_for
from .handler import BearerIdentityMixin, RESTRequestHandler


class ScheduleUpdateRequest(BaseModel):
    """Body of ``PUT /api/v1/schedule/{scheduled_task_id}``.

    Every field is optional: an omitted field keeps its current value.
    """

    schedule: Optional[ScheduleSpec] = None
    prompt: Optional[str] = None
    agent: Optional[str] = None


class ScheduleRESTRequestHandler(BearerIdentityMixin, RESTRequestHandler):
    """
    API router that exposes endpoints to manage scheduled tasks.

    Endpoints:
    - GET /api/v1/schedule: List the caller's scheduled tasks
    - GET /api/v1/schedule/{scheduled_task_id}: Definition plus last-run status
    - PUT /api/v1/schedule/{scheduled_task_id}: Change the schedule or the message
    - DELETE /api/v1/schedule/{scheduled_task_id}: Stop the task and soft-delete the row

    An ``Authoriser`` is mandatory: every route acts on a specific owner's tasks, so none of
    them may run without a resolved identity.
    """

    SCHEDULE_PATH = "/api/v1/schedule"
    SCHEDULE_ITEM_PATH = "/api/v1/schedule/{scheduled_task_id}"

    def __init__(self, authoriser: Optional[Authoriser] = None, service: Optional[ScheduledTaskService] = None):
        """
        :param authoriser: Resolves a Bearer token to the owning identity. Required.
        :param service: The shared scheduling service; resolved from config when omitted.
        :raises AKConfigError: No Authoriser was supplied, so ownership could not be enforced.
        """
        self._log = logging.getLogger("ak.api.schedule")
        if authoriser is None:
            raise AKConfigError(
                "scheduler.enabled requires an Authoriser on the schedule routes — every scheduled task must have an authenticated owner"
            )
        self._authoriser = authoriser
        self._service = service or SchedulerFactory.service()

    def get_router(self) -> APIRouter:
        """
        Returns the APIRouter instance.
        """
        router = APIRouter()

        @router.get(self.SCHEDULE_PATH)
        def list_scheduled_tasks(request: Request, limit: Optional[int] = None, cursor: Optional[str] = None):
            self._require_service()
            owner_id = self._resolve_user(request)
            # Mapped like every other route: a bad cursor is client input, so it answers 400
            # rather than escaping as an unhandled 500.
            with self._mapped_errors():
                page = self._service.list(owner_id=owner_id, limit=limit, cursor=cursor)
            return {
                "scheduled_tasks": [task.model_dump(mode="json") for task in page.items],
                "next_cursor": page.next_cursor,
            }

        @router.get(self.SCHEDULE_ITEM_PATH)
        def get_scheduled_task(scheduled_task_id: str, request: Request):
            self._require_service()
            owner_id = self._resolve_user(request)
            with self._mapped_errors():
                return self._service.get(scheduled_task_id, owner_id=owner_id).model_dump(mode="json")

        @router.put(self.SCHEDULE_ITEM_PATH)
        def update_scheduled_task(scheduled_task_id: str, body: ScheduleUpdateRequest, request: Request):
            self._require_service()
            owner_id = self._resolve_user(request)
            with self._mapped_errors():
                task = self._service.update(
                    scheduled_task_id,
                    owner_id=owner_id,
                    spec=body.schedule,
                    prompt=body.prompt,
                    agent=body.agent,
                )
            return task.model_dump(mode="json")

        @router.delete(self.SCHEDULE_ITEM_PATH)
        def delete_scheduled_task(scheduled_task_id: str, request: Request):
            self._require_service()
            owner_id = self._resolve_user(request)
            with self._mapped_errors():
                self._service.delete(scheduled_task_id, owner_id=owner_id)
            return {"scheduled_task_id": scheduled_task_id, "deleted": True}

        return router

    def _require_service(self) -> None:
        """Reject the request when the handler was built without a service (scheduling disabled).

        :raises HTTPException: 404 when scheduling is not enabled for this deployment.
        """
        if self._service is None:
            raise HTTPException(status_code=404, detail="Scheduling is not enabled for this deployment")

    @staticmethod
    def _mapped_errors():
        """Translate the service's typed failures into their HTTP statuses.

        :return: A context manager that re-raises service errors as HTTPExceptions.
        """
        return _ServiceErrorMapping()


class _ServiceErrorMapping:
    """Re-raises ``ScheduledTaskService`` failures as ``HTTPException``s.

    The status itself comes from ``scheduler.http_status_for``, which every other surface reads
    too, so this class only translates the number into FastAPI's envelope.
    """

    def __enter__(self) -> "_ServiceErrorMapping":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if exc_type is None or not issubclass(exc_type, SchedulerError):
            return False
        raise HTTPException(status_code=http_status_for(exc_value), detail=str(exc_value)) from exc_value
