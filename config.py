import os
import sys
import json
from pathlib import Path
from typing import Dict, Any
from dotenv import load_dotenv
from agents import set_default_openai_api, set_tracing_disabled

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = BASE_DIR / "skill"
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
PLOTS_DIR = BASE_DIR / "plots"
CHATLOG_DIR = BASE_DIR / "chatlog"
WORKSPACE_DIR = BASE_DIR / "workspace"

EMBED_PYTHON_DIR = BASE_DIR / "env" / "python-3.12.10-embed-amd64"
PYTHON_EXE = EMBED_PYTHON_DIR / "python.exe" if (EMBED_PYTHON_DIR / "python.exe").exists() else Path(sys.executable)

for d in (SKILL_DIR, DATA_DIR, OUTPUT_DIR, PLOTS_DIR, CHATLOG_DIR, WORKSPACE_DIR):
    d.mkdir(parents=True, exist_ok=True)

DEFAULT_IGNORE = {
    ".DS_Store", "__pycache__", ".ipynb_checkpoints", ".git", ".venv",
    "node_modules", "*.tmp", "*.bak"
}
TEXT_PREVIEW_EXT = {
    ".txt", ".log", ".md", ".markdown", ".py", ".r", ".R", ".sh",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".tsv",
    ".html", ".css", ".js", ".ts", ".sql"
}
CODE_EXT = {
    ".py", ".r", ".R", ".sh", ".js", ".ts", ".html", ".css",
    ".json", ".yaml", ".yml", ".toml", ".sql"
}

def load_json_file_simple(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def load_agent_config() -> Dict[str, Any]:
    cfg = load_json_file_simple(BASE_DIR / "agent.json", {})
    if not isinstance(cfg, dict):
        cfg = {}
    default = {
        "name": "Generic Skill Orchestrator Agent",
        "model": os.getenv("OPENAI_MODEL", "deepseek-chat"),
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1"),
        "max_turns": 30,
        "instructions": (
            "你是通用 Skill Orchestrator Agent。你负责理解用户目标、选择和编排 /skill 目录下的工具。"
            "不要假设固定技能名。输入来自 /data，输出进入当前 run 的 OUTPUT_DIR。"
        ),
        "enabled_skills": "*",
    }
    default.update(cfg)
    return default

AGENT_CONFIG = load_agent_config()
os.environ.setdefault("OPENAI_BASE_URL", AGENT_CONFIG.get("base_url", "https://api.deepseek.com/v1"))
set_default_openai_api("chat_completions")
set_tracing_disabled(True)
