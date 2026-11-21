import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.main import load_supported_flows
from engine.dialogue import DialogueEngine
from integration.llm import LlmClient, ModelUnavailableError
from integration.travel_client import TravelClient
from knowledge.retrieval import KnowledgeProvider
from plan.models import REFUND, TurnPlan
from plan.product_models import ProductRequest
from plan.travel_dates import business_zone, calendar_today
from plan.turn_planner import TurnPlanner
from plan.validator import PlanRejectedError, TurnPlanValidator
from repository.dialogue import ReservedTurn
from repository.operations import OperationRepository
from task.action.itinerary import OrderItineraryAction
from task.action.order_status import OrderStatusAction
from task.after_sales import AfterSales, load_sales_config
from task.flow.executor import DialogueState, FlowExecutor


ROOT = Path(__file__).resolve().parents[1]


def make_system(plans):
    # 固定模型计划，测试真正的状态推进、归属和写入边界；不访问模型、中台或数据库。
    llm = Mock(spec=LlmClient, propose=AsyncMock(side_effect=[
        json.dumps(plan, ensure_ascii=False) if isinstance(plan, dict) else plan for plan in plans
    ]))
    travel = Mock(spec=TravelClient)
    travel.list_orders.return_value = {'total': 2, 'list': [
        {'orderId': i, 'orderNo': f'ORD{i:010d}'} for i in [73, 74]
    ]}

    async def order(user_id, order_id):
        return {'orderId': order_id, 'orderNo': f'ORD{order_id:010d}', 'userId': user_id,
                'statusCode': 'paid', 'paidAmount': 100, 'currencyCode': 'CNY', 'items': [{
                    'orderItemId': order_id, 'statusCode': 'paid', 'saleAmount': 100,
                    'productTypeCode': 'hotel_room', 'productName': '酒店套房',
                    'travelTime': '2027-01-07', 'travelEndTime': '2027-01-10',
                }]}

    travel.get_order.side_effect = order
    travel.list_order_payments.return_value = {'list': []}
    travel.list_order_refund_records.return_value = {'list': []}
    travel.list_refund_requests.return_value = {'list': [], 'total': 0}
    operations = Mock(spec=OperationRepository)
    operations.find.return_value = None
    operations.latest_uncertain.return_value = None
    zone = business_zone('Asia/Shanghai')
    sales = AfterSales(load_sales_config(ROOT / 'flow_config/after_sales.yaml'), travel, operations, zone, False)
    executor = FlowExecutor(load_supported_flows(), {
        'query_order_status': OrderStatusAction(travel, 'Asia/Shanghai'),
        'query_order_itinerary': OrderItineraryAction(travel, 'Asia/Shanghai'),
    })
    planner = TurnPlanner(llm, zone=zone)
    knowledge = KnowledgeProvider(ROOT / 'resources/faq.yaml', ROOT / 'resources/travel_guide.md')
    engine = DialogueEngine(executor, planner, knowledge, zone, sales)
    return SimpleNamespace(engine=engine, llm=llm, travel=travel, operations=operations,
                           sales=sales, executor=executor, validator=TurnPlanValidator(executor, zone, sales),
                           claim=ReservedTurn(uuid4(), uuid4(), uuid4(), 0, {}))


async def say(system, text, state=None):
    # 每轮都从序列化快照恢复，验证的是跨请求状态而非偶然共用同一个内存对象。
    state = DialogueState.model_validate((state or DialogueState()).model_dump(mode='json'))
    return await system.engine.run(state, 73, {'type': 'text', 'text': text}, system.claim)


def start_refund(**kwargs):
    return {'route': 'task', 'command': 'start_flow', 'flow_id': REFUND, **kwargs}


def complete_refund(amount='50', reason='行程有变'):
    return start_refund(order_number='ORD0000000073', sales={
        'refund_type': {'value': 'partial', 'source': '部分'},
        'amount': {'value': amount, 'source': amount + '元'}, 'reason': reason,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['这家酒店不住了，麻烦退订吧', '想把订好的那笔退掉', '我想申请退回这次的钱'])
async def test_refund_start_uses_model_even_without_old_keywords(text):
    system = make_system([start_refund()])
    result = await say(system, text)
    assert result.state.active_task.flow_id == REFUND
    assert result.state.active_task.step_id == 'order'
    system.llm.propose.assert_awaited_once()
    assert system.operations.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['我要退的是 ORD0000000073，那笔酒店订单', '麻烦处理这个单子：ORD0000000073'])
