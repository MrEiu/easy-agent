import time
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional
import config
import state
import utils

def get_run(run_id: str) -> Dict[str, Any]:
    if run_id not in state.RUNS:
        raise KeyError(f"run not found: {run_id}")
    return state.RUNS[run_id]


def manifest_path(run: Dict[str, Any]) -> Path:
    return Path(run["manifest_abs_path"])


def events_path(run: Dict[str, Any]) -> Path:
    return Path(run["events_abs_path"])


def summary_path(username: str, session_id: str) -> Path:
    return config.CHATLOG_DIR / utils.sanitize_username(username) / f"{utils.safe_id(session_id, 'session')}.summary.json"


def session_chatlog_path(username: str, session_id: str) -> Path:
    return config.CHATLOG_DIR / utils.sanitize_username(username) / f"chat_history_{utils.safe_id(session_id, 'session')}.md"


def create_run_context(session_id: str, username: str, query: str, client_run_id: Optional[str] = None) -> Dict[str, Any]:
    username = utils.sanitize_username(username)
    run_id = utils.safe_id(client_run_id, "run") if client_run_id else f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    out_dir = utils.run_output_dir(username, run_id)
    for sub in ("params", "logs", "artifacts", "temp"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)

    run = {
        "id": run_id,
        "session_id": session_id,
        "username": username,
        "query": query,
        "status": "planned",
        "started_at": utils.utc_now(),
        "ended_at": None,
        "output_dir": utils.rel_public_path(out_dir),
        "output_abs_dir": str(out_dir),
        "manifest_path": utils.rel_public_path(out_dir / "run_manifest.json"),
        "manifest_abs_path": str(out_dir / "run_manifest.json"),
        "events_path": utils.rel_public_path(out_dir / "logs" / "events.jsonl"),
        "events_abs_path": str(out_dir / "logs" / "events.jsonl"),
        "summary_path": utils.rel_public_path(summary_path(username, session_id)),
        "logs": [],
        "api_traces": [],
        "artifacts": [],
        "preflight": {},
        "plan": {"steps": []},
        "manifest": {},
        "output": "",
        "error": None,
        "error_classification": None,
    }
    state.RUNS[run_id] = run
    write_manifest(run)
    return run


def write_manifest(run: Dict[str, Any]) -> None:
    plan = run.get("plan") or {"steps": []}
    manifest = {
        "schema": "generic_skill_run_manifest.v1",
        "run_id": run["id"],
        "username": run["username"],
        "session_id": run["session_id"],
        "query": run.get("query", ""),
        "status": run.get("status", "planned"),
        "started_at": run.get("started_at"),
        "ended_at": run.get("ended_at"),
        "output_dir": run.get("output_dir"),
        "manifest_path": run.get("manifest_path"),
        "events_path": run.get("events_path"),
        "summary_path": run.get("summary_path"),
        "plan": plan,
        "preflight": run.get("preflight", {}),
        "artifacts": run.get("artifacts", []),
        "error": run.get("error"),
        "error_classification": run.get("error_classification"),
    }
    run["manifest"] = manifest
    utils.write_json(manifest_path(run), manifest)


def event(run: Dict[str, Any], event_type: str, step: str, status: str, message: str = "", **extra) -> None:
    item = {
        "time": utils.utc_now(),
        "timestamp": utils.now_short(),
        "type": event_type,
        "step": step,
        "status": status,
        "message": message,
        **extra,
    }
    run.setdefault("logs", []).append(item)
    utils.append_jsonl(events_path(run), item)
    write_manifest(run)

    if state.active_run_id == run["id"]:
        state.global_execution_logs.append(item)


def register_artifact(run: Dict[str, Any], path: Path, title: Optional[str] = None) -> None:
    if not path.exists() or not path.is_file():
        return
    public = utils.rel_public_path(path)
    artifact = {
        "path": public,
        "url": public,
        "name": path.name,
        "title": title or path.name,
        "type": utils.file_type(path),
        "size": path.stat().st_size,
        "modified": path.stat().st_mtime,
    }
    existing = {a.get("path") for a in run.setdefault("artifacts", [])}
    if public not in existing:
        run["artifacts"].append(artifact)
        utils.append_jsonl(events_path(run), {
            "time": utils.utc_now(),
            "timestamp": utils.now_short(),
            "type": "artifact",
            "step": "artifact",
            "status": "success",
            "message": public,
            "path": public,
            "artifact_type": artifact["type"],
        })
    write_manifest(run)


def scan_run_artifacts(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    session_workspace = get_session_workspace(run["username"], run["session_id"])
    for p in session_workspace.rglob("*"):
        if p.is_file() and not utils.match_ignore(p, session_workspace):
            register_artifact(run, p)
    return run.get("artifacts", [])


def get_session_history(session_id: str) -> List[Dict[str, str]]:
    if session_id not in state.SESSIONS_MAP:
        state.SESSIONS_MAP[session_id] = []
    return state.SESSIONS_MAP[session_id]


def save_chat_to_disk(session_id: str, role: str, content: str, username: str = "guest") -> None:
    path = session_chatlog_path(username, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"### 🗣️ [{role.upper()}] - {utils.utc_now()}\n{content}\n\n---\n\n")


def load_session_summary(username: str, session_id: str) -> Dict[str, Any]:
    return utils.load_json_file(summary_path(username, session_id), {
        "username": username,
        "session_id": session_id,
        "user_goal": "",
        "last_run_id": "",
        "last_status": "",
        "known_outputs": [],
        "notes": [],
        "updated_at": None,
    })


def update_session_summary(username: str, session_id: str, run: Dict[str, Any], output: str = "") -> None:
    summary = load_session_summary(username, session_id)
    summary.update({
        "username": username,
        "session_id": session_id,
        "last_run_id": run["id"],
        "last_status": run.get("status"),
        "last_query": run.get("query"),
        "last_output_dir": run.get("output_dir"),
        "updated_at": utils.utc_now(),
    })
    known = {x for x in summary.get("known_outputs", [])}
    for a in run.get("artifacts", []):
        if a.get("path"):
            known.add(a["path"])
    summary["known_outputs"] = sorted(known)
    if output:
        summary["last_response_excerpt"] = output[:2000]
    utils.write_json(summary_path(username, session_id), summary)


def get_session_workspace(username: str, session_id: str) -> Path:
    path = config.WORKSPACE_DIR / utils.sanitize_username(username) / utils.safe_id(session_id, "session")
    path.mkdir(parents=True, exist_ok=True)
    return path

