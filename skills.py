import os
import re
import sys
import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional
from starlette.concurrency import run_in_threadpool

from agents import Agent, Runner, function_tool
import config
import state
import utils
import run_manager

def read_skill_descriptor(skill_path: Path) -> Optional[Dict[str, Any]]:
    for name in ("skill.json", "manifest.json", "agent.json"):
        p = skill_path / name
        if p.exists():
            data = utils.load_json_file(p, None)
            if isinstance(data, dict):
                return data
    return None


def discover_skills() -> List[Dict[str, Any]]:
    skills = []

    # Skill directories with skill.json or common entrypoints.
    for p in config.SKILL_DIR.iterdir() if config.SKILL_DIR.exists() else []:
        if utils.match_ignore(p, config.SKILL_DIR):
            continue
        if p.is_dir():
            descriptor = read_skill_descriptor(p) or {}
            entry = descriptor.get("entry")
            if not entry:
                for candidate in ("run.py", "main.py", "run.R", "main.R", "run.sh", "main.sh"):
                    if (p / candidate).exists():
                        entry = candidate
                        break
            if entry:
                entry_path = p / entry
                skills.append({
                    "id": descriptor.get("id") or p.name,
                    "name": descriptor.get("name") or p.name,
                    "description": descriptor.get("description") or "",
                    "path": p.relative_to(config.SKILL_DIR).as_posix(),
                    "entry": entry,
                    "skill_file": entry_path.relative_to(config.SKILL_DIR).as_posix(),
                    "runtime": descriptor.get("runtime") or infer_runtime(entry_path),
                    "params_schema": descriptor.get("params_schema"),
                    "descriptor": utils.rel_public_path(p / "skill.json") if (p / "skill.json").exists() else "",
                    "valid": entry_path.exists(),
                    "warnings": [] if (p / "README.md").exists() or (p / "README.txt").exists() else ["No README found"],
                })
        elif p.is_file() and p.suffix.lower() in {".py", ".r", ".sh"}:
            skills.append({
                "id": p.stem,
                "name": p.stem,
                "description": "",
                "path": p.name,
                "entry": p.name,
                "skill_file": p.name,
                "runtime": infer_runtime(p),
                "params_schema": "",
                "descriptor": "",
                "valid": True,
                "warnings": ["Single-file skill; no skill.json descriptor"],
            })

    # Filter by enabled_skills whitelist
    enabled = config.AGENT_CONFIG.get("enabled_skills", "*")
    if enabled != "*":
        if isinstance(enabled, str):
            enabled = [enabled]
        if isinstance(enabled, list):
            enabled_set = set(enabled)
            skills = [s for s in skills if s["id"] in enabled_set or s["path"] in enabled_set or s["skill_file"] in enabled_set]

    skills.sort(key=lambda x: x["id"])
    return skills


def infer_runtime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".py":
        return "python"
    if ext == ".r":
        return "Rscript"
    if ext == ".sh":
        return "bash"
    return "unknown"


def list_data_inventory(max_items: int = 300) -> List[Dict[str, Any]]:
    files = utils.scan_root("data", config.DATA_DIR, max_files=max_items)
    return files[:max_items]


def preflight_check(run: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    errors, warnings = [], []
    inputs, outputs = [], []
    skills = discover_skills()
    skill_files = {s["skill_file"] for s in skills}
    skill_ids = {s["id"] for s in skills}

    if not skills:
        warnings.append("未发现可执行 skill。请上传 skill 或检查 /skill 目录。")
    if not any(config.DATA_DIR.rglob("*")):
        warnings.append("未发现输入数据。请上传数据到 /data。")

    out_dir = Path(run["output_abs_dir"])
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        test = out_dir / ".write_test"
        test.write_text("ok", encoding="utf-8")
        test.unlink(missing_ok=True)
        outputs.append(run["output_dir"])
    except Exception as e:
        errors.append(f"输出目录不可写: {run['output_dir']} ({e})")

    for step in plan.get("steps", []):
        skill = step.get("skill") or step.get("skill_file")
        if skill:
            if skill not in skill_files and skill not in skill_ids:
                warnings.append(f"计划步骤引用的 skill 未直接匹配: {skill}。运行阶段 Agent 会尝试重新选择。")

        for key in ("input", "input_path", "input_file", "data", "data_path"):
            val = step.get(key)
            if isinstance(val, str) and val:
                inputs.append(val)
                try:
                    p = utils.resolve_public_path(val)
                    if not p.exists():
                        warnings.append(f"输入路径不存在: {val}")
                except Exception:
                    pass

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "inputs": sorted(set(inputs)),
        "outputs": sorted(set(outputs)),
        "skill_count": len(skills),
        "data_count": len(list_data_inventory(1000)),
        "checked_at": utils.utc_now(),
    }


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end+1])
        except Exception:
            return None
    return None


