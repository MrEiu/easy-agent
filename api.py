import os
import time
import uuid
import json
import mimetypes
from pathlib import Path
from typing import List, Dict, Any, Optional

import uvicorn
from starlette.responses import JSONResponse, HTMLResponse, PlainTextResponse, FileResponse
from starlette.concurrency import run_in_threadpool

from agents import Agent, Runner
import config
import state
import utils
import run_manager
import skills
import tracing  # Ensure HTTPX client patched send & trace hooks are loaded/registered

async def get_homepage(request):
    try:
        return HTMLResponse((config.BASE_DIR / "index.html").read_text(encoding="utf-8"))
    except Exception:
        return HTMLResponse("index.html not found", status_code=404)


async def config_api(request):
    cfg = config.load_agent_config()
    return JSONResponse({
        "agent": {
            "name": cfg.get("name"),
            "model": cfg.get("model"),
            "base_url": cfg.get("base_url"),
            "max_turns": cfg.get("max_turns"),
        },
        "users_enabled": bool(utils.load_users()),
        "ignore_rules": utils.read_ignore_rules(),
        "output_structure": "/output/<username>/<run_id>/",
    })


async def login_api(request):
    body = await request.json()
    account = str(body.get("account") or body.get("username") or "").strip()
    password = str(body.get("password") or "").strip()

    for user in utils.load_users():
        if account in {user["username"], user["account"]} and password == user["password"]:
            token = uuid.uuid4().hex
            state.AUTH_TOKENS[token] = {
                "username": user["username"],
                "name": user["name"],
                "account": user["account"],
            }
            return JSONResponse({
                "success": True,
                "token": token,
                "username": user["username"],
                "name": user["name"],
                "account": user["account"],
            })
    return JSONResponse({"success": False, "error": "账号或密码错误"}, status_code=401)


async def plan_api(request):
    body = await request.json()
    query = body.get("query", "")
    session_id = body.get("session_id", "default")
    username = utils.get_username_from_mapping(body)

    run = run_manager.create_run_context(session_id, username, query)
    state.active_run_id = run["id"]

    history = run_manager.get_session_history(session_id)
    history.append({"role": "user", "content": query})
    run_manager.save_chat_to_disk(session_id, "USER", query, username)

    run_manager.event(run, "plan", "create_plan", "running", "Generating execution plan")
    plan = await skills.create_plan_with_agent(query, session_id, username, run)
    run["plan"] = plan
    preflight = skills.preflight_check(run, plan)
    run["preflight"] = preflight
    run["status"] = "planned"

    run_manager.event(run, "plan", "preflight", "success" if preflight["ok"] else "active", json.dumps(preflight, ensure_ascii=False))
    run_manager.write_manifest(run)

    return JSONResponse({
        "success": True,
        "run_id": run["id"],
        "username": username,
        "output_dir": run["output_dir"],
        "manifest_path": run["manifest_path"],
        "events_path": run["events_path"],
        "summary_path": run["summary_path"],
        "plan": plan,
        "preflight": preflight,
        "started_at": run["started_at"],
    })


