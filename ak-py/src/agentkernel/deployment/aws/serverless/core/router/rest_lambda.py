import json
import logging
import traceback
from typing import Any, Callable, Dict, Optional

from ......core.chat_service import ChatService
from ......core.config import AKConfig
from ......core.model import BaseRequest, ExecutionMode
from ......scheduler import SchedulerError, SchedulerFactory, http_status_for
from ....core.response_store import ResponseDBHandler
from ....core.sqs_handler import SQSHandler
from .common import BaseLambdaRouter


class ScheduleRequestError(Exception):
    """A schedule request that must be answered with its own status rather than a generic 500.

    Carries the status so only the scheduling path can select one. Ordinary chat requests keep
    the generic 500 they have always had, and no unrelated exception text is echoed to a client.
    """

    def __init__(self, status_code: int, message: str):
        """
        :param status_code: The status this rejection is answered with.
        :param message: The client-facing reason.
        """
        super().__init__(message)
        self.status_code = status_code


class UnauthenticatedScheduleError(ScheduleRequestError):
    """A schedule request arrived with no API Gateway authorizer context.

    Python cannot see whether Terraform attached the authorizer to the route, so the identity
    requirement is enforced per request rather than at initialization.
    """

    def __init__(self, message: str):
        """
        :param message: The client-facing reason.
        """
        super().__init__(401, message)


