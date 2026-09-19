"""领域核心测试：超卖防护、换住原子性、版本化结算、隐私授权、停机恢复核对。"""

import os
import tempfile
import unittest

from community import (
    CommunityStore,
    ConflictError,
    DomainError,
    PermissionDenied,
)

CLOCK_8 = lambda: "2026-08-01T09:00:00"  # noqa: E731


class SeedMixin:
    """两个村（云南/贵州）、两处院落、规则 v1。"""

    def seed(self, store):
        self.v1 = store.add_village("沙溪", "云南")
        self.v2 = store.add_village("肇兴", "贵州")
        self.c1 = store.add_courtyard(
            self.v1.village_id,
            "东篱院",
            20000,
            [
                {"name": "东屋", "nightly_price_fen": 12000},
                {"name": "西屋", "nightly_price_fen": 10000},
            ],
        )
        self.c2 = store.add_courtyard(
            self.v2.village_id,
            "临水院",
            18000,
            [
                {"name": "山景房", "nightly_price_fen": 11000},
                {"name": "临水房", "nightly_price_fen": 9000},
            ],
        )
        self.east, self.west = (r.room_id for r in self.c1.rooms)
        self.hill, self.shore = (r.room_id for r in self.c2.rooms)
        self.rule1 = store.add_rule("2026-01-01", 2000, 4000, 3000, 3000, "首版规则")

    def activate(self, store, agreement_id, today):
        for role in ("villager", "operator", "resident"):
            store.confirm_agreement(agreement_id, role, today)


class AgreementInvariantTest(SeedMixin, unittest.TestCase):
    def setUp(self):
        self.store = CommunityStore(clock=CLOCK_8)
        self.seed(self.store)

    def test_span_must_be_two_weeks_to_one_year(self):
        with self.assertRaises(DomainError):
            self.store.create_agreement(
                self.v1.village_id, self.c1.courtyard_id, "room",
                [self.east], "甲", "2026-09-01", "2026-09-10",
            )
        with self.assertRaises(DomainError):
            self.store.create_agreement(
                self.v1.village_id, self.c1.courtyard_id, "room",
                [self.east], "甲", "2026-09-01", "2027-09-10",
            )

    def test_overlapping_active_agreement_is_rejected(self):
        first = self.store.create_agreement(
            self.v1.village_id, self.c1.courtyard_id, "room",
            [self.east], "甲", "2026-08-14", "2026-10-14",
        )
        self.activate(self.store, first.agreement_id, "2026-08-10")
        with self.assertRaises(ConflictError):
            self.store.create_agreement(
                self.v1.village_id, self.c1.courtyard_id, "room",
                [self.east], "乙", "2026-10-01", "2026-11-01",
            )
        # 首尾相接不算冲突
        self.store.create_agreement(
            self.v1.village_id, self.c1.courtyard_id, "room",
            [self.east], "乙", "2026-10-14", "2026-11-14",
        )

    def test_whole_courtyard_conflicts_with_room(self):
        room_ag = self.store.create_agreement(
            self.v1.village_id, self.c1.courtyard_id, "room",
            [self.west], "甲", "2026-09-01", "2026-10-01",
        )
        self.activate(self.store, room_ag.agreement_id, "2026-08-20")
        with self.assertRaises(ConflictError):
            self.store.create_agreement(
                self.v1.village_id, self.c1.courtyard_id, "whole",
                [], "乙", "2026-09-15", "2026-10-15",
            )

    def test_pending_does_not_block_but_activation_checks_again(self):
        p1 = self.store.create_agreement(
            self.v1.village_id, self.c1.courtyard_id, "room",
            [self.east], "甲", "2026-09-01", "2026-09-20",
        )
        p2 = self.store.create_agreement(
            self.v1.village_id, self.c1.courtyard_id, "room",
            [self.east], "乙", "2026-09-10", "2026-09-30",
        )
        self.activate(self.store, p1.agreement_id, "2026-08-15")
        self.store.confirm_agreement(p2.agreement_id, "villager", "2026-08-16")
        self.store.confirm_agreement(p2.agreement_id, "operator", "2026-08-16")
        with self.assertRaises(ConflictError):
            self.store.confirm_agreement(p2.agreement_id, "resident", "2026-08-16")
        self.assertEqual(self.store._agreement(p2.agreement_id).status, "pending")

    def test_closure_blocks_new_agreement(self):
        self.store.add_closure(
            self.c1.courtyard_id, [self.east], "2026-09-01", "2026-09-11", "屋顶翻修"
        )
        with self.assertRaises(ConflictError):
            self.store.create_agreement(
                self.v1.village_id, self.c1.courtyard_id, "room",
                [self.east], "甲", "2026-09-05", "2026-09-25",
            )

    def test_swap_is_atomic_across_villages(self):
        # 山景房先被生效协约占用
        blocker = self.store.create_agreement(
            self.v2.village_id, self.c2.courtyard_id, "room",
            [self.hill], "丙", "2026-09-01", "2026-10-01",
        )
        self.activate(self.store, blocker.agreement_id, "2026-08-20")
        before = len(self.store.agreements)
        with self.assertRaises(ConflictError):
            self.store.create_swap(
                {"village_id": self.v1.village_id, "courtyard_id": self.c1.courtyard_id,
                 "room_ids": [self.west], "resident_id": "会员B"},
                {"village_id": self.v2.village_id, "courtyard_id": self.c2.courtyard_id,
                 "room_ids": [self.hill], "resident_id": "会员A"},
                "2026-09-01", "2026-10-01",
            )
        # 任一侧失败则整组不落地
        self.assertEqual(len(self.store.agreements), before)
        self.assertFalse(
            any(e["type"] == "swap_created" for e in self.store.events)
        )

    def test_leave_retains_right(self):
        ag = self.store.create_agreement(
            self.v1.village_id, self.c1.courtyard_id, "room",
            [self.east], "甲", "2026-08-14", "2026-10-14",
        )
        self.activate(self.store, ag.agreement_id, "2026-08-10")
        self.store.add_leave(ag.agreement_id, "2026-09-15", "2026-09-22", "回城办事")
        rights = self.store.rights_on(self.c1.courtyard_id, "2026-09-18")
        east = next(r for r in rights if r["room_id"] == self.east)
        self.assertEqual(east["status"], "occupied")
        self.assertTrue(east["on_leave"])
        # 临时离村不释放容量：他人仍不能签同一房间
        with self.assertRaises(ConflictError):
            self.store.create_agreement(
                self.v1.village_id, self.c1.courtyard_id, "room",
                [self.east], "乙", "2026-09-15", "2026-10-01",
            )


