from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: Optional[str] = None
    mysql_database: str = "xisiyun_asr"

    deepseek_api_key: Optional[str] = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_seconds: int = Field(default=30, ge=1, le=600)

    max_concurrent_tasks: int = Field(default=3, ge=1, le=32)
    max_upload_size_mb: int = Field(default=50, ge=1, le=1024)
    storage_upload_dir: Path = Path("./uploads")
    pipeline_max_auto_retries: int = Field(default=3, ge=1, le=10)

    @property
    def database_url(self) -> str:
        pwd = f":{self.mysql_password}" if self.mysql_password else ""
        return (
            f"mysql+asyncmy://{self.mysql_user}{pwd}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
            "?charset=utf8mb4"
        )