async def test_order_inside_a_sentence_reaches_the_existing_refund_task(text):
    system = make_system([start_refund(), {'route': 'task', 'command': 'set_slots', 'order_number': 'ORD0000000073'}])
    first = await say(system, '我想退款')
    result = await say(system, text, first.state)
    assert result.state.active_task.task_id == first.state.active_task.task_id
    assert result.state.active_task.step_id == 'refund_type'
    context = json.loads(system.llm.propose.call_args.args[1])
    assert context['active_task']['step_id'] == 'order'
    assert '订单' in context['current_question']
    system.travel.get_order.assert_awaited_once_with(73, 73)


@pytest.mark.asyncio
@pytest.mark.parametrize('phrase', ['都退了', '整笔退回', '全部退款'])
async def test_enum_uses_normalized_value_with_original_expression(phrase):
    system = make_system([start_refund(order_number='ORD0000000073'), {
        'route': 'task', 'command': 'set_slots', 'sales': {'refund_type': {'value': 'full', 'source': phrase}},
    }])
    first = await say(system, '申请退款 ORD0000000073')
    result = await say(system, phrase, first.state)
    assert result.state.active_task.sales.refund_type == 'full'
    assert result.state.active_task.step_id == 'reason'


@pytest.mark.asyncio
async def test_multiple_sales_fields_and_chinese_amount_only_preview():
    system = make_system([start_refund(order_number='ORD0000000073', sales={
        'refund_type': {'value': 'partial', 'source': '部分'},
        'amount': {'value': '50', 'source': '五十块'}, 'reason': '行程有变',
    })])
    result = await say(system, 'ORD0000000073 申请部分退款五十块，行程有变')
    task = result.state.active_task
    assert task.step_id == 'confirm'
    assert task.sales.confirmation.target['amount'] == '50.00'
    system.travel.create_refund_request.assert_not_awaited()
    assert system.operations.mock_calls == []


@pytest.mark.asyncio
async def test_changing_order_invalidates_old_confirmation_and_preserves_reason():
    system = make_system([complete_refund(), {'route': 'task', 'command': 'update_slots',
        'intent_text': '换成 ORD0000000074', 'order_number': 'ORD0000000074'}])
    first = await say(system, 'ORD0000000073 部分退款50元，行程有变')
    old_id = first.state.active_task.sales.confirmation.id
    result = await say(system, '还是换成 ORD0000000074', first.state)
    task = result.state.active_task
    assert task.sales.order_id == task.sales.item_id == 74
    assert task.sales.refund_type == 'partial'
    assert task.sales.reason == '行程有变'
    assert task.sales.amount is None and task.sales.confirmation is None
    assert task.step_id == 'amount'
    rejected = await system.engine.run(result.state, 73, {'type': 'object', 'object': {
        'type': 'confirmation', 'id': str(old_id), 'decision': 'confirm'}}, system.claim)
    assert '失效' in rejected.messages[0]['text']
    system.travel.create_refund_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_changing_amount_and_reason_creates_a_new_consistent_summary():
    system = make_system([complete_refund(), {'route': 'task', 'command': 'update_slots',
        'intent_text': '改成二十元，原因改为不方便出行', 'sales': {
            'amount': {'value': '20', 'source': '二十元'}, 'reason': '不方便出行',
        }}])
    first = await say(system, 'ORD0000000073 部分退款50元，行程有变')
    result = await say(system, '改成二十元，原因改为不方便出行', first.state)
    card = result.state.active_task.sales.confirmation
    assert card.id != first.state.active_task.sales.confirmation.id
    assert card.target['amount'] == '20.00'
    assert card.target['reason'] == '不方便出行'
    assert system.operations.mock_calls == []


@pytest.mark.asyncio
async def test_pause_resume_and_cancel_do_not_require_magic_words():
    system = make_system([start_refund(),
        {'route': 'task', 'command': 'pause_task', 'intent_text': '这件事先放一放'},
        {'route': 'task', 'command': 'resume_task', 'intent_text': '接着弄刚才那件事'},
        {'route': 'task', 'command': 'cancel_task', 'intent_text': '算了先不办了'}])
    first = await say(system, '退订吧')
    paused = await say(system, '这件事先放一放', first.state)
    assert paused.state.active_task is None
    assert paused.state.paused_tasks[0].paused_by_user
    resumed = await say(system, '接着弄刚才那件事', paused.state)
    assert resumed.state.active_task.task_id == first.state.active_task.task_id
    done = await say(system, '算了先不办了', resumed.state)
    assert done.state.active_task is None and not done.state.paused_tasks
    assert system.operations.mock_calls == []


@pytest.mark.asyncio
async def test_insert_order_query_restores_refund_but_does_not_reuse_the_input():
    system = make_system([start_refund(), {'route': 'task', 'command': 'start_flow',
        'flow_id': 'order_status_query', 'order_number': 'ORD0000000073'}])
    first = await say(system, '退款吧')
    result = await say(system, '先看 ORD0000000073 的支付情况', first.state)
    assert result.state.active_task.task_id == first.state.active_task.task_id
    assert result.state.active_task.step_id == 'order'
    assert result.state.active_task.sales.order_id is None
    assert result.state.current_order_id == 73
    assert not result.state.paused_tasks


