"""领域模型单元测试：覆盖防超卖、版本化结算、限时授权、换住与快照。"""

import threading
import unittest

from domain import Community, DomainError


def build_two_villages(c: Community):
    """云栖村(v1)/黔岭村(v2)，各一座两房院落 + 容量 1 的书屋资源。"""
    v1 = c.register_village({"name": "云栖村"})["id"]
    v2 = c.register_village({"name": "黔岭村"})["id"]
    c1 = c.register_courtyard({"village_id": v1, "name": "一号院", "member_capacity": 2})["id"]
    c2 = c.register_courtyard({"village_id": v2, "name": "二号院", "member_capacity": 1})["id"]
    rooms1 = [c.register_room({"courtyard_id": c1, "name": n})["id"] for n in ("东房", "西房")]
    rooms2 = [c.register_room({"courtyard_id": c2, "name": n})["id"] for n in ("南房", "北房")]
    book1 = c.register_resource({"village_id": v1, "name": "书屋", "capacity": 1})["id"]
    book2 = c.register_resource({"village_id": v2, "name": "书屋", "capacity": 1})["id"]
    return {
        "v1": v1, "v2": v2, "c1": c1, "c2": c2,
        "r1": rooms1[0], "r2": rooms1[1], "r3": rooms2[0], "r4": rooms2[1],
        "book1": book1, "book2": book2,
    }


def rule_v1(c: Community):
    return c.register_rule({
        "version": 1, "effective_on": "2026-01-01",
        "deposit_per_room": 1000, "member_fee_per_month": 1500,
        "service_hour_per_month": 4,
        "village_share": 0.5, "operator_share": 0.3, "resident_share": 0.2,
    })


