import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from engine.dialogue import DialogueEngine
from integration.llm import LlmClient
from integration.regions import AreaCatalog
from integration.travel_client import TravelClient
from plan.models import PRODUCT
from plan.turn_planner import TurnPlanner
from task.action.products import ACTION_NAME, OUTCOMES, ProductAction
from task.flow.executor import TEMPLATES, DialogueState, FlowExecutor
from task.flow.loader import load_flows


@pytest.mark.asyncio
async def test_filtered_hotels_clear_old_name_and_keep_dates_for_room_types():
    root = Path(__file__).resolve().parents[1]
    areas = AreaCatalog(root / "resources" / "travel_areas.csv")
    flows = load_flows(root / "flow_config" / "product_query.yaml", action_routes={ACTION_NAME: OUTCOMES}, templates=TEMPLATES)
    hotel = {
        "hotelId": 36, "hotelName": "三亚亚龙湾迎宾馆", "starRatingCode": "5", "hotelTypeCode": "resort",
        "minSalePriceAmount": 1833.72, "currencyCode": "CNY", "availableRoomCount": 4,
    }
    hotels = [hotel, {**hotel, "hotelName": "四星度假酒店", "starRatingCode": "4"},
              {**hotel, "hotelName": "五星商务酒店", "hotelTypeCode": "business"}]
    dates = {"checkInDate": "2026-11-20", "checkOutDate": "2026-11-21"}
    queries = []

    def upstream(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if request.url.path == "/api/v1/hotels":
            queries.append(dict(params))
            stars, types = params.get_list("starRatingCodes"), params.get_list("hotelTypeCodes")
            # 模拟中台按两个字段各自过滤，缺少任一条件都会混入不符合要求的酒店。
            matches = [row for row in hotels
                       if (not stars or row["starRatingCode"] in stars)
                       and (not types or row["hotelTypeCode"] in types)
                       and params.get("keyword", "") in row["hotelName"]]
            return httpx.Response(200, json={"list": matches, "total": len(matches)})
        if request.url.path == "/api/v1/hotels/36":
            return httpx.Response(200, json=hotel)
        assert request.url.path == "/api/v1/hotels/36/room-types"
        assert dict(params) == dates
        return httpx.Response(200, json={"list": [{"roomTypeName": "家庭房", "firstNightSalePriceAmount": 1932.84}]})

    async with httpx.AsyncClient(base_url="http://test/api/v1/", transport=httpx.MockTransport(upstream)) as http:
        travel = TravelClient(http, timeout_seconds=5)
        executor = FlowExecutor(flows, {ACTION_NAME: ProductAction(travel, areas, "Asia/Shanghai")}, areas)
        plans = [{"route": "task", "command": "set_slots", "product": {
            "kind": "hotel", "area": "三亚", "date_text": "2026-11-20", "end_date_text": "2026-11-21",
            "hotel_star_rating": "5", "hotel_star_text": "五星级", "hotel_type": "resort", "hotel_type_text": "度假",
        }}, {"route": "task", "command": "set_slots", "product": {
            "kind": "hotel", "keyword": "三亚亚龙湾迎宾馆", "detail": True,
        }}]
        llm = Mock(spec=LlmClient, propose=AsyncMock(side_effect=[json.dumps(plan) for plan in plans]))
        engine = DialogueEngine(executor, TurnPlanner(llm))
        task = executor.start(PRODUCT)
        task.step_id = "collect_product"
        # 复现老会话将筛选条件错误留在酒店名里的情况，且快照里没有新字段。
        state = DialogueState.model_validate({"active_task": {
            **task.model_dump(mode="json"),
            "slots": {"product_request": {"kind": "hotel", "keyword": "五星级度假酒店"}},
        }})
        result = await engine.run(state, 73, {
            "type": "text", "text": "2026-11-20 入住、2026-11-21 退房，三亚有哪些五星级度假酒店",
        })
        assert queries == [{**dates, "areaId": "2197", "pageNo": "1", "pageSize": "20",
                            "starRatingCodes": "5", "hotelTypeCodes": "resort"}]
        reply = result.messages[0]["text"]
        assert all(item in reply for item in ("三亚亚龙湾迎宾馆", "5 星", "度假型", "1833.72 元起", "房型"))
        assert "四星度假酒店" not in reply and "五星商务酒店" not in reply

        result = await engine.run(result.state, 73, {"type": "text", "text": "三亚亚龙湾迎宾馆的房型"})
        assert queries[-1] == {**queries[0], "keyword": "三亚亚龙湾迎宾馆"}
        assert "家庭房" in result.messages[0]["text"]
        assert "1932.84" in result.messages[0]["text"]