async def run_plan_api(request):
    body = await request.json()
    run_id = body.get("run_id")
    if not run_id or run_id not in state.RUNS:
        return JSONResponse({"success": False, "error": "run_id not found"}, status_code=404)

    run = state.RUNS[run_id]
    state.active_run_id = run_id
    state.abort_flag = False
    state.global_execution_logs = []
    state.api_interceptions = []
    run["status"] = "running"
    run["started_at"] = run.get("started_at") or utils.utc_now()
    run_manager.write_manifest(run)

    run_manager.event(run, "run", "start", "running", f"Output: {run['output_dir']}")

    cfg = config.load_agent_config()
    history = run_manager.get_session_history(run["session_id"])
    summary = run_manager.load_session_summary(run["username"], run["session_id"])

    prompt = f"""
你是通用 Skill Orchestrator Agent。你需要运行用户确认的执行计划。

当前 run:
{json.dumps({k: run[k] for k in ['id','username','session_id','query','output_dir']}, ensure_ascii=False, indent=2)}

计划:
{json.dumps(run.get('plan', {}), ensure_ascii=False, indent=2)}

规则:
1. 当前的系统是windows 64位。数据输入文件夹为项目根目录下的相对路径 data/。读取文件只能读取项目内的文件(同级及以下)。
2. 在调用 execute_skill 前，先用 read_project_file 读取技能所在目录下的说明文件。
   - 特别注意：scRNA-skills 相关模块需调用 R 解释器（`env/R-4.6.2/bin/Rscript.exe`）运行。
3. 运行任务规则：
   - 技能任务（`skill` 不为空）：必须调用 `execute_skill(skill_file, args, step_id)`。
   - 日常写文件任务（`write_file_path` 不为空）：必须调用 `write_workspace_file(path, content, step_id)`。
   - 通用命令或脚本：必须调用 `execute_workspace_command(cmd, step_id)`。执行 Python 脚本时，必须使用内部解释器相对路径 `env/python-3.12.10-embed-amd64/python.exe`。
4. 失败处理：如果任何步骤出现错误，请分析原因修改再尝试，最多五次。五次不行则停止，禁止更改环境安装包等。
5. 系统会自动获取日志和收集产出文件并追加到最终报告中，你只需关注执行步骤和分析错误，完成后直接输出简要结束语即可，不需要自己生成长篇报告。
""".strip()

    try:
        agent = Agent(
            name=cfg.get("name", "Generic Skill Orchestrator Agent"),
            instructions=cfg.get("instructions", "你是通用 Skill Orchestrator Agent。"),
            model=cfg.get("model", "deepseek-chat"),
            tools=[
                skills.list_skills,
                skills.list_data_files,
                skills.read_project_file,
                skills.execute_skill,
                skills.write_workspace_file,
                skills.execute_workspace_command
            ],
        )

        result = await run_in_threadpool(Runner.run_sync, agent, prompt, max_turns=int(cfg.get("max_turns", 30)), conversation_id=run["session_id"])

        if state.abort_flag:
            raise RuntimeError("User Aborted")

        output = result.final_output or ""
        run["ended_at"] = utils.utc_now()
        run_manager.scan_run_artifacts(run)

        any_failed = any(step.get("status") == "failed" for step in run.get("plan", {}).get("steps", []))

        # 自动生成常规报告和日志汇总
        report_lines = ["\n\n### 📊 运行报告 (自动生成)\n"]
        for step in run.get("plan", {}).get("steps", []):
            status = step.get("status", "pending")
            report_lines.append(f"- **{step.get('title', '未知步骤')}**: {status}")
            if step.get("message"):
                report_lines.append(f"  - 详情: {step.get('message')}")
                
        if run.get("artifacts"):
            report_lines.append("\n#### 📄 产出文件\n")
            for a in run["artifacts"]:
                report_lines.append(f"- [{a['name']}]({a['url']})")
                
        if any_failed and run.get("error_classification"):
            report_lines.append("\n#### ❌ 错误信息\n")
            report_lines.append(f"```json\n{json.dumps(run['error_classification'], ensure_ascii=False, indent=2)}\n```")

        output = output + "\n" + "\n".join(report_lines)
        run["output"] = output

        if any_failed:
            run["status"] = "failed"
            run_manager.event(run, "run", "complete", "failed", "Run completed with step failure(s)")
            run_manager.write_manifest(run)

            history.append({"role": "agent", "content": output})
            run_manager.save_chat_to_disk(run["session_id"], "AGENT", output, run["username"])
            run_manager.update_session_summary(run["username"], run["session_id"], run, output)

            return JSONResponse({
                "success": False,
                "error": "Pipeline step execution failed",
                "run_id": run_id,
                "output": output,
                "manifest": run["manifest"],
                "artifacts": run.get("artifacts", []),
                "logs": run.get("logs", []),
                "api_traces": run.get("api_traces", []),
            })

        run["status"] = "success"
        run_manager.event(run, "run", "complete", "success", "Run completed")
        run_manager.write_manifest(run)

        history.append({"role": "agent", "content": output})
        run_manager.save_chat_to_disk(run["session_id"], "AGENT", output, run["username"])
        run_manager.update_session_summary(run["username"], run["session_id"], run, output)

        return JSONResponse({
            "success": True,
            "run_id": run_id,
            "output": output,
            "manifest": run["manifest"],
            "artifacts": run.get("artifacts", []),
            "logs": run.get("logs", []),
            "api_traces": run.get("api_traces", []),
        })
    except Exception as e:
        run["status"] = "failed"
        run["ended_at"] = utils.utc_now()
        run["error"] = str(e)
        if not run.get("error_classification"):
            run["error_classification"] = skills.classify_error(str(e))
        run_manager.event(run, "run", "failed", "error", str(e), error_classification=run.get("error_classification"))
        run_manager.write_manifest(run)
        run_manager.save_chat_to_disk(run["session_id"], "SYSTEM_ERROR", str(e), run["username"])
        run_manager.update_session_summary(run["username"], run["session_id"], run, str(e))
        return JSONResponse({
            "success": False,
            "run_id": run_id,
            "error": str(e),
            "error_classification": run.get("error_classification"),
            "manifest": run.get("manifest"),
            "logs": run.get("logs", []),
            "api_traces": run.get("api_traces", []),
        }, status_code=500)


