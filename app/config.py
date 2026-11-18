from pydantic import PositiveFloat
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 从当前工作目录读取 .env；同名系统环境变量优先，额外配置项会被忽略。
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 探活不需要这两个地址；执行数据库迁移时才要求填写 SUPPORT_DATABASE_URL。
    travel_api_base_url: str | None = None
    support_database_url: str | None = None
    # 业务时区用于解释“今天”“下周一”等日期，不是数据库的存储时区。
    business_timezone: str = "Asia/Shanghai"
    travel_api_timeout_seconds: PositiveFloat = 5
