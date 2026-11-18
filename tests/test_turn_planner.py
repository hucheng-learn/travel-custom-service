from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from integration.llm import LlmClient
from integration.regions import AreaCatalog
from plan.models import PRODUCT
from plan.product_models import ProductClues
from plan.turn_planner import TurnPlanner
from task.flow.executor import DialogueState


@pytest.mark.asyncio
async def test_complete_hotel_detail_uses_conditions_from_the_message():
    areas = AreaCatalog(Path(__file__).resolve().parents[1] / "resources" / "travel_areas.csv")
    # 用不符合计划结构的模型响应复现 P01，确认房型查询可以直接使用原文条件。
    llm = Mock(spec=LlmClient, propose=AsyncMock(return_value="[]"))
    planner = TurnPlanner(llm, areas)
    text = "2026-11-20 入住、2026-11-21 退房，北京「北京饭店诺金」的房型详情"

    plan = await planner.plan(text, DialogueState(), waiting_for_order=False)

    assert (plan.route, plan.command, plan.flow_id) == ("task", "start_flow", PRODUCT)
    assert plan.product == ProductClues(
        kind="hotel", area="北京", keyword="北京饭店诺金",
        date_text="2026-11-20", end_date_text="2026-11-21", detail=True,
    )
    llm.propose.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "keyword", "model_calls"),
    [
        ("2026-11-20 北京有哪些景点", None, 1),
        ("2026-11-20 北京「故宫博物院」门票票种和开放时间", "故宫博物院", 0),
    ],
)
async def test_scenic_query_keeps_original_conditions_when_model_plan_is_invalid(text, keyword, model_calls):
    areas = AreaCatalog(Path(__file__).resolve().parents[1] / "resources" / "travel_areas.csv")
    # 复现 P04 的格式错误：模型把是否查详情写成了文本。
    llm = Mock(spec=LlmClient, propose=AsyncMock(return_value='{"route":"task","product":{"detail":"票种和开放时间"}}'))

    plan = await TurnPlanner(llm, areas).plan(text, DialogueState(), waiting_for_order=False)

    assert (plan.route, plan.command, plan.flow_id) == ("task", "start_flow", PRODUCT)
    assert plan.product == ProductClues(
        kind="scenic", area="北京", date_text="2026-11-20", keyword=keyword, detail=keyword is not None,
    )
    assert llm.propose.await_count == model_calls
