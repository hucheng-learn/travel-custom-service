# 旅游智能客服

当前支持订单和支付、行程、六类商品、平台知识、退款申请及客服工单。会话状态和历史保存在客服库，前端可切换 JSON 或 SSE。

## 启动与探活

在项目根目录打开 PowerShell。已安装依赖时，不需要先建库或填写 `.env`：

```powershell
uv --no-cache run --no-sync python -m app.main
```

另开一个 PowerShell 窗口，或在浏览器打开同一地址：

```powershell
Invoke-RestMethod http://127.0.0.1:18082/health | ConvertTo-Json -Compress
```

预期返回 `{"status":"ok"}`。这是 HTTP 探活，不检查 MySQL 或旅游中台。

## 本地对话调试页

浏览器打开 `http://127.0.0.1:18082/`，填已有的中台测试用户 ID，就可以直接发消息，页面会自动准备会话。要复用之前在 Swagger 创建的会话，可另外填写原 `sessionId`。调试页自动生成 `messageId`；网络错误后用「重试上一条」会沿用原 ID。

左栏话术只填入输入框，不会自动发送；右栏可看任务状态、发送订单卡片和查工单。发送区可切换 SSE，确认卡可以提交或拒绝。测试用户头不是正式认证，请只在本机使用。

## 初始化客服数据库（MySQL 8）

1. 用有建库权限的账户在 MySQL 客户端中执行 `migrations/create_database.sql`，创建独立的 `travel-custom-service` 库。不要把客服表建在旅游中台库。
2. 执行 `Copy-Item .env.example .env`（已有 `.env` 时不要覆盖），填写 `SUPPORT_DATABASE_URL`。数据库口令含 `@`、`#` 等字符时，对口令部分做 URL 编码。数据库迁移不需要填写旅游中台地址。
3. 应用迁移并确认当前版本：

```powershell
uv --no-cache run --no-sync alembic upgrade head
uv --no-cache run --no-sync alembic current
```

首版迁移 `0001` 创建会话、对话轮次、写操作回执、工单和工单事件五张表。内部关联加外键；中台的用户和订单仅保存 ID，不跨库建外键。表内时间按 UTC 保存，`updated_at` 初始由数据库填入，后续由业务更新语句同步维护。

想先看建表 SQL，可运行 `uv --no-cache run --no-sync alembic upgrade head --sql`，只在终端输出 SQL，不连接数据库。不要同时执行导出的 SQL 和在线迁移。

## 旅游中台只读联调

在项目根目录的 `.env` 填写 `TRAVEL_API_BASE_URL`（例如 `http://127.0.0.1:8000`）和可选的 `TRAVEL_API_TIMEOUT_SECONDS`。用同一个测试用户调用下列方法：

```python
from integration.travel_client import TravelClient


async def check_travel(user_id: int, order_id: int) -> None:
    async with TravelClient.from_settings() as travel:
        await travel.get_me(user_id)
        await travel.list_orders(user_id)
        await travel.get_order(user_id, order_id)
        await travel.list_order_payments(user_id, order_id)
        await travel.list_order_refund_records(user_id, order_id)
        refunds = await travel.list_refund_requests(user_id)
        if refunds["list"]:
            await travel.get_refund_request(user_id, refunds["list"][0]["refundRequestId"])
```

传实际测试用户 ID 和该用户的已支付订单 ID。客户端会统一传 `X-User-Id`，直接返回中台的 JSON 字段；超时、网络失败、非成功 HTTP 状态和异常响应会抛出不同异常，不会当作空结果。客户端只发 GET，不提交退款。没有退款申请时，列表为空是正常结果，但退款详情需要另选有申请记录的样本验收。

## 阶段一：查询订单与支付

在 `.env` 配好中台和客服库地址、完成迁移后，启动客服服务并打开 `http://127.0.0.1:18082/docs`。以下使用中台的有效测试用户 ID 作为 `X-User-Id`，仅适用于本机联调。

1. 调用 `POST /api/v1/support/sessions`，记下返回的 `sessionId`。
2. 调用 `POST /api/v1/support/sessions/{session_id}/messages`，发送下方示例。助手应追问订单号。
3. 再调同一接口，换一个 `messageId`，把 `text` 换成该用户的一笔真实订单号，不是订单 ID。
4. `GET /api/v1/support/sessions/{session_id}/state` 看任务状态；`GET /api/v1/support/sessions/{session_id}/messages` 看历史。也可以在第 2、3 步之间重启服务，继续使用同一会话。

```json
{"messageId":"3ced54d0-7d8f-479d-a718-137a7f37e284","type":"text","text":"帮我查订单"}
```

一条新消息配一个 UUID；网络重试沿用原来的 ID 和内容。

## 阶段二：模型计划与多轮控制

`.env` 的 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 会被直接使用。模型只判断意图和提取订单号；订单、支付和行程仍从旅游中台查。模型暂不可用时使用本地规则，格式或指令不合法时提示澄清。模型请求包含当前输入和任务类型，不包含客服库的历史、用户 ID 或中台订单详情。

用同一个 `sessionId` 和 `X-User-Id`，每条新消息换一个 `messageId`：