def fallback_plan(query: str, skills: List[Dict[str, Any]]) -> Dict[str, Any]:
    steps = []
    if skills:
        for i, s in enumerate(skills[:5]):
            steps.append({
                "id": f"step_{i+1}",
                "title": f"Run {s['name']}",
                "description": s.get("description") or "Execute detected generic skill.",
                "skill": s["skill_file"],
                "args": "--output \"$OUTPUT_DIR\"",
                "expected_outputs": [],
            })
    else:
        steps.append({
            "id": "step_1",
            "title": "Inspect workspace",
            "description": "No skill detected. Upload a skill package first.",
            "skill": "",
            "args": "",
            "expected_outputs": [],
        })
    return {
        "goal": query,
        "mode": "fallback",
        "steps": steps,
        "requires_confirmation": True,
    }


async def create_plan_with_agent(query: str, session_id: str, username: str, run: Dict[str, Any]) -> Dict[str, Any]:
    cfg = config.load_agent_config()
    skills = discover_skills()
    data_files = list_data_inventory(200)
    summary = run_manager.load_session_summary(username, session_id)

    prompt = f"""
你是通用 skill 编排规划器。你只能输出 JSON，不要输出 Markdown。

目录:
- /skill: 技能
- /data: 数据
- 当前输出目录: {run['output_dir']}

用户目标:
{query}

可用 skill:
{json.dumps(skills, ensure_ascii=False, indent=2)[:12000]}

数据文件:
{json.dumps(data_files, ensure_ascii=False, indent=2)[:12000]}

会话摘要:
{json.dumps(summary, ensure_ascii=False, indent=2)[:4000]}

请生成通用执行计划 JSON，格式:
{{
  "goal": "...",
  "requires_confirmation": true,
  "steps": [
    {{
      "id": "step_1",
      "title": "简短标题",
      "description": "做什么",
      "skill": "如果有可用且匹配的领域专用技能文件，填写其相对 /skill 的路径（如 scRNA-skills/run.py）；若是日常通用任务（如写文件、执行通用命令行指令），此处必须留空 (\"\")",
      "args": "命令行参数或命令。如果 skill 为空但需要执行通用命令行指令，则此处填写要执行的 Shell 命令（运行 Python 脚本时必须使用相对地址 env/python-3.12.10-embed-amd64/python.exe，例如 env/python-3.12.10-embed-amd64/python.exe draw.py）；如果是技能任务，则填写技能所需的命令行参数",
      "write_file_path": "可选。如果是日常写文件任务，填写待写入的目标文件相对路径（如 script.py）",
      "write_file_content": "可选。如果是日常写文件任务，填写待写入的完整文件内容",
      "expected_outputs": ["/output/..."]
    }}
  ]
}}

要求:
1. 区分领域专用技能与日常任务。日常通用任务（如新建/写入代码文件、执行简单 Python 脚本/Shell 命令行）请勿使用任何 skill（即 skill 设为空字符串 `""`），直接通过配置 args（运行命令，注意运行 Python 脚本时必须使用相对路径的内置 Python 解释器 env/python-3.12.10-embed-amd64/python.exe）或 write_file_path/write_file_content（写入文件）来描述。
2. 只有特定或复杂的领域级专业操作（如 scRNA 测序分析等）才匹配并调用具体的专用 skill 脚本。
3. 所有输出必须进入当前输出目录或其子目录。
""".strip()

    planner = Agent(
        name="Generic Plan Builder",
        instructions="你只输出 JSON。不要执行工具。不要输出 Markdown。",
        model=cfg.get("model", "deepseek-chat"),
        tools=[],
    )
    try:
        result = await run_in_threadpool(Runner.run_sync, planner, prompt, max_turns=3)
        data = extract_json_object(result.final_output or "")
        if isinstance(data, dict) and isinstance(data.get("steps"), list):
            return data
    except Exception:
        pass

    return fallback_plan(query, skills)


