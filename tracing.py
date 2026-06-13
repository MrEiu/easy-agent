import httpx
from typing import Dict, Any
import state
import utils

original_sync_send = httpx.Client.send

def patched_sync_send(self, request, *args, **kwargs):
    if state.abort_flag:
        raise Exception("User Aborted Operation")
    req_body = request.content.decode("utf-8", errors="ignore") if request.content else ""
    append_trace({"type": "request", "method": request.method, "url": str(request.url), "body": req_body, "timestamp": utils.now_short()})
    response = original_sync_send(self, request, *args, **kwargs)
    try:
        response.read()
        res_body = response.text
    except Exception:
        res_body = "<无法读取响应体>"
    append_trace({"type": "response", "status": response.status_code, "body": res_body, "timestamp": utils.now_short()})
    return response

httpx.Client.send = patched_sync_send

original_async_send = httpx.AsyncClient.send

async def patched_async_send(self, request, *args, **kwargs):
    if state.abort_flag:
        raise Exception("User Aborted Operation")
    req_body = request.content.decode("utf-8", errors="ignore") if request.content else ""
    append_trace({"type": "request", "method": request.method, "url": str(request.url), "body": req_body, "timestamp": utils.now_short()})
    response = await original_async_send(self, request, *args, **kwargs)
    try:
        await response.aread()
        res_body = response.text
    except Exception:
        res_body = "<无法读取响应体>"
    append_trace({"type": "response", "status": response.status_code, "body": res_body, "timestamp": utils.now_short()})
    return response

httpx.AsyncClient.send = patched_async_send


def append_trace(item: Dict[str, Any]) -> None:
    rid = state.active_run_id
    if rid and rid in state.RUNS:
        state.RUNS[rid].setdefault("api_traces", []).append(item)
    state.api_interceptions.append(item)