async def analyze_compat_api(request):
    body = await request.json()
    fake_request = type("Req", (), {"json": lambda self: body})()
    plan_response = await plan_api(fake_request)
    if plan_response.status_code >= 400:
        return plan_response
    plan_data = json.loads(plan_response.body.decode("utf-8"))
    body["run_id"] = plan_data["run_id"]
    fake_request2 = type("Req", (), {"json": lambda self: body})()
    return await run_plan_api(fake_request2)


async def get_logs_api(request):
    run_id = request.query_params.get("run_id")
    if run_id and run_id in state.RUNS:
        run = state.RUNS[run_id]
        return JSONResponse({"logs": run.get("logs", []), "api_traces": run.get("api_traces", [])})
    return JSONResponse({"logs": state.global_execution_logs, "api_traces": state.api_interceptions})


async def get_runs_api(request):
    return JSONResponse({"runs": list(state.RUNS.values())})


async def get_run_api(request):
    run_id = request.path_params["run_id"]
    run = state.RUNS.get(run_id)
    if not run:
        # Try loading manifest from disk by searching output.
        for p in config.OUTPUT_DIR.rglob(f"run_manifest_{run_id}.json"):
            data = utils.load_json_file(p, None)
            if isinstance(data, dict) and data.get("run_id") == run_id:
                return JSONResponse({"id": run_id, "manifest": data, **data})
        return JSONResponse({"error": "run not found"}, status_code=404)
    return JSONResponse(run)


async def stop_api(request):
    state.abort_flag = True
    if state.active_subprocess:
        try:
            state.active_subprocess.terminate()
            state.active_subprocess.kill()
        except Exception:
            pass
        state.active_subprocess = None
    if state.active_run_id and state.active_run_id in state.RUNS:
        run = state.RUNS[state.active_run_id]
        run["status"] = "interrupted"
        run["ended_at"] = utils.utc_now()
        run_manager.event(run, "run", "stop", "error", "User interrupted")
        run_manager.write_manifest(run)
    return JSONResponse({"success": True})


async def clear_session_api(request):
    body = await request.json()
    session_id = body.get("session_id", "default")
    username = utils.get_username_from_mapping(body)
    if session_id in state.SESSIONS_MAP:
        state.SESSIONS_MAP[session_id].clear()
    run_manager.save_chat_to_disk(session_id, "SYSTEM", "--- SESSION CLEARED BY USER ---", username)
    return JSONResponse({"success": True})


async def workspace_ignore_api(request):
    return PlainTextResponse("\n".join(utils.read_ignore_rules()))


