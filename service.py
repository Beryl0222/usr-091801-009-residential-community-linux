"""旅居社区长期协约的运行入口与 JSON API。

状态在每条写命令成功后原子落盘（临时文件 + os.replace），进程被突然杀掉后
重启即可从最近一次完整快照恢复；读到半个文件的情况会回退到上一份快照。
"""

import argparse
import json
import os
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from domain import Community, DomainError

SERVICE_ID = "residential-community"
SERVICE_NAME = "旅居社区长期协约"
DEFAULT_STATE_FILE = "community_state.json"


def health_payload():
    """返回健康检查内容。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Store:
    """持有社区聚合根并负责快照持久化。"""

    def __init__(self, path: str):
        self.path = path
        self.write_lock = __import__("threading").Lock()
        self.community = self._load()

    def _load(self) -> Community:
        if not os.path.exists(self.path):
            return Community()
        with open(self.path, "r", encoding="utf-8") as handle:
            try:
                snapshot = json.load(handle)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"状态文件 {self.path} 已损坏（可能写入时被截断），"
                    "请使用备份恢复，拒绝用空状态覆盖旧账") from exc
        return Community.from_snapshot(snapshot)

    def save(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, tmp_path = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.community.to_snapshot(), handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise


# (方法名, 是否落盘) —— 命令全部 POST，查询全部 GET
COMMANDS = {
    "villages": "register_village",
    "courtyards": "register_courtyard",
    "rooms": "register_room",
    "resources": "register_resource",
    "rules": "register_rule",
    "agreements": "confirm_agreement",
    "bookings": "create_booking",
    "absences": "register_absence",
    "swaps": "request_swap",
    "closures": "register_closure",
    "resource-bookings": "book_resource",
    "proposals": "create_proposal",
    "activities": "create_activity",
    "documents": "register_document",
    "grants": "grant_document",
    "settlements": "confirm_settlement_party",
}

# POST /api/<resource>/<action> -> 领域方法名（ID 等参数全部放 body）
ACTIONS = {
    "agreements": {"cancel": "cancel_agreement"},
    "bookings": {"cancel": "cancel_booking"},
    "swaps": {"accept": "accept_swap", "cancel": "cancel_swap"},
    "proposals": {"vote": "vote_proposal", "close": "close_proposal"},
    "activities": {"signup": "signup_activity", "cancel-signup": "cancel_signup"},
    "grants": {"revoke": "revoke_grant"},
    "periods": {"close": "close_period", "reopen": "reopen_period"},
}


class Handler(BaseHTTPRequestHandler):
    """HTTP 适配层：解析 -> 调领域方法 -> JSON 响应/落盘。"""

    store: Store = None  # 由 make_server 注入到类属性

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._write_json(200, health_payload())
            return
        if parsed.path != "/api/query":
            self.send_error(404)
            return
        query = parse_qs(parsed.query)
        try:
            kind = query.pop("kind", [None])[0]
            params = {key: values[0] for key, values in query.items()}
            result = self._dispatch_query(kind, params)
        except DomainError as exc:
            self._write_json(409, {"error": str(exc)})
            return
        except (KeyError, ValueError) as exc:
            self._write_json(409, {"error": f"查询参数缺失或非法：{exc}"})
            return
        self._write_json(200, result)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.send_error(404)
            return
        payload = self._read_body()
        if payload is None:
            return  # 非法 JSON 时 _read_body 已写出 400
        parts = [p for p in parsed.path.split("/") if p]
        # 命令执行与快照落盘在同一把锁内，杜绝“旧快照覆盖新状态”
        with self.store.write_lock:
            try:
                result = self._dispatch(parts, payload)
            except DomainError as exc:
                self._write_json(409, {"error": str(exc)})
                return
            except (KeyError, ValueError, TypeError) as exc:
                self._write_json(409, {"error": f"请求参数缺失或非法：{exc}"})
                return
            self.store.save()
        self._write_json(200, result)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._write_json(400, {"error": f"请求体不是合法 JSON：{exc}"})
            return None
        return payload if isinstance(payload, dict) else {}

    def _dispatch(self, parts: list[str], payload: dict):
        if len(parts) == 2:
            resource = parts[1]
            if resource not in COMMANDS:
                raise DomainError(f"未知资源：{resource}")
            return getattr(self.store.community, COMMANDS[resource])(payload)
        if len(parts) == 3:
            resource, action = parts[1], parts[2]
            method_name = ACTIONS.get(resource, {}).get(action)
            if not method_name:
                raise DomainError(f"未知操作：{resource}/{action}")
            return getattr(self.store.community, method_name)(payload)
        raise DomainError("未知端点")

    def _dispatch_query(self, kind: str, params: dict):
        community = self.store.community
        if kind == "usage":
            return community.usage_on(int(params["village_id"]), params["day"])
        if kind == "rights":
            return community.rights_of(params["person_id"], params["day"])
        if kind == "period":
            result = community.get_period(int(params["village_id"]), params["period"])
            if result is None:
                raise DomainError("该账期尚未封账")
            return result
        if kind == "grants":
            return {"grants": community.list_grants()}
        if kind == "view-document":
            return community.view_document(
                {"document_id": params["document_id"], "grantee_id": params["grantee_id"]},
                now_iso=params.get("now"))
        raise DomainError(f"未知查询：{kind}")

    def _write_json(self, status: int, body: dict | list):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        return


def make_server(host: str, port: int, state_path: str) -> ThreadingHTTPServer:
    Handler.store = Store(state_path)
    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--data", default=os.environ.get("STATE_FILE", DEFAULT_STATE_FILE))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["status"] == "ok"
        Store(args.data)  # 快照可正常加载才算健康
        print("基础检查通过")
        return
    server = make_server(args.host, args.port, args.data)
    server.serve_forever()


if __name__ == "__main__":
    main()
