from fastapi import FastAPI


# Uvicorn 的 app.main:app 指向这个对象：前半段是模块，后半段是变量名。
app = FastAPI(title="旅游智能客服")


@app.get("/health")
async def health() -> dict[str, str]:
    # 只检查 HTTP 服务能否响应，不调用 MySQL、旅游中台或模型。
    return {"status": "ok"}