@pytest.mark.asyncio
async def test_knowledge_interruption_does_not_consume_sales_description():
    system = make_system([start_refund(), {'route': 'knowledge', 'knowledge_topic': 'refund_arrival'}])
    first = await say(system, '退款吧')
    result = await say(system, '钱通常要等几天能回来', first.state)
    assert result.route == 'knowledge' and '没有提供固定到账时限' in result.messages[0]['text']
    assert result.state.active_task.step_id == 'order'
    assert result.state.active_task.sales.reason is None


@pytest.mark.asyncio
async def test_textual_submission_intent_can_only_preview_even_when_write_enabled():
    system = make_system([complete_refund(), {'route': 'task', 'command': 'preview', 'intent_text': '就这样提交吧'}])
    first = await say(system, 'ORD0000000073 部分退款50元，行程有变')
    system.sales._refund_write_enabled = True
    result = await say(system, '就这样提交吧', first.state)
    assert result.state.active_task.step_id == 'confirm'
    system.travel.create_refund_request.assert_not_awaited()
    assert system.operations.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['ambiguous', 'multiple'])
async def test_model_clarification_is_not_overridden_by_shortcut_words(reason):
    system = make_system([{'route': 'clarify', 'clarify_reason': reason}])
    result = await say(system, '帮我查订单')
    assert result.route == 'clarify' and result.state.active_task is None
    system.travel.list_orders.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['[]', ModelUnavailableError('offline')])
async def test_model_failure_preserves_task_and_does_not_guess_from_free_text(failure):
    system = make_system([start_refund(), failure, failure] if isinstance(failure, str) else [start_refund(), failure])
    first = await say(system, '我要退订')
    result = await say(system, '这个先别弄了，过两天再说', first.state)
    assert result.route == 'clarify'
    assert result.state.model_dump() == first.state.model_dump()
    assert '不可用' in result.messages[0]['text'] or '可靠理解' in result.messages[0]['text']


@pytest.mark.parametrize('text, payload', [
    ('ORD0000000073XYZ', {'order_number': 'ORD0000000073'}),
    ('都退了', {'sales': {'refund_type': {'value': 'full', 'source': '全部退回'}}}),
    ('退50元', {'sales': {'amount': {'value': '500', 'source': '50元'}}}),
    ('退五十元', {'sales': {'amount': {'value': '500', 'source': '五十元'}}}),
    ('订单2026-11-20', {'order_number': '2026-11-20'}),
])
def test_hallucinated_or_mismatched_values_are_rejected_before_business_calls(text, payload):
    system = make_system([])
    with pytest.raises(PlanRejectedError):
        system.validator.check_text(TurnPlan.model_validate(start_refund(**payload)), DialogueState(), text)
    assert system.travel.mock_calls == [] and system.operations.mock_calls == []


def test_task_target_must_exist_in_this_session():
    system = make_system([])
    with pytest.raises(PlanRejectedError):
        system.validator.check_text(TurnPlan(route='task', command='resume_task', task_id=uuid4(),
                                           intent_text='接着办'), DialogueState(), '接着办')


def test_model_date_normalization_keeps_raw_expression_and_checks_known_dates():
    system = make_system([])
    today = calendar_today(business_zone('Asia/Shanghai'))
    expected = today + timedelta(days=7 + 1 - today.weekday())
    plan = TurnPlan(route='task', command='start_flow', flow_id='itinerary_date_query',
                    date_text='下个礼拜二', date_value=expected)
    checked = system.validator.check_text(plan, DialogueState(), '下个礼拜二有什么安排')
    assert checked.reference.travel_date == expected
    for raw, normalized in [('明天', today + timedelta(days=5)), ('2026-02-30', date(2026, 3, 2)),
                            ('2026-02-30入住', date(2026, 3, 2))]:
        with pytest.raises(PlanRejectedError):
            system.validator.check_text(plan.model_copy(update={'date_text': raw, 'date_value': normalized}),
                                        DialogueState(), raw + '的安排')


@pytest.mark.asyncio
async def test_context_does_not_send_old_reason_description_or_confirmation_target():
    system = make_system([complete_refund(), {'route': 'clarify'}])
    first = await say(system, 'ORD0000000073 部分退款50元，行程有变')
    await say(system, '那个先等等', first.state)
    context = json.loads(system.llm.propose.call_args.args[1])
    assert context['active_task']['sales']['has_reason']
    assert '行程有变' not in json.dumps(context, ensure_ascii=False)
    assert str(first.state.active_task.sales.confirmation.id) not in json.dumps(context)