def update_plan_step_status(run: Dict[str, Any], step_id: str, status: str, message: str = "") -> None:
    for step in run.get("plan", {}).get("steps", []):
        if step.get("id") == step_id:
            step["status"] = status
            step["message"] = message
            step["updated_at"] = utils.utc_now()
            break
    run_manager.write_manifest(run)


def classify_error(stderr: str, stdout: str = "", returncode: Optional[int] = None) -> Dict[str, Any]:
    text = f"{stderr}\n{stdout}".lower()
    if "no module named" in text or "modulenotfounderror" in text:
        typ = "missing_python_package"
        suggestion = "安装缺失的 Python 包，或检查当前 Python 环境。"
    elif "there is no package called" in text or "library(" in text and "error" in text:
        typ = "missing_r_package"
        suggestion = "安装缺失的 R 包，或检查 R library 路径。"
    elif "no such file" in text or "cannot open file" in text or "file not found" in text:
        typ = "input_file_not_found"
        suggestion = "检查 /data 路径、参数文件和 skill 引用的输入文件。"
    elif "permission denied" in text:
        typ = "permission_denied"
        suggestion = "检查文件权限和输出目录写权限。"
    elif "memory" in text or "cannot allocate" in text or "std::bad_alloc" in text:
        typ = "memory_limit"
        suggestion = "降低数据规模、拆分样本或增加内存。"
    elif "timeout" in text or "timed out" in text:
        typ = "timeout"
        suggestion = "增加 timeout_seconds 或优化 skill。"
    elif returncode not in (None, 0):
        typ = "script_error"
        suggestion = "查看 stderr/stdout，定位脚本内部错误。"
    else:
        typ = "unknown_error"
        suggestion = "查看原始日志并尝试单独运行该 skill。"
    return {
        "error_type": typ,
        "suggested_fix": suggestion,
        "returncode": returncode,
        "stderr_excerpt": stderr[-4000:] if stderr else "",
        "stdout_excerpt": stdout[-2000:] if stdout else "",
    }


# ============================================================
# Tool functions used by Agent during run_plan
# ============================================================
@function_tool
def list_skills(sub_dir: str = "") -> str:
    skills = discover_skills()
    if sub_dir:
        skills = [s for s in skills if s["path"].startswith(sub_dir)]
    return json.dumps(skills, ensure_ascii=False, indent=2)


@function_tool
def list_data_files(sub_dir: str = "") -> str:
    base = utils.safe_join(config.DATA_DIR, sub_dir)
    files = []
    if base.exists():
        for p in base.rglob("*"):
            if p.is_file() and not utils.match_ignore(p, config.DATA_DIR):
                files.append({"path": utils.rel_public_path(p), "size": p.stat().st_size, "type": utils.file_type(p)})
    return json.dumps(files[:500], ensure_ascii=False, indent=2)


@function_tool
def read_project_file(file_path: str) -> str:
    try:
        p = utils.resolve_public_path(file_path)
        if not p.exists() or not p.is_file():
            return f"未找到文件: {file_path}"
        if p.suffix.lower() not in config.TEXT_PREVIEW_EXT:
            return f"非文本文件，不直接读取: {file_path}"
        if p.stat().st_size > 2_000_000:
            return f"文件过大，不直接读取: {file_path}"
        content = p.read_text(encoding="utf-8", errors="ignore")
        return content[:20000] + "\n...[截断]..." if len(content) > 20000 else content
    except Exception as e:
        return f"读取异常: {e}"


@function_tool
def save_analysis_report(filename: str, content: str) -> str:
    rid = state.active_run_id
    if not rid or rid not in state.RUNS:
        return "没有 active run。"
    run = state.RUNS[rid]
    safe_name = os.path.basename(filename) or "report.md"
    if not safe_name.endswith((".md", ".txt")):
        safe_name += ".md"
    path = Path(run["output_abs_dir"]) / safe_name
    path.write_text(content, encoding="utf-8")
    run_manager.register_artifact(run, path, "Analysis Report")
    run_manager.event(run, "artifact", "report", "success", utils.rel_public_path(path), path=utils.rel_public_path(path))
    return f"文件已保存: {utils.rel_public_path(path)}"


