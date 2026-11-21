import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
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
@pytest.mark.parametrize("replies, city_id, district, has_match", [
    (["江苏省", "南京市"], "804", "江苏省 / 南京市 / 鼓楼区", False),
    (["江苏省", "江苏省 / 南京市下的鼓楼区有哪些景点"], "804", "江苏省 / 南京市 / 鼓楼区", False),
    (["福建省 / 福州市 / 鼓楼区"], "1140", "福建省 / 福州市 / 鼓楼区", True),
])
async def test_scenic_area_follow_up_narrows_candidates_and_keeps_date(replies, city_id, district, has_match):
    root = Path(__file__).resolve().parents[1]
    areas = AreaCatalog(root / "resources" / "travel_areas.csv")
    flows = load_flows(root / "flow_config" / "product_query.yaml", action_routes={ACTION_NAME: OUTCOMES}, templates=TEMPLATES)
    queries = []

    def scenic_inventory(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/scenic-spots"
        queries.append(dict(request.url.params))
        if request.url.params["areaId"] == "804":
            return httpx.Response(200, json={"list": [
                {"scenicName": "中山陵园风景区", "address": "江苏省南京市玄武区石象路7号"},
            ], "total": 1})
        if request.url.params["pageNo"] == "1":
            # 首屏都是同城其他区的景点，路名里出现“鼓楼区”也不能算命中。
            return httpx.Response(200, json={"list": [
                {"scenicName": "其他区景点", "address": "福建省福州市台江区鼓楼区路"},
            ] * 100, "total": 102})
        return httpx.Response(200, json={"list": [
            {"scenicName": "三坊七巷", "address": "福建省福州市鼓楼区南后街"},
            {"scenicName": "地址缺失的景点"},
        ], "total": 102})

    async with httpx.AsyncClient(base_url="http://test/api/v1/", transport=httpx.MockTransport(scenic_inventory)) as http:
        action = ProductAction(TravelClient(http, timeout_seconds=5), areas, "Asia/Shanghai")
        executor = FlowExecutor(flows, {ACTION_NAME: action}, areas)
        plans = [{"route": "task", "command": "start_flow", "flow_id": "product_query", "product": {
            "kind": "scenic", "area": "鼓楼区", "date_text": "2026-11-20",
        }}]
        plans.extend({"route": "task", "command": "set_slots", "product": {
            "kind": "scenic", "area": reply.removesuffix("有哪些景点"),
        }} for reply in replies)
        llm = Mock(spec=LlmClient, propose=AsyncMock(side_effect=[json.dumps(plan) for plan in plans]))
        engine = DialogueEngine(executor, TurnPlanner(llm))

        result = await engine.run(DialogueState(), 73, {"type": "text", "text": "2026-11-20 鼓楼区有哪些景点"})
        assert "福州市 / 鼓楼区" in result.messages[0]["text"]
        if len(replies) == 2:
            result = await engine.run(result.state, 73, {"type": "text", "text": replies[0]})
            candidates = result.messages[0]["text"]
            assert "南京市 / 鼓楼区" in candidates and "徐州市 / 鼓楼区" in candidates
            assert "福州市 / 鼓楼区" not in candidates and "开封市 / 鼓楼区" not in candidates
        assert queries == []

        result = await engine.run(result.state, 73, {"type": "text", "text": replies[-1]})
        assert queries == [
            {"areaId": city_id, "travelDate": "2026-11-20", "pageNo": page_no, "pageSize": "100"}
            for page_no in (["1", "2"] if has_match else ["1"])
        ]
        message = result.messages[0]["text"]
        assert f"2026-11-20 在{district}" in message
        if has_match:
            assert "三坊七巷" in message
            assert "其他区景点" not in message and "地址缺失的景点" not in message
        else:
            assert "没有查到" in message and "中山陵园风景区" not in message
        assert llm.propose.await_count == len(plans)
