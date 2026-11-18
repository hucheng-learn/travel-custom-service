import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.support import register_error_handlers, router
from app.tracing import TraceMiddleware
from engine.dialogue import DialogueEngine
from integration.llm import LlmClient
from integration.regions import AreaCatalog
from integration.travel_client import TravelClient
from knowledge.retrieval import KnowledgeProvider
from plan.travel_dates import business_zone
from plan.turn_planner import TurnPlanner
from repository.dialogue import DialogueRepository
from repository.operations import OperationRepository
from service.dialogue import SupportService
from task.action import date_itinerary, products
from task.action.itinerary import ACTION_NAME as ITINERARY_ACTION
from task.action.itinerary import OrderItineraryAction
from task.action.order_status import ACTION_NAME, OUTCOMES, OrderStatusAction
from task.after_sales import AfterSales, load_sales_config
from task.flow.executor import TEMPLATES, FlowExecutor
from task.flow.loader import LoadedFlow, load_flows


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
FLOW_DIR = ROOT / "flow_config"
RESOURCES = ROOT / "resources"
FLOW_FILES = ("order_status_query.yaml", "itinerary_query.yaml", "itinerary_date_query.yaml", "product_query.yaml")
ACTION_ROUTES = {
    ACTION_NAME: OUTCOMES, ITINERARY_ACTION: OUTCOMES,
    date_itinerary.ACTION_NAME: date_itinerary.OUTCOMES, products.ACTION_NAME: products.OUTCOMES,
}


def load_supported_flows() -> dict[str, LoadedFlow]:
    flows: dict[str, LoadedFlow] = {}
    # 每个 YAML 独立计算流程版本，新增行程流程不会让旧订单任务失效。
    for name in FLOW_FILES:
        loaded = load_flows(FLOW_DIR / name, action_routes=ACTION_ROUTES, templates=TEMPLATES)
        if flows.keys() & loaded.keys():
            raise ValueError("流程配置里有重复的 flowId")
        flows.update(loaded)
    return flows


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    flows = load_supported_flows()
    sales_config = load_sales_config(FLOW_DIR / "after_sales.yaml")
    async with AsyncExitStack() as stack:
        # 启动只准备资源，不连接数据库或调用中台；未配置时仍可探活。
        app.state.support_service = None
        if settings.support_database_url:
            url = make_url(settings.support_database_url)
            if url.drivername != "mysql+asyncmy" or not url.database:
                raise ValueError("SUPPORT_DATABASE_URL 必须使用 mysql+asyncmy 并指定客服数据库")
            database = create_async_engine(url, pool_pre_ping=True)
            stack.push_async_callback(database.dispose)
            sessions = async_sessionmaker(database, expire_on_commit=False)
            repository = DialogueRepository(sessions)
            operations = OperationRepository(sessions, sales_config["ticket_next_step"])
            travel = (
                await stack.enter_async_context(TravelClient.from_settings(settings))
                if settings.travel_api_base_url else None
            )
            llm = None
            if settings.llm_base_url or settings.llm_model or settings.llm_api_key:
                model_http = await stack.enter_async_context(httpx.AsyncClient())
                try:
                    llm = LlmClient(model_http, settings)
                except (ValueError, httpx.InvalidURL):
                    logger.warning("模型配置不完整或地址无效，使用本地对话规则")
            # 地区种子和知识资料由本应用本地加载，模型不能自行提供任何地区 ID 或政策。
            areas_path = ROOT / settings.travel_areas_csv if settings.travel_areas_csv else RESOURCES / "travel_areas.csv"
            areas = AreaCatalog(areas_path)
            zone = business_zone(settings.business_timezone)
            knowledge = KnowledgeProvider(RESOURCES / "faq.yaml", RESOURCES / "travel_guide.md")
            actions = {
                ACTION_NAME: OrderStatusAction(travel, settings.business_timezone),
                ITINERARY_ACTION: OrderItineraryAction(travel, settings.business_timezone),
                date_itinerary.ACTION_NAME: date_itinerary.DateItineraryAction(travel, zone),
                products.ACTION_NAME: products.ProductAction(travel, areas, settings.business_timezone),
            }
            after_sales = AfterSales(sales_config, travel, operations, zone, settings.refund_write_enabled)
            engine = DialogueEngine(FlowExecutor(flows, actions, areas), TurnPlanner(llm, areas), knowledge, zone, after_sales)
            app.state.support_service = SupportService(repository, engine, travel, operations)
            stack.push_async_callback(app.state.support_service.drain)
        yield


# Uvicorn 的 app.main:app 指向这个对象：前半段是模块，后半段是变量名。
app = FastAPI(title="旅游智能客服", lifespan=lifespan)
app.add_middleware(TraceMiddleware)
app.include_router(router)
register_error_handlers(app)
# 首页和静态资源仍与客服 API 同源，但不再使用 /demo 前缀。
app.mount("/assets", StaticFiles(directory=ROOT / "fronted" / "assets"), name="fronted-assets")


@app.get("/", response_class=FileResponse, include_in_schema=False)
async def fronted_page() -> FileResponse:
    # 调试页和接口同源，避免配置 CORS；页面不读取服务端的 .env。
    return FileResponse(
        ROOT / "fronted" / "index.html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        },
    )


@app.get("/health")
async def health() -> dict[str, str]:
    # 只检查 HTTP 服务能否响应，不调用 MySQL、旅游中台或模型。
    return {"status": "ok"}


def main() -> None:
    # 开发时修改 Python 代码会自动重载。传模块路径，让 Uvicorn 能重新导入应用。
    uvicorn.run("app.main:app", host="127.0.0.1", port=18082, reload=True)


# 只有直接运行模块时才启动服务；导入 app 时不会额外启动一个进程。
if __name__ == "__main__":
    main()