class DefaultEndpointsHandler:
    """Provides default endpoint routes depending on EXECUTION_MODE."""

    def __init__(self):
        self._log = logging.getLogger("ak.aws.lambda.default_endpoints")
        self._default_chat_path = "default_chat_path"
        self._default_chat_method = "POST"
        self._config = AKConfig.get()
        self._response_store = ResponseDBHandler().get_store() if self._config.execution.response_store is not None else None
        self._chat_service = ChatService()
        SchedulerFactory.validate_config()
        self._schedule_service = SchedulerFactory.service()

    def get_default_endpoint_info(self):
        """:return: (default_chat_path, default_chat_method, default_user_polling_method)."""
        default_user_polling_method = "GET" if self._config.execution.mode == ExecutionMode.REST_ASYNC else None
        return (
            self._default_chat_path,
            self._default_chat_method,
            default_user_polling_method,
        )

    def get_routes(self) -> Dict[str, Dict[str, Callable]]:
        """Return route mappings (path -> method -> handler) based on execution mode."""

        input_queue_url = SQSHandler.get_input_queue_url()
        exec_mode = self._config.execution.mode

        if not input_queue_url:
            self._log.info("Queues not configured. Therefore, using Request Handler lambda for chat processing")
            return {self._default_chat_path: {self._default_chat_method: self._handle_agent_chat}}

        if self._config.execution.response_store is None:
            raise ValueError(
                "execution.response_store is required when using queue-based execution. " "Please configure a response store in your configuration."
            )

        if exec_mode == ExecutionMode.REST_SYNC:
            self._log.info("Initialized REST_SYNC endpoint.")
            return {self._default_chat_path: {self._default_chat_method: self._handle_rest_sync}}

        if exec_mode == ExecutionMode.REST_ASYNC:
            self._log.info("Initialized REST_ASYNC endpoints.")
            default_user_polling_method = "GET" if exec_mode == ExecutionMode.REST_ASYNC else None
            return {
                self._default_chat_path: {
                    self._default_chat_method: self._handle_async_submit,
                    default_user_polling_method: self._handle_async_poll,
                }
            }

        if exec_mode == ExecutionMode.STREAM:
            self._log.info("Initialized STREAM endpoint.")
            return {self._default_chat_path: {self._default_chat_method: self._handle_stream}}

        raise ValueError(f"Unsupported EXECUTION_MODE: {exec_mode}")

    def _parse_body(self, event: Dict[str, Any]) -> BaseRequest:
        body = event.get("body")
        body_dict = json.loads(body) if isinstance(body, str) else (body or {})
        return BaseRequest.from_payload(body_dict)

    def _build_failure_body(self, request_id: Optional[str] = None, status: Optional[str] = None, message: Optional[str] = None) -> Dict[str, Any]:
        error_body = {"error": message or "An unexpected error occurred"}
        if status is not None:
            error_body["status"] = status
        if request_id is not None:
            error_body["request_id"] = request_id
        return error_body

    def _handle_request(
        self,
        event: Dict[str, Any],
        operation: Callable[[BaseRequest, Dict[str, Any]], Any],
    ) -> tuple[int, Dict[str, Any]]:
        """Run ``operation`` with standard request parsing and error handling.

        :param operation: Returns either a body (answered 200) or an explicit
            ``(statusCode, body)`` pair.
        """
        request_id = None
        try:
            request = self._parse_body_or_reject(event)
            request_id = request.request_id
            result = operation(request, event)
            # (statusCode, body) will be handled in aklambda.py
            return result if isinstance(result, tuple) else (200, result)

        # Only the scheduling path raises this, so an ordinary request's failures are untouched
        # by the statuses below and never have their exception text echoed to the client.
        except ScheduleRequestError as e:
            self._log.warning(f"Schedule request rejected with {e.status_code}: {e}")
            return (e.status_code, self._build_failure_body(request_id, message=str(e)))

        # Log and hide unexpected failures behind a generic 500 response.
        except Exception as e:
            self._log.error(f"Request failed: {e}\n{traceback.format_exc()}")
            return (500, self._build_failure_body(request_id))  # (statusCode, body) will be handled in aklambda.py

    def _parse_body_or_reject(self, event: Dict[str, Any]) -> BaseRequest:
        """Parse the body, answering 400 when an *unparseable* body is a schedule request.

        A malformed ``schedule`` block fails here, in pydantic, before the schedule branch can
        reject it, and a caller asking for a schedule is owed the reason. Every other parse
        failure is re-raised so an ordinary chat request keeps the generic 500 it has always had.

        :param event: API Gateway event.
        :return: The parsed request envelope.
        :raises ScheduleRequestError: The body carries a ``schedule`` block and does not parse.
        """
        try:
            return self._parse_body(event)
        except ValueError as e:
            if not self._carries_schedule(event):
                raise
            raise ScheduleRequestError(400, str(e)) from e

    @staticmethod
    def _carries_schedule(event: Dict[str, Any]) -> bool:
        """Whether the raw body asks to be scheduled, read without validating it.

        Read from the raw event rather than the parsed model, because the only caller is the
        path where parsing already failed.

        :param event: API Gateway event.
        :return: True when a ``schedule`` block is present at either envelope level.
        """
        raw = event.get("body")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(payload, dict):
            return False
        nested = payload.get("body")
        if isinstance(nested, dict) and nested.get("schedule") is not None:
            return True
        return payload.get("schedule") is not None

    def _maybe_schedule(self, payload: BaseRequest, event: Dict[str, Any]) -> Optional[tuple[int, Dict[str, Any]]]:
        """Register the request to run later when it carries a ``schedule`` block.

        Every failure leaves here as a ``ScheduleRequestError`` carrying its status, so the
        statuses match the REST chat route's rather than collapsing into one.

        :param payload: The parsed request envelope.
        :param event: The API Gateway event, carrying the authorizer context.
        :return: The 201 acknowledgement, or None when this is an ordinary chat request.
        :raises ScheduleRequestError: 400 when scheduling is disabled or the schedule is
            invalid, 401 with no authorizer context, 403/409 on an ownership or state conflict.
        """
        body = payload.body
        if body is None or body.schedule is None:
            return None
        if self._schedule_service is None:
            raise ScheduleRequestError(400, "Scheduling is not enabled for this deployment")

        owner_id = event.get("requestContext", {}).get("authorizer", {}).get("principalId")
        if not owner_id:
            raise UnauthenticatedScheduleError("a scheduled task requires an authenticated caller")

        try:
            ack = self._schedule_service.create(
                spec=body.schedule,
                prompt=body.prompt,
                agent=body.agent,
                owner_id=owner_id,
                request_id=payload.request_id,
            )
        except (SchedulerError, ValueError) as e:
            raise ScheduleRequestError(http_status_for(e), str(e)) from e

        self._log.info(f"Scheduled task registered: {ack.scheduled_task_id} for owner {owner_id}")
        return (201, ack.model_dump(mode="json", exclude_none=True))

    def _send_to_queue(self, payload: BaseRequest) -> Dict[str, Any]:
        """Send request payload to the SQS input queue.

        :raises ValueError: body, request_id, or session_id is missing.
        """
        request_body = payload.body
        if request_body is None:
            raise ValueError("body is required")
        if not payload.request_id:
            raise ValueError("request_id is required")

        session_id = request_body.session_id
        if not session_id:
            raise ValueError("session_id is required")

        response = SQSHandler.send_message_to_input_queue(
            message_body=request_body.model_dump(exclude_none=True),
            attributes={"message_deduplication_id": payload.request_id},
            request_id=payload.request_id,
            user_id=payload.user_id,
        )
        return response

    def _get_message(self, payload: BaseRequest) -> Dict[str, Any]:
        """Fetch the response message for ``payload.request_id``.

        :raises ValueError: request_id is missing.
        """
        request_id = payload.request_id
        if not request_id:
            raise ValueError("request_id is required")
        return self._response_store.get_message_with_retry(request_id=request_id, get_and_delete=True)

    def _handle_rest_sync(self, event: Dict[str, Any], context: Any) -> tuple[int, Dict[str, Any]]:
        """Send request to queue and immediately fetch response."""

        def sync_operation(payload: BaseRequest, request_event: Dict[str, Any]) -> Any:
            ack = self._maybe_schedule(payload, request_event)
            if ack is not None:
                # Nothing is enqueued and there is no run to wait for, so the response-store
                # wait is skipped entirely.
                return ack

            request_id = payload.request_id
            self._log.info(f"Performing REST_SYNC operation for payload: '{payload}'")
            queue_result = self._send_to_queue(payload)
            self._log.info(f"Message sent to input queue, response from send_message function: '{queue_result}'")

            message = self._get_message(payload)
            self._log.info(f"Fetched message from database: {message}")
            message = (
                message
                if message
                else self._build_failure_body(
                    request_id=request_id,
                    status="NOT_FOUND",
                    message=f"No response message found for request_id: '{request_id}'. Try increasing the retry_count or delay in the response store configuration.",
                )
            )
            self._log.info(f"Returning response for REST_SYNC operation: '{message}'")

            return message

        return self._handle_request(event, sync_operation)

    def _handle_async_submit(self, event: Dict[str, Any], context: Any) -> tuple[int, Dict[str, Any]]:
        """Submit message to queue (async mode)."""

        def submit_operation(payload: BaseRequest, request_event: Dict[str, Any]) -> Any:
            ack = self._maybe_schedule(payload, request_event)
            if ack is not None:
                return ack

            self._log.info(f"Performing REST_ASYNC submit operation for payload: '{payload}'")
            queue_result = self._send_to_queue(payload)

            self._log.info(f"Message sent to input queue, response from send_message function: '{queue_result}'")
            response_body = {"status": "ACCEPTED", "request_id": payload.request_id}

            self._log.info(f"Returning response for REST_ASYNC submit operation: '{response_body}'")
            return response_body

        return self._handle_request(event, submit_operation)

    def _handle_async_poll(self, event: Dict[str, Any], context: Any) -> tuple[int, Dict[str, Any]]:
        """Poll database for messages (async mode)."""

        def poll_operation(payload: BaseRequest, request_event: Dict[str, Any]) -> Dict[str, Any]:
            self._log.info(f"Performing REST_ASYNC poll operation for payload: '{payload}'")

            request_id = payload.request_id
            message = self._get_message(payload)
            self._log.info(f"Fetched message from database: {message}")
            response_body = (
                message
                if message
                else self._build_failure_body(
                    request_id=request_id,
                    status="NOT_FOUND",
                    message=f"No response message found for request_id '{request_id}'. The message may be unavailable. Please try again.",
                )
            )

            self._log.info(f"Returning response for REST_ASYNC poll operation: '{response_body}'")
            return response_body

        return self._handle_request(event, poll_operation)

    def _handle_agent_chat(self, event: Dict[str, Any], context: Any) -> Dict[str, Any]:
        """Process chat request directly without queue."""

        try:
            request = self._parse_body(event)
            if request.body is None:
                raise ValueError("body is required")
            status_code, res_body = self._chat_service.process_chat_request(request.body)

            return {
                "statusCode": status_code,
                "body": json.dumps(res_body),
            }

        except Exception as e:
            self._log.error(f"Chat error: {e}\n{traceback.format_exc()}")

            return {
                "statusCode": 500,
                "body": json.dumps({"error": "Error processing request", "session_id": None}),
            }

    def _handle_stream(self, event: Dict[str, Any], context: Any) -> tuple[int, Dict[str, Any]]:
        """Reject: SSE streaming isn't supported via REST API Gateway in Lambda."""
        return (
            400,
            {
                "error": (
                    "SSE streaming requires a Lambda Function URL with InvokeMode: RESPONSE_STREAM. "
                    "Use streaming_handler as your Lambda entry point."
                )
            },
        )


