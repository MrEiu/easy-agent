import os
import argparse
import sys

# 解析参数
parser = argparse.ArgumentParser(description="Skill Orchestrator API Server")
parser.add_argument("--dir", type=str, default="", help="设置项目总地址(包含 data/ 和 skill/ 等目录)。默认自动获取当前代码父文件夹地址。")
parser.add_argument("--host", type=str, default="127.0.0.1", help="API server host")
parser.add_argument("--port", type=int, default=8000, help="API server port")
args, unknown = parser.parse_known_args()

if args.dir:
    os.environ["OPENAIQAQ_WORKDIR"] = os.path.abspath(args.dir)

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

import config
import api

routes = [
    Route("/", api.get_homepage),
    Route("/api/config", api.config_api, methods=["GET"]),
    Route("/api/login", api.login_api, methods=["POST"]),

    Route("/api/plan", api.plan_api, methods=["POST"]),
    Route("/api/run_plan", api.run_plan_api, methods=["POST"]),
    Route("/api/analyze", api.analyze_compat_api, methods=["POST"]),

    Route("/api/logs", api.get_logs_api, methods=["GET"]),
    Route("/api/stop", api.stop_api, methods=["POST"]),
    Route("/api/clear", api.clear_session_api, methods=["POST"]),

    Route("/api/runs", api.get_runs_api, methods=["GET"]),
    Route("/api/runs/{run_id}", api.get_run_api, methods=["GET"]),
]

app = Starlette(
    debug=True,
    routes=routes,
    middleware=[Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])],
)

if __name__ == "__main__":
    import subprocess
    import sys
    
    # 启动独立的资源管理器进程 (explorer.py, 端口 6475)
    explorer_path = os.path.join(os.path.dirname(__file__), "explorer.py")
    explorer_process = subprocess.Popen(
        [sys.executable, explorer_path],
        env=os.environ.copy()
    )
    
    try:
        uvicorn.run(app, host=args.host or os.getenv("HOST", "127.0.0.1"), port=args.port or int(os.getenv("PORT", "8000")))
    finally:
        print("正在停止资源管理器服务...")
        explorer_process.terminate()
        try:
            explorer_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            explorer_process.kill()