class CommunityAndPrivacyTest(SeedMixin, unittest.TestCase):
    def setUp(self):
        self.store = CommunityStore(clock=CLOCK_8)
        self.seed(self.store)

    def test_resource_capacity_and_reason_required(self):
        garden = self.store.add_resource(self.v1.village_id, "garden", "共享菜地", 2)
        self.store.book_resource(garden.resource_id, "2026-09-05", 1, "甲的番茄畦", "甲")
        self.store.book_resource(garden.resource_id, "2026-09-05", 1, "乙的香草畦", "乙")
        with self.assertRaises(ConflictError):
            self.store.book_resource(garden.resource_id, "2026-09-05", 1, "丙的豆架", "丙")
        with self.assertRaises(DomainError):
            self.store.book_resource(garden.resource_id, "2026-09-06", 1, "", "丙")

    def test_activity_capacity(self):
        act = self.store.create_activity(self.v1.village_id, "火把节分享会", "2026-09-20", 2)
        self.store.signup_activity(act.activity_id, "甲")
        self.store.signup_activity(act.activity_id, "乙")
        with self.assertRaises(ConflictError):
            self.store.signup_activity(act.activity_id, "丙")
        with self.assertRaises(DomainError):
            self.store.signup_activity(act.activity_id, "甲")

    def test_proposal_voting(self):
        prop = self.store.create_proposal(self.v1.village_id, "甲", "书屋延长开放到22点")
        self.store.vote_proposal(prop.proposal_id, "乙", True)
        with self.assertRaises(DomainError):
            self.store.vote_proposal(prop.proposal_id, "乙", False)
        self.store.decide_proposal(prop.proposal_id, True)
        with self.assertRaises(DomainError):
            self.store.vote_proposal(prop.proposal_id, "丙", True)

    def test_material_requires_time_limited_grant(self):
        mat = self.store.add_material("甲", "健康证明", "……", "2026-08-15T10:00:00")
        with self.assertRaises(PermissionDenied):
            self.store.view_material(mat.material_id, "村医", "2026-08-20T09:00:00")
        self.store.grant_access(mat.material_id, "村医", "2026-09-01T00:00:00")
        viewed = self.store.view_material(mat.material_id, "村医", "2026-08-30T12:00:00")
        self.assertEqual(viewed.content, "……")
        # 到期自动失效
        with self.assertRaises(PermissionDenied):
            self.store.view_material(mat.material_id, "村医", "2026-09-02T00:00:00")
        swept = self.store.sweep_grants("2026-09-02T00:00:00")
        self.assertEqual(swept, 1)
        # 收回后即使查看时刻早于原到期时间也不再放行
        with self.assertRaises(PermissionDenied):
            self.store.view_material(mat.material_id, "村医", "2026-08-30T12:00:00")
        self.assertIn("material_viewed", {e["type"] for e in self.store.events})

    def test_unconfirmed_worklog_not_settled(self):
        ag = self.store.create_agreement(
            self.v1.village_id, self.c1.courtyard_id, "room",
            [self.east], "甲", "2026-08-14", "2026-09-14",
        )
        self.activate(self.store, ag.agreement_id, "2026-08-10")
        work = self.store.add_worklog(ag.agreement_id, "运营小李", 120, "2026-08-20", "创业辅导")
        self.store.confirm_worklog(work.work_id, "villager")
        st = self.store.settle(ag.agreement_id, "2026-08")
        self.assertEqual(st.service_fen, 0)
        # 三方确认后重开一月才计入（本月已结算，幂等返回原单）
        self.store.confirm_worklog(work.work_id, "operator")
        self.store.confirm_worklog(work.work_id, "resident")
        self.assertTrue(self.store._worklog(work.work_id).confirmed)


