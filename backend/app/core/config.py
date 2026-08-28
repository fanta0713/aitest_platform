"""
测试任务管理系统 - 独立后台
配置管理
"""
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # 禅道配置
    zentao_url: str = "http://192.2.100.30:8000"
    zentao_token: str = ""
    zentao_admin_account: str = "admin"
    zentao_admin_password: str = ""

    # JWT密钥
    secret_key: str = "change-me-in-production"

    # 数据库配置
    db_host: str = "testtask-db"
    db_port: int = 5432
    db_name: str = "testtask"
    db_user: str = "testtask"
    db_password: str = "testtask123"
    
    @property
    def database_url(self) -> str:
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
    
    @property
    def sync_database_url(self) -> str:
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
    
    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