class CapacityTest(unittest.TestCase):
    def setUp(self):
        self.c = Community()
        self.w = build_two_villages(self.c)
        rule_v1(self.c)

    def _agreement(self, person, room, village=None, rent=3000,
                   start="2026-08-01", end="2026-11-30", confirmed_on="2026-08-01",
                   kind="ROOM", courtyard_id=None):
        payload = {"village_id": village or self.w["v1"], "person_id": person,
                   "person_name": person, "kind": kind, "monthly_rent": rent,
                   "start": start, "end": end, "confirmed_on": confirmed_on}
        if kind == "ROOM":
            payload["room_id"] = room
        if courtyard_id is not None:
            payload["courtyard_id"] = courtyard_id
        return self.c.confirm_agreement(payload)

    def test_overlapping_room_nights_rejected(self):
        a = self._agreement("p1", self.w["r1"])
        self.c.create_booking({"agreement_id": a["id"], "type": "ROOM",
                               "start": "2026-08-01", "end": "2026-11-30"})
        # 另一协约哪怕只重叠一晚也不能卖
        a2 = self._agreement("p2", self.w["r1"], start="2026-11-29", end="2026-12-31")
        with self.assertRaisesRegex(DomainError, "超卖"):
            self.c.create_booking({"agreement_id": a2["id"], "type": "ROOM",
                                   "start": "2026-11-29", "end": "2026-12-05"})

    def test_adjacent_nights_without_overlap_are_fine(self):
        a = self._agreement("p1", self.w["r1"], end="2026-09-01")
        self.c.create_booking({"agreement_id": a["id"], "type": "ROOM",
                               "start": "2026-08-01", "end": "2026-09-01"})
        a2 = self._agreement("p2", self.w["r1"], start="2026-09-01", end="2026-10-01")
        self.c.create_booking({"agreement_id": a2["id"], "type": "ROOM",
                               "start": "2026-09-01", "end": "2026-10-01"})

    def test_whole_courtyard_cannot_oversell_occupied_room(self):
        a = self._agreement("p1", self.w["r1"], start="2026-09-01", end="2026-10-01")
        self.c.create_booking({"agreement_id": a["id"], "type": "ROOM",
                               "start": "2026-09-01", "end": "2026-10-01"})
        aw = self.c.confirm_agreement({
            "village_id": self.w["v1"], "person_id": "fam", "person_name": "一家人",
            "kind": "WHOLE", "courtyard_id": self.w["c1"], "monthly_rent": 6000,
            "start": "2026-09-01", "end": "2026-12-01", "confirmed_on": "2026-08-01"})
        with self.assertRaisesRegex(DomainError, "整院超卖"):
            self.c.create_booking({"agreement_id": aw["id"], "type": "WHOLE",
                                   "start": "2026-09-15", "end": "2026-09-20"})
        # 不重叠的整院可以
        self.c.create_booking({"agreement_id": aw["id"], "type": "WHOLE",
                               "start": "2026-11-01", "end": "2026-12-01"})

    def test_member_capacity_and_room_conflict(self):
        am = self.c.confirm_agreement({
            "village_id": self.w["v1"], "person_id": "m1", "person_name": "游民甲",
            "kind": "MEMBER", "courtyard_id": self.w["c1"],
            "start": "2026-08-01", "end": "2026-10-01", "confirmed_on": "2026-08-01"})
        self.c.create_booking({"agreement_id": am["id"], "type": "MEMBER",
                               "room_id": self.w["r1"],
                               "start": "2026-08-01", "end": "2026-09-15"})
        am2 = self.c.confirm_agreement({
            "village_id": self.w["v1"], "person_id": "m2", "person_name": "游民乙",
            "kind": "MEMBER", "courtyard_id": self.w["c1"],
            "start": "2026-08-01", "end": "2026-10-01", "confirmed_on": "2026-08-01"})
        # 同院不同房、会员名额 2 以内：可以
        self.c.create_booking({"agreement_id": am2["id"], "type": "MEMBER",
                               "room_id": self.w["r2"],
                               "start": "2026-08-01", "end": "2026-08-15"})
        am3 = self.c.confirm_agreement({
            "village_id": self.w["v1"], "person_id": "m3", "person_name": "游民丙",
            "kind": "MEMBER", "courtyard_id": self.w["c1"],
            "start": "2026-08-01", "end": "2026-10-01", "confirmed_on": "2026-08-01"})
        with self.assertRaisesRegex(DomainError, "会员名额已满"):
            self.c.create_booking({"agreement_id": am3["id"], "type": "MEMBER",
                                   "room_id": self.w["r1"],
                                   "start": "2026-08-10", "end": "2026-08-12"})
        # 8/20 游民乙已走，名额未满；但游民甲仍住东房，同房间物理冲突
        with self.assertRaisesRegex(DomainError, "防止超卖"):
            self.c.create_booking({"agreement_id": am3["id"], "type": "MEMBER",
                                   "room_id": self.w["r1"],
                                   "start": "2026-08-20", "end": "2026-08-25"})

    def test_concurrent_race_for_same_night_has_single_winner(self):
        self._agreement("host", self.w["r1"], start="2026-08-01", end="2027-01-01")
        # 20 个线程抢同一间空房的同一晚，恰好一个成功
        results = []

        def race(i):
            ai = self._agreement(f"racer{i}", self.w["r1"],
                                 start="2027-01-10", end="2027-02-01",
                                 confirmed_on="2026-08-01")
            try:
                self.c.create_booking({"agreement_id": ai["id"], "type": "ROOM",
                                       "start": "2027-01-10", "end": "2027-01-12"})
                results.append("win")
            except DomainError:
                results.append("lose")

        threads = [threading.Thread(target=race, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("win"), 1)


class AbsenceSwapClosureTest(unittest.TestCase):
    def setUp(self):
        self.c = Community()
        self.w = build_two_villages(self.c)
        rule_v1(self.c)
        self.a1 = self.c.confirm_agreement({
            "village_id": self.w["v1"], "person_id": "p1", "person_name": "老张",
            "kind": "ROOM", "room_id": self.w["r1"], "monthly_rent": 3000,
            "start": "2026-08-01", "end": "2026-11-30", "confirmed_on": "2026-08-01"})
        self.a2 = self.c.confirm_agreement({
            "village_id": self.w["v2"], "person_id": "p2", "person_name": "小李",
            "kind": "ROOM", "room_id": self.w["r3"], "monthly_rent": 2000,
            "start": "2026-08-01", "end": "2026-11-30", "confirmed_on": "2026-08-01"})
        self.b1 = self.c.create_booking({"agreement_id": self.a1["id"], "type": "ROOM",
                                         "start": "2026-08-01", "end": "2026-11-30"})["id"]
        self.b2 = self.c.create_booking({"agreement_id": self.a2["id"], "type": "ROOM",
                                         "start": "2026-08-01", "end": "2026-11-30"})["id"]

    def test_absence_keeps_rights_but_marks_away(self):
        self.c.register_absence({"booking_id": self.b1,
                                 "start": "2026-09-01", "end": "2026-09-06",
                                 "reason": "回城看病"})
        usage = self.c.usage_on(self.w["v1"], "2026-09-03")
        occ = usage["courtyards"][0]["rooms"][0]["occupants"]
        self.assertEqual(len(occ), 1)
        self.assertFalse(occ[0]["present"])
        self.assertEqual(occ[0]["state"], "temporarily_away")
        # 权益仍在：别人不能订这间房
        ax = self.c.confirm_agreement({
            "village_id": self.w["v1"], "person_id": "px", "person_name": "过客",
            "kind": "ROOM", "room_id": self.w["r1"], "monthly_rent": 100,
            "start": "2026-09-01", "end": "2026-10-01", "confirmed_on": "2026-08-01"})
        with self.assertRaises(DomainError):
            self.c.create_booking({"agreement_id": ax["id"], "type": "ROOM",
                                   "start": "2026-09-02", "end": "2026-09-04"})

    def test_absence_blocks_closure_but_swapout_allows_it(self):
        self.c.register_absence({"booking_id": self.b1,
                                 "start": "2026-09-01", "end": "2026-09-06",
                                 "reason": "回城"})
        with self.assertRaisesRegex(DomainError, "持有使用权"):
            self.c.register_closure({"courtyard_id": self.w["c1"],
                                     "start": "2026-09-02", "end": "2026-09-05",
                                     "reason": "修瓦"})
        # 换住到 v2 空闲的北房（r4），换出时段原房释放 → 可封闭
        s = self.c.request_swap({"agreement_id": self.a1["id"], "booking_id": self.b1,
                                 "to_room_id": self.w["r4"],
                                 "start": "2026-09-10", "end": "2026-09-20"})["id"]
        self.c.accept_swap({"swap_id": s})
        self.c.register_closure({"courtyard_id": self.w["c1"],
                                 "start": "2026-09-10", "end": "2026-09-20",
                                 "reason": "修瓦"})
        # 封闭段延伸到回村日仍冲突
        with self.assertRaises(DomainError):
            self.c.register_closure({"courtyard_id": self.w["c1"],
                                     "start": "2026-09-19", "end": "2026-09-22",
                                     "reason": "补漆"})

    def test_paired_cross_village_swap_is_atomic(self):
        s1 = self.c.request_swap({"agreement_id": self.a1["id"], "booking_id": self.b1,
                                  "to_room_id": self.w["r3"],
                                  "start": "2026-09-10", "end": "2026-09-20"})["id"]
        s2 = self.c.request_swap({"agreement_id": self.a2["id"], "booking_id": self.b2,
                                  "to_room_id": self.w["r1"],
                                  "start": "2026-09-10", "end": "2026-09-20"})["id"]
        result = self.c.accept_swap({"swap_id": s1, "pair_swap_id": s2})
        self.assertEqual(result["swap"]["status"], "AGREED")
        self.assertEqual(result["paired_swap"]["status"], "AGREED")

        # 9/15：p1 在 v2，p2 在 v1，双方都在村
        u1 = self.c.usage_on(self.w["v1"], "2026-09-15")
        u2 = self.c.usage_on(self.w["v2"], "2026-09-15")
        self.assertEqual(u1["courtyards"][0]["rooms"][0]["occupants"][0]["person_id"], "p2")
        self.assertEqual(u2["courtyards"][0]["rooms"][0]["occupants"][0]["person_id"], "p1")

        # 9/5：未换住，各人在本村
        u1b = self.c.usage_on(self.w["v1"], "2026-09-05")
        self.assertEqual(u1b["courtyards"][0]["rooms"][0]["occupants"][0]["person_id"], "p1")

        # 重复接受幂等
        again = self.c.accept_swap({"swap_id": s1})
        self.assertEqual(again["swap"]["status"], "AGREED")
        self.assertEqual(len(self.c.bookings), 4)  # 不多建换入预约

    def test_swap_pair_validation(self):
        # 非跨村方向、时段不一致、房间不互为目标都要拒绝
        s1 = self.c.request_swap({"agreement_id": self.a1["id"], "booking_id": self.b1,
                                  "to_room_id": self.w["r3"],
                                  "start": "2026-09-10", "end": "2026-09-20"})["id"]
        bad = self.c.request_swap({"agreement_id": self.a2["id"], "booking_id": self.b2,
                                   "to_room_id": self.w["r2"],  # 不是 p1 的房间
                                   "start": "2026-09-10", "end": "2026-09-20"})["id"]
        with self.assertRaisesRegex(DomainError, "对方换入房间"):
            self.c.accept_swap({"swap_id": s1, "pair_swap_id": bad})

    def test_cancel_swap_restores_rights(self):
        s1 = self.c.request_swap({"agreement_id": self.a1["id"], "booking_id": self.b1,
                                  "to_room_id": self.w["r4"],
                                  "start": "2026-09-10", "end": "2026-09-20"})["id"]
        self.c.accept_swap({"swap_id": s1})
        self.c.cancel_swap({"swap_id": s1})
        u = self.c.usage_on(self.w["v1"], "2026-09-15")
        occ = u["courtyards"][0]["rooms"][0]["occupants"]
        self.assertEqual(occ[0]["person_id"], "p1")

    def test_direct_cross_village_booking_rejected(self):
        with self.assertRaisesRegex(DomainError, "换住"):
            self.c.create_booking({"agreement_id": self.a1["id"], "type": "ROOM",
                                   "room_id": self.w["r3"],
                                   "start": "2026-09-01", "end": "2026-09-05"})


class RuleAndSettlementTest(unittest.TestCase):
    def setUp(self):
        self.c = Community()
        self.w = build_two_villages(self.c)
        rule_v1(self.c)
        self.a = self.c.confirm_agreement({
            "village_id": self.w["v1"], "person_id": "p1", "person_name": "老张",
            "kind": "ROOM", "room_id": self.w["r1"], "monthly_rent": 3000,
            "start": "2026-08-15", "end": "2026-10-15", "confirmed_on": "2026-08-01"})
        self.c.create_booking({"agreement_id": self.a["id"], "type": "ROOM",
                               "start": "2026-08-15", "end": "2026-10-15"})

    def _rule_v2(self):
        self.c.register_rule({
            "version": 2, "effective_on": "2026-09-01",
            "deposit_per_room": 9999, "member_fee_per_month": 8000,
            "service_hour_per_month": 99,
            "village_share": 0.9, "operator_share": 0.1, "resident_share": 0.0})

    def _confirm_all(self, period):
        for role in ("village", "operator", "resident"):
            self.c.confirm_settlement_party(
                {"agreement_id": self.a["id"], "period": period, "role": role})

    def test_rule_is_immutable_and_old_contract_keeps_old_version(self):
        with self.assertRaisesRegex(DomainError, "不可覆盖"):
            rule_v1(self.c)
        self._rule_v2()  # 9 月新政策
        # 老协约冻结的仍是 v1
        self.assertEqual(self.a["rule_version"], 1)
        self.assertEqual(self.a["deposit"], 1000)

        self._confirm_all("2026-09")
        record = self.c.close_period({"village_id": self.w["v1"], "period": "2026-09"})
        st = record["settlements"][0]
        self.assertEqual(st["rule_version"], 1)
        self.assertEqual({s["party"]: s["rule_version"] for s in st["splits"]},
                         {"village": 1, "operator": 1, "resident": 1})
        # v1 分成 0.5/0.3/0.2；9 月整月 3000
        by_party = {s["party"]: s["amount"] for s in st["splits"]}
        self.assertEqual(by_party, {"village": 1500.0, "operator": 900.0, "resident": 600.0})

    def test_proration_by_days_in_partial_month(self):
        self._confirm_all("2026-08")
        record = self.c.close_period({"village_id": self.w["v1"], "period": "2026-08"})
        st = record["settlements"][0]
        self.assertEqual(st["days"], 17)  # 8/15 ~ 8/31
        self.assertAlmostEqual(st["base"], round(3000 * 17 / 31, 2))
        self.assertAlmostEqual(st["service_hours"], round(4 * 17 / 31, 2))

    def test_close_requires_three_confirmations_and_is_idempotent_blocked(self):
        self.c.confirm_settlement_party(
            {"agreement_id": self.a["id"], "period": "2026-09", "role": "village"})
        with self.assertRaisesRegex(DomainError, "operator/resident"):
            self.c.close_period({"village_id": self.w["v1"], "period": "2026-09"})
        self._confirm_all("2026-09")
        self.c.close_period({"village_id": self.w["v1"], "period": "2026-09"})
        with self.assertRaisesRegex(DomainError, "已封账"):
            self.c.close_period({"village_id": self.w["v1"], "period": "2026-09"})

    def test_reopen_leaves_audit_event_and_recompute_keeps_version(self):
        self._confirm_all("2026-09")
        self.c.close_period({"village_id": self.w["v1"], "period": "2026-09"})
        self.c.reopen_period({"village_id": self.w["v1"], "period": "2026-09",
                              "reason": "补登记一笔工时"})
        self._rule_v2()
        self.c.close_period({"village_id": self.w["v1"], "period": "2026-09"})
        record = self.c.get_period(self.w["v1"], "2026-09")
        self.assertEqual(record["settlements"][0]["rule_version"], 1)
        kinds = [e["kind"] for e in self.c.events]
        self.assertIn("period.reopened", kinds)

    def test_member_settlement_uses_member_fee(self):
        am = self.c.confirm_agreement({
            "village_id": self.w["v1"], "person_id": "m1", "person_name": "数字游民",
            "kind": "MEMBER", "courtyard_id": self.w["c1"],
            "start": "2026-09-01", "end": "2026-11-01", "confirmed_on": "2026-08-01"})
        for role in ("village", "operator", "resident"):
            self.c.confirm_settlement_party(
                {"agreement_id": am["id"], "period": "2026-09", "role": role})
            self.c.confirm_settlement_party(
                {"agreement_id": self.a["id"], "period": "2026-09", "role": role})
        record = self.c.close_period({"village_id": self.w["v1"], "period": "2026-09"})
        member_st = next(s for s in record["settlements"] if s["kind"] == "MEMBER")
        self.assertEqual(member_st["base_kind"], "member_fee")
        self.assertEqual(member_st["base"], 1500.0)


class ResourceProposalDocumentTest(unittest.TestCase):
    def setUp(self):
        self.c = Community()
        self.w = build_two_villages(self.c)
        rule_v1(self.c)

    def test_resource_capacity_and_reason_is_recorded(self):
        a = self.c.confirm_agreement({
            "village_id": self.w["v1"], "person_id": "p1", "person_name": "老张",
            "kind": "ROOM", "room_id": self.w["r1"], "monthly_rent": 3000,
            "start": "2026-09-01", "end": "2026-10-01", "confirmed_on": "2026-08-01"})
        self.c.book_resource({"resource_id": self.w["book1"], "agreement_id": a["id"],
                              "start": "2026-09-10", "end": "2026-09-11",
                              "reason": "代收快递临时存放"})
        with self.assertRaisesRegex(DomainError, "容量已满"):
            self.c.book_resource({"resource_id": self.w["book1"], "agreement_id": a["id"],
                                  "start": "2026-09-10", "end": "2026-09-12",
                                  "reason": "另一伙人也要用"})
        u = self.c.usage_on(self.w["v1"], "2026-09-10")
        rb = u["resources"][0]["occupants"][0]
        self.assertEqual(rb["reason"], "代收快递临时存放")
        self.assertEqual(u["resources"][0]["remaining"], 0)

    def test_proposal_and_activity_capacity(self):
        p = self.c.create_proposal({"village_id": self.w["v1"], "creator_id": "p1",
                                    "title": "共建鸡舍", "body": "预算 2000"})
        v = self.c.vote_proposal({"proposal_id": p["id"], "person_id": "p1",
                                  "stance": "support"})
        self.assertEqual(v["support"], 1)
        act = self.c.create_activity({"village_id": self.w["v1"],
                                      "resource_id": self.w["book1"],
                                      "start": "2026-09-12", "end": "2026-09-13",
                                      "title": "读书会", "capacity": 2})
        self.c.signup_activity({"activity_id": act["id"], "person_id": "p1"})
        self.c.signup_activity({"activity_id": act["id"], "person_id": "p2"})
        with self.assertRaisesRegex(DomainError, "名额已满"):
            self.c.signup_activity({"activity_id": act["id"], "person_id": "p3"})
        self.c.cancel_signup({"activity_id": act["id"], "person_id": "p1"})
        self.c.signup_activity({"activity_id": act["id"], "person_id": "p3"})

    def test_document_grant_time_boxed_and_scoped(self):
        doc = self.c.register_document({"person_id": "p1", "kind": "health",
                                        "label": "年度体检表"})["id"]
        with self.assertRaisesRegex(DomainError, "未获授权"):
            self.c.view_document({"document_id": doc, "grantee_id": "medic-1"},
                                 now_iso="2026-09-19T00:00:00Z")
        self.c.grant_document({"document_id": doc, "grantee_id": "medic-1",
                               "grantee_role": "medical", "ttl_seconds": 3600,
                               "now": "2026-09-19T00:00:00Z"})
        view = self.c.view_document({"document_id": doc, "grantee_id": "medic-1"},
                                    now_iso="2026-09-19T00:30:00Z")
        self.assertEqual(view["kind"], "health")
        self.assertEqual(view["expires_at"], "2026-09-19T01:00:00Z")
        # 到期自动收回
        with self.assertRaisesRegex(DomainError, "到期收回"):
            self.c.view_document({"document_id": doc, "grantee_id": "medic-1"},
                                 now_iso="2026-09-19T01:00:01Z")
        # 其他服务人员不搭车
        with self.assertRaisesRegex(DomainError, "未获授权"):
            self.c.view_document({"document_id": doc, "grantee_id": "medic-2"},
                                 now_iso="2026-09-19T00:30:00Z")
        # 内容从不落库
        self.assertNotIn("content", self.c.documents[doc])

    def test_grant_revoke_and_renew(self):
        doc = self.c.register_document({"person_id": "p1", "kind": "identity",
                                        "label": "居住证"})["id"]
        g = self.c.grant_document({"document_id": doc, "grantee_id": "op-1",
                                   "grantee_role": "operator", "ttl_seconds": 60})
        self.c.revoke_grant({"grant_id": g["id"]})
        with self.assertRaises(DomainError):
            self.c.view_document({"document_id": doc, "grantee_id": "op-1"})
        grants = self.c.list_grants()
        self.assertEqual(grants[0]["status"], "REVOKED")


class SnapshotTest(unittest.TestCase):
    def test_roundtrip_preserves_all_state(self):
        c = Community()
        w = build_two_villages(c)
        rule_v1(c)
        a = c.confirm_agreement({
            "village_id": w["v1"], "person_id": "p1", "person_name": "老张",
            "kind": "ROOM", "room_id": w["r1"], "monthly_rent": 3000,
            "start": "2026-08-01", "end": "2026-11-30", "confirmed_on": "2026-08-01"})
        c.create_booking({"agreement_id": a["id"], "type": "ROOM",
                          "start": "2026-08-01", "end": "2026-11-30"})
        restored = Community.from_snapshot(c.to_snapshot())
        self.assertEqual(restored.agreements[a["id"]]["rule_version"], 1)
        self.assertEqual(len(restored.rooms), 4)
        # ID 序列在恢复后继续递增，不与已有 ID 冲突
        before = restored._next_id
        new_id = restored.register_village({"name": "新寨"})["id"]
        self.assertEqual(new_id, before)


if __name__ == "__main__":
    unittest.main()
