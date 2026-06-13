import os
import time
import uuid
import json
import zipfile
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional
import config
import state

def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def now_short() -> str:
    return time.strftime("%H:%M:%S")


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, item: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def sanitize_username(value: str | None) -> str:
    raw = (value or "guest").strip()
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in ("-", "_", "."))
    return safe or "guest"


def safe_id(value: str | None, prefix: str = "run") -> str:
    raw = (value or "").strip()
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in ("-", "_", "."))
    return safe or f"{prefix}_{uuid.uuid4().hex[:10]}"


def load_users() -> List[Dict[str, str]]:
    data = load_json_file(config.BASE_DIR / "user.json", {"users": []})
    users = data if isinstance(data, list) else data.get("users", []) if isinstance(data, dict) else []
    out = []
    for u in users:
        if not isinstance(u, dict):
            continue
        username = str(u.get("username") or u.get("account") or u.get("name") or "").strip()
        account = str(u.get("account") or username).strip()
        password = str(u.get("password") or "").strip()
        name = str(u.get("name") or username).strip()
        if username and password:
            out.append({"name": name, "username": username, "account": account, "password": password})
    return out


def username_from_token(token: str | None) -> Optional[str]:
    token = (token or "").strip()
    if token and token in state.AUTH_TOKENS:
        return sanitize_username(state.AUTH_TOKENS[token]["username"])
    return None


def get_username_from_mapping(mapping: Dict[str, Any]) -> str:
    token_user = username_from_token(str(mapping.get("auth_token") or ""))
    if token_user:
        return token_user
    return sanitize_username(mapping.get("username") or "guest")


def run_output_dir(username: str, session_id: str) -> Path:
    return config.OUTPUT_DIR / sanitize_username(username) / safe_id(session_id, "session")


def rel_public_path(path: Path) -> str:
    p = path.resolve()
    for root_name, root in (("workspace", config.WORKSPACE_DIR), ("output", config.OUTPUT_DIR), ("data", config.DATA_DIR), ("skill", config.SKILL_DIR), ("plots", config.PLOTS_DIR), ("chatlog", config.CHATLOG_DIR)):
        try:
            rel = p.relative_to(root.resolve()).as_posix()
            return f"/{root_name}/{rel}"
        except Exception:
            pass
    return str(path)


def read_ignore_rules() -> List[str]:
    rules = set(config.DEFAULT_IGNORE)
    for p in (config.BASE_DIR / ".ignore", config.OUTPUT_DIR / ".ignore", config.SKILL_DIR / ".ignore", config.DATA_DIR / ".ignore"):
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        rules.add(line)
            except Exception:
                pass
    return sorted(rules)


def match_ignore(path: Path, base: Path) -> bool:
    try:
        rel = path.relative_to(base).as_posix()
    except Exception:
        rel = path.name
    name = path.name
    for rule in read_ignore_rules():
        r = rule.strip().replace("\\", "/")
        if not r:
            continue
        if r.endswith("/"):
            folder = r.rstrip("/")
            if rel == folder or rel.startswith(folder + "/") or f"/{folder}/" in rel:
                return True
        elif "*" in r:
            import fnmatch
            if fnmatch.fnmatch(name, r) or fnmatch.fnmatch(rel, r):
                return True
        else:
            if name == r or rel == r or f"/{r}/" in rel:
                return True
    return False


def safe_join(root: Path, user_path: str) -> Path:
    user_path = (user_path or "").strip().replace("\\", "/").lstrip("/")
    if not user_path or user_path in {".", "./"}:
        return root.resolve()
    candidate = (root / user_path).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError("安全拦截：路径越界")
    return candidate


def resolve_public_path(public_path: str) -> Path:
    p = (public_path or "").strip().replace("\\", "/").split("?", 1)[0]
    if p.startswith("/workspace/") or p.startswith("workspace/"):
        return safe_join(config.WORKSPACE_DIR, p.replace("/workspace/", "", 1).replace("workspace/", "", 1))
    if p.startswith("/skill/") or p.startswith("skill/"):
        return safe_join(config.SKILL_DIR, p.replace("/skill/", "", 1).replace("skill/", "", 1))
    if p.startswith("/data/") or p.startswith("data/"):
        return safe_join(config.DATA_DIR, p.replace("/data/", "", 1).replace("data/", "", 1))
    if p.startswith("/output/") or p.startswith("output/"):
        return safe_join(config.OUTPUT_DIR, p.replace("/output/", "", 1).replace("output/", "", 1))
    if p.startswith("/plots/") or p.startswith("plots/"):
        return safe_join(config.PLOTS_DIR, p.replace("/plots/", "", 1).replace("plots/", "", 1))
    if p.startswith("/chatlog/") or p.startswith("chatlog/"):
        return safe_join(config.CHATLOG_DIR, p.replace("/chatlog/", "", 1).replace("chatlog/", "", 1))
    
    # 备选路径解析支持 (Fallback resolution for prefix-free relative paths)
    if state.active_run_id and state.active_run_id in state.RUNS:
        run = state.RUNS[state.active_run_id]
        workplace = Path(run["output_abs_dir"]) / "workplace"
        try:
            return safe_join(workplace, p)
        except ValueError:
            pass

    try:
        return safe_join(config.BASE_DIR, p)
    except ValueError:
        raise ValueError("仅允许访问 /workspace、/skill、/data、/output、/plots、/chatlog 下的文件或项目内相对路径")


def file_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".png":
        return "image"
    if ext in {".csv", ".tsv"}:
        return "table"
    if ext in {".md", ".markdown"}:
        return "markdown"
    if ext in config.CODE_EXT:
        return "code"
    if ext in {".txt", ".log", ".jsonl"}:
        return "text"
    return "download"


def scan_root(root_name: str, root_path: Path, max_files: int = 1000) -> List[Dict[str, Any]]:
    files = []
    if not root_path.exists():
        return files
    for p in root_path.rglob("*"):
        if len(files) >= max_files:
            break
        if p.is_file() and not match_ignore(p, root_path):
            public_path = rel_public_path(p)
            files.append({
                "name": p.name,
                "path": public_path,
                "url": public_path,
                "type": file_type(p),
                "size": p.stat().st_size,
                "modified": p.stat().st_mtime,
                "root": root_name,
                "download_only": file_type(p) == "download",
            })
    files.sort(key=lambda x: x["modified"], reverse=True)
    return files


def safe_extract_zip(zip_path: Path, target_dir: Path) -> List[str]:
    extracted = []
    with zipfile.ZipFile(zip_path, "r") as z:
        for member in z.infolist():
            name = member.filename.replace("\\", "/")
            if not name or name.startswith("/") or ".." in Path(name).parts:
                continue
            dest = (target_dir / name).resolve()
            target_resolved = target_dir.resolve()
            if dest != target_resolved and target_resolved not in dest.parents:
                continue
            if member.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(member) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted.append(dest.relative_to(target_dir).as_posix())
    return extracted
