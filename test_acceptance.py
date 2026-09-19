"""端到端验收：两村换住、跨月结算、突然停机后恢复核对。

用真实子进程运行 service.py（而非线程内调用），随后 SIGKILL 模拟突然停机，
再用同一状态文件重启——所有核对都走 HTTP，贴近验收现场。
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from urllib.error import HTTPError


class ApiClient:
    def __init__(self, base_url):
        self.base_url = base_url

    def post(self, path, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def get(self, path, **params):
        from urllib.parse import urlencode
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            return exc.code, json.load(exc)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class AcceptanceTest(unittest.TestCase):
    service_file = os.path.join(os.path.dirname(__file__), "service.py")

    def _start(self):
        port = free_port()
        proc = subprocess.Popen(
            [sys.executable, self.service_file, "--port", str(port),
             "--host", "127.0.0.1", "--data", self.data_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        client = ApiClient(f"http://127.0.0.1:{port}")
        for _ in range(50):
            try:
                client.get("/health")
                return proc, client
            except OSError:
                time.sleep(0.1)
        proc.kill()
        raise RuntimeError("服务未在预期时间内启动")

    def setUp(self):
        fd, self.data_path = tempfile.mkstemp(prefix="community-", suffix=".json")
        os.close(fd)
        os.unlink(self.data_path)  # 从空状态开始
        self.proc, self.api = self._start()

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if os.path.exists(self.data_path):
            os.unlink(self.data_path)

    def _kill_abruptly(self):
        """SIGKILL：没有任何优雅退出钩子，模拟突然停机。"""
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=5)

    def _restart(self):
        self.proc, self.api = self._start()

    # ----- 验收主链路 -----------------------------------------------------

    def test_full_acceptance_flow_with_abrupt_shutdown(self):
        api = self.api

        # 两个村、各一座两房院落
        _, v1 = api.post("/api/villages", {"name": "云栖村"})
        _, v2 = api.post("/api/villages", {"name": "黔岭村"})
        _, c1 = api.post("/api/courtyards",
                         {"village_id": v1["id"], "name": "一号院", "member_capacity": 2})
        _, c2 = api.post("/api/courtyards",
                         {"village_id": v2["id"], "name": "二号院", "member_capacity": 2})
        _, r1 = api.post("/api/rooms", {"courtyard_id": c1["id"], "name": "东房"})
        _, r2 = api.post("/api/rooms", {"courtyard_id": c1["id"], "name": "西房"})
        _, r3 = api.post("/api/rooms", {"courtyard_id": c2["id"], "name": "南房"})
        _, r4 = api.post("/api/rooms", {"courtyard_id": c2["id"], "name": "北房"})
        _, lib1 = api.post("/api/resources",
                           {"village_id": v1["id"], "name": "书屋", "capacity": 1})

        # v1 规则：押金 1000/间，月会员费 1500，月工时 4，分成 5:3:2
        _, rule1 = api.post("/api/rules", {
            "version": 1, "effective_on": "2026-01-01",
            "deposit_per_room": 1000, "member_fee_per_month": 1500,
            "service_hour_per_month": 4,
            "village_share": 0.5, "operator_share": 0.3, "resident_share": 0.2})

        # 老张在云栖、小李在黔岭，都是 8/15 ~ 10/15 的长租，跨 8/9/10 三个月
        _, a1 = api.post("/api/agreements", {
            "village_id": v1["id"], "person_id": "laozhang", "person_name": "老张",
            "kind": "ROOM", "room_id": r1["id"], "monthly_rent": 3000,
            "start": "2026-08-15", "end": "2026-10-16", "confirmed_on": "2026-08-01"})
        _, a2 = api.post("/api/agreements", {
            "village_id": v2["id"], "person_id": "xiaoli", "person_name": "小李",
            "kind": "ROOM", "room_id": r3["id"], "monthly_rent": 2000,
            "start": "2026-08-15", "end": "2026-10-16", "confirmed_on": "2026-08-01"})
        self.assertEqual(a1["rule_version"], 1)
        self.assertEqual(a1["deposit"], 1000)
        _, b1 = api.post("/api/bookings", {"agreement_id": a1["id"], "type": "ROOM",
                                           "start": "2026-08-15", "end": "2026-10-16"})
        _, b2 = api.post("/api/bookings", {"agreement_id": a2["id"], "type": "ROOM",
                                           "start": "2026-08-15", "end": "2026-10-16"})

        # 书屋 9/10 被老张占用，理由必须可追溯
        status, body = api.post("/api/resource-bookings", {
            "resource_id": lib1["id"], "agreement_id": a1["id"],
            "start": "2026-09-10", "end": "2026-09-11",
            "reason": "快递代收点临时堆放"})
        self.assertEqual(status, 200)
        status, body = api.post("/api/resource-bookings", {
            "resource_id": lib1["id"], "agreement_id": a1["id"],
            "start": "2026-09-10", "end": "2026-09-12", "reason": "撞档"})
        self.assertEqual(status, 409)

        # 两村同时办理换住：9/10~9/20 老张去南房、小李来东房
        _, s1 = api.post("/api/swaps", {
            "agreement_id": a1["id"], "booking_id": b1["id"], "to_room_id": r3["id"],
            "start": "2026-09-10", "end": "2026-09-20"})
        _, s2 = api.post("/api/swaps", {
            "agreement_id": a2["id"], "booking_id": b2["id"], "to_room_id": r1["id"],
            "start": "2026-09-10", "end": "2026-09-20"})
        status, paired = api.post("/api/swaps/accept",
                                  {"swap_id": s1["id"], "pair_swap_id": s2["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(paired["swap"]["status"], "AGREED")

        # 老张 9/5 临时离村 3 天，权益保留
        api.post("/api/absences", {"booking_id": b1["id"],
                                   "start": "2026-09-04", "end": "2026-09-07",
                                   "reason": "回城办事"})

        # 9 月新政策 v2 生效（9/1 登记，10/1 生效）：老合同 9 月不被追溯
        api.post("/api/rules", {
            "version": 2, "effective_on": "2026-10-01",
            "deposit_per_room": 2000, "member_fee_per_month": 2000,
            "service_hour_per_month": 8,
            "village_share": 0.7, "operator_share": 0.2, "resident_share": 0.1})

        # 三方分别确认 8 月、9 月账单（两个村各自的协约）
        for period in ("2026-08", "2026-09"):
            for agreement_id in (a1["id"],):
                for role in ("village", "operator", "resident"):
                    status, _ = api.post("/api/settlements", {
                        "agreement_id": agreement_id, "period": period, "role": role})
                    self.assertEqual(status, 200)
        for period in ("2026-08", "2026-09"):
            for role in ("village", "operator", "resident"):
                api.post("/api/settlements", {
                    "agreement_id": a2["id"], "period": period, "role": role})

        # 跨月封账（8 月按 17 天、9 月整月）
        _, aug = api.post("/api/periods/close",
                          {"village_id": v1["id"], "period": "2026-08"})
        _, sep = api.post("/api/periods/close",
                          {"village_id": v1["id"], "period": "2026-09"})
        _, sep_v2 = api.post("/api/periods/close",
                             {"village_id": v2["id"], "period": "2026-09"})
        aug_st = aug["settlements"][0]
        sep_st = sep["settlements"][0]
        self.assertEqual(aug_st["days"], 17)
        self.assertEqual(sep_st["days"], 30)

        # 身份/健康材料：授权 1 小时，仅授权人员可看
        _, doc = api.post("/api/documents",
                          {"person_id": "laozhang", "kind": "health", "label": "体检表"})
        status, denied = api.get("/api/query", kind="view-document",
                                 document_id=doc["id"], grantee_id="medic-9")
        self.assertEqual(status, 409)
        _, grant = api.post("/api/grants", {
            "document_id": doc["id"], "grantee_id": "medic-1",
            "grantee_role": "medical", "ttl_seconds": 3600,
            "now": "2026-09-19T00:00:00Z"})
        status, view = api.get("/api/query", kind="view-document",
                               document_id=doc["id"], grantee_id="medic-1",
                               now="2026-09-19T00:30:00Z")
        self.assertEqual(status, 200)
        status, expired = api.get("/api/query", kind="view-document",
                                  document_id=doc["id"], grantee_id="medic-1",
                                  now="2026-09-19T02:00:00Z")
        self.assertEqual(status, 409)

        # ===== 突然停机 =====
        self._kill_abruptly()
        self._restart()
        api = self.api

        # 健康检查仍正常
        _, health = api.get("/health")
        self.assertEqual(health["status"], "ok")

        # 恢复后核对 9/15（换住中段）：东房占用者是小李且来自黔岭村
        _, usage = api.get("/api/query", kind="usage",
                           village_id=v1["id"], day="2026-09-15")
        east = next(r for c in usage["courtyards"] for r in c["rooms"]
                    if r["name"] == "东房")
        self.assertEqual(len(east["occupants"]), 1)
        occ = east["occupants"][0]
        self.assertEqual(occ["person_id"], "xiaoli")
        self.assertEqual(occ["type"], "SWAP")
        self.assertIn("跨村换入", occ["reason"])

        # 老张 9/15 的使用权在黔岭村南房；东房只是换出
        _, rights = api.get("/api/query", kind="rights",
                            person_id="laozhang", day="2026-09-15")
        held = [r for r in rights["rights"] if r["holds_right"]]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["village_id"], v2["id"])
        self.assertEqual(held[0]["room_id"], r3["id"])
        self.assertEqual(held[0]["state"], "in_village")
        swapped_out = next(r for r in rights["rights"] if not r["holds_right"])
        self.assertEqual(swapped_out["state"], "swapped_out")

        # 9/5 老张临时离村：权益仍在、状态为 away
        _, rights_away = api.get("/api/query", kind="rights",
                                 person_id="laozhang", day="2026-09-05")
        self.assertEqual(rights_away["rights"][0]["state"], "temporarily_away")
        self.assertTrue(rights_away["rights"][0]["holds_right"])

        # 公共资源占用理由可追溯
        _, usage_res = api.get("/api/query", kind="usage",
                               village_id=v1["id"], day="2026-09-10")
        lib = usage_res["resources"][0]
        self.assertEqual(lib["occupants"][0]["reason"], "快递代收点临时堆放")
        self.assertEqual(lib["remaining"], 0)

        # 每笔分成都必须带着冻结的规则版本，且老合同仍是 v1
        for record in (aug, sep):
            (st,) = record["settlements"]
            self.assertEqual(st["rule_version"], 1)
            for split in st["splits"]:
                self.assertEqual(split["rule_version"], 1)
        # 重新查封账结果（走持久化读取）
        _, sep_reloaded = api.get("/api/query", kind="period",
                                  village_id=v1["id"], period="2026-09")
        splits = {s["party"]: s["amount"] for s in sep_reloaded["settlements"][0]["splits"]}
        self.assertEqual(splits, {"village": 1500.0, "operator": 900.0, "resident": 600.0})
        self.assertEqual(sep_v2["settlements"][0]["splits"][0]["rule_version"], 1)

        # 恢复后防超卖仍然生效
        status, conflict = api.post("/api/bookings", {
            "agreement_id": a1["id"], "type": "ROOM",
            "start": "2026-09-15", "end": "2026-09-16"})
        self.assertEqual(status, 409)

        # 恢复后授权状态继续生效/过期
        status, _ = api.get("/api/query", kind="view-document",
                            document_id=doc["id"], grantee_id="medic-1",
                            now="2026-09-19T00:40:00Z")
        self.assertEqual(status, 200)
        status, _ = api.get("/api/query", kind="view-document",
                            document_id=doc["id"], grantee_id="medic-1",
                            now="2026-09-19T03:00:00Z")
        self.assertEqual(status, 409)

        # 10 月封账：老张协约仍冻结 v1，v2 新政策不追溯
        for role in ("village", "operator", "resident"):
            api.post("/api/settlements", {
                "agreement_id": a1["id"], "period": "2026-10", "role": role})
        _, oct_rec = api.post("/api/periods/close",
                              {"village_id": v1["id"], "period": "2026-10"})
        oct_st = oct_rec["settlements"][0]
        self.assertEqual(oct_st["days"], 15)  # 10/1 ~ 10/15
        self.assertTrue(all(s["rule_version"] == 1 for s in oct_st["splits"]))

    def test_state_file_is_never_a_truncated_snapshot(self):
        # 先制造一些已落盘状态
        _, v1 = self.api.post("/api/villages", {"name": "云栖村"})
        self.assertTrue(os.path.exists(self.data_path))
        self._kill_abruptly()
        # 正式快照始终完整可读（写入走临时文件 + 原子替换）
        with open(self.data_path, "r", encoding="utf-8") as handle:
            snapshot = json.load(handle)
        self.assertIn("villages", snapshot)

        # 若快照被外力损坏，服务必须拒绝启动，绝不能用空状态覆盖旧账
        with open(self.data_path, "w", encoding="utf-8") as handle:
            handle.write('{"format": 1, "villages":')  # 模拟截断
        port = free_port()
        proc = subprocess.Popen(
            [sys.executable, self.service_file, "--port", str(port),
             "--host", "127.0.0.1", "--data", self.data_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        exit_code = proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
        self.assertNotEqual(exit_code, 0)

    def test_concurrent_http_bookings_single_winner(self):
        api = self.api
        _, v1 = api.post("/api/villages", {"name": "云栖村"})
        _, c1 = api.post("/api/courtyards",
                         {"village_id": v1["id"], "name": "一号院", "member_capacity": 0})
        _, r1 = api.post("/api/rooms", {"courtyard_id": c1["id"], "name": "东房"})
        api.post("/api/rules", {
            "version": 1, "effective_on": "2026-01-01",
            "deposit_per_room": 1000, "member_fee_per_month": 1500,
            "service_hour_per_month": 4,
            "village_share": 0.5, "operator_share": 0.3, "resident_share": 0.2})
        agreement_ids = []
        for i in range(10):
            _, ag = api.post("/api/agreements", {
                "village_id": v1["id"], "person_id": f"p{i}", "person_name": f"人{i}",
                "kind": "ROOM", "room_id": r1["id"], "monthly_rent": 100,
                "start": "2027-01-01", "end": "2027-02-01", "confirmed_on": "2026-01-01"})
            agreement_ids.append(ag["id"])

        import concurrent.futures

        def book(aid):
            return api.post("/api/bookings", {
                "agreement_id": aid, "type": "ROOM",
                "start": "2027-01-10", "end": "2027-01-12"})[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            statuses = list(pool.map(book, agreement_ids))
        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(409), 9)


if __name__ == "__main__":
    unittest.main()
