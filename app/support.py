import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from plan.models import OrderReference
from repository.dialogue import MessageIdConflictError, SessionNotFoundError, TurnInProgressError
from repository.operations import TicketNotFoundError
from service.dialogue import SupportService, UnauthenticatedError, UpstreamUnavailableError


router = APIRouter(prefix="/api/v1/support", tags=["客服对话"])


class StorageUnavailableError(RuntimeError):
    pass


class IncomingMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: UUID = Field(alias="messageId")


class TextMessage(IncomingMessage):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("消息不能是空白文本")
        # 保留原文用于记录和去重；识别订单号时再去除多余空白。
        return value


class OrderCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["order"]
    id: str

    @field_validator("id")
    @classmethod
    def valid_order_id(cls, value: str) -> str:
        # 只接收卡片的标识；展示字段和归属都由服务端重新查询。
        return OrderReference(kind="order_id", value=value).value


class ConfirmationCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["confirmation"]
    id: UUID
    decision: Literal["confirm", "reject"]


class OrderMessage(IncomingMessage):
    type: Literal["object"]
    object: Annotated[OrderCard | ConfirmationCard, Field(discriminator="type")]


def get_user_id(value: Annotated[str | None, Header(alias="X-User-Id")] = None) -> int:
    # 仅用于本机演示：对外部署必须由认证层提供身份，不能信任任意请求头。
    if value is None or len(value) > 19 or not value.isascii() or not value.isdecimal():
        raise UnauthenticatedError
    user_id = int(value)
    if not 0 < user_id < 2**63:
        raise UnauthenticatedError
    return user_id


def get_support_service(request: Request) -> SupportService:
    service = getattr(request.app.state, "support_service", None)
    if service is None:
        raise StorageUnavailableError
    return service


type UserId = Annotated[int, Depends(get_user_id)]
type Service = Annotated[SupportService, Depends(get_support_service)]
type Message = Annotated[TextMessage | OrderMessage, Body(discriminator="type")]


@router.post("/sessions", status_code=201)
async def create_session(user_id: UserId, service: Service) -> dict:
    return await service.create_session(user_id)


@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: UUID, body: Message, user_id: UserId, service: Service, request: Request) -> dict:
    return await service.message(
        session_id, user_id, body.message_id, body.model_dump(mode="json", exclude={"message_id"}), trace_for(request),
    )


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream_turn(
    accepted: dict, response: dict | asyncio.Task[dict], trace_id: str,
) -> AsyncIterator[str]:
    yield _event("accepted", accepted)
    try:
        if isinstance(response, asyncio.Task):
            while not response.done():
                finished, _ = await asyncio.wait({response}, timeout=4)
                if not finished:
                    yield ": waiting\n\n"
            response = response.result()
        # 只有完整轮次落库后才回放文本；对象和最终快照都由 done 一并返回。
        for index, message in enumerate(response["messages"]):
            if message["type"] == "text":
                text = message["text"]
                for offset in range(0, len(text), 28):
                    yield _event("delta", {"messageIndex": index, "text": text[offset:offset + 28]})
        yield _event("done", response)
    except Exception as error:
        _, payload = error_payload(error, trace_id)
        yield _event("error", payload)


@router.post("/sessions/{session_id}/messages/stream")
async def stream_message(
    session_id: UUID, body: Message, user_id: UserId, service: Service, request: Request,
) -> StreamingResponse:
    input_data = body.model_dump(mode="json", exclude={"message_id"})
    trace_id = trace_for(request)
    # 开流前先确认身份、归属和幂等冲突；这类错误仍正常返回 HTTP 状态码。
    claim = await service.reserve_message(session_id, user_id, body.message_id, input_data)
    if isinstance(claim, dict):
        turn_id = claim["turnId"]
        response = claim
    else:
        turn_id = str(claim.turn_id)
        response = service.start_reserved(claim, user_id, body.message_id, input_data, trace_id)
    accepted = {"sessionId": str(session_id), "messageId": str(body.message_id), "turnId": turn_id}
    return StreamingResponse(_stream_turn(accepted, response, trace_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@router.get("/sessions/{session_id}/messages")
async def get_history(
    session_id: UUID,
    user_id: UserId,
    service: Service,
    before_seq: Annotated[int | None, Query(alias="beforeSeq", ge=1)] = None,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
) -> dict:
    return await service.history(session_id, user_id, before_seq, page_size)


@router.get("/sessions/{session_id}/state")
async def get_state(session_id: UUID, user_id: UserId, service: Service) -> dict:
    return await service.state(session_id, user_id)


@router.get("/tickets/{ticket_id}")
async def get_ticket(
    ticket_id: Annotated[int, Path(gt=0, le=2**63 - 1)], user_id: UserId, service: Service,
) -> dict:
    return await service.get_ticket(user_id, ticket_id)


ERRORS = {
    UnauthenticatedError: (401, "UNAUTHENTICATED", "需要有效的用户身份。", False),
    SessionNotFoundError: (404, "SESSION_NOT_FOUND", "没有找到这个会话。", False),
    TicketNotFoundError: (404, "TICKET_NOT_FOUND", "没有找到这个工单。", False),
    MessageIdConflictError: (409, "MESSAGE_ID_CONFLICT", "相同消息 ID 不能用于不同内容。", False),
    TurnInProgressError: (409, "TURN_IN_PROGRESS", "这轮对话正在处理，请稍后重试。", True),
    RequestValidationError: (422, "INVALID_REQUEST", "请求格式不正确，请检查消息类型、字段或参数。", False),
    StorageUnavailableError: (503, "STORAGE_UNAVAILABLE", "客服数据库暂时不可用。", True),
    SQLAlchemyError: (503, "STORAGE_UNAVAILABLE", "客服数据库暂时不可用。", True),
    UpstreamUnavailableError: (503, "UPSTREAM_UNAVAILABLE", "旅游中台暂时无法确认用户身份。", True),
}


def trace_for(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or uuid4().hex


def error_payload(error: Exception, trace_id: str) -> tuple[int, dict]:
    # HTTP JSON 和 SSE 共用错误格式，绝不把数据库异常正文或旅客资料直接返回。
    kind = next((kind for kind in type(error).__mro__ if kind in ERRORS), None)
    status, code, message, retryable = ERRORS.get(kind, (500, "INTERNAL_ERROR", "处理失败，请稍后重试。", True))
    return status, {"error": {"code": code, "message": message, "traceId": trace_id, "retryable": retryable}}


def register_error_handlers(app: FastAPI) -> None:
    async def handle_error(request: Request, error: Exception) -> JSONResponse:
        trace_id = trace_for(request)
        status, payload = error_payload(error, trace_id)
        return JSONResponse(status_code=status, content=payload, headers={"X-Trace-Id": trace_id})

    for kind in (*ERRORS, Exception):
        app.add_exception_handler(kind, handle_error)