@pytest.mark.asyncio
async def test_valid_confirmation_button_still_submits_once_and_replays():
    system = make_system([complete_refund()])
    first = await say(system, 'ORD0000000073 部分退款50元，行程有变')
    card = first.state.active_task.sales.confirmation
    system.sales._refund_write_enabled = True
    row_id = uuid4()
    system.operations.prepare_refund.return_value = SimpleNamespace(id=row_id, status='pending')
    system.operations.mark_sending.return_value = True
    receipt = {'refundRequestId': 1, 'refundRequestNo': 'RF0001', 'statusCode': 'pending',
               'orderId': 73, 'orderItemId': 73, 'requestedAmount': '50.00'}
    system.travel.create_refund_request.return_value = receipt

    async def save(operation_id, status, result):
        return SimpleNamespace(id=operation_id, type='refund', status=status, result_json=result)

    system.operations.complete_refund.side_effect = save
    incoming = {'type': 'object', 'object': {'type': 'confirmation', 'id': str(card.id), 'decision': 'confirm'}}
    done = await system.engine.run(first.state, 73, incoming, system.claim)
    assert done.state.active_task is None
    assert 'RF0001' in done.messages[0]['text']
    system.travel.create_refund_request.assert_awaited_once_with(73, 73, 73, 50.0, '行程有变')
    saved = system.operations.complete_refund.call_args
    system.operations.find.return_value = await save(row_id, saved.args[1], saved.args[2])
    replay = await system.engine.run(done.state, 73, incoming, system.claim)
    assert replay.messages == done.messages
    system.travel.create_refund_request.assert_awaited_once()
    system.llm.propose.assert_awaited_once()


@pytest.mark.asyncio
async def test_ticket_collects_natural_fields_and_only_creates_on_button():
    system = make_system([{'route': 'task', 'command': 'start_flow', 'flow_id': 'ticket_submit', 'sales': {
        'ticket_type': {'value': 'complaint', 'source': '投诉'},
        'no_order': {'value': 'true', 'source': '不关联订单'}, 'description': '房间很脏',
    }}])
    first = await say(system, '我想投诉，不关联订单，房间很脏')
    task = first.state.active_task
    assert task.step_id == 'confirm'
    assert task.sales.no_order and task.sales.description == '房间很脏'
    system.operations.create_ticket.assert_not_awaited()
    result = {'ticketId': 1, 'ticketNo': 'TK001', 'status': 'open', 'nextStep': '请保留编号。'}
    system.operations.create_ticket.return_value = SimpleNamespace(type='ticket', status='succeeded', result_json=result)
    done = await system.engine.run(first.state, 73, {'type': 'object', 'object': {
        'type': 'confirmation', 'id': str(task.sales.confirmation.id), 'decision': 'confirm'}}, system.claim)
    assert done.state.active_task is None
    assert 'TK001' in done.messages[0]['text']
    system.operations.create_ticket.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_array_wrapper_is_protocol_only_but_multiple_plans_are_rejected():
    system = make_system([json.dumps([start_refund()]), json.dumps([start_refund(), start_refund()])])
    first = await say(system, '退订吧')
    assert first.state.active_task.flow_id == REFUND
    rejected = await say(system, '同时办两次', first.state)
    assert rejected.route == 'clarify'
    assert '多个' in rejected.messages[0]['text']
    assert rejected.state.model_dump() == first.state.model_dump()


@pytest.mark.asyncio
async def test_invalid_structure_is_repaired_once_and_still_passes_business_validation():
    system = make_system(['[]', start_refund(order_number='MADE_UP')])
    result = await say(system, '帮我办理退款')
    assert result.route == 'clarify'
    assert result.state.active_task is None
    assert system.llm.propose.await_count == 2
    assert '上一次响应格式无效' in system.llm.propose.call_args.args[0]
    system.travel.list_orders.assert_not_awaited()
    assert system.operations.mock_calls == []


def test_new_hotel_filters_clear_stale_name_and_keep_collected_dates():
    system = make_system([])
    task = system.executor.start('product_query')
    task.step_id = 'collect_product'
    task.slots['product_request'] = ProductRequest(kind='hotel', area='三亚',
        business_date=date(2026, 12, 1), end_date=date(2026, 12, 3), keyword='五星级度假酒店')
    plan = TurnPlan.model_validate({'route': 'task', 'command': 'set_slots', 'product': {
        'hotel_star_rating': '4', 'hotel_star_text': '四星', 'hotel_type': 'business', 'hotel_type_text': '商务',
    }})
    checked = system.validator.check_text(plan, DialogueState(active_task=task), '这次找四星商务型的')
    assert checked.reference.keyword is None
    assert checked.reference.business_date == date(2026, 12, 1)
    assert checked.reference.end_date == date(2026, 12, 3)
    assert checked.reference.hotel_star_rating == '4'
