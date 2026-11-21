import json
from unittest.mock import AsyncMock, Mock

import pytest

from integration.llm import LlmClient
from plan.models import PRODUCT
from plan.product_models import ProductClues
from plan.turn_planner import TurnPlanner
from task.flow.executor import DialogueState


@pytest.mark.asyncio
async def test_complete_hotel_detail_uses_conditions_from_the_message():
    # 模型理解用户表达，业务测试固定模型输出，核对原有查询条件仍完整传递。
    llm = Mock(spec=LlmClient, propose=AsyncMock(return_value=json.dumps({
        "route": "task", "command": "start_flow", "flow_id": "product_query", "product": {
            "kind": "hotel", "area": "北京", "keyword": "北京饭店诺金",
            "date_text": "2026-11-20", "end_date_text": "2026-11-21", "detail": True,
        },
    })))
    planner = TurnPlanner(llm)
    text = "2026-11-20 入住、2026-11-21 退房，北京「北京饭店诺金」的房型详情"

    plan = await planner.plan(text, DialogueState())

    assert (plan.route, plan.command, plan.flow_id) == ("task", "start_flow", PRODUCT)
    assert plan.product == ProductClues(
        kind="hotel", area="北京", keyword="北京饭店诺金",
        date_text="2026-11-20", end_date_text="2026-11-21", detail=True,
    )
    llm.propose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "2026-11-20 北京有哪些景点",
        "2026-11-20 北京「故宫博物院」门票票种和开放时间",
    ],
)
async def test_invalid_model_plan_does_not_guess_a_product_query(text):
    # 复现 P04 的格式错误：模型把是否查详情写成了文本。
    llm = Mock(spec=LlmClient, propose=AsyncMock(return_value='{"route":"task","product":{"detail":"票种和开放时间"}}'))

    plan = await TurnPlanner(llm).plan(text, DialogueState())

    assert plan.route == "clarify"
    assert plan.clarify_reason == "invalid_model_output"
    assert llm.propose.await_count == 2  # 结构修正一次仍无效，不能回退为自由文本商品查询。
