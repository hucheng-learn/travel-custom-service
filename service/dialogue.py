import asyncio
import json
import logging
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from engine.dialogue import DialogueEngine
from integration.travel_client import TravelClient, TravelClientError, TravelHTTPError
from plan.models import SLOT_FOR_FLOW
from repository.dialogue import DialogueRepository, ReserveResult, ReservedTurn
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
        # 为 SSE 建立后台轮次任务并纳入集合：连接断开不取消轮次，关闭进程前由 drain 等待落库。
        # claim 是本轮在数据库里的预留记录，user_id 和 input_data 来自这次请求，
        # worker 跑完会返回可以直接推给前端的响应字典。
        worker = asyncio.create_task(self.run_reserved(claim, user_id, message_id, input_data, trace_id))
        self._workers.add(worker)
        worker.add_done_callback(self._worker_done)
        return worker

    def _worker_done(self, worker: asyncio.Task[dict]) -> None:
        # 轮次结束后从集合移除；取一次异常，避免 asyncio 输出“未取回异常”的告警。
        self._workers.discard(worker)
        if not worker.cancelled():
            worker.exception()

    async def drain(self) -> None:
        # 应用关闭前等待所有在途轮次落库，避免对话执行到一半被中断。
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)

    async def get_ticket(self, user_id: int, ticket_id: int) -> dict:
        # 按工单主键查询，存储层用 user_id 限制只能查本人；工单功能未装配时按不存在处理。
        if self._operations is None:
            raise TicketNotFoundError
        return await self._operations.get_ticket(user_id, ticket_id)

    async def validate_identity(self, user_id: int) -> None:
        # 调中台 GET /me 校验身份：401/403/404 视为未认证，网络或响应异常视为上游不可用。
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
        # identity 是中台返回的当前用户信息，userId 必须和请求一致，statusCode 为 inactive 视为不可用。
        if "userId" not in identity or not isinstance(identity.get("statusCode"), str):
            raise UpstreamUnavailableError
        if str(identity["userId"]) != str(user_id) or identity["statusCode"] == "inactive":
            raise UnauthenticatedError

    async def create_session(self, user_id: int) -> dict:
        # 先确认中台身份，再创建归属该用户的空白会话。
        # row 是落库后的会话记录，初始状态是空的 DialogueState。
        await self.validate_identity(user_id)
        row = await self._repository.create(user_id, DialogueState().model_dump(mode="json"))
        return {"sessionId": str(row.id), "userId": row.user_id, "createdAt": _api_time(row.created_at)}

    async def list_sessions(self, user_id: int, page_no: int) -> dict:
        # 分页列出本人的会话，标题取首轮输入前 60 字，供客服页左栏的历史列表。
        page_size = 20
        # rows 每项是（会话 ID、最后更新时间、首条输入），多查一条用来判断还有没有下一页。
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
        # 调试页右侧卡片：tickets 读客服库，其余种类从中台拉本人订单、退款等卡片。
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
    ) -> ReserveResult:
        # 算出这条消息内容的哈希指纹；重试命中时直接回放旧结果，不再调用模型或中台。
        # digest 只用于判断“同一个 messageId 的内容有没有变”，不保存消息原文本身。
        digest = sha256(json.dumps(input_data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return await self._repository.reserve(session_id, user_id, message_id, digest, input_data)

    async def message(
        self, session_id: UUID, user_id: int, message_id: UUID, input_data: dict, trace_id: str = "-",
    ) -> dict:
        # 先占住这一轮并检查是不是重复消息；处理过的直接把上次结果返回。
        # reserve_result 二选一：replay 是已完成轮次的响应，claim 是新预留的轮次凭据。
        reserve_result = await self.reserve_message(session_id, user_id, message_id, input_data)
        if reserve_result.replay is not None:
            return reserve_result.replay  # 相同 messageId 和内容已经完成，直接重放，不再查询中台。
        assert reserve_result.claim is not None
        return await self.run_reserved(reserve_result.claim, user_id, message_id, input_data, trace_id)

    async def run_reserved(
        self, claim: ReservedTurn, user_id: int, message_id: UUID, input_data: dict, trace_id: str = "-",
    ) -> dict:
        try:
            # state 是上次提交的状态快照；result 带本轮新状态、走向和要返回的消息。
            state = DialogueState.model_validate(claim.snapshot)
            # 还原上次保存的会话状态，由引擎决定这一轮走哪条路径：
            # 识别意图、校验条件、推进查询流程，退款和工单走售后分支。
            result = await self._engine.run(state, user_id, input_data, claim)
        except TravelHTTPError as error:
            # 中台 401：标记本轮失败并释放处理标记，由路由转成未认证响应。
            await self._repository.fail(claim, "UNAUTHENTICATED")
            raise UnauthenticatedError from error
        except Exception as error:
            # 其他异常同样标记失败，按 traceId 记录轮次位置，不记录消息原文。
            await self._repository.fail(claim, "TURN_FAILED")
            logger.warning("support_turn trace_id=%s session_id=%s turn_id=%s error=%s",
                           trace_id, claim.session_id, claim.turn_id, type(error).__name__)
            raise
        response = {
            "sessionId": str(claim.session_id), "messageId": str(message_id), "turnId": str(claim.turn_id),
            "route": result.route, "messages": result.messages, "stateVersion": claim.version + 1,
        }
        # 新快照和回复在同一个短事务里提交，落库之后才返回或推送。
        await self._repository.finish(claim, result.state.model_dump(mode="json"), response)
        # task 只是打日志用：记录这轮结束后停在哪个流程和步骤。
        task = result.state.active_task
        logger.info("support_turn trace_id=%s session_id=%s turn_id=%s route=%s flow=%s step=%s",
                    trace_id, claim.session_id, claim.turn_id, result.route,
                    task.flow_id if task else "-", task.step_id if task else "-")
        return response

    async def history(self, session_id: UUID, user_id: int, before_seq: int | None, page_size: int) -> dict:
        # 按 seq 游标分页读取已完成轮次，只返回公开字段；nextBeforeSeq 供前台继续翻更早记录。
        # rows 是这一页的轮次，next_before_seq 为 None 表示没有更早的记录。
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
        # 读取最后一次已提交的状态快照，输出活动任务、缺槽位、待确认和暂停任务位置。
        row = await self._repository.get(session_id, user_id)
        # snapshot 是落库的完整会话状态，task 是其中的活动任务。
        snapshot = DialogueState.model_validate(row.state_json)
        task = snapshot.active_task
        # 暂停的任务只展示标识和位置，不暴露内部快照。
        # pending 是售后任务当前待确认的卡片，有值时把状态标成 waiting_confirmation。
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
