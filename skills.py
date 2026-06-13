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

目录与路径说明:
- skill/: 技能文件夹（位于项目根目录下的相对路径，如 skill/scRNA-skills）
- data/: 数据文件夹（位于项目根目录下的相对路径）
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
      "skill": "如果有可用且匹配的专用技能文件，填写其相对 skill/ 的路径（如 scRNA-skills/run.py）；若是日常通用任务，此处必须留空 (\"\")",
      "args": "命令行参数或命令。如果 skill 为空但需要执行通用命令行指令，填写要执行的命令；若是技能任务，填写技能所需的命令行参数",
      "write_file_path": "可选。若是日常写文件任务，填写待写入的目标文件相对路径",
      "write_file_content": "可选。若是日常写文件任务，填写待写入的完整文件内容",
      "expected_outputs": ["/output/..."]
    }}
  ]
}}

要求:
1. 区分领域专用技能与日常任务。日常通用任务请勿使用任何 skill（设为 `""`）。
2. 闲聊与咨询：如果不需要执行工具，步骤规划为空列表（steps 为 []），requires_confirmation 设为 false。
3. 复杂任务拆分：如涉及多个不同模块，必须拆分为多个独立步骤。
4. 路径要求：使用相对路径访问 data/ 和 skill/。
""".strip()

    planner = Agent(
        name="Generic Plan Builder",
        instructions="你只输出 JSON。不要执行工具。不要输出 Markdown。",
        model=cfg.get("model", "deepseek-chat"),
        tools=[],
    )
    try:
        result = await run_in_threadpool(Runner.run_sync, planner, prompt, max_turns=3, conversation_id=session_id)
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
def read_xlsx_summary(file_path: Path, max_rows: int = 15) -> str:
    import zipfile
    import xml.etree.ElementTree as ET
    try:
        with zipfile.ZipFile(file_path, 'r') as z:
            wb_xml = z.read('xl/workbook.xml')
            wb_tree = ET.fromstring(wb_xml)
            ns = {'ns': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
                  'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
            
            sheets = []
            for sheet in wb_tree.findall('.//ns:sheet', ns):
                sheets.append({
                    'name': sheet.attrib.get('name'),
                    'id': sheet.attrib.get('sheetId'),
                    'r_id': sheet.attrib.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
                })
            
            shared_strings = []
            if 'xl/sharedStrings.xml' in z.namelist():
                ss_xml = z.read('xl/sharedStrings.xml')
                ss_tree = ET.fromstring(ss_xml)
                for t in ss_tree.findall('.//ns:t', ns):
                    shared_strings.append(t.text or '')
            
            summary = []
            summary.append(f"【Excel 预览】文件: {file_path.name}")
            summary.append(f"工作表列表 (Sheets): {', '.join([s['name'] for s in sheets])}\n")
            
            for idx, sheet in enumerate(sheets[:2]): # Preview at most first 2 sheets
                sheet_name = sheet['name']
                sheet_file = f"xl/worksheets/sheet{idx+1}.xml"
                if sheet_file not in z.namelist():
                    sheet_files = [f for f in z.namelist() if f.startswith("xl/worksheets/sheet")]
                    if len(sheet_files) > idx:
                        sheet_file = sheet_files[idx]
                    else:
                        continue
                
                sheet_xml = z.read(sheet_file)
                sheet_tree = ET.fromstring(sheet_xml)
                
                rows_dict = {}
                for row_elem in sheet_tree.findall('.//ns:row', ns):
                    row_idx = int(row_elem.attrib.get('r', 1))
                    row_data = {}
                    for c_elem in row_elem.findall('.//ns:c', ns):
                        cell_ref = c_elem.attrib.get('r', '')
                        col_letter = ''.join(filter(str.isalpha, cell_ref))
                        
                        val_elem = c_elem.find('ns:v', ns)
                        val = val_elem.text if val_elem is not None else ''
                        
                        t = c_elem.attrib.get('t', '')
                        if t == 's' and val:
                            try:
                                val = shared_strings[int(val)]
                            except (ValueError, IndexError):
                                pass
                        elif t == 'b' and val:
                            val = 'TRUE' if val == '1' else 'FALSE'
                        row_data[col_letter] = val
                    rows_dict[row_idx] = row_data
                
                if not rows_dict:
                    summary.append(f"### 工作表 (Sheet): {sheet_name} (空)\n")
                    continue
                
                all_cols = set()
                for rd in rows_dict.values():
                    all_cols.update(rd.keys())
                
                sorted_cols = sorted(list(all_cols), key=lambda x: (len(x), x))
                
                summary.append(f"### 工作表 (Sheet): {sheet_name} (前 {max_rows} 行预览)")
                summary.append("| " + " | ".join(sorted_cols) + " |")
                summary.append("| " + " | ".join(["---"] * len(sorted_cols)) + " |")
                
                for r_i in sorted(rows_dict.keys())[:max_rows]:
                    row_cells = []
                    for col in sorted_cols:
                        row_cells.append(str(rows_dict[r_i].get(col, '')).strip().replace('\n', ' ').replace('|', '\\|'))
                    summary.append("| " + " | ".join(row_cells) + " |")
                summary.append("")
                
            return "\n".join(summary)
    except Exception as e:
        return f"读取 Excel 文件失败 {file_path.name}: {e}"


def read_csv_summary(file_path: Path, delimiter: str, max_rows: int = 15) -> str:
    import csv
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.reader(f, delimiter=delimiter)
            rows = []
            for i, row in enumerate(reader):
                if i >= max_rows:
                    break
                rows.append(row)
        
        if not rows:
            return f"文件 {file_path.name} 为空表格。"
            
        summary = []
        summary.append(f"【表格预览】文件: {file_path.name} (前 {max_rows} 行)")
        headers = rows[0]
        summary.append("| " + " | ".join(headers) + " |")
        summary.append("| " + " | ".join(["---"] * len(headers)) + " |")
        for row in rows[1:]:
            padded_row = row + [""] * (len(headers) - len(row))
            summary.append("| " + " | ".join([val.replace('\n', ' ').replace('|', '\\|').strip() for val in padded_row]) + " |")
        return "\n".join(summary)
    except Exception as e:
        return f"读取表格失败 {file_path.name}: {e}"


@function_tool
def list_skills(sub_dir: str = "") -> str:
    """
    List all available scRNA-seq analysis or other workflow skill scripts inside the /skill directory.
    
    Args:
        sub_dir: Optional subdirectory under /skill to narrow down the search (e.g., 'scRNA-skills').
        
    Returns:
        A JSON string containing the metadata of all discovered skills (ID, name, description, script file path, runtime, params_schema).
    """
    skills = discover_skills()
    if sub_dir:
        skills = [s for s in skills if s["path"].startswith(sub_dir)]
    return json.dumps(skills, ensure_ascii=False, indent=2)


@function_tool
def list_data_files(sub_dir: str = "") -> str:
    """
    List all data files inside the data directory (relative path 'data/').
    
    Args:
        sub_dir: Optional subdirectory inside the data directory to search (e.g. 'subfolder').
        
    Returns:
        A JSON string containing the list of files, including relative paths starting with 'data/', size in bytes, and file types.
    """
    base = utils.safe_join(config.DATA_DIR, sub_dir)
    files = []
    if base.exists():
        for p in base.rglob("*"):
            if p.is_file() and not utils.match_ignore(p, config.DATA_DIR):
                try:
                    rel_path = "data/" + p.relative_to(config.DATA_DIR).as_posix()
                except Exception:
                    rel_path = utils.rel_public_path(p).lstrip("/")
                files.append({"path": rel_path, "size": p.stat().st_size, "type": utils.file_type(p)})
    return json.dumps(files[:500], ensure_ascii=False, indent=2)


@function_tool
def read_project_file(file_path: str) -> str:
    """
    Read the content of a text, CSV/TSV table, or Excel (.xlsx) file in the project workspace, skills, or data directory.
    If the file is a CSV, TSV, or Excel file, it is automatically parsed and formatted as a clean Markdown table preview.
    
    Args:
        file_path: Relative path of the file to read (e.g., 'data/pbmc4k_annotation.xlsx', 'skill/scRNA-skills/README.md').
        
    Returns:
        The file text content (up to 20,000 characters), or a formatted Markdown table preview for tabular/Excel files, or an error message.
    """
    try:
        p = utils.resolve_public_path(file_path)
        if not p.exists() or not p.is_file():
            return f"未找到文件: {file_path}"
            
        ext = p.suffix.lower()
        if ext == ".xlsx":
            return read_xlsx_summary(p)
        if ext in {".csv", ".tsv"}:
            delimiter = "\t" if ext == ".tsv" else ","
            return read_csv_summary(p, delimiter)
            
        if ext not in config.TEXT_PREVIEW_EXT:
            return f"非文本或不支持的预览文件类型，不直接读取: {file_path}"
        if p.stat().st_size > 2_000_000:
            return f"文件过大，不直接读取: {file_path}"
        content = p.read_text(encoding="utf-8", errors="ignore")
        return content[:20000] + "\n...[截断]..." if len(content) > 20000 else content
    except Exception as e:
        return f"读取异常: {e}"


@function_tool
def save_analysis_report(filename: str, content: str) -> str:
    """
    Save the final analysis report (normally in markdown format) to the active run's output directory.
    
    Args:
        filename: The filename for the report (e.g., 'report.md').
        content: The text/markdown content of the report.
        
    Returns:
        A confirmation message containing the public URL path to the saved file.
    """
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
    """
    Scan for newly generated output files inside the current run workplace directory so they are registered as artifacts.
    
    Args:
        sub_dir: Optional subdirectory under the run workplace to scan.
        
    Returns:
        A text report listing all newly discovered files or a message indicating no files were found.
    """
    return scan_output_files_impl(sub_dir)


@function_tool
def write_workspace_file(path: str, content: str, step_id: str = "") -> str:
    """
    Create or overwrite a file in the active run's workspace.
    
    Args:
        path: Relative path of the file to write (e.g., 'script.py', 'parameters.json').
        content: The text content to write into the file.
        step_id: Optional step ID to update the plan progress status.
        
    Returns:
        A success or failure message.
    """
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
    """
    Execute a shell command in the active run's workspace.
    When running Python scripts, you MUST use the internal relative Python interpreter:
    'env/python-3.12.10-embed-amd64/python.exe' (e.g., 'env/python-3.12.10-embed-amd64/python.exe script.py --arg1').
    
    Args:
        cmd: The exact shell command string to execute.
        timeout_seconds: Timeout limit for command execution in seconds (defaults to 1200).
        step_id: Optional step ID to update the plan progress status.
        
    Returns:
        Stdout and stderr outputs of the execution, or an error description.
    """
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
    """
    Run a specific pre-defined skill script from the /skill directory.
    Before invoking a skill, make sure you read its README/description documentation to understand its arguments.
    
    Args:
        skill_file: The relative file path of the skill script under /skill (e.g., 'scRNA-skills/seurat_qc.r').
        args: Command-line arguments to pass to the skill script.
        timeout_seconds: Timeout limit for execution in seconds (defaults to 1200).
        step_id: Optional step ID to update the plan progress status.
        
    Returns:
        Execution stdout/stderr outputs or error logs.
    """
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
