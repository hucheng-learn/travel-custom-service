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
from task.action.order_status import OUTCOMES
from task.flow.executor import TEMPLATES, DialogueState, FlowExecutor
from task.flow.loader import load_flows


@pytest.mark.asyncio
async def test_order_itinerary_collects_order_number_and_displays_hotel_stay():
    root = Path(__file__).resolve().parents[1]
    flows = load_flows(root / "flow_config" / "itinerary_query.yaml", action_routes={ACTION_NAME: OUTCOMES}, templates=TEMPLATES)
    order_number = "ORD0000000073"

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-User-Id"] == "73"
        if request.url.path == "/api/v1/orders":
            return httpx.Response(200, json={"list": [{"orderId": 73, "orderNo": order_number}], "total": 1})
        assert request.url.path == "/api/v1/orders/73"
        # 同一个订单明细同时包含展示字段和额外字段，确认回复只放所需信息。
        return httpx.Response(200, json={"orderId": 73, "orderNo": order_number, "items": [{
            "productTypeCode": "hotel_room", "productName": "套房", "hotelName": "白天鹅宾馆", "roomTypeName": "套房",
            "travelTime": "2027-01-07 00:00:00", "travelEndTime": "2027-01-10 00:00:00",
            "travelerName": "张旅客", "documentNumber": "TEST-DOCUMENT", "phone": "TEST-PHONE",
        }]})

    async with httpx.AsyncClient(base_url="http://test/api/v1/", transport=httpx.MockTransport(upstream)) as http:
        action = OrderItineraryAction(TravelClient(http, timeout_seconds=5), "Asia/Shanghai")
        executor = FlowExecutor(flows, {ACTION_NAME: action})
        llm = Mock(spec=LlmClient, propose=AsyncMock(return_value='{"route":"clarify"}'))
        engine = DialogueEngine(executor, TurnPlanner(llm))

        result = await engine.run(DialogueState(), 73, {"type": "text", "text": "帮我查行程"})
        assert result.state.active_task is not None and result.state.active_task.flow_id == ITINERARY
        assert "订单号" in result.messages[0]["text"]

        result = await engine.run(result.state, 73, {"type": "text", "text": order_number})
        assert result.state.active_task is None
        reply = result.messages[0]["text"]
        assert all(field in reply for field in ("白天鹅宾馆", "套房", "入住 2027-01-07", "退房 2027-01-10", "入住人 张**"))
        assert all(field not in reply for field in ("张旅客", "TEST-DOCUMENT", "TEST-PHONE"))
        llm.propose.assert_not_awaited()