1. 发「帮我查订单」，等助手追问。再发「帮我查行程」，此时订单查询会暂停，状态接口的 `pausedTasks` 可看到原任务。
2. 按下方格式发订单卡片。这里卡片用本人的真实 **`orderId`**；如果直接发文本，用 **`orderNo`**。行程完成后，助手会回到第一项任务并重新提示补订单号，不把行程的订单自动套到第一项任务上。
3. 可发「暂停所有任务」保留现有查询条件但不继续查询，之后发「继续上一个任务」恢复。发「取消当前任务」则结束该查询；这些操作不会取消预订，也不会退款。

```json
{
  "messageId": "8b836e63-c68c-49b3-896c-884bdc901202",
  "type": "object",
  "object": {"type": "order", "id": "90001"}
}
```

`90001` 只是示例。对方用户的订单卡片不会返回任何订单数据。

## 阶段三：行程、商品与知识

在 `/docs` 的原消息接口里继续发文本。**每次只把表格一行的「发送内容」填进 `text`**，不要把多行拼成一条消息。新消息换 `messageId`，同一会话保持 `sessionId` 和 `X-User-Id` 不变。不需要执行数据库迁移。

| 验收目标 | 发送内容 | 应看到的结果 |
| --- | --- | --- |
| 按出行日 | 下周一的出行 | 按业务时区和出行日匹配本人订单，而不是按下单日。酒店退房日单独标记；扫描不完整会注明。 |
| 酒店第 1 轮 | 明天北京有哪些酒店 | 追问退房日期。 |
| 酒店第 2 轮 | 后天退房 | 沿用原会话，显示满足入住和退房日期的可用酒店。 |
| 景点 | 明天北京有哪些景点 | 显示当天有库存的景点。 |
| 高铁 | 明天北京到上海的高铁 | 查询真实车次。 |
| 机票 | 明天北京到上海的机票 | 查询真实航班。 |
| 汽车票 | 明天北京到天津的汽车票 | 查询真实汽车班次。 |
| 接送服务 | 明天北京有哪些接机服务 | 列出服务和载客量。 |
| 接送报价 | 明天从北京到北京的接送多少钱 | 有多个服务时先选择，再查报价。 |
| 知识命中 | 退款审核通过就到账了吗 | 回答两者区别并附来源。 |
| 缺少证据 | 退款多久到账 | 说明没有到账时限。 |
| 知识澄清 | 这个政策是什么 | 追问具体政策。 |

产品查询取中台当时的可用结果，测试日期没有供给时可换日期。详细房型、票种、舱位、席别和接送报价都使用具体商品接口。知识内容由 `resources/faq.yaml` 和 `resources/travel_guide.md` 提供，不替代个别订单的业务信息，不承诺退款时间或金额。

地区映射 `resources/travel_areas.csv` 来自中台同一批 `seeds/1_dimension/areas.csv`，仅保留地区标识和名称层级。同名地名请给省市区全称；中台更换种子时，可让 `TRAVEL_AREAS_CSV` 指向对应的 CSV。默认按 `Asia/Shanghai` 解释中文相对日期。

## 阶段四：客服工单与退款

- **工单**：在首页依次发送「提交工单」「投诉」「无订单」和一段问题描述，每句话分别发送。也可改发本人订单号或订单卡片。核对摘要后点击「确认提交」，拿到工单编号和 ID；点击卡片的「查工单」可查处理状态。用另一位测试用户查同一工单应返回 404。
- **退款预检**：依次发送「申请退款」、本人可退款订单号，必要时选明细，再填「全额」或「部分退款」、金额和原因。到确认卡之前不发出中台写请求。可在确认前发「修改金额 50」，旧确认卡会失效；也可以直接点「不提交」。
- **退款提交默认关闭**：只有确认 `TRAVEL_API_BASE_URL` 指向允许写入的测试中台时，才在 `.env` 设置 `REFUND_WRITE_ENABLED=true` 并重启。启用后点「确认提交」会真实创建中台退款申请。返回待核对时不要尝试另发申请，可发「查退款进度」核对；已知编号可发「查退款进度 申请编号」。也可用「查退款进度 订单号 TR... 明细 123」按订单明细缩小范围。
- **防重边界**：同一消息 ID 回放原回复，同一确认 ID 不重复提交。中台没有跨系统幂等键；超时只核对唯一匹配的申请，不能确认时保留待核对，不能把无回执当成未提交。

## 阶段五：SSE 和联调排障

1. 在发送区打开「SSE」，发送正常查询。SSE 调用与 JSON 接口使用同一轮次，文字在落库后分块展示，结束事件会带完整回复和卡片。
2. 等待时点「断开流」，再点「重试上一条」。重试沿用原消息 ID：处理中会提示稍后再试，完成后回放同一轮，不重复写入。关闭 SSE 后也能用该按钮经 JSON 重试。
3. 页面出错时记下提示里的追踪号；接口响应包含 `X-Trace-Id`。服务终端的 `support_http`、`support_turn` 日志可按追踪号串起 HTTP 状态、耗时、轮次和流程位置，不记录消息原文或密钥。

售后复用首版数据库的工单、审计和操作表，不需要新增迁移。验收时先做第三阶段的只读用例，再做工单和退款预检；真实退款写入只在隔离测试中台让用户主动确认。
