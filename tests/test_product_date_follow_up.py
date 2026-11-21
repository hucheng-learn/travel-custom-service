import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.main import load_supported_flows
from engine.dialogue import DialogueEngine
from integration.llm import LlmClient
from integration.regions import AreaCatalog
from integration.travel_client import TravelClient
from plan.models import DATE_ITINERARY, PRODUCT, TurnPlan
from plan.product_models import ProductRequest
from plan.travel_dates import business_zone
from plan.turn_planner import TurnPlanner
from plan.validator import PlanRejectedError, TurnPlanValidator
from task.action.date_itinerary import ACTION_NAME as DATE_ACTION, DateItineraryAction
from task.action.products import ACTION_NAME, ProductAction
from task.flow.executor import DialogueState, FlowExecutor


@pytest.fixture
def system(monkeypatch):
    # 固定业务日期并模拟中台，只验证真实规划/校验/执行链路，不访问模型、数据库或真实库存。
    monkeypatch.setattr('plan.validator.calendar_today', lambda zone: date(2026, 11, 21))
    monkeypatch.setattr('plan.turn_planner.calendar_today', lambda zone: date(2026, 11, 21))
    areas = AreaCatalog(Path(__file__).resolve().parents[1] / 'resources/travel_areas.csv')
    travel = Mock(spec=TravelClient)
    travel.search_products.return_value = {'list': [
        {'scenicName': '三坊七巷', 'address': '福建省福州市鼓楼区南后街'},
        {'scenicName': '其他区景点', 'address': '福建省福州市台江区其他路'},
    ], 'total': 2}
    travel.list_orders.return_value = {'list': [], 'total': 0}
    executor = FlowExecutor(load_supported_flows(), {
        ACTION_NAME: ProductAction(travel, areas, 'Asia/Shanghai'),
        DATE_ACTION: DateItineraryAction(travel, business_zone('Asia/Shanghai')),
    }, areas)
    llm = Mock(spec=LlmClient, propose=AsyncMock())
    return SimpleNamespace(
        executor=executor, travel=travel, llm=llm, validator=TurnPlanValidator(executor),
        engine=DialogueEngine(executor, TurnPlanner(llm)),
    )


def waiting_state(system, **changes):
    task = system.executor.start(PRODUCT)
    task.step_id = 'collect_product'
    task.missing_slots = ['business_date']
    task.slots['product_request'] = ProductRequest(kind='scenic', area='福建省 / 福州市 / 鼓楼区', **changes)
    return DialogueState(active_task=task)


async def say(system, text, state):
    # 每轮从 JSON 快照恢复，避免仅在同一个内存对象中成立的跨轮结果。
    state = DialogueState.model_validate(state.model_dump(mode='json'))
    return await system.engine.run(state, 73, {'type': 'text', 'text': text})


@pytest.mark.asyncio
@pytest.mark.parametrize('date_payload', [
    {'date_text': '明天'},
    {'date_text': '明天', 'date_value': '2026-11-22'},
    {'product': {'date_text': '明天'}},
    {'date_text': '明天', 'product': {'date_text': '明天', 'date_value': '2026-11-22'}},
])
async def test_district_choice_then_date_reaches_scenic_query(system, date_payload):
    plans = [
        {'route': 'task', 'command': 'start_flow', 'flow_id': PRODUCT,
         'product': {'kind': 'scenic', 'area': '鼓楼区'}},
        {'route': 'task', 'command': 'set_slots', 'product': {'area': '福建省 / 福州市 / 鼓楼区'}},
        {'route': 'task', 'command': 'set_slots', **date_payload},
    ]
    system.llm.propose.side_effect = [json.dumps(plan) for plan in plans]
    first = await say(system, '鼓楼区都有什么景点', DialogueState())
    assert '多个候选' in first.messages[0]['text']
    second = await say(system, '福建省 / 福州市 / 鼓楼区', first.state)
    assert '出行日期' in second.messages[0]['text']
    assert second.state.active_task.missing_slots == ['business_date']
    system.travel.search_products.assert_not_awaited()
    result = await say(system, '明天', second.state)
    system.travel.search_products.assert_awaited_once_with('scenic', {
        'areaId': 1140, 'travelDate': '2026-11-22', 'pageNo': 1, 'pageSize': 100,
    })
    assert result.route == 'task' and result.state.active_task is None
    assert '三坊七巷' in result.messages[0]['text']
    assert '其他区景点' not in result.messages[0]['text']
    context = json.loads(system.llm.propose.await_args.args[1])
    assert context['active_task']['slots']['product_request']['area'] == '福建省 / 福州市 / 鼓楼区'