def scan_output_files_impl(sub_dir: str = "") -> str:
    rid = state.active_run_id
    if not rid or rid not in state.RUNS:
        return "没有 active run。"
    run = state.RUNS[rid]
    session_workspace = run_manager.get_session_workspace(run["username"], run["session_id"])
    base = utils.safe_join(session_workspace, sub_dir)
    if not base.exists():
        return f"工作区目录不存在: {sub_dir}"
    rows = []
    for p in base.rglob("*"):
        if p.is_file() and not utils.match_ignore(p, session_workspace):
            run_manager.register_artifact(run, p)
            rows.append(utils.rel_public_path(p))
    return "输出文件:\n" + "\n".join(rows[:300]) if rows else "无输出文件。"


@function_tool
def scan_output_files(sub_dir: str = "") -> str:
    return scan_output_files_impl(sub_dir)


@function_tool
def write_workspace_file(path: str, content: str, step_id: str = "") -> str:
    if state.abort_flag:
        return "操作已中断"
    rid = state.active_run_id
    if not rid or rid not in state.RUNS:
        return "没有 active run。"
    run = state.RUNS[rid]

    step_id = step_id or "write_file"
    update_plan_step_status(run, step_id, "running", f"写入文件 {path}")
    run_manager.event(run, "step", f"write {path}", "running", path, step_id=step_id)

    try:
        target_path = Path(path)
        if not target_path.is_absolute():
            session_workspace = run_manager.get_session_workspace(run["username"], run["session_id"])
            target_path = (session_workspace / target_path).resolve()
        
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding='utf-8')
        
        scan_output_files_impl("")
        update_plan_step_status(run, step_id, "success", "写入成功")
        run_manager.event(run, "step", f"write {path}", "success", "写入成功", step_id=step_id, returncode=0)
        return f"【写入成功】文件已保存至: {utils.rel_public_path(target_path)}"
    except Exception as e:
        cls = classify_error(str(e), "", None)
        run["error_classification"] = cls
        update_plan_step_status(run, step_id, "failed", str(e))
        run_manager.event(run, "step", f"write {path}", "error", str(e), step_id=step_id, returncode=1, error_classification=cls)
        run_manager.write_manifest(run)
        return f"【写入失败】{e}。\n【重要系统指令】该步骤执行异常。你必须【立刻停止】后续步骤的执行，不可尝试自行修复或调试。请直接向用户报告异常原因并询问进一步指示。"


