"""配置加载：所有敏感信息只来自环境变量或项目根目录的 .env（已 gitignore）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
# VIDEOLRN_HOME 让同一份代码可以带多套状态/产物运行（远端内测把 .env、output、
# .state 放在独立目录，代码目录本身保持只读）。默认仍是项目根目录。
HOME = Path(os.environ.get("VIDEOLRN_HOME") or ROOT).expanduser().resolve()
STATE_DIR = HOME / ".state"
OUTPUT_DIR = HOME / "output"


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
    # 云端 ASR（OpenAI 兼容 /audio/transcriptions）
    asr_backend: str = "local"
    asr_base_url: str = ""
    asr_api_key: str = ""
    asr_model: str = ""
    asr_language: str = "zh"
    asr_timeout: float = 180.0
    asr_chunk_seconds: float = 300.0
    asr_response_format: str = "json"
    share_server: str = ""
    share_token: str = ""
    share_name: str = ""
    auto_clean_video: bool = False
    state_dir: Path = field(default=STATE_DIR)
    output_dir: Path = field(default=OUTPUT_DIR)

    @property
    def llm_key(self) -> str:
        return self.deepseek_api_key or os.getenv("LLM_API_KEY", "")

    def require_pku_credentials(self) -> None:
        if not self.student_id or not self.password:
            raise SystemExit(
                "缺少 PKU_STUDENT_ID / PKU_PASSWORD。\n"
                "网页里点右上角「设置」填写学号与密码；"
                "命令行也可写 .env 或设置环境变量。"
            )


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def load_settings() -> Settings:
    load_dotenv(HOME / ".env")
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
        asr_backend=os.getenv("VIDEOLRN_ASR_BACKEND", "local").strip().lower(),
        asr_base_url=os.getenv("ASR_BASE_URL", "").strip().rstrip("/"),
        asr_api_key=(
            os.getenv("ASR_API_KEY", "").strip()
            or os.getenv("SILICONFLOW_API_KEY", "").strip()
            or os.getenv("GROQ_API_KEY", "").strip()
        ),
        asr_model=os.getenv("ASR_MODEL", "").strip(),
        asr_language=os.getenv("ASR_LANGUAGE", "zh").strip() or "zh",
        asr_timeout=_env_float("ASR_TIMEOUT", 180.0),
        asr_chunk_seconds=max(30.0, _env_float("ASR_CHUNK_SECONDS", 300.0)),
        asr_response_format=os.getenv("ASR_RESPONSE_FORMAT", "json").strip() or "json",
        share_server=os.getenv("SHARE_SERVER", "").strip().rstrip("/"),
        share_token=os.getenv("SHARE_TOKEN", "").strip(),
        share_name=os.getenv("SHARE_NAME", "").strip(),
        auto_clean_video=os.getenv("VIDEOLRN_AUTO_CLEAN_VIDEO", "").strip().lower()
        in ("1", "true", "yes", "on"),
        state_dir=HOME / ".state",
        output_dir=HOME / "output",
    )
