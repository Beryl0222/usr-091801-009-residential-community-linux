"""旅居社区长期协约的运行入口：健康检查 + 领域 API。"""

import argparse
import json
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from community import (
    CommunityStore,
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDenied,
)
from community.api import Api

SERVICE_ID = "residential-community"
SERVICE_NAME = "旅居社区长期协约"


def health_payload():
    """返回健康检查内容。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """健康检查与领域 API 的 HTTP 入口。"""

    api = None  # 由 make_server 绑定

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, health_payload())
            return
        if parsed.path.startswith("/api/") and self.api is not None:
            self._dispatch("GET", parsed)
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and self.api is not None:
            self._dispatch("POST", parsed)
            return
        self.send_error(404)

    def _dispatch(self, method, parsed):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            self._send_json(400, {"error": "请求体不是合法 JSON"})
            return
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            status, payload = self.api.handle(method, parsed.path, query, body)
        except NotFoundError as exc:
            status, payload = 404, {"error": str(exc)}
        except PermissionDenied as exc:
            status, payload = 403, {"error": str(exc)}
        except ConflictError as exc:
            status, payload = 409, {"error": str(exc)}
        except DomainError as exc:
            status, payload = 400, {"error": str(exc)}
        except (KeyError, TypeError, ValueError) as exc:
            status, payload = 400, {"error": f"参数错误：{exc}"}
        self._send_json(status, payload)

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def make_server(host, port, store):
    """绑定领域 store 的 HTTP 服务。"""
    handler = type("BoundHandler", (Handler,), {"api": Api(store)})
    return ThreadingHTTPServer((host, port), handler)


def self_check():
    """基础检查：健康负载 + 领域核心在临时目录跑通并恢复。"""
    assert health_payload()["status"] == "ok"
    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/data.json"
        store = CommunityStore(path=path, clock=lambda: "2026-09-01T09:00:00")
        village = store.add_village("自检村", "云南")
        courtyard = store.add_courtyard(
            village.village_id,
            "自检院",
            20000,
            [{"name": "东屋", "nightly_price_fen": 12000}],
        )
        store.add_rule("2026-01-01", 2000, 4000, 3000, 3000, "自检规则")
        agreement = store.create_agreement(
            village.village_id,
            courtyard.courtyard_id,
            "room",
            [courtyard.rooms[0].room_id],
            "自检旅居者",
            "2026-09-01",
            "2026-09-15",
        )
        for role in ("villager", "operator", "resident"):
            store.confirm_agreement(agreement.agreement_id, role, "2026-08-25")
        store.settle(agreement.agreement_id, "2026-09")
        restored = CommunityStore.load(path, clock=lambda: "2026-09-01T09:00:00")
        assert restored.settlements_of(agreement.agreement_id), "恢复后结算单缺失"
    print("基础检查通过")


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data", default="community_data.json", help="数据快照文件")
    args = parser.parse_args()
    if args.check:
        self_check()
        return
    store = CommunityStore.load(args.data)
    make_server("0.0.0.0", args.port, store).serve_forever()


if __name__ == "__main__":
    main()
