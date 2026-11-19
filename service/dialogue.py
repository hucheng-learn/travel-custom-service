import asyncio
import json
import logging
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from engine.dialogue import DialogueEngine
from integration.travel_client import TravelClient, TravelClientError, TravelHTTPError
from plan.models import SLOT_FOR_FLOW
from repository.dialogue import DialogueRepository, ReservedTurn
from repository.operations import OperationRepository, TicketNotFoundError
from service.objects import ObjectKind, travel_objects
from task.flow.executor import DialogueState


logger = logging.getLogger("uvicorn.error")


class UnauthenticatedError(PermissionError):
    pass


class UpstreamUnavailableError(RuntimeError):
    pass


def _api_time(value: datetime) -> str:
    return value.replace(tzinfo=UTC).isoformat()


class SupportService:
    def __init__(
        self, repository: DialogueRepository, engine: DialogueEngine, travel: TravelClient | None,
        operations: OperationRepository | None = None,
    ) -> None:
        self._repository = repository
        self._engine = engine
        self._travel = travel
        self._operations = operations
        self._workers: set[asyncio.Task[dict]] = set()

    def start_reserved(
        self, claim: ReservedTurn, user_id: int, message_id: UUID, input_data: dict, trace_id: str,
    ) -> asyncio.Task[dict]:
        # SSE 连接断开不取消业务任务；进程正常关闭时先等待这些轮次落库。
        worker = asyncio.create_task(self.run_reserved(claim, user_id, message_id, input_data, trace_id))
        self._workers.add(worker)
        worker.add_done_callback(self._worker_done)
        return worker

    def _worker_done(self, worker: asyncio.Task[dict]) -> None:
        self._workers.discard(worker)
        if not worker.cancelled():
            worker.exception()

    async def drain(self) -> None:
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)

    async def get_ticket(self, user_id: int, ticket_id: int) -> dict:
        if self._operations is None:
            raise TicketNotFoundError
        return await self._operations.get_ticket(user_id, ticket_id)

    async def validate_identity(self, user_id: int) -> None:
        if self._travel is None:
            raise UpstreamUnavailableError
        try:
            identity = await self._travel.get_me(user_id)
        except TravelHTTPError as error:
            if error.status_code in {401, 403, 404}:
                raise UnauthenticatedError from error
            raise UpstreamUnavailableError from error
        except TravelClientError as error:
            raise UpstreamUnavailableError from error
        if "userId" not in identity or not isinstance(identity.get("statusCode"), str):
            raise UpstreamUnavailableError
        if str(identity["userId"]) != str(user_id) or identity["statusCode"] == "inactive":
            raise UnauthenticatedError

    async def create_session(self, user_id: int) -> dict:
        # 先确认中台身份，再创建归属该用户的空白会话。
        await self.validate_identity(user_id)
        row = await self._repository.create(user_id, DialogueState().model_dump(mode="json"))
        return {"sessionId": str(row.id), "userId": row.user_id, "createdAt": _api_time(row.created_at)}

    async def list_sessions(self, user_id: int, page_no: int) -> dict:
        page_size = 20
        rows = await self._repository.list_sessions(user_id, (page_no - 1) * page_size, page_size + 1)
        sessions = []
        for session_id, updated_at, first_input in rows[:page_size]:
            title = "新对话"
            if first_input and first_input["type"] == "text":
                title = " ".join(first_input["text"].split())[:60] or title
            elif first_input and first_input.get("object", {}).get("type") == "order":
                title = "订单咨询"
            sessions.append({"sessionId": str(session_id), "title": title, "updatedAt": _api_time(updated_at)})
        return {"list": sessions, "hasMore": len(rows) > page_size}

    async def list_objects(self, user_id: int, kind: ObjectKind, page_no: int) -> dict:
        if kind == "tickets":
            page_size = 10
            rows = await self._operations.list_tickets(
                user_id, (page_no - 1) * page_size, page_size + 1,
            ) if self._operations is not None else []
            return {"list": rows[:page_size], "hasMore": len(rows) > page_size}

        if self._travel is None:
            raise UpstreamUnavailableError
        try:
            cards, has_more = await travel_objects(self._travel, user_id, kind, page_no)
        except TravelHTTPError as error:
            if error.status_code in {401, 403}:
                raise UnauthenticatedError from error
            raise UpstreamUnavailableError from error
        except (TravelClientError, ValueError) as error:
            # 中台异常单独返回，页面可以就地重试，不当成空列表。
            raise UpstreamUnavailableError from error
        return {"list": cards, "hasMore": has_more}

    async def reserve_message(
        self, session_id: UUID, user_id: int, message_id: UUID, input_data: dict,
    ) -> ReservedTurn | dict:
        # 文本和卡片的内容都参与摘要；重试命中时不重复调用模型或中台。
        digest = sha256(json.dumps(input_data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return await self._repository.reserve(session_id, user_id, message_id, digest, input_data)

    async def message(
        self, session_id: UUID, user_id: int, message_id: UUID, input_data: dict, trace_id: str = "-",
    ) -> dict:
        claim = await self.reserve_message(session_id, user_id, message_id, input_data)
        if isinstance(claim, dict):
            return claim  # 相同 messageId 和内容已经完成，直接重放，不再查询中台。
        return await self.run_reserved(claim, user_id, message_id, input_data, trace_id)

    async def run_reserved(
        self, claim: ReservedTurn, user_id: int, message_id: UUID, input_data: dict, trace_id: str = "-",
    ) -> dict:
        try:
            state = DialogueState.model_validate(claim.snapshot)
            result = await self._engine.run(state, user_id, input_data, claim)
        except TravelHTTPError as error:
            await self._repository.fail(claim, "UNAUTHENTICATED")
            raise UnauthenticatedError from error
        except Exception as error:
            await self._repository.fail(claim, "TURN_FAILED")
            logger.warning("support_turn trace_id=%s session_id=%s turn_id=%s error=%s",
                           trace_id, claim.session_id, claim.turn_id, type(error).__name__)
            raise
        response = {
            "sessionId": str(claim.session_id), "messageId": str(message_id), "turnId": str(claim.turn_id),
            "route": result.route, "messages": result.messages, "stateVersion": claim.version + 1,
        }
        await self._repository.finish(claim, result.state.model_dump(mode="json"), response)
        task = result.state.active_task
        logger.info("support_turn trace_id=%s session_id=%s turn_id=%s route=%s flow=%s step=%s",
                    trace_id, claim.session_id, claim.turn_id, result.route,
                    task.flow_id if task else "-", task.step_id if task else "-")
        return response

    async def history(self, session_id: UUID, user_id: int, before_seq: int | None, page_size: int) -> dict:
        rows, next_before_seq = await self._repository.history(session_id, user_id, before_seq, page_size)
        return {
            "sessionId": str(session_id),
            "list": [
                {
                    "seq": row.seq, "turnId": str(row.id), "messageId": str(row.message_id), "input": row.input_json,
                    "route": row.response_json["route"], "messages": row.response_json["messages"],
                    "createdAt": _api_time(row.created_at),
                }
                for row in rows if row.response_json is not None
            ],
            "nextBeforeSeq": next_before_seq,
        }

    async def state(self, session_id: UUID, user_id: int) -> dict:
        row = await self._repository.get(session_id, user_id)
        snapshot = DialogueState.model_validate(row.state_json)
        task = snapshot.active_task
        # 暂停的任务只展示标识和位置，不暴露内部快照。
        pending = task.sales.confirmation if task and task.sales else None
        active = None if task is None else {
            "taskId": str(task.task_id), "flowId": task.flow_id, "stepId": task.step_id,
            "status": "waiting_confirmation" if pending else "waiting_slot", "slots": task.model_dump(mode="json")["slots"],
            "missingSlots": task.missing_slots if task.missing_slots is not None else [SLOT_FOR_FLOW[task.flow_id]],
            "pendingConfirmationId": str(pending.id) if pending else None,
        }
        return {
            "sessionId": str(row.id), "version": row.version, "activeTask": active,
            "pausedTasks": [
                {"taskId": str(paused.task_id), "flowId": paused.flow_id, "stepId": paused.step_id}
                for paused in snapshot.paused_tasks
            ],
            "updatedAt": _api_time(row.updated_at),
        }
