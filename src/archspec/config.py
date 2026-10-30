"""集中保存本地运行配置，避免路径和限制散落在业务代码里。"""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """第一阶段配置；以后部署时可改为从环境变量读取。"""

    # 测试/独立实例可指定数据根；默认仍使用项目原来的 data，不搬旧文件。
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("ARCHSPEC_DATA_DIR", "data")))
    max_file_bytes: int = 20 * 1024 * 1024

    @property
    def standard_verification_enabled(self) -> bool:
        return os.getenv("STANDARD_VERIFICATION_ENABLED", "false").lower() == "true"

    @property
    def standard_admin_token(self) -> str | None:
        return os.getenv("STANDARD_ADMIN_TOKEN") or None

    @property
    def standard_admin_actor_id(self) -> str | None:
        return os.getenv("STANDARD_ADMIN_ACTOR_ID") or None

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def model_cache_dir(self) -> Path:
        """本地语义模型缓存目录；data 已被 Git 忽略，不会上传大模型文件。"""

        return self.data_dir / "models"

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.data_dir / 'archspec.db'}"

    @property
    def deepseek_api_key(self) -> str | None:
        """只从服务端环境变量读取密钥，绝不提供给浏览器。"""

        return os.getenv("DEEPSEEK_API_KEY") or None

    @property
    def deepseek_base_url(self) -> str:
        return os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")

    @property
    def deepseek_model(self) -> str:
        return os.getenv("DEEPSEEK_MODEL", "deepseek-flash")

    @property
    def deepseek_timeout_seconds(self) -> float:
        return float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "45"))

    @property
    def agent_max_steps(self) -> int:
        return min(max(int(os.getenv("AGENT_MAX_STEPS", "4")), 1), 8)

    @property
    def agent_max_tool_calls(self) -> int:
        return min(max(int(os.getenv("AGENT_MAX_TOOL_CALLS", "8")), 1), 16)

    @property
    def agent_max_material_characters(self) -> int:
        configured = int(os.getenv("AGENT_MAX_MATERIAL_CHARACTERS", "4000"))
        return min(max(configured, 500), 8000)


settings = Settings()
