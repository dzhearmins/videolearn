"""配置加载：所有敏感信息只来自环境变量或项目根目录的 .env（已 gitignore）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / ".state"
OUTPUT_DIR = ROOT / "output"


@dataclass
class Settings:
    student_id: str = ""
    password: str = ""
    deepseek_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    vision_model: str = ""
    whisper_model: str = "small"
    asr_prompt: str = ""
    share_server: str = ""
    share_token: str = ""
    share_name: str = ""
    state_dir: Path = field(default=STATE_DIR)
    output_dir: Path = field(default=OUTPUT_DIR)

    @property
    def llm_key(self) -> str:
        return self.deepseek_api_key or os.getenv("LLM_API_KEY", "")

    def require_pku_credentials(self) -> None:
        if not self.student_id or not self.password:
            raise SystemExit(
                "缺少 PKU_STUDENT_ID / PKU_PASSWORD。\n"
                "请复制 .env.example 为 .env 并填写，或在当前终端设置这两个环境变量。"
            )


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    llm_model = os.getenv("LLM_MODEL", "deepseek-chat").strip()

    return Settings(
        student_id=os.getenv("PKU_STUDENT_ID", "").strip(),
        password=os.getenv("PKU_PASSWORD", ""),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        llm_model=llm_model,
        vision_model=os.getenv("VISION_MODEL", "").strip() or llm_model,
        whisper_model=os.getenv("VIDEOLRN_WHISPER_MODEL", "small").strip(),
        asr_prompt=os.getenv("VIDEOLRN_ASR_PROMPT", "").strip(),
        share_server=os.getenv("SHARE_SERVER", "").strip().rstrip("/"),
        share_token=os.getenv("SHARE_TOKEN", "").strip(),
        share_name=os.getenv("SHARE_NAME", "").strip(),
    )