class RESTLambdaRouter(BaseLambdaRouter):
    """Router for AWS Lambda events from API Gateway REST API v1.

    Handlers are registered per (method, route); routes are normalized before lookup, and
    dispatch raises ValueError when nothing matches.
    """

    def __init__(self):
        super().__init__()
        self._default_chat_path = None
        self._default_chat_method = None
        self._default_user_polling_method = None
        self._routes: Dict[str, Dict[str, Callable[[Dict[str, Any], Any], Any]]] = {}

        self._endpoints_handler = DefaultEndpointsHandler()
        (
            self._default_chat_path,
            self._default_chat_method,
            self._default_user_polling_method,
        ) = self._endpoints_handler.get_default_endpoint_info()
        self._routes = self._endpoints_handler.get_routes()

        if SchedulerFactory.enabled():
            from .schedule_lambda import ScheduleEndpointsHandler

            self._routes.update(ScheduleEndpointsHandler().get_routes())

        self._log.info(f"Registered REST Routes: {self._routes}")

    @staticmethod
    def _normalize_method(method: Optional[str]) -> str:
        """Normalize HTTP method to uppercase, defaulting to GET."""
        return (method or "GET").upper()

    def register(self, route: str, method: Optional[str] = None) -> Callable[[Callable], Callable]:
        """Return a decorator that registers a handler for the given HTTP route and method.

        :raises ValueError: method is not provided.
        """
        if method is None:
            raise ValueError("HTTP method is required for REST routes")

        norm_route = self._normalize_path(route)
        norm_method = self._normalize_method(method)

        def _decorator(func: Callable[[Dict[str, Any], Any], Any]) -> Callable:
            self._log.info(f"Registering route {norm_method} {norm_route} -> {func.__name__}")

            methods = self._routes.setdefault(norm_route, {})
            if norm_method in methods:
                self._log.warning(f"Route {norm_method} {norm_route} already exists. Skipping.")
                return func
            methods[norm_method] = func
            return func

        return _decorator

    def _resolve_by_resource_template(
        self,
        event: Dict[str, Any],
        method: str,
        env_base_path: Optional[str],
    ) -> Optional[Callable[[Dict[str, Any], Any], Any]]:
        """Resolve a route registered under its API Gateway resource template rather than a literal path.

        This router matches exact path strings and has no path-parameter support, so a route
        like ``/schedule/{scheduled_task_id}`` needs the resource template API Gateway puts on
        the event. Only reached after a literal-path lookup already failed.

        :return: The matching handler, or None.
        """
        resource = event.get("resource")
        if not resource or not env_base_path:
            return None
        template = resource.removeprefix(env_base_path)
        return self._routes.get(template, {}).get(method)

    def dispatch(self, event: Dict[str, Any], context: Any) -> Optional[Dict[str, Any]]:
        """Dispatch an API Gateway REST event to its registered handler.

        :raises ValueError: No registered route matches the request.
        """
        self._log.info("Dispatching REST endpoint")
        method = self._normalize_method(event.get("httpMethod"))
        event_path = event.get("path") or event.get("resource") or "/"
        self._log.info(f"Event path: {event_path}, Method: {method}")

        converted_event_path = self._default_chat_path
        env_base_path, env_agent_endpoint = self._get_base_paths_from_env()
        if env_base_path and env_agent_endpoint:
            converted_event_path = (
                self._default_chat_path
                if event_path == env_agent_endpoint and method in [self._default_chat_method, self._default_user_polling_method]
                else event_path.removeprefix(env_base_path)
            )
        else:
            self._log.warning("Environment variables not provided; using default agent handler")
            method = self._default_user_polling_method if method == self._default_user_polling_method else self._default_chat_method

        self._log.info(f"Converted event path: {converted_event_path}")
        methods = self._routes.get(converted_event_path, {})
        handler = methods.get(method)
        if not handler:
            handler = self._resolve_by_resource_template(event, method, env_base_path)
        if not handler:
            self._log.warning(f"No registered route found for API Gateway path -> '{event_path}' and method '{method}'")
            raise ValueError(f"No registered route found for API Gateway path -> '{event_path}' and method '{method}'")
        result = handler(event, context)
        self._log.debug(f"Lambda function result: {result}")
        return result
