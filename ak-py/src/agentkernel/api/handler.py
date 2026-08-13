import logging
from abc import ABC, abstractmethod
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import ConfigDict

from ..auth import Authoriser
from ..core import Config
from ..core.chat_service import ChatService
from ..core.model import BaseChatRequest, BaseRunRequest, ExecutionMode
from ..core.runtime import Runtime


class BearerIdentityMixin:
    """Resolves the caller's identity from a Bearer token via a configured ``Authoriser``.

    Shared by every route layer that needs an authenticated subject, so the thread and
    schedule routes read and reject tokens identically. Subclasses must set ``_authoriser``.
    """

    _authoriser: Optional[Authoriser] = None

    def _resolve_user(self, request: Request) -> Optional[str]:
        """
        Resolve the caller's user_id via the configured Authoriser.
        :param request: The incoming FastAPI request.
        :return: The resolved user_id, or None when no Authoriser is configured.
        :raises HTTPException: 401 when a token is missing or rejected.
        """
        if self._authoriser is None:
            return None
        auth_header = request.headers.get("authorization")
        if auth_header is None:
            raise HTTPException(status_code=401, detail="Missing authorization header")
        scheme, _, token = auth_header.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        user_id = self._authoriser.authorise(token)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return user_id


class RESTRequestHandler(ABC):
    @abstractmethod
    def get_router(self) -> APIRouter:
        """
        Returns the APIRouter instance which has configured routes
        E.g.:
        - GET /api/v1/agents: List available agents

        def list_agents(self):
            from ..core.runtime import Runtime
            return {"agents": list(Runtime.current().agents().keys())}

        def get_router(self) -> APIRouter:
            router = APIRouter()
            router.add_api_route("/api/v1/agents", self.list_agents, methods=["GET"])
            return router

        """
        pass


class AgentRESTRequestHandler(RESTRequestHandler):
    """
    API routers that expose endpoints to interact with Agent Kernel.
    Endpoints:
    - GET /api/v1/agents: List available agents
    - POST /api/v1/chat: Run an agent (streams SSE when execution.mode=stream, otherwise returns JSON)
    - POST /api/v1/chat-multipart: Same as /api/v1/chat but accepts multipart form data with file/image uploads
    """

    # Endpoint paths — shared with subclasses so route paths stay consistent.
    AGENTS_PATH = "/api/v1/agents"
    CHAT_PATH = "/api/v1/chat"
    CHAT_MULTIPART_PATH = "/api/v1/chat-multipart"

    class BaseMultimodalRunRequest(BaseChatRequest):
        """Chat request with multipart file and image uploads (UploadFile format)."""

        files: Optional[List[UploadFile]] = None
        images: Optional[List[UploadFile]] = None
        model_config = ConfigDict(extra="allow")

    def __init__(self):
        self._log = logging.getLogger("ak.api.agent")
        self._max_file_size = Config.get().api.max_file_size
        self.chat_service = ChatService(rest_api_mode=True)

    def list_agents(self):
        return {"agents": list(Runtime.current().agents().keys())}

    @staticmethod
    def _reject_schedule(body: BaseRunRequest) -> None:
        """Reject a chat body asking to be scheduled on a route that cannot schedule it.

        Scheduling requires queue mode; this route runs the prompt immediately, so an unrejected
        ``schedule`` block would silently run now instead of later.

        :param body: The chat body as supplied.
        :raises HTTPException: 400 when the body carries a ``schedule`` block.
        """
        if getattr(body, "schedule", None) is None:
            return
        raise HTTPException(status_code=400, detail="Scheduling requires queue mode; this deployment runs requests directly")

    async def run(self, body: BaseRunRequest):
        self._reject_schedule(body)
        if Config.get().execution.mode == ExecutionMode.STREAM:
            try:
                gen = await self.chat_service.process_stream_chat_async(req=body, sse_format=True)
                return StreamingResponse(gen, media_type="text/event-stream")
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Streaming failed: {str(e)}")
        return await self.chat_service.process_async_chat_request(req=body)

    async def run_multipart(
        self,
        prompt: str = Form(...),
        agent: Optional[str] = Form(None),
        session_id: Optional[str] = Form(None),
        user_id: Optional[str] = Form(None),
        group_id: Optional[str] = Form(None),
        thread_name: Optional[str] = Form(None),
        files: Optional[List[UploadFile]] = File(None),
        images: Optional[List[UploadFile]] = File(None),
    ):
        req = AgentRESTRequestHandler.BaseMultimodalRunRequest(
            prompt=prompt,
            agent=agent,
            session_id=session_id,
            user_id=user_id,
            group_id=group_id,
            thread_name=thread_name,
            files=files,
            images=images,
        )
        if Config.get().execution.mode == ExecutionMode.STREAM:
            try:
                gen = await self.chat_service.process_stream_chat_async(req=req, sse_format=True)
                return StreamingResponse(gen, media_type="text/event-stream")
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Streaming failed: {str(e)}")
        return await self.chat_service.process_async_chat_request(req=req)

    def get_router(self) -> APIRouter:
        """
        Returns the APIRouter instance.
        """
        router = APIRouter()
        router.add_api_route(self.AGENTS_PATH, self.list_agents, methods=["GET"])
        router.add_api_route(self.CHAT_PATH, self.run, methods=["POST"])
        router.add_api_route(self.CHAT_MULTIPART_PATH, self.run_multipart, methods=["POST"])
        return router
