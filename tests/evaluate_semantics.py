import asyncio
import json
from pathlib import Path
from unittest.mock import Mock
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from app.config import Settings
from app.main import load_supported_flows
from integration.llm import LlmClient
from plan.models import REFUND, PRODUCT
from datetime import timedelta
from plan.travel_dates import calendar_today
from plan.travel_dates import business_zone
from plan.turn_planner import TurnPlanner
from plan.validator import TurnPlanValidator
from repository.operations import OperationRepository
from task.after_sales import AfterSales, load_sales_config
from task.flow.executor import DialogueState, FlowExecutor


class RecordingClient(LlmClient):
    # 只在这个使用虚构输入的验收脚本记录响应，生产日志仍不记录用户原文和模型正文。
    async def propose(self, prompt, context):
        result = await super().propose(prompt, context)
        self.responses[json.loads(context)['user_text']] = result
        return result


# 只调用当前已配置的模型，使用虚构对话和本地状态；不调用中台或写入业务库。
async def main():
    settings = Settings()
    zone = business_zone(settings.business_timezone)
    sales = AfterSales(load_sales_config(ROOT/'flow_config/after_sales.yaml'), None, Mock(spec=OperationRepository), zone, False)
    executor = FlowExecutor(load_supported_flows(), {})
    validator = TurnPlanValidator(executor, zone, sales)
    def active(step):
        task = sales.start(REFUND).task
        task.step_id = step
        task.missing_slots = [step]
        if step != 'order':
            task.sales.order_id = task.sales.item_id = 73
            task.sales.order_no = 'ORD0000000073'
        return DialogueState(active_task=task)
    paused = active('order')
    paused.paused_tasks = [paused.active_task]
    paused.active_task = None
    cases = [
        ('refund_start', '这家酒店不住了，麻烦退订吧', DialogueState(), 'start_flow', REFUND),
        ('refund_start_alt', '想把订好的那笔退掉', DialogueState(), 'start_flow', REFUND),
        ('order_sentence', '我要退的是 ORD0000000073，那笔酒店订单', active('order'), 'set_slots', None),
        ('full', '都退了', active('refund_type'), 'set_slots', None),
        ('partial_amount', '只要退五十元就行', active('amount'), 'set_slots', None),
        ('change_amount', '金额还是改成二十块吧', active('review'), 'update_slots', None),
        ('change_order', '还是换成 ORD0000000074', active('review'), 'update_slots', None),
        ('pause', '这件事先放一放', active('order'), 'pause_task', None),
        ('resume', '接着弄刚才那件事', paused, 'resume_task', None),
        ('cancel', '算了先不办退款了', active('order'), 'cancel_task', None),
        ('negation', '我不是要退款，先看看订单有没有付款', DialogueState(), 'start_flow', 'order_status_query'),
        ('arrival', '钱通常要等几天能回来', active('order'), None, None),
        ('refund_status', '上回申请的那笔退款现在走到哪了', active('order'), 'refund_status', None),
        ('text_confirm', '没问题，直接给我提交吧', active('confirm'), 'preview', None),
        ('date', '下个礼拜二我有什么出行安排', DialogueState(), 'start_flow', 'itinerary_date_query'),
        ('hotel', '2026-12-01入住，12月3日退房，找三亚五星级度假酒店', DialogueState(), 'start_flow', PRODUCT),
        ('multiple', '查一下订单支付，再看看明天北京有哪些酒店', DialogueState(), None, None),
    ]
    results=[]
    gate=asyncio.Semaphore(3)
    async with httpx.AsyncClient() as client:
        model=RecordingClient(client, settings)
        model.responses={}
        planner=TurnPlanner(model, zone=zone)
        async def evaluate(case):
            name,text,state,command,flow=case
            async with gate:
                plan=await planner.plan(text,state,question=sales.waiting_prompt(state.active_task) if state.active_task else None)
            error=None
            try:
                checked = validator.check_text(plan,state,text)
            except ValueError as ex:
                error=str(ex)
            success=(plan.command==command and (flow is None or plan.flow_id==flow) and error is None
                     and plan.clarify_reason not in {'model_unavailable','invalid_model_output'})
            if name=='arrival': success=success and plan.route=='knowledge' and plan.knowledge_topic=='refund_arrival'
            if name=='multiple': success=success and plan.route=='clarify' and plan.clarify_reason=='multiple'
            if name=='order_sentence': success=success and plan.order_number=='ORD0000000073'
            if name=='full': success=success and bool(plan.sales and plan.sales.refund_type and plan.sales.refund_type.value=='full')
            if name in {'partial_amount','change_amount'}:
                success=success and bool(plan.sales and plan.sales.amount and plan.sales.amount.value in
                    ({'50','50.0','50.00'} if name=='partial_amount' else {'20','20.0','20.00'}))
            if name=='date':
                today=calendar_today(zone)
                success=success and plan.date_value==today+timedelta(days=8-today.weekday())
            if name=='hotel':
                success=success and bool(plan.product and plan.product.area=='三亚' and
                    plan.product.hotel_star_rating=='5' and plan.product.hotel_type=='resort')
            item={'name':name,'passed':success,'plan':plan.model_dump(mode='json',exclude_none=True),'validation_error':error}
            if plan.clarify_reason == 'invalid_model_output':
                raw=model.responses.get(text,'')
                item['raw_synthetic_output']=raw
                try:
                    from plan.models import TurnPlan
                    TurnPlan.model_validate_json(raw)
                except ValueError as ex:
                    item['schema_error']=str(ex)
            results.append(item)
            print(json.dumps(item,ensure_ascii=False),flush=True)
        await asyncio.gather(*(evaluate(case) for case in cases))
    (ROOT/'.screenshots').mkdir(exist_ok=True)
    (ROOT/'.screenshots/model-evaluation.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'{sum(item["passed"] for item in results)}/{len(results)} passed',flush=True)
    return all(item['passed'] for item in results)

if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(main()) else 1)
