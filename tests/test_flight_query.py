import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from engine.dialogue import DialogueEngine
from integration.llm import LlmClient
from integration.regions import AreaCatalog
from integration.travel_client import TravelClient
from plan.turn_planner import TurnPlanner
from task.action.products import ACTION_NAME, OUTCOMES, ProductAction
from task.flow.executor import TEMPLATES, DialogueState, FlowExecutor
from task.flow.loader import load_flows


@pytest.mark.asyncio
async def test_p05_flight_query_shows_only_prices_for_bookable_cabins():
    root = Path(__file__).resolve().parents[1]
    areas = AreaCatalog(root / "resources" / "travel_areas.csv")
    flows = load_flows(root / "flow_config" / "product_query.yaml", action_routes={ACTION_NAME: OUTCOMES}, templates=TEMPLATES)
    llm = Mock(spec=LlmClient, propose=AsyncMock(return_value=json.dumps({
        "route": "task", "command": "start_flow", "flow_id": "product_query", "product": {
            "kind": "flight", "origin": "北京", "destination": "上海", "date_text": "2026-11-20",
            "keyword": "CA1501", "detail": True,
        },
    })))
    travel = Mock(spec=TravelClient)
    # 模拟列表仍带着低价时，详情也要按舱位库存展示真正能订的价格。
    travel.search_products.return_value = {"total": 1, "list": [{
        "departureId": 733, "flightNo": "CA1501", "departureTime": "2026-11-20 13:00:00",
        "arrivalTime": "2026-11-20 15:10:00", "minSalePriceAmount": 1942, "currencyCode": "CNY",
    }]}
    travel.get_product.return_value = {"cabins": [
        {"cabinClassCode": "business", "salePriceAmount": 1942, "availableInventory": 0},
        {"cabinClassCode": "economy", "salePriceAmount": 2281, "availableInventory": 9},
    ]}
    executor = FlowExecutor(flows, {ACTION_NAME: ProductAction(travel, areas, "Asia/Shanghai")}, areas)
    engine = DialogueEngine(executor, TurnPlanner(llm))

    result = await engine.run(DialogueState(), 73, {
        "type": "text", "text": "2026-11-20 北京到上海 CA1501 航班舱位",
    })

    assert result.route == "task"
    assert result.state.active_task is None
    reply = result.messages[0]["text"]
    assert "CA1501" in reply and "13:00" in reply and "15:10" in reply
    assert "经济舱，2281.00 元，余 9 张" in reply
    assert "商务舱，不可售" in reply
    assert "1942" not in reply
    llm.propose.assert_awaited_once()
    travel.search_products.assert_awaited_once_with("flight", {
        "pageNo": 1, "pageSize": 20, "departureAreaId": 2, "arrivalAreaId": 786, "departureDate": "2026-11-20",
    })
    travel.get_product.assert_awaited_once_with("flight", 733)