@function_tool
def execute_workspace_command(cmd: str, timeout_seconds: int = 1200, step_id: str = "") -> str:
    if state.abort_flag:
        return "操作已中断"
    rid = state.active_run_id
    if not rid or rid not in state.RUNS:
        return "没有 active run。"
    run = state.RUNS[rid]

    step_id = step_id or "execute_command"
    update_plan_step_status(run, step_id, "running", cmd)
    run_manager.event(run, "step", cmd, "running", cmd, step_id=step_id)

    try:
        session_workspace = run_manager.get_session_workspace(run["username"], run["session_id"])

        env = os.environ.copy()
        if config.EMBED_PYTHON_DIR.exists():
            embed_dir = str(config.EMBED_PYTHON_DIR)
            scripts_dir = str(config.EMBED_PYTHON_DIR / "Scripts")
            env["PATH"] = embed_dir + os.pathsep + scripts_dir + os.pathsep + env.get("PATH", "")
        env.update({
            "SKILL_DIR": str(config.SKILL_DIR),
            "DATA_DIR": str(config.DATA_DIR),
            "GLOBAL_OUTPUT_DIR": str(config.OUTPUT_DIR),
            "OUTPUT_DIR": str(Path(run["output_abs_dir"])),
            "WORKSPACE_DIR": str(session_workspace),
            "RUN_ID": run["id"],
            "USERNAME": run["username"],
            "RUN_MANIFEST": str(run_manager.manifest_path(run)),
            "RUN_EVENTS": str(run_manager.events_path(run)),
        })

        state.active_subprocess = subprocess.Popen(
            cmd,
            shell=True,
            cwd=str(session_workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        stdout, stderr = state.active_subprocess.communicate(timeout=max(1, int(timeout_seconds)))
        rc = state.active_subprocess.returncode
        state.active_subprocess = None

        scan_output_files_impl("")

        if state.abort_flag:
            raise RuntimeError("操作被用户强制中断")

        if rc == 0:
            update_plan_step_status(run, step_id, "success", "进程结束")
            run_manager.event(run, "step", cmd, "success", "进程结束", step_id=step_id, returncode=rc)
            return f"【执行成功】{cmd}\n\n【STDOUT】\n{stdout[-12000:]}"
        else:
            cls = classify_error(stderr, stdout, rc)
            run["error_classification"] = cls
            update_plan_step_status(run, step_id, "failed", stderr[-1000:])
            run_manager.event(run, "step", cmd, "error", stderr[-4000:], step_id=step_id, returncode=rc, error_classification=cls)
            run_manager.write_manifest(run)
            return (
                f"【执行失败】{cmd}\n退出码: {rc}\n\n"
                f"【错误分类】\n{json.dumps(cls, ensure_ascii=False, indent=2)}\n\n"
                f"【STDERR】\n{stderr[-12000:]}\n\n"
                f"【STDOUT】\n{stdout[-8000:]}\n\n"
                "【重要系统指令】该步骤执行已失败。根据交互规则，你必须【立刻停止】后续步骤的执行。严禁自行编写脚本安装包、修复环境或尝试调试。请直接将此错误整理报告给用户，并询问用户的进一步指示。"
            )
    except subprocess.TimeoutExpired:
        if state.active_subprocess:
            state.active_subprocess.kill()
            state.active_subprocess = None
        cls = classify_error("timeout", "", None)
        run["error_classification"] = cls
        update_plan_step_status(run, step_id, "failed", "timeout")
        run_manager.event(run, "step", cmd, "error", "timeout", step_id=step_id, error_classification=cls)
        run_manager.write_manifest(run)
        return f"命令执行超时: {cmd}。\n【重要系统指令】该步骤超时失败。你必须【立刻停止】后续步骤的执行，不可尝试自行修复或调试。请直接向用户报告超时原因并询问进一步指示。"
    except Exception as e:
        if state.active_subprocess:
            try:
                state.active_subprocess.kill()
            except Exception:
                pass
            state.active_subprocess = None
        cls = classify_error(str(e), "", None)
        run["error_classification"] = cls
        update_plan_step_status(run, step_id, "failed", str(e))
        run_manager.event(run, "step", cmd, "error", str(e), step_id=step_id, error_classification=cls)
        run_manager.write_manifest(run)
        return f"系统异常或中断: {e}。\n【重要系统指令】该步骤执行异常。你必须【立刻停止】后续步骤的执行，不可尝试自行修复或调试。请直接向用户报告异常原因并询问进一步指示。"


@function_tool
def execute_skill(skill_file: str, args: str = "", timeout_seconds: int = 1200, step_id: str = "") -> str:
    if state.abort_flag:
        return "操作已中断"
    rid = state.active_run_id
    if not rid or rid not in state.RUNS:
        return "没有 active run。"
    run = state.RUNS[rid]

    step_id = step_id or "execute_skill"
    update_plan_step_status(run, step_id, "running", skill_file)
    run_manager.event(run, "step", skill_file, "running", args, step_id=step_id)

    try:
        script_path = utils.safe_join(config.SKILL_DIR, skill_file)
        if not script_path.exists() or not script_path.is_file():
            raise FileNotFoundError(f"未找到技能文件: /skill/{skill_file}")

        ext = script_path.suffix.lower()
        if ext == ".r":
            exe = shutil.which("Rscript")
            if not exe:
                raise RuntimeError("未找到 Rscript")
            cmd = [exe, str(script_path)]
        elif ext == ".py":
            cmd = [str(config.PYTHON_EXE), str(script_path)]
        elif ext == ".sh":
            cmd = [shutil.which("bash") or "bash", str(script_path)]
        else:
            raise RuntimeError(f"暂不支持的技能类型: {ext}")

        extra_args = shlex.split(args or "")
        cmd.extend(extra_args)

        session_workspace = run_manager.get_session_workspace(run["username"], run["session_id"])

        env = os.environ.copy()
        if config.EMBED_PYTHON_DIR.exists():
            embed_dir = str(config.EMBED_PYTHON_DIR)
            scripts_dir = str(config.EMBED_PYTHON_DIR / "Scripts")
            env["PATH"] = embed_dir + os.pathsep + scripts_dir + os.pathsep + env.get("PATH", "")
        env.update({
            "SKILL_DIR": str(config.SKILL_DIR),
            "DATA_DIR": str(config.DATA_DIR),
            "GLOBAL_OUTPUT_DIR": str(config.OUTPUT_DIR),
            "OUTPUT_DIR": str(Path(run["output_abs_dir"])),
            "WORKSPACE_DIR": str(session_workspace),
            "RUN_ID": run["id"],
            "USERNAME": run["username"],
            "RUN_MANIFEST": str(run_manager.manifest_path(run)),
            "RUN_EVENTS": str(run_manager.events_path(run)),
        })

        state.active_subprocess = subprocess.Popen(
            cmd,
            cwd=str(session_workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        stdout, stderr = state.active_subprocess.communicate(timeout=max(1, int(timeout_seconds)))
        rc = state.active_subprocess.returncode
        state.active_subprocess = None

        scan_output_files_impl("")

        if state.abort_flag:
            raise RuntimeError("操作被用户强制中断")

        if rc == 0:
            update_plan_step_status(run, step_id, "success", "进程结束")
            run_manager.event(run, "step", skill_file, "success", "进程结束", step_id=step_id, returncode=rc)
            return f"【执行成功】/skill/{skill_file}\n\n【STDOUT】\n{stdout[-12000:]}"
        else:
            cls = classify_error(stderr, stdout, rc)
            run["error_classification"] = cls
            update_plan_step_status(run, step_id, "failed", stderr[-1000:])
            run_manager.event(run, "step", skill_file, "error", stderr[-4000:], step_id=step_id, returncode=rc, error_classification=cls)
            run_manager.write_manifest(run)
            return (
                f"【执行失败】/skill/{skill_file}\n退出码: {rc}\n\n"
                f"【错误分类】\n{json.dumps(cls, ensure_ascii=False, indent=2)}\n\n"
                f"【STDERR】\n{stderr[-12000:]}\n\n"
                f"【STDOUT】\n{stdout[-8000:]}\n\n"
                "【重要系统指令】该步骤执行已失败。根据交互规则，你必须【立刻停止】后续步骤的执行。严禁自行编写脚本安装包、修复环境或尝试调试。请直接将此错误整理报告给用户，并询问用户的进一步指示。"
            )
    except subprocess.TimeoutExpired:
        if state.active_subprocess:
            state.active_subprocess.kill()
            state.active_subprocess = None
        cls = classify_error("timeout", "", None)
        run["error_classification"] = cls
        update_plan_step_status(run, step_id, "failed", "timeout")
        run_manager.event(run, "step", skill_file, "error", "timeout", step_id=step_id, error_classification=cls)
        run_manager.write_manifest(run)
        return f"技能执行超时: /skill/{skill_file}。\n【重要系统指令】该步骤超时失败。你必须【立刻停止】后续步骤的执行，不可尝试自行修复或调试。请直接向用户报告超时原因并询问进一步指示。"
    except Exception as e:
        if state.active_subprocess:
            try:
                state.active_subprocess.kill()
            except Exception:
                pass
            state.active_subprocess = None
        cls = classify_error(str(e), "", None)
        run["error_classification"] = cls
        update_plan_step_status(run, step_id, "failed", str(e))
        run_manager.event(run, "step", skill_file, "error", str(e), step_id=step_id, error_classification=cls)
        run_manager.write_manifest(run)
        return f"系统异常或中断: {e}。\n【重要系统指令】该步骤执行异常。你必须【立刻停止】后续步骤的执行，不可尝试自行修复或调试。请直接向用户报告异常原因并询问进一步指示。"