class AcceptanceTest(SeedMixin, unittest.TestCase):
    """验收场景：两村同时换住 → 跨月结算 → 停机 → 恢复后核对。"""

    def test_swap_settle_crash_recover(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "data.json")
        store = CommunityStore(path=path, clock=CLOCK_8)
        self.seed(store)

        # 旅居者甲：东屋长住 08-14 ~ 10-14，确认时锁定规则 v1
        ag_a = store.create_agreement(
            self.v1.village_id, self.c1.courtyard_id, "room",
            [self.east], "甲", "2026-08-14", "2026-10-14",
        )
        self.activate(store, ag_a.agreement_id, "2026-08-10")
        ag_a = store._agreement(ag_a.agreement_id)
        self.assertEqual(ag_a.rule_version, 1)
        self.assertEqual(ag_a.deposit_fen, 61 * 12000 * 2000 // 10000)

        # 两村同时办理换住：会员A 住贵州山景房，会员B 住云南西屋
        swap_a, swap_b = store.create_swap(
            {"village_id": self.v2.village_id, "courtyard_id": self.c2.courtyard_id,
             "room_ids": [self.hill], "resident_id": "会员A"},
            {"village_id": self.v1.village_id, "courtyard_id": self.c1.courtyard_id,
             "room_ids": [self.west], "resident_id": "会员B"},
            "2026-09-01", "2026-10-01",
        )
        self.assertEqual(swap_a.swap_group_id, swap_b.swap_group_id)
        self.activate(store, swap_a.agreement_id, "2026-08-20")
        self.activate(store, swap_b.agreement_id, "2026-08-20")

        # 维修封闭、临时离村、公共资源占用、活动、工时
        store.add_closure(self.c1.courtyard_id, [self.east],
                          "2026-09-01", "2026-09-11", "屋顶翻修")
        store.add_leave(ag_a.agreement_id, "2026-09-15", "2026-09-22", "回城办事")
        garden = store.add_resource(self.v1.village_id, "garden", "共享菜地", 4)
        bookroom = store.add_resource(self.v1.village_id, "bookroom", "乡村书屋", 2)
        store.book_resource(garden.resource_id, "2026-09-05", 2, "甲的番茄畦", "甲")
        store.book_resource(bookroom.resource_id, "2026-09-05", 1, "读书会场地", "会员B")
        work = store.add_worklog(ag_a.agreement_id, "运营小李", 120, "2026-08-20", "创业辅导")
        for role in ("villager", "operator", "resident"):
            store.confirm_worklog(work.work_id, role)

        # 敏感材料：授权村医短期查看
        mat = store.add_material("甲", "健康证明", "……", "2026-08-15T10:00:00")
        store.grant_access(mat.material_id, "村医", "2026-09-01T00:00:00")
        store.view_material(mat.material_id, "村医", "2026-08-30T12:00:00")

        # 跨月结算：先结 8 月
        st_aug = store.settle(ag_a.agreement_id, "2026-08")
        self.assertEqual(st_aug.billable_nights, 18)  # 08-14 ~ 08-31
        self.assertEqual(st_aug.gross_fen, 18 * 12000)
        self.assertEqual(st_aug.service_fen, 3000 * 120 // 60)
        self.assertEqual(st_aug.rule_version, 1)

        # ---- 突然停机：丢弃内存态，仅从快照恢复 ----
        del store
        store = CommunityStore.load(path, clock=lambda: "2026-09-02T09:00:00")

        # 恢复后发布新政策（9 月起生效），老合同不被追溯
        store.add_rule("2026-09-01", 3000, 5000, 2000, 3600, "秋季新策")
        ag_b = store.create_agreement(
            self.v2.village_id, self.c2.courtyard_id, "room",
            [self.shore], "乙", "2026-09-10", "2026-10-10",
        )
        self.activate(store, ag_b.agreement_id, "2026-09-05")
        self.assertEqual(store._agreement(ag_b.agreement_id).rule_version, 2)

        # 恢复后继续跨月结算
        st_sep = store.settle(ag_a.agreement_id, "2026-09")
        self.assertEqual(st_sep.closed_nights, 10)   # 09-01 ~ 09-10 封闭
        self.assertEqual(st_sep.billable_nights, 20)
        self.assertEqual(st_sep.rule_version, 1)     # 老合同仍按 v1
        st_b = store.settle(ag_b.agreement_id, "2026-09")
        self.assertEqual(st_b.rule_version, 2)
        st_swap = store.settle(swap_a.agreement_id, "2026-09")
        self.assertEqual(st_swap.billable_nights, 30)

        # 幂等：停机重跑不产生重复结算单
        again = store.settle(ag_a.agreement_id, "2026-08")
        self.assertEqual(again.settlement_id, st_aug.settlement_id)
        self.assertEqual(len(store.settlements), 4)

        # 核对 1：恢复后某天（封闭+换住期间）的使用权
        rights = store.rights_on(self.c1.courtyard_id, "2026-09-05")
        east = next(r for r in rights if r["room_id"] == self.east)
        west = next(r for r in rights if r["room_id"] == self.west)
        self.assertEqual(east["status"], "closed")
        self.assertEqual(east["reason"], "屋顶翻修")
        self.assertEqual(east["holder"], "甲")  # 协约保留，被封闭覆盖
        self.assertEqual(west["status"], "occupied")
        self.assertEqual(west["holder"], "会员B")
        self.assertEqual(west["mode"], "swap")

        # 核对 2：某天公共资源占用理由
        occupancy = store.occupancy_on("2026-09-05", self.v1.village_id)
        by_kind = {row["kind"]: row for row in occupancy}
        self.assertEqual(by_kind["garden"]["used"], 2)
        self.assertEqual(by_kind["garden"]["occupancy"][0]["reason"], "甲的番茄畦")
        self.assertEqual(by_kind["bookroom"]["occupancy"][0]["reason"], "读书会场地")

        # 核对 3：每笔分成采用的规则版本
        for st in store.settlements_of(ag_a.agreement_id):
            for line in st.shares:
                self.assertEqual(line.rule_version, 1)
        for line in store.settlements_of(ag_b.agreement_id)[0].shares:
            self.assertEqual(line.rule_version, 2)
        # 分成完整：三方合计等于总流水
        for st in store.settlements.values():
            gross_lines = [l for l in st.shares if l.party != "service_pool"]
            self.assertEqual(sum(l.amount_fen for l in gross_lines), st.gross_fen)

        # 核对 4：授权到期后恢复环境仍不可查看，且留痕完整
        with self.assertRaises(PermissionDenied):
            store.view_material(mat.material_id, "村医", "2026-09-02T00:00:00")
        self.assertEqual(store.sweep_grants("2026-09-02T00:00:00"), 1)
        types = [e["type"] for e in store.events]
        self.assertIn("material_viewed", types)
        self.assertEqual(types.count("settlement_created"), 4)
        self.assertEqual([e["seq"] for e in store.events], list(range(1, len(types) + 1)))


if __name__ == "__main__":
    unittest.main()
