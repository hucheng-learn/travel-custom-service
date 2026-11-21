from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from engine.dialogue import DialogueEngine
from integration.llm import LlmClient
from integration.travel_client import TravelClient
from plan.input_values import extract_order_number
from plan.travel_dates import business_zone
from plan.turn_planner import TurnPlanner
from repository.dialogue import ReservedTurn
from repository.operations import OperationRepository
from task.after_sales import AfterSales, load_sales_config
from task.flow.executor import DialogueState, FlowExecutor


@pytest.mark.parametrize('text', [
    'ORD0000000073', '订单 ORD0000000073', '订单：ORD0000000073',
    '订单ORD0000000073', '订单号 ORD0000000073', '订单号：ORD0000000073。',
])
def test_extract_order_number_accepts_common_labels(text):
    # 用户输入“订单 + 编号”与“订单号 + 编号”含义相同，都应得到完整编号。
    assert extract_order_number(text) == 'ORD0000000073'


@pytest.mark.parametrize('text', [
    '订单', '订单号', '2026-11-20', '订单 2026-11-20',
    '订单 ORD0000000073 ORD0000000199', '取消订单 ORD0000000073',
])
def test_extract_order_number_does_not_guess_from_ambiguous_input(text):
    # 保留整句匹配和日期排除，不能因放宽前缀而截取多个订单或业务指令中的一段。
    assert extract_order_number(text) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('owner_id', [73, 199])
@pytest.mark.parametrize('incoming', [
    {'type': 'text', 'text': '订单 ORD0000000073'},
    {'type': 'text', 'text': 'ORD0000000073'},
    {'type': 'object', 'object': {'type': 'order', 'id': '73'}},
])
async def test_refund_order_input_validates_ownership_before_advancing(incoming, owner_id):
    # 复现截图的两轮对话并恢复快照；只模拟订单读取，不发真实退款或数据库写操作。
    travel = Mock(spec=TravelClient)
    travel.list_orders = AsyncMock(return_value={
        'total': 1, 'list': [{'orderId': 73, 'orderNo': 'ORD0000000073'}],
    })
    travel.get_order = AsyncMock(return_value={
        'orderId': 73, 'orderNo': 'ORD0000000073', 'userId': owner_id,
        'items': [{'orderItemId': 73}],
    })
    operations = Mock(spec=OperationRepository)
    plans = ['{"route":"task","command":"start_flow","flow_id":"refund_request"}']
    if incoming['type'] == 'text':
        plans.append('{"route":"task","command":"set_slots","order_number":"ORD0000000073"}')
    llm = Mock(spec=LlmClient, propose=AsyncMock(side_effect=plans))
    config = load_sales_config(Path(__file__).resolve().parents[1] / 'flow_config' / 'after_sales.yaml')
    sales = AfterSales(config, travel, operations, business_zone('Asia/Shanghai'), False)
    engine = DialogueEngine(FlowExecutor({}, {}), TurnPlanner(llm), after_sales=sales)
    claim = ReservedTurn(uuid4(), uuid4(), uuid4(), 0, {})

    first = await engine.run(DialogueState(), 73, {'type': 'text', 'text': '我想退款'}, claim)
    assert first.state.active_task.step_id == 'order'
    state = DialogueState.model_validate(first.state.model_dump(mode='json'))
    second = await engine.run(state, 73, incoming, claim)

    task = second.state.active_task
    if owner_id == 73:
        assert task.sales.order_id == 73
        assert task.sales.order_no == 'ORD0000000073'
        assert task.sales.item_id == 73
        assert task.step_id == 'refund_type'
        assert task.missing_slots == ['refund_type']
    else:
        # 放宽文本前缀后仍不能接受别人的订单，必须留在订单收集步骤并说明原因。
        assert task.sales.order_id is None
        assert task.step_id == 'order'
        assert '本人' in second.messages[0]['text']
    travel.get_order.assert_awaited_once_with(73, 73)
    assert llm.propose.await_count == len(plans)
    assert operations.mock_calls == []
