from pathlib import Path

from pydantic import Field, PositiveFloat, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


# IDE 的工作目录可能是 app/；从配置文件的位置找项目根目录，避免漏读 .env。
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    # 同名系统环境变量优先于 .env，未声明的配置项会被忽略。
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # 探活不需要这两个地址；创建会话需要中台和客服库，数据库迁移只需要客服库。
    travel_api_base_url: str | None = None
    support_database_url: str | None = None
    # 业务时区用于解释“今天”“下周一”等日期，不是数据库的存储时区。
    business_timezone: str = "Asia/Shanghai"
    travel_api_timeout_seconds: PositiveFloat = 5
    # 中台重建种子后，可以改用同一批次的地区 CSV，不需要改代码。
    travel_areas_csv: Path | None = None

    # 模型理解文本并生成计划，超时后仅接受有限明确输入；最多12秒，售后另限16秒。
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_timeout_seconds: PositiveFloat = Field(default=8, le=12)

    # 测试读链路默认不发真实退款；只有拟操作测试中台时才显式打开。
    refund_write_enabled: bool = False
