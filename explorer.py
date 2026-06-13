import os
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

import config
import api

routes = [
    Route("/api/workspace/tree", api.workspace_tree_api, methods=["GET"]),
    Route("/api/workspace/ignore", api.workspace_ignore_api, methods=["GET"]),
    Route("/api/skills/validate", api.skills_validate_api, methods=["GET"]),
    Route("/api/file/preview", api.file_preview_api, methods=["GET"]),
    Route("/api/download", api.download_api, methods=["GET"]),
    Route("/api/upload/data", api.upload_data_api, methods=["POST"]),
    Route("/api/upload/skill", api.upload_skill_api, methods=["POST"]),

    Mount("/workspace", app=StaticFiles(directory=str(config.WORKSPACE_DIR)), name="workspace"),
    Mount("/output", app=StaticFiles(directory=str(config.OUTPUT_DIR)), name="output"),
    Mount("/data", app=StaticFiles(directory=str(config.DATA_DIR)), name="data"),
    Mount("/skill", app=StaticFiles(directory=str(config.SKILL_DIR)), name="skill"),
    Mount("/plots", app=StaticFiles(directory=str(config.PLOTS_DIR)), name="plots"),
    Mount("/chatlog", app=StaticFiles(directory=str(config.CHATLOG_DIR)), name="chatlog"),
]

app = Starlette(
    debug=True,
    routes=routes,
    middleware=[Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])],
)

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "6475")))
