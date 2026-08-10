"""Scheduled-task management routes for the serverless REST surface.

The serverless REST layer is not FastAPI, so these mirror ``api/schedule.py``'s behaviour
against the hand-rolled ``{path: {method: handler}}`` table. They are registered under
their API Gateway resource templates, which ``RESTLambdaRouter.dispatch`` resolves through
its resource-template fallback.
"""

import json
import logging
import traceback
from typing import Any, Callable, Dict, Optional

from ......core.model import ScheduleSpec
from ......scheduler import (
    ScheduledTaskService,
    SchedulerConflictError,
    SchedulerFactory,
    SchedulerNotFoundError,
    SchedulerPermissionError,
    ScheduleValidationError,
)


class ScheduleEndpointsHandler:
    """Serves ``/schedule`` and ``/schedule/{scheduled_task_id}`` on API Gateway.

    Identity comes from the request authorizer's ``principalId``: Python cannot observe
    whether Terraform attached the authorizer, so a request without that context is
    rejected per request with 401 rather than at initialization.
    """

    LIST_PATH = "/schedule"
    ITEM_PATH = "/schedule/{scheduled_task_id}"

    _STATUS_BY_ERROR = (
        (SchedulerNotFoundError, 404),
        (SchedulerPermissionError, 403),
        (SchedulerConflictError, 409),
        (ScheduleValidationError, 400),
    )

    def __init__(self, service: Optional[ScheduledTaskService] = None):
        """
        :param service: The shared scheduling service; resolved from config when omitted.
        """
        self._log = logging.getLogger("ak.aws.lambda.schedule")
        self._service = service or SchedulerFactory.service()

    def get_routes(self) -> Dict[str, Dict[str, Callable[[Dict[str, Any], Any], Any]]]:
        """
        Return the schedule route table, keyed by API Gateway resource template.

        :return: Dictionary mapping resource templates → HTTP methods → handler functions
        """
        return {
            self.LIST_PATH: {"GET": self._handle_list},
            self.ITEM_PATH: {
                "GET": self._handle_get,
                "PUT": self._handle_update,
                "DELETE": self._handle_delete,
            },
        }

    # ------------------------------------------------------------------ routes

    def _handle_list(self, event: Dict[str, Any], context: Any) -> tuple[int, Dict[str, Any]]:
        """List the caller's scheduled tasks."""

        def operation(owner_id: str) -> Dict[str, Any]:
            parameters = event.get("queryStringParameters") or {}
            limit = parameters.get("limit")
            page = self._service.list(
                owner_id=owner_id,
                limit=int(limit) if limit else None,
                cursor=parameters.get("cursor"),
            )
            return {
                "scheduled_tasks": [task.model_dump(mode="json") for task in page.items],
                "next_cursor": page.next_cursor,
            }

        return self._handle(event, operation)

    def _handle_get(self, event: Dict[str, Any], context: Any) -> tuple[int, Dict[str, Any]]:
        """Return one scheduled task's definition and last-run status."""

        def operation(owner_id: str) -> Dict[str, Any]:
            return self._service.get(self._path_id(event), owner_id=owner_id).model_dump(mode="json")

        return self._handle(event, operation)

    def _handle_update(self, event: Dict[str, Any], context: Any) -> tuple[int, Dict[str, Any]]:
        """Change one scheduled task's schedule or message."""

        def operation(owner_id: str) -> Dict[str, Any]:
            body = self._parse_body(event)
            schedule = body.get("schedule")
            task = self._service.update(
                self._path_id(event),
                owner_id=owner_id,
                spec=ScheduleSpec.model_validate(schedule) if schedule else None,
                prompt=body.get("prompt"),
                agent=body.get("agent"),
            )
            return task.model_dump(mode="json")

        return self._handle(event, operation)

    def _handle_delete(self, event: Dict[str, Any], context: Any) -> tuple[int, Dict[str, Any]]:
        """Stop a scheduled task and soft-delete its row."""

        def operation(owner_id: str) -> Dict[str, Any]:
            scheduled_task_id = self._path_id(event)
            self._service.delete(scheduled_task_id, owner_id=owner_id)
            return {"scheduled_task_id": scheduled_task_id, "deleted": True}

        return self._handle(event, operation)

    # ------------------------------------------------------------------ helpers

    def _handle(self, event: Dict[str, Any], operation: Callable[[str], Dict[str, Any]]) -> tuple[int, Dict[str, Any]]:
        """Resolve the caller, run the operation, and map failures onto HTTP statuses.

        :param event: API Gateway event.
        :param operation: The route body, executed with the resolved owner id.
        :return: API Gateway formatted response (statusCode, body).
        """
        if self._service is None:
            return (404, {"error": "Scheduling is not enabled for this deployment"})

        owner_id = event.get("requestContext", {}).get("authorizer", {}).get("principalId")
        if not owner_id:
            return (401, {"error": "Unauthorized"})

        try:
            return (200, operation(owner_id))
        except Exception as e:
            for error_type, status_code in self._STATUS_BY_ERROR:
                if isinstance(e, error_type):
                    self._log.warning(f"Schedule request rejected with {status_code}: {e}")
                    return (status_code, {"error": str(e)})
            if isinstance(e, ValueError):
                self._log.warning(f"Invalid schedule request: {e}")
                return (400, {"error": str(e)})
            self._log.error(f"Schedule request failed: {e}\n{traceback.format_exc()}")
            return (500, {"error": "An unexpected error occurred"})

    @staticmethod
    def _path_id(event: Dict[str, Any]) -> str:
        """Read the scheduled task id from the matched path parameters.

        :param event: API Gateway event.
        :return: The scheduled task id.
        :raises ValueError: The path carried no id.
        """
        scheduled_task_id = (event.get("pathParameters") or {}).get("scheduled_task_id")
        if not scheduled_task_id:
            raise ValueError("scheduled_task_id is required in the path")
        return scheduled_task_id

    @staticmethod
    def _parse_body(event: Dict[str, Any]) -> Dict[str, Any]:
        """Read the request body as a dict.

        :param event: API Gateway event.
        :return: The parsed body, empty when absent.
        """
        body = event.get("body")
        parsed = json.loads(body) if isinstance(body, str) else (body or {})
        if not isinstance(parsed, dict):
            raise ValueError("request body must be a JSON object")
        return parsed
