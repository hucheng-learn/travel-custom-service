# 旅游智能客服

当前包含 FastAPI 启动入口、环境配置、业务模块目录和客服数据库首版迁移。会话接口、流程执行器和旅游中台联调尚未实现。

## 启动与探活

在项目根目录打开 PowerShell。已安装依赖时，不需要先建库或填写 `.env`：

```powershell
uv --no-cache run --no-sync uvicorn app.main:app --host 127.0.0.1 --port 18082 --reload
```

另开一个 PowerShell 窗口，或在浏览器打开同一地址：

```powershell
Invoke-RestMethod http://127.0.0.1:18082/health | ConvertTo-Json -Compress
```

预期返回 `{"status":"ok"}`。这是 HTTP 探活，不检查 MySQL 或旅游中台。

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
