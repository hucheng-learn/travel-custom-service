import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from engine.dialogue import DialogueEngine
from integration.llm import LlmClient
from integration.travel_client import TravelClient
from plan.models import ITINERARY
from plan.turn_planner import TurnPlanner
from task.action.itinerary import ACTION_NAME, OrderItineraryAction
from task.action.order_status import ACTION_NAME as STATUS_ACTION_NAME
from task.action.order_status import OUTCOMES, OrderStatusAction
from task.flow.executor import TEMPLATES, DialogueState, FlowExecutor
from task.flow.loader import load_flows


@pytest.mark.asyncio
async def test_pending_order_card_answers_followup():
    root = Path(__file__).resolve().parents[1]
    routes = {ACTION_NAME: OUTCOMES, STATUS_ACTION_NAME: OUTCOMES}
    flows = load_flows(root / "flow_config" / "itinerary_query.yaml", action_routes=routes, templates=TEMPLATES)
    flows.update(load_flows(root / "flow_config" / "order_status_query.yaml", action_routes=routes, templates=TEMPLATES))

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-User-Id"] == "73"
        assert request.url.path == "/api/v1/orders/73"
        return httpx.Response(200, json={"orderId": 73, "orderNo": "ORD0000000073", "statusCode": "paid", "items": [{
            "productTypeCode": "hotel_room", "productName": "套房", "hotelName": "白天鹅宾馆", "roomTypeName": "套房",
            "travelTime": "2027-01-07 00:00:00", "travelEndTime": "2027-01-10 00:00:00",
            "travelerName": "张旅客", "documentNumber": "TEST-DOCUMENT", "phone": "TEST-PHONE",
        }]})

    async with httpx.AsyncClient(base_url="http://test/api/v1/", transport=httpx.MockTransport(upstream)) as http:
        travel = TravelClient(http, timeout_seconds=5)
        executor = FlowExecutor(flows, {
            ACTION_NAME: OrderItineraryAction(travel, "Asia/Shanghai"),
            STATUS_ACTION_NAME: OrderStatusAction(travel, "Asia/Shanghai"),
        })
        llm = Mock(spec=LlmClient, propose=AsyncMock(return_value=json.dumps({
            "route": "task", "command": "start_flow", "flow_id": "itinerary_query", "use_current_order": True,
        })))
        engine = DialogueEngine(executor, TurnPlanner(llm))

        # 发送订单卡片时没有进行中的任务：先暂存这笔订单并追问要查哪项。
        result = await engine.run(DialogueState(), 73, {"type": "object", "object": {"type": "order", "id": "73"}})
        assert result.route == "clarify"
        assert result.state.pending_order_id == 73

        # 模拟两次请求之间从会话快照恢复，回答“行程”时直接用暂存的订单查行程。
        state = DialogueState.model_validate(result.state.model_dump(mode="json"))
        result = await engine.run(state, 73, {"type": "text", "text": "行程"})
        assert result.state.pending_order_id is None
        assert result.state.active_task is None
        reply = result.messages[0]["text"]
        assert all(field in reply for field in ("白天鹅宾馆", "入住 2027-01-07", "退房 2027-01-10"))
        llm.propose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "previous_order_query"),
    [("帮我查行程", False), ("具体是哪一天入住的", False), ("具体是哪一天入住的", True)],
)
async def test_order_itinerary_displays_hotel_stay(question, previous_order_query):
    root = Path(__file__).resolve().parents[1]
    routes = {ACTION_NAME: OUTCOMES, STATUS_ACTION_NAME: OUTCOMES}
    flows = load_flows(root / "flow_config" / "itinerary_query.yaml", action_routes=routes, templates=TEMPLATES)
    flows.update(load_flows(root / "flow_config" / "order_status_query.yaml", action_routes=routes, templates=TEMPLATES))
    order_number = "ORD0000000073"

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-User-Id"] == "73"
        if request.url.path == "/api/v1/orders":
            return httpx.Response(200, json={"list": [{"orderId": 73, "orderNo": order_number}], "total": 1})
        if request.url.path == "/api/v1/orders/73/payments":
            return httpx.Response(200, json={"list": []})
        assert request.url.path == "/api/v1/orders/73"
        # 同一个订单明细同时包含展示字段和额外字段，确认回复只放所需信息。
        return httpx.Response(200, json={"orderId": 73, "orderNo": order_number, "statusCode": "paid", "items": [{
            "productTypeCode": "hotel_room", "productName": "套房", "hotelName": "白天鹅宾馆", "roomTypeName": "套房",
            "travelTime": "2027-01-07 00:00:00", "travelEndTime": "2027-01-10 00:00:00",
            "travelerName": "张旅客", "documentNumber": "TEST-DOCUMENT", "phone": "TEST-PHONE",
        }]})

    async with httpx.AsyncClient(base_url="http://test/api/v1/", transport=httpx.MockTransport(upstream)) as http:
        travel = TravelClient(http, timeout_seconds=5)
        executor = FlowExecutor(flows, {
            ACTION_NAME: OrderItineraryAction(travel, "Asia/Shanghai"),
            STATUS_ACTION_NAME: OrderStatusAction(travel, "Asia/Shanghai"),
        })
        llm = Mock(spec=LlmClient, propose=AsyncMock(return_value='{"route":"clarify"}'))
        engine = DialogueEngine(executor, TurnPlanner(llm))

        state = DialogueState()
        if previous_order_query:
            llm.propose.return_value = json.dumps({
                "route": "task", "command": "start_flow", "flow_id": "order_status_query",
                "order_number": order_number,
            })
            result = await engine.run(state, 73, {"type": "text", "text": f"帮我查下订单 {order_number}"})
            assert result.state.current_order_id == 73
            assert result.state.active_task is None
            llm.propose.assert_awaited_once()
            llm.propose.reset_mock()
            # 模拟两次 API 请求之间从会话快照恢复，而不是沿用内存对象。
            state = DialogueState.model_validate(result.state.model_dump(mode="json"))

        llm.propose.return_value = json.dumps({
            "route": "task", "command": "start_flow", "flow_id": "itinerary_query", "use_current_order": previous_order_query,
        })
        result = await engine.run(state, 73, {"type": "text", "text": question})
        if not previous_order_query:
            assert result.state.active_task is not None and result.state.active_task.flow_id == ITINERARY
            assert "订单号" in result.messages[0]["text"]
            state = DialogueState.model_validate(result.state.model_dump(mode="json"))
            llm.propose.return_value = json.dumps({"route": "task", "command": "set_slots", "order_number": order_number})
            result = await engine.run(state, 73, {"type": "text", "text": order_number})
        assert result.state.active_task is None
        reply = result.messages[0]["text"]
        assert all(field in reply for field in ("白天鹅宾馆", "套房", "入住 2027-01-07", "退房 2027-01-10", "入住人 张**"))
        assert all(field not in reply for field in ("张旅客", "TEST-DOCUMENT", "TEST-PHONE"))
        assert llm.propose.await_count == (1 if previous_order_query else 2)
