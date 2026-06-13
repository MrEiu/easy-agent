from typing import Dict, List, Any
import subprocess

AUTH_TOKENS: Dict[str, Dict[str, str]] = {}
SESSIONS_MAP: Dict[str, List[Dict[str, str]]] = {}
RUNS: Dict[str, Dict[str, Any]] = {}
active_subprocess: subprocess.Popen | None = None
abort_flag = False
active_run_id: str | None = None
global_execution_logs: List[Dict[str, Any]] = []
api_interceptions: List[Dict[str, Any]] = []
