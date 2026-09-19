"""HTTP API 冒烟测试：路由、状态码映射与既有健康检查契约。"""

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from community import CommunityStore
from service import make_server


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = CommunityStore(clock=lambda: "2026-08-01T09:00:00")
        cls.server = make_server("127.0.0.1", 0, cls.store)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, body=None, expect=200):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.base + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=2) as resp:
                self.assertEqual(resp.status, expect)
                return json.load(resp)
        except HTTPError as err:
            payload = json.loads(err.read() or b"{}")
            self.assertEqual(err.code, expect, payload)
            err.close()
            return payload

    def test_full_flow_over_http(self):
        self.assertEqual(self.call("GET", "/health")["status"], "ok")

        village = self.call("POST", "/api/villages", {"name": "沙溪", "region": "云南"})
        courtyard = self.call("POST", "/api/courtyards", {
            "village_id": village["village_id"], "name": "东篱院",
            "whole_price_fen": 20000,
            "rooms": [{"name": "东屋", "nightly_price_fen": 12000}],
        })
        self.call("POST", "/api/rules", {
            "effective_from": "2026-01-01", "deposit_bp": 2000,
            "villager_share_bp": 4000, "operator_share_bp": 3000,
            "service_hour_rate_fen": 3000,
        })
        room_id = courtyard["rooms"][0]["room_id"]
        agreement = self.call("POST", "/api/agreements", {
            "village_id": village["village_id"], "courtyard_id": courtyard["courtyard_id"],
            "mode": "room", "room_ids": [room_id], "resident_id": "甲",
            "start": "2026-08-14", "end": "2026-10-14",
        })
        for role in ("villager", "operator", "resident"):
            self.call("POST", f"/api/agreements/{agreement['agreement_id']}/confirm",
                      {"role": role, "today": "2026-08-10"})
        rights = self.call(
            "GET", f"/api/rights?courtyard_id={courtyard['courtyard_id']}&day=2026-09-01")
        self.assertEqual(rights[0]["status"], "occupied")

        # 超卖 → 409
        conflict = self.call("POST", "/api/agreements", {
            "village_id": village["village_id"], "courtyard_id": courtyard["courtyard_id"],
            "mode": "whole", "resident_id": "乙",
            "start": "2026-09-01", "end": "2026-10-01",
        }, expect=409)
        self.assertIn("error", conflict)

        # 结算幂等
        body = {"agreement_id": agreement["agreement_id"], "period": "2026-09"}
        first = self.call("POST", "/api/settlements", body)
        second = self.call("POST", "/api/settlements", body)
        self.assertEqual(first["settlement_id"], second["settlement_id"])
        self.assertEqual(first["rule_version"], 1)

        # 敏感材料：无授权 → 403，授权后 → 200
        mat = self.call("POST", "/api/materials",
                        {"resident_id": "甲", "kind": "健康证明", "content": "……"})
        staff = quote("村医")
        self.call("GET", f"/api/materials/{mat['material_id']}?staff_id={staff}", expect=403)
        self.call("POST", f"/api/materials/{mat['material_id']}/grants",
                  {"staff_id": "村医", "expires_at": "2026-09-01T00:00:00"})
        viewed = self.call("GET", f"/api/materials/{mat['material_id']}?staff_id={staff}")
        self.assertEqual(viewed["content"], "……")

    def test_error_mapping(self):
        self.call("GET", "/api/nope", expect=404)
        self.call("GET", "/api/rights", expect=400)  # 缺查询参数
        req = Request(self.base + "/api/villages", data=b"{bad json",
                      method="POST", headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as err:
            urlopen(req, timeout=2)
        self.assertEqual(err.exception.code, 400)
        err.exception.close()


if __name__ == "__main__":
    unittest.main()