@pytest.mark.asyncio
@pytest.mark.parametrize('text, payload', [
    ('明天', {'date_text': '明天', 'date_value': '2026-11-23'}),
    ('明天', {'date_text': '明天', 'product': {'date_text': '明天', 'date_value': '2026-11-23'}}),
    ('明天或后天', {'date_text': '明天', 'product': {'date_text': '后天'}}),
    ('2027年1月2日', {'date_text': '2027年1月2日', 'product': {'date_text': '1月2日'}}),
    ('明天', {'date_text': '后天'}),
    ('明天', {'date_value': '2026-11-22'}),
    ('明天', {'date_text': '明天', 'product': {'date_value': '2026-11-22'}}),
    ('下个月', {'date_text': '下个月', 'date_value': '2026-12-01'}),
])
async def test_conflicting_or_ungrounded_dates_preserve_task_and_do_not_query(system, text, payload):
    state = waiting_state(system)
    before = state.model_dump(mode='json')
    system.llm.propose.return_value = json.dumps({'route': 'task', 'command': 'set_slots', **payload})
    result = await say(system, text, state)
    assert result.route == 'clarify'
    assert result.state.model_dump(mode='json') == before
    assert '日期' in result.messages[0]['text']
    assert '商品条件' not in result.messages[0]['text']
    assert system.travel.mock_calls == []


@pytest.mark.parametrize('change', [
    {'order_number': 'ORD0000000073'},
    {'use_current_order': True},
    {'sales': {'reason': '行程有变'}},
    {'product': {'keyword': '三坊七巷'}},
    {'command': 'update_slots', 'intent_text': '明天'},
    {'command': 'start_flow', 'flow_id': PRODUCT},
    {'task_id': str(uuid4())},
    {'flow_id': DATE_ITINERARY},
])
def test_correction_does_not_accept_mixed_payload_or_change_target(system, change):
    state = waiting_state(system)
    before = state.model_dump(mode='json')
    plan = TurnPlan.model_validate({'route': 'task', 'command': 'set_slots', 'date_text': '明天', **change})
    with pytest.raises(PlanRejectedError):
        system.validator.check_text(plan, state, '明天')
    assert state.model_dump(mode='json') == before
    assert system.travel.mock_calls == []


@pytest.mark.parametrize('missing, saved_date', [
    (['area'], None), (['end_date'], date(2026, 11, 21)), (['keyword'], date(2026, 11, 21)),
    (['business_date'], date(2026, 11, 21)),
])
def test_correction_only_fills_an_actually_missing_start_date(system, missing, saved_date):
    state = waiting_state(system, business_date=saved_date)
    state.active_task.missing_slots = missing
    before = state.model_dump(mode='json')
    with pytest.raises(PlanRejectedError, match='日期'):
        system.validator.check_text(TurnPlan(route='task', command='set_slots', date_text='明天'), state, '明天')
    assert state.model_dump(mode='json') == before


@pytest.mark.asyncio
async def test_explicit_itinerary_switch_keeps_product_task_for_resume(system):
    state = waiting_state(system)
    task_id = state.active_task.task_id
    system.llm.propose.return_value = json.dumps({
        'route': 'task', 'command': 'start_flow', 'flow_id': DATE_ITINERARY, 'date_text': '明天',
    })
    result = await say(system, '查明天的行程', state)
    system.travel.search_products.assert_not_awaited()
    system.travel.list_orders.assert_awaited_once()
    # 行程完成后自动恢复原景点任务，仍然等待日期，不把这次的行程日期填进去。
    assert result.state.active_task.task_id == task_id
    assert result.state.active_task.slots['product_request'].business_date is None
    assert result.state.active_task.missing_slots == ['business_date']
    assert any('2026-11-22 没查到出行或住宿安排' in message['text'] for message in result.messages)


@pytest.mark.asyncio
async def test_full_itinerary_sentence_is_not_repaired_as_a_date(system):
    state = waiting_state(system)
    system.llm.propose.return_value = json.dumps({
        'route': 'task', 'command': 'set_slots', 'date_text': '查明天的行程', 'date_value': '2026-11-22',
    })
    result = await say(system, '查明天的行程', state)
    assert result.route == 'clarify'
    assert result.state.model_dump(mode='json') == state.model_dump(mode='json')
    assert system.travel.mock_calls == []


def test_hotel_start_date_correction_keeps_filters_and_still_waits_for_checkout(system):
    state = waiting_state(system)
    task = state.active_task
    task.slots['product_request'] = ProductRequest(kind='hotel', area='三亚', hotel_star_rating='5')
    plan = TurnPlan(route='task', command='set_slots', date_text='明天')
    checked = system.validator.check_text(plan, state, '明天')
    assert checked.reference.business_date == date(2026, 11, 22)
    assert checked.reference.area == '三亚' and checked.reference.hotel_star_rating == '5'
    task.slots['product_request'] = checked.reference
    assert system.executor.resume_prompt(task) == '请告诉我哪天退房。'
    assert task.missing_slots == ['end_date']
    assert plan.product is None and plan.date_text == '明天'


@pytest.mark.asyncio
async def test_unrecognized_hotel_date_asks_for_checkin_in_plain_language(system):
    state = waiting_state(system)
    state.active_task.slots['product_request'] = ProductRequest(kind='hotel', area='三亚')
    system.llm.propose.return_value = json.dumps({
        'route': 'task', 'command': 'set_slots', 'date_text': '下个月', 'date_value': '2026-12-01',
    })
    result = await say(system, '下个月', state)
    assert result.route == 'clarify'
    assert '入住日期' in result.messages[0]['text']
    assert '商品条件' not in result.messages[0]['text']
    assert result.state.model_dump(mode='json') == state.model_dump(mode='json')