async def workspace_tree_api(request):
    root = request.query_params.get("root", "all").lower()
    username = utils.sanitize_username(request.query_params.get("username") or "guest")
    session_id = request.query_params.get("session_id", "default")
    run_id = request.query_params.get("run_id")
    max_files = int(request.query_params.get("max_files", "1500"))

    session_workspace = run_manager.get_session_workspace(username, session_id, run_id)
    roots = {
        "workspace": session_workspace,
        "skill": config.SKILL_DIR,
        "data": config.DATA_DIR,
        "output": config.OUTPUT_DIR / username if (config.OUTPUT_DIR / username).exists() else config.OUTPUT_DIR,
    }
    selected = roots.items() if root == "all" else [(root, roots[root])] if root in roots else roots.items()

    files: List[Dict[str, Any]] = []
    for name, path in selected:
        files.extend(utils.scan_root(name, path, max_files=max_files))

    # Include all output if requested by all, but user-specific comes first.
    files.sort(key=lambda x: x.get("modified", 0), reverse=True)
    return JSONResponse({"files": files[:max_files]})


async def skills_validate_api(request):
    skills_list = skills.discover_skills()
    return JSONResponse({"success": True, "skills": skills_list})


async def file_preview_api(request):
    public_path = request.query_params.get("path", "")
    max_bytes = int(request.query_params.get("max_bytes", "200000"))
    table_rows = int(request.query_params.get("table_rows", "20"))

    try:
        p = utils.resolve_public_path(public_path)
        if not p.exists() or not p.is_file():
            return JSONResponse({"success": False, "error": "file not found"}, status_code=404)

        ftype = utils.file_type(p)
        size = p.stat().st_size
        info = {
            "success": True,
            "path": public_path,
            "name": p.name,
            "type": ftype,
            "size": size,
            "download_url": f"/api/download?path={public_path}",
            "previewable": ftype in {"image", "markdown", "code", "text", "table"},
            "truncated": False,
        }

        if ftype == "image":
            return JSONResponse({**info, "url": public_path})

        if ftype in {"markdown", "code", "text"}:
            if size > max_bytes:
                content = p.read_bytes()[:max_bytes].decode("utf-8", errors="ignore")
                info["truncated"] = True
            else:
                content = p.read_text(encoding="utf-8", errors="ignore")
            return JSONResponse({**info, "content": content})

        if ftype == "table":
            if size > max_bytes:
                info["truncated"] = True
            lines = []
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f):
                    if i >= table_rows:
                        break
                    lines.append(line.rstrip("\n"))
            return JSONResponse({**info, "content": "\n".join(lines), "table_rows": table_rows})

        return JSONResponse({**info, "previewable": False})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def download_api(request):
    public_path = request.query_params.get("path", "")
    try:
        p = utils.resolve_public_path(public_path)
        if not p.exists() or not p.is_file():
            return JSONResponse({"error": "file not found"}, status_code=404)
        return FileResponse(str(p), filename=p.name, media_type=mimetypes.guess_type(str(p))[0] or "application/octet-stream")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def upload_data_api(request):
    form = await request.form()
    files = form.getlist("files") or ([form["file"]] if "file" in form else [])
    saved = []
    for upload in files:
        filename = os.path.basename(upload.filename or f"data_{int(time.time())}")
        dest = config.DATA_DIR / filename
        with open(dest, "wb") as f:
            f.write(await upload.read())
        saved.append(f"/data/{filename}")
    return JSONResponse({"success": True, "saved": saved})


async def upload_skill_api(request):
    form = await request.form()
    files = form.getlist("files") or ([form["file"]] if "file" in form else [])
    saved, extracted = [], []
    for upload in files:
        filename = os.path.basename(upload.filename or f"skill_{int(time.time())}")
        dest = config.SKILL_DIR / filename
        with open(dest, "wb") as f:
            f.write(await upload.read())
        if filename.lower().endswith(".zip"):
            extracted.extend(utils.safe_extract_zip(dest, config.SKILL_DIR))
            try:
                dest.unlink()
            except Exception:
                pass
        else:
            saved.append(f"/skill/{filename}")
    return JSONResponse({"success": True, "saved": saved, "extracted": [f"/skill/{x}" for x in extracted]})
