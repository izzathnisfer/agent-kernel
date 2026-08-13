import json
import logging
import traceback
from typing import Any, Callable, Dict, Optional, Tuple

from pydantic import BaseModel

from ......auth.handler import AuthValidator
from ......core.chat_service import ChatService
from ......core.config import AKConfig, ExecutionMode
from ......core.model import BaseRequest, StreamChunk
from ......scheduler import CreateAck, SchedulerError, SchedulerFactory, http_status_for
from ....core.sqs_handler import SQSHandler
from ....core.websocket_service import AWSWebSocketHandler, WebSocketConnectionStore
from .common import BaseLambdaRouter


class LambdaWSHandler(AWSWebSocketHandler):
    """Base class for Lambda WebSocket route handlers; adds Lambda API Gateway event parsing to AWSWebSocketHandler."""

    class WSMessageInfo(BaseModel):
        """WebSocket message information."""

        user_id: str
        request: BaseRequest

    def __init__(self):
        """Initialize the Lambda WebSocket handler from configuration."""
        config = AKConfig.get()
        if not config.websocket_api.connection_table or not config.websocket_api.connection_table.table_name:
            raise ValueError("websocket_api.connection_table.table_name is required for WebSocket handler")
        connection_store = WebSocketConnectionStore(
            table_name=config.websocket_api.connection_table.table_name,
            ttl=config.websocket_api.connection_table.ttl,
        )
        super().__init__(connection_store=connection_store)
        self._config = config
        self.CONNECT_ROUTE = "$connect"
        self.DISCONNECT_ROUTE = "$disconnect"
        self.DEFAULT_ROUTE = "$default"
        self.CHAT_ROUTE = config.websocket_api.chat_route

    def _parse_body(self, event: Dict[str, Any]) -> BaseRequest:
        body = event.get("body")
        body_dict = json.loads(body) if isinstance(body, str) and body else (body or {})
        return BaseRequest.from_payload(body_dict)

    def _extract_connection_id(self, event: Dict[str, Any]) -> str:
        """:raises ValueError: connectionId is missing."""
        request_context = event.get("requestContext", {})
        connection_id = request_context.get("connectionId")
        if not connection_id:
            raise ValueError("WebSocket event missing requestContext.connectionId")
        return connection_id

    def _parse_event_to_wsmessage(self, event: Dict[str, Any]) -> "LambdaWSHandler.WSMessageInfo":
        """:raises ValueError: connection_id is missing or has no associated user_id."""
        request = self._parse_body(event)
        connection_id = self._extract_connection_id(event)
        user_id = self.get_user_id(connection_id)
        if not user_id:
            raise ValueError(f"No user_id found for connection_id: {connection_id}")
        return self.WSMessageInfo(user_id=user_id, request=request)

    def _handle_msg_and_brdcst(
        self,
        event: Dict[str, Any],
        operation: Callable[[WSMessageInfo], Dict[str, Any]],
        message_type: Optional["LambdaWSHandler.MessageType"] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Run ``operation`` on the parsed message and broadcast its result to the user."""
        user_id = None
        try:
            ws_message_info = self._parse_event_to_wsmessage(event)
            user_id = ws_message_info.user_id
            brdcstin_msg = operation(ws_message_info)
            endpoint_url = LambdaWSHandler.construct_endpoint_url(event)
            self.broadcast(endpoint_url=endpoint_url, message=brdcstin_msg, user_id=user_id, message_type=message_type)
            return (
                200,
                self._build_lambda_response(user_id=user_id, msg="Request processed successfully", success=True),
            )
        except Exception as e:
            self._log.error(f"Request failed: {e}\n{traceback.format_exc()}")
            return (
                500,
                self._build_lambda_response(user_id=user_id, msg="Request processing failed", success=False),
            )

    def _build_lambda_response(
        self,
        user_id: Optional[str] = None,
        msg: Optional[str] = None,
        success: bool = True,
    ) -> Dict[str, Any]:
        """Build a standardized response body."""
        msg = msg or ("Operation successful" if success else "An unexpected error occurred")
        body = {"status": "SUCCESS", "message": msg} if success else {"status": "FAILED", "message": msg}
        if user_id:
            body["user_id"] = user_id
        return body


class ConnectionRoutesHandler(LambdaWSHandler):
    """Handles WebSocket connection lifecycle routes ($connect, $disconnect).

    Authentication is mandatory: ``auth_validator`` validates the JWT and user_id comes from
    its ``userId`` claim.
    """

    _log = logging.getLogger("ak.aws.serverless.connection_routes")

    def __init__(self, auth_validator: AuthValidator):
        super().__init__()
        self.auth_validator = auth_validator

    def _extract_auth_token(self, event: Dict[str, Any]) -> Optional[str]:
        query_params = event.get("queryStringParameters", {})
        if isinstance(query_params, dict):
            return query_params.get("token")
        return None

    def get_routes(self) -> Dict[str, Callable[[Dict[str, Any], Any], Any]]:
        return {
            self.CONNECT_ROUTE: self._handle_connect,
            self.DISCONNECT_ROUTE: self._handle_disconnect,
        }

    def _handle_connect(self, event: Dict[str, Any], context: Optional[Any] = None) -> Tuple[int, Dict[str, Any]]:
        """Validate the token and admit the connection; user_id comes from its ``userId`` claim."""
        try:
            connection_id = self._extract_connection_id(event)

            token = self._extract_auth_token(event)
            if not token:
                return 401, self._build_lambda_response(msg="Authentication token is required", success=False)

            validation_result = self.auth_validator.validate(token)
            if not validation_result.is_valid:
                return 401, self._build_lambda_response(msg=validation_result.error_msg or "Authentication failed", success=False)

            user_id = None
            if validation_result.claims:
                user_id = validation_result.claims.get("userId")
            if not user_id:
                return 401, self._build_lambda_response(msg="'userId' claim is required in JWT token", success=False)

            self.on_connect(connection_id=connection_id, user_id=user_id)

            return 200, self._build_lambda_response(user_id=user_id, msg="WebSocket connection established", success=True)

        except Exception as e:
            self._log.error(f"WebSocket $connect failed: {e}\n{traceback.format_exc()}")
            return 500, self._build_lambda_response(msg="Failed to establish WebSocket connection", success=False)

    def _handle_disconnect(self, event: Dict[str, Any], context: Optional[Any] = None) -> Tuple[int, Dict[str, Any]]:
        try:
            connection_id = self._extract_connection_id(event)

            self.on_disconnect(connection_id=connection_id)
            return 200, self._build_lambda_response(msg="WebSocket connection closed", success=True)
        except Exception as e:
            self._log.error(f"WebSocket $disconnect failed: {e}\n{traceback.format_exc()}")
            return 500, self._build_lambda_response(msg="Failed to close WebSocket connection", success=False)


class SystemRoutesHandler(LambdaWSHandler):
    """Handles WebSocket application routes ($default, /chat); assumes the connection is already authenticated."""

    _log = logging.getLogger("ak.aws.serverless.system_routes")

    def __init__(self):
        super().__init__()
        if not self.CHAT_ROUTE:
            raise ValueError("websocket_api.chat_route must be configured")
        self._chat_service = ChatService()
        # No Authoriser check: a WebSocket connection is authenticated at $connect, so the
        # identity requirement holds here without one.
        SchedulerFactory.validate_config()
        self._schedule_service = SchedulerFactory.service()

    def _is_queue_mode(self) -> bool:
        return self._config.execution.queues.input.url is not None

    def get_routes(self) -> Dict[str, Callable[[Dict[str, Any], Any], Any]]:
        return {
            self.DEFAULT_ROUTE: self._handle_default,
            self.CHAT_ROUTE: self._get_chat_handler_by_mode(),
        }

    def _get_chat_handler_by_mode(self) -> Callable:
        """Pick the chat handler for the current execution mode and queue configuration."""
        if self._config.execution.mode == ExecutionMode.STREAM:
            return self._handle_queue_mode if self._is_queue_mode() else self._handle_stream_direct
        return self._handle_queue_mode if self._is_queue_mode() else self._handle_direct_chat

    def _handle_default(self, event: Dict[str, Any], context: Optional[Any] = None) -> Tuple[int, Dict[str, Any]]:
        def _process_default(ws_message_info: "LambdaWSHandler.WSMessageInfo") -> Dict[str, Any]:
            self.on_default()
            requested_route = ws_message_info.request.route
            return {"status": "FAILED", "message": f"Route '{requested_route}' not found"}

        return self._handle_msg_and_brdcst(
            event,
            _process_default,
            self.MessageType.SYSTEM_RESPONSE,
        )

    def _handle_direct_chat(self, event: Dict[str, Any], context: Optional[Any] = None) -> Tuple[int, Dict[str, Any]]:
        """Handle direct chat request without queue."""

        # Checked before delegating: _handle_msg_and_brdcst can only answer 200 or 500.
        rejection = self._reject_direct_mode_schedule(event)
        if rejection is not None:
            return rejection

        def _process_chat(ws_message_info: "LambdaWSHandler.WSMessageInfo") -> Dict[str, Any]:
            request = ws_message_info.request
            if request.body is None:
                raise ValueError("body is required")
            _, res_body = self._chat_service.process_chat_request(request.body)
            return res_body

        return self._handle_msg_and_brdcst(
            event,
            _process_chat,
            message_type=self.MessageType.CHAT_RESPONSE,
        )

    def _handle_stream_direct(self, event: Dict[str, Any], context: Optional[Any] = None) -> Tuple[int, Dict[str, Any]]:
        """Handle direct streaming chat (non-queue STREAM mode): stream agent chunks via WebSocket."""
        user_id = None
        session_id = None
        try:
            ws_message_info = self._parse_event_to_wsmessage(event)
            user_id = ws_message_info.user_id
            request = ws_message_info.request
            if request.body is None:
                raise ValueError("body is required")
            # Direct mode consumes no input queue, so a schedule is refused rather than registered.
            if request.body.schedule is not None:
                return self._direct_mode_rejection(user_id)
            session_id = request.body.session_id

            endpoint_url = LambdaWSHandler.construct_endpoint_url(event)

            for raw_chunk in self._chat_service.process_stream_chat_sync(req=request.body):
                chunk_dict = json.loads(raw_chunk)
                self.broadcast(endpoint_url=endpoint_url, message=chunk_dict, user_id=user_id, message_type=self.MessageType.STREAM_CHUNK)

            return 200, self._build_lambda_response(user_id=user_id, msg="Stream completed successfully", success=True)
        except Exception as e:
            self._log.error(f"Stream direct request failed: {e}\n{traceback.format_exc()}")
            try:
                endpoint_url = LambdaWSHandler.construct_endpoint_url(event)
                error_chunk = StreamChunk(error=str(e), done=True)
                error_chunk_dict = error_chunk.model_dump(exclude_none=True)
                if session_id:
                    error_chunk_dict["session_id"] = session_id
                self.broadcast(endpoint_url=endpoint_url, message=error_chunk_dict, user_id=user_id, message_type=self.MessageType.STREAM_CHUNK)
            except Exception:
                pass
            return 500, self._build_lambda_response(user_id=user_id, msg="Stream request processing failed", success=False)

    def _reject_direct_mode_schedule(self, event: Dict[str, Any]) -> Optional[Tuple[int, Dict[str, Any]]]:
        """Refuse a frame asking to be scheduled on a deployment that consumes no input queue.

        A timer fires onto the input queue; in direct mode nothing consumes it, so a
        registration here would be acknowledged and then never run. Parses the frame a second
        time (cheap: one frame) to produce a rejection the generic handler can't express.

        :return: The 400 rejection, or None when the frame carries no ``schedule`` block.
        """
        try:
            ws_message_info = self._parse_event_to_wsmessage(event)
        except Exception:  # noqa: BLE001 — not a scheduling question; let the normal path report it
            return None
        body = ws_message_info.request.body
        if body is None or body.schedule is None:
            return None
        return self._direct_mode_rejection(ws_message_info.user_id)

    def _direct_mode_rejection(self, user_id: Optional[str]) -> Tuple[int, Dict[str, Any]]:
        """Build the direct-mode refusal, so both direct chat paths answer it identically."""
        message = "Scheduling requires queue mode; this deployment runs requests directly"
        self._log.warning(f"Rejected a schedule frame on a direct-mode deployment for user_id={user_id}")
        return (400, self._build_lambda_response(user_id=user_id, msg=message, success=False))

    def _create_scheduled_task(
        self,
        event: Dict[str, Any],
        ws_message_info: "LambdaWSHandler.WSMessageInfo",
    ) -> Tuple[int, Dict[str, Any]]:
        """Register a chat frame to run later and broadcast the acknowledgement.

        Sent here rather than by the response handler, so it goes straight out on the caller's
        live connection without passing through the queues.
        """
        user_id = ws_message_info.user_id
        try:
            ack = self._register_schedule(ws_message_info)
        except (SchedulerError, ValueError) as e:
            # Mapped, not collapsed to 400: an ownership or state conflict reads the same here as
            # on the REST route.
            status_code = http_status_for(e)
            self._log.warning(f"Scheduled task creation failed for user_id={user_id} with {status_code}: {e}")
            return (status_code, self._build_lambda_response(user_id=user_id, msg=str(e), success=False))

        self._broadcast_ack(ack, event, user_id)
        response_body = self._build_lambda_response(user_id=user_id, msg="Request scheduled successfully", success=True)
        response_body["scheduled_task_id"] = ack.scheduled_task_id
        return (201, response_body)

    def _register_schedule(self, ws_message_info: "LambdaWSHandler.WSMessageInfo") -> CreateAck:
        """Register the frame's ``schedule`` block against the connection's authenticated user.

        Reached from the queue-mode chat paths only; direct-mode paths refuse before here.

        :raises ValueError: Scheduling is not enabled for this deployment.
        :raises SchedulerError: The schedule was rejected.
        """
        if self._schedule_service is None:
            raise ValueError("Scheduling is not enabled for this deployment")
        body = ws_message_info.request.body
        return self._schedule_service.create(
            spec=body.schedule,
            prompt=body.prompt,
            agent=body.agent,
            owner_id=ws_message_info.user_id,
            request_id=ws_message_info.request.request_id,
        )

    def _broadcast_ack(self, ack: CreateAck, event: Dict[str, Any], user_id: str) -> None:
        """Push the creation acknowledgement over the caller's connection.

        In stream mode it's a single terminal frame: nothing is generated at creation time, so
        there are no token deltas to precede it.
        """
        payload = ack.model_dump(mode="json", exclude_none=True)
        if self._config.execution.mode == ExecutionMode.STREAM:
            message_type = self.MessageType.STREAM_CHUNK
            payload = {**payload, "done": True}
        else:
            message_type = self.MessageType.CHAT_RESPONSE

        self.broadcast(
            endpoint_url=LambdaWSHandler.construct_endpoint_url(event),
            message=payload,
            user_id=user_id,
            message_type=message_type,
        )

    def _handle_queue_mode(self, event: Dict[str, Any], context: Optional[Any] = None) -> Tuple[int, Dict[str, Any]]:
        """Send the chat request to the SQS input queue; response arrives via output queue -> ResponseHandler -> WebSocket."""
        user_id = None
        try:
            ws_message_info = self._parse_event_to_wsmessage(event)
            user_id = ws_message_info.user_id
            request = ws_message_info.request
            request_body = request.body
            if request_body is None:
                raise ValueError("body is required")
            if not request.request_id:
                raise ValueError("request_id is required")

            # Checked before session_id: without this branch a frame carrying a schedule would
            # be enqueued and executed immediately instead of scheduled.
            if request_body.schedule is not None:
                return self._create_scheduled_task(event, ws_message_info)

            session_id = request_body.session_id
            if not session_id:
                raise ValueError("session_id is required")

            self._log.info(f"Sending WebSocket request to queue: request_id={request.request_id}, session_id={session_id}")

            endpoint_url = LambdaWSHandler.construct_endpoint_url(event)

            response = SQSHandler.send_message_to_input_queue(
                message_body=request_body.model_dump(exclude_none=True),
                attributes={"message_deduplication_id": request.request_id},
                request_id=request.request_id,
                user_id=user_id,
                custom_message_attributes=[
                    SQSHandler.CustomAttribute(name="endpoint_url", value=endpoint_url, datatype=SQSHandler.AttributeDataType.STRING)
                ],
            )

            self._log.info(f"Request sent to input queue successfully: {response}")

            response_body = self._build_lambda_response(user_id=user_id, msg="Request queued successfully", success=True)
            response_body["request_id"] = request.request_id

            return 200, response_body
        except Exception as e:
            self._log.error(f"Queue request failed: {e}\n{traceback.format_exc()}")
            return (
                500,
                self._build_lambda_response(user_id=user_id, msg="Request processing failed", success=False),
            )


class WSLambdaRouter(BaseLambdaRouter):
    """Router for AWS Lambda events from API Gateway WebSocket APIs.

    Handlers are registered per route; routes are normalized before lookup, and dispatch
    raises ValueError when nothing matches.
    """

    def __init__(self, connection_routes: bool = False, system_routes: bool = True, auth_validator: Optional[AuthValidator] = None):
        """
        :param connection_routes: Include $connect and $disconnect routes (requires auth_validator).
        :param system_routes: Include $default and /chat routes.
        :raises ValueError: connection_routes is True but auth_validator is not provided.
        """
        super().__init__()
        self._log.info("Initializing WebSocket routes")

        self._base_route_handler = LambdaWSHandler()

        self._websocket_routes: Dict[str, Callable[[Dict[str, Any], Any], Any]] = {}

        if connection_routes:
            if not auth_validator:
                raise ValueError("auth_validator is required when connection_routes is True")
            self._log.info("Initializing connection routes handler")
            self._websocket_routes.update(ConnectionRoutesHandler(auth_validator=auth_validator).get_routes())

        if system_routes:
            self._log.info("Initializing system routes handler")
            self._websocket_routes.update(SystemRoutesHandler().get_routes())

        self._log.info(f"Registered WebSocket Routes: {self._websocket_routes}")

    def _get_ws_handler_function(self, handler_logic_func: Callable[[Dict[str, Any], Any], Any]):
        """Wrap a handler function so its result is broadcast over WebSocket."""

        def _handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
            try:
                ws_message_info = self._base_route_handler._parse_event_to_wsmessage(event)
                user_id = ws_message_info.user_id
                res_msg_to_brdcst = handler_logic_func(event, context)
                endpoint_url = LambdaWSHandler.construct_endpoint_url(event)
                self._base_route_handler.broadcast(endpoint_url=endpoint_url, message=res_msg_to_brdcst, user_id=user_id)
                return 200, self._base_route_handler._build_lambda_response(user_id=user_id, msg="Message broadcast successfully", success=True)
            except Exception as e:
                self._log.error(f"WebSocket handler failed: {e}\n{traceback.format_exc()}")
                return 500, self._base_route_handler._build_lambda_response(msg="WebSocket handler encountered an error", success=False)

        return _handler

    def register(self, route: str, method: Optional[str] = None) -> Callable[[Callable], Callable]:
        """Return a decorator that registers a WebSocket handler for the given route.

        :param method: Not used — WebSocket routes have no HTTP method; kept for interface compatibility.
        :raises ValueError: A method is provided.
        """
        if method is not None:
            raise ValueError("HTTP method is not allowed in WebSocket mode")

        norm_route = self.normalize_ws_route(route)

        def _decorator(wrapped_func: Callable[[Dict[str, Any], Any], Any]) -> Callable:
            self._log.info(f"Registering WebSocket route {norm_route} -> {wrapped_func.__name__}")

            wrapped_func = self._get_ws_handler_function(handler_logic_func=wrapped_func)

            if norm_route in self._websocket_routes:
                self._log.warning(f"WebSocket route {norm_route} already exists. Skipping.")
                return wrapped_func

            self._websocket_routes[norm_route] = wrapped_func
            return wrapped_func

        return _decorator

    def _broadcast_error(self, event: Dict[str, Any], error_message: str) -> None:
        try:
            request_context = event.get("requestContext", {})
            connection_id = request_context.get("connectionId")

            if not connection_id:
                self._log.warning("Cannot broadcast error: missing connectionId")
                return

            user_id = self._base_route_handler.get_user_id(connection_id)
            if not user_id:
                self._log.warning(f"Cannot broadcast error: no user_id found for connection_id: {connection_id}")
                return

            endpoint_url = LambdaWSHandler.construct_endpoint_url(event)
            self._base_route_handler.broadcast(
                endpoint_url=endpoint_url,
                message={"status": "FAILED", "message": error_message},
                user_id=user_id,
                message_type=LambdaWSHandler.MessageType.SYSTEM_RESPONSE,
            )
            self._log.info(f"Error broadcasted to user {user_id}: {error_message}")
        except Exception as e:
            self._log.error(f"Failed to broadcast error: {e}\n{traceback.format_exc()}")

    def dispatch(self, event: Dict[str, Any], context: Any) -> Optional[Dict[str, Any]]:
        """Dispatch an API Gateway WebSocket event to its registered handler.

        :raises ValueError: No registered route matches the request.
        """
        try:
            self._log.info("Dispatching WebSocket endpoint")
            request_context = event.get("requestContext", {})
            route_key = request_context.get("routeKey")
            connection_id = request_context.get("connectionId")

            if not route_key:
                self._log.warning("WebSocket event missing routeKey")
                raise ValueError("WebSocket event missing routeKey")

            norm_route_key = self.normalize_ws_route(route_key)
            self._log.info(f"Normalized route key: '{route_key}', Connection ID: '{connection_id}'")
            handler = self._websocket_routes.get(norm_route_key)

            if not handler:
                self._log.warning(f"No registered WebSocket route found for route key -> '{route_key}'")
                raise ValueError(f"No registered WebSocket route found for route key -> '{route_key}'")

            result = handler(event, context)
            self._log.debug(f"WebSocket handler result: {result}")
            return result
        except Exception as e:
            self._log.error(f"Error during dispatch: {e}\n{traceback.format_exc()}")
            self._broadcast_error(event, str(e))
            raise
