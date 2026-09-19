"""旅居社区长期协约的领域核心。

CommunityStore 持有全部领域状态并提供业务操作：
- 协约生命周期：创建（待确认）→ 村民/运营方/旅居者三方确认 → 生效；
- 超卖防护：生效协约与维修封闭共同占用房间权益，新协约/换住不得与之重叠；
- 跨村换住：两边协约在同一锁内原子创建，任一侧不可用则整组不落地；
- 版本化结算：协约生效时锁定规则版本，结算单逐笔记录所采用的版本，
  老合同不被新政策追溯改价；同一 (协约, 月份) 结算幂等，停机重跑不产生重复单；
- 隐私授权：身份/健康材料仅凭限时授权查看，到期自动收回，查看留痕；
- 持久化：每次变更后写 JSON 快照（临时文件 + 原子替换），停机后可整体恢复。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict
from datetime import datetime

from .model import (
    MODE_ROOM,
    MODE_SWAP,
    MODE_WHOLE,
    PARTIES,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    Activity,
    Agreement,
    Booking,
    Closure,
    Courtyard,
    Grant,
    Leave,
    Material,
    Proposal,
    Resource,
    Room,
    RuleVersion,
    Settlement,
    ShareLine,
    Village,
    WorkLog,
    each_day,
    month_bounds,
    nights,
    overlap,
)

MIN_NIGHTS = 14   # 最短两周
MAX_NIGHTS = 366  # 最长一年


class DomainError(Exception):
    """业务规则拒绝。"""


class NotFoundError(DomainError):
    """对象不存在。"""


class ConflictError(DomainError):
    """容量或权益冲突（超卖防护）。"""


class PermissionDenied(DomainError):
    """未获授权的访问。"""


class CommunityStore:
    """全部领域状态与业务操作；可选 JSON 文件持久化，停机后可恢复。"""

    def __init__(self, path=None, clock=None):
        self.path = path
        self.clock = clock or (lambda: datetime.now().isoformat(timespec="seconds"))
        self.lock = threading.RLock()
        self._seq = 0
        self.villages = {}
        self.courtyards = {}
        self.rules = []
        self.agreements = {}
        self.closures = {}
        self.resources = {}
        self.bookings = {}
        self.activities = {}
        self.proposals = {}
        self.materials = {}
        self.grants = {}
        self.worklogs = {}
        self.settlements = {}  # key: "agreement_id:period"
        self.events = []

    # ---------- 基础设施 ----------

    def today(self):
        return self.clock()[:10]

    def _next_id(self, prefix):
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def _emit(self, event_type, **data):
        self.events.append(
            {"seq": len(self.events) + 1, "ts": self.clock(), "type": event_type, "data": data}
        )

    def _save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ---------- 持久化 ----------

    def to_dict(self):
        return {
            "seq": self._seq,
            "villages": [asdict(v) for v in self.villages.values()],
            "courtyards": [asdict(c) for c in self.courtyards.values()],
            "rules": [asdict(r) for r in self.rules],
            "agreements": [asdict(a) for a in self.agreements.values()],
            "closures": [asdict(c) for c in self.closures.values()],
            "resources": [asdict(r) for r in self.resources.values()],
            "bookings": [asdict(b) for b in self.bookings.values()],
            "activities": [asdict(a) for a in self.activities.values()],
            "proposals": [asdict(p) for p in self.proposals.values()],
            "materials": [asdict(m) for m in self.materials.values()],
            "grants": [asdict(g) for g in self.grants.values()],
            "worklogs": [asdict(w) for w in self.worklogs.values()],
            "settlements": [asdict(s) for s in self.settlements.values()],
            "events": self.events,
        }

    @classmethod
    def load(cls, path, clock=None):
        store = cls(path=path, clock=clock)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                store._restore(json.load(fh))
        return store

    def _restore(self, data):
        self._seq = data["seq"]
        self.villages = {v["village_id"]: Village(**v) for v in data["villages"]}
        self.courtyards = {
            c["courtyard_id"]: Courtyard.from_dict(c) for c in data["courtyards"]
        }
        self.rules = [RuleVersion(**r) for r in data["rules"]]
        self.agreements = {
            a["agreement_id"]: Agreement.from_dict(a) for a in data["agreements"]
        }
        self.closures = {c["closure_id"]: Closure(**c) for c in data["closures"]}
        self.resources = {r["resource_id"]: Resource(**r) for r in data["resources"]}
        self.bookings = {b["booking_id"]: Booking(**b) for b in data["bookings"]}
        self.activities = {a["activity_id"]: Activity(**a) for a in data["activities"]}
        self.proposals = {p["proposal_id"]: Proposal(**p) for p in data["proposals"]}
        self.materials = {m["material_id"]: Material(**m) for m in data["materials"]}
        self.grants = {g["grant_id"]: Grant(**g) for g in data["grants"]}
        self.worklogs = {w["work_id"]: WorkLog(**w) for w in data["worklogs"]}
        self.settlements = {
            f"{s['agreement_id']}:{s['period']}": Settlement.from_dict(s)
            for s in data["settlements"]
        }
        self.events = data["events"]

    # ---------- 查找 ----------

    def _get(self, table, key, label):
        try:
            return table[key]
        except KeyError:
            raise NotFoundError(f"{label}不存在：{key}") from None

    def _village(self, vid):
        return self._get(self.villages, vid, "村落")

    def _courtyard(self, cid):
        return self._get(self.courtyards, cid, "院落")

    def _agreement(self, aid):
        return self._get(self.agreements, aid, "协约")

    def _resource(self, rid):
        return self._get(self.resources, rid, "公共资源")

    def _activity(self, aid):
        return self._get(self.activities, aid, "活动")

    def _proposal(self, pid):
        return self._get(self.proposals, pid, "提案")

    def _material(self, mid):
        return self._get(self.materials, mid, "材料")

    def _worklog(self, wid):
        return self._get(self.worklogs, wid, "工时记录")

    # ---------- 村落与院落 ----------

    def add_village(self, name, region=""):
        with self.lock:
            village = Village(self._next_id("vil"), name, region)
            self.villages[village.village_id] = village
            self._emit("village_added", village_id=village.village_id, name=name)
            self._save()
            return village

    def add_courtyard(self, village_id, name, whole_price_fen, rooms):
        """rooms: [{"name": ..., "nightly_price_fen": ...}, ...]"""
        with self.lock:
            self._village(village_id)
            if not rooms:
                raise DomainError("院落至少需要一个房间")
            courtyard = Courtyard(
                courtyard_id=self._next_id("cty"),
                village_id=village_id,
                name=name,
                whole_price_fen=whole_price_fen,
                rooms=[
                    Room(self._next_id("rm"), r["name"], r["nightly_price_fen"])
                    for r in rooms
                ],
            )
            self.courtyards[courtyard.courtyard_id] = courtyard
            self._emit(
                "courtyard_added",
                courtyard_id=courtyard.courtyard_id,
                village_id=village_id,
                rooms=len(courtyard.rooms),
            )
            self._save()
            return courtyard

    # ---------- 规则版本 ----------

    def add_rule(
        self,
        effective_from,
        deposit_bp,
        villager_share_bp,
        operator_share_bp,
        service_hour_rate_fen,
        note="",
    ):
        with self.lock:
            for bp in (deposit_bp, villager_share_bp, operator_share_bp):
                if not 0 <= bp <= 10000:
                    raise DomainError("比例基点须在 0~10000 之间")
            if villager_share_bp + operator_share_bp > 10000:
                raise DomainError("村民与运营方分成合计不能超过 100%")
            version = self.rules[-1].version + 1 if self.rules else 1
            rule = RuleVersion(
                version,
                effective_from,
                deposit_bp,
                villager_share_bp,
                operator_share_bp,
                service_hour_rate_fen,
                note,
            )
            self.rules.append(rule)
            self._emit("rule_added", version=version, effective_from=effective_from)
            self._save()
            return rule

    def current_rule(self, on_date):
        """on_date 当日生效的规则版本。"""
        candidates = [r for r in self.rules if r.effective_from <= on_date]
        if not candidates:
            raise DomainError(f"{on_date} 无生效的结算规则")
        return max(candidates, key=lambda r: (r.effective_from, r.version))

    # ---------- 协约 ----------

    def _validate_span(self, start, end):
        span = nights(start, end)
        if span < MIN_NIGHTS or span > MAX_NIGHTS:
            raise DomainError(f"协约租期须为两周至一年，当前 {span} 天")

    def _validate_rooms(self, courtyard, room_ids):
        known = {r.room_id for r in courtyard.rooms}
        unknown = sorted(set(room_ids) - known)
        if unknown:
            raise DomainError(f"院落 {courtyard.courtyard_id} 中不存在房间：{unknown}")

    def _agreement_rooms(self, agreement):
        if agreement.mode == MODE_WHOLE:
            return [r.room_id for r in self.courtyards[agreement.courtyard_id].rooms]
        return list(agreement.room_ids)

    def _assert_rooms_free(self, courtyard_id, room_ids, start, end, ignore=None):
        """目标房间在 [start, end) 内不得与生效协约或维修封闭重叠。"""
        courtyard = self._courtyard(courtyard_id)
        targets = set(room_ids) or {r.room_id for r in courtyard.rooms}
        for other in self.agreements.values():
            if other.courtyard_id != courtyard_id or other.status != STATUS_ACTIVE:
                continue
            if other.agreement_id == ignore:
                continue
            if not overlap(other.start, other.end, start, end):
                continue
            clash = sorted(targets & set(self._agreement_rooms(other)))
            if clash:
                raise ConflictError(
                    f"房间 {clash} 在 {start}~{end} 已被协约 {other.agreement_id} 占用"
                )
        for closure in self.closures.values():
            if closure.courtyard_id != courtyard_id:
                continue
            if not overlap(closure.start, closure.end, start, end):
                continue
            closed = set(closure.room_ids) or {r.room_id for r in courtyard.rooms}
            clash = sorted(targets & closed)
            if clash:
                raise ConflictError(
                    f"房间 {clash} 在 {closure.start}~{closure.end} 维修封闭：{closure.reason}"
                )

    def _new_agreement(
        self, village_id, courtyard_id, mode, room_ids, resident_id, start, end, swap_group_id=""
    ):
        courtyard = self._courtyard(courtyard_id)
        if courtyard.village_id != village_id:
            raise DomainError(f"院落 {courtyard_id} 不属于村落 {village_id}")
        self._validate_span(start, end)
        if mode == MODE_WHOLE:
            rooms = [r.room_id for r in courtyard.rooms]
            price = courtyard.whole_price_fen
        else:
            if not room_ids:
                raise DomainError("按房间或换住模式须指定房间")
            self._validate_rooms(courtyard, room_ids)
            rooms = list(room_ids)
            price = sum(r.nightly_price_fen for r in courtyard.rooms if r.room_id in rooms)
        self._assert_rooms_free(courtyard_id, rooms, start, end)
        agreement = Agreement(
            agreement_id=self._next_id("agr"),
            village_id=village_id,
            courtyard_id=courtyard_id,
            mode=mode,
            room_ids=rooms,
            resident_id=resident_id,
            start=start,
            end=end,
            nightly_price_fen=price,
            swap_group_id=swap_group_id,
        )
        self.agreements[agreement.agreement_id] = agreement
        return agreement

    def create_agreement(self, village_id, courtyard_id, mode, room_ids, resident_id, start, end):
        """创建待确认协约（按房间 / 整院）。"""
        if mode not in (MODE_ROOM, MODE_WHOLE):
            raise DomainError("普通协约仅支持 room / whole，换住请用 create_swap")
        with self.lock:
            agreement = self._new_agreement(
                village_id, courtyard_id, mode, room_ids, resident_id, start, end
            )
            self._emit(
                "agreement_created",
                agreement_id=agreement.agreement_id,
                mode=mode,
                start=start,
                end=end,
            )
            self._save()
            return agreement

    def create_swap(self, side_a, side_b, start, end):
        """跨村换住：两侧协约原子创建，任一侧不可用则整组不落地。

        side: {"village_id", "courtyard_id", "room_ids", "resident_id"}
        """
        with self.lock:
            for side in (side_a, side_b):
                courtyard = self._courtyard(side["courtyard_id"])
                if courtyard.village_id != side["village_id"]:
                    raise DomainError("换住侧信息与院落所属村落不符")
                self._validate_span(start, end)
                self._validate_rooms(courtyard, side["room_ids"])
                self._assert_rooms_free(
                    side["courtyard_id"], side["room_ids"], start, end
                )
            if side_a["courtyard_id"] == side_b["courtyard_id"] and set(
                side_a["room_ids"]
            ) & set(side_b["room_ids"]):
                raise ConflictError("同一院落内换住的房间不能重叠")
            group = self._next_id("swap")
            made = [
                self._new_agreement(
                    side["village_id"],
                    side["courtyard_id"],
                    MODE_SWAP,
                    side["room_ids"],
                    side["resident_id"],
                    start,
                    end,
                    swap_group_id=group,
                )
                for side in (side_a, side_b)
            ]
            self._emit(
                "swap_created",
                swap_group_id=group,
                agreements=[a.agreement_id for a in made],
                start=start,
                end=end,
            )
            self._save()
            return made

    def confirm_agreement(self, agreement_id, role, today=None):
        """三方（villager/operator/resident）逐方确认；齐后生效并锁定规则版本。"""
        today = today or self.today()
        with self.lock:
            agreement = self._agreement(agreement_id)
            if agreement.status != STATUS_PENDING:
                raise DomainError(f"协约 {agreement_id} 当前状态不可确认：{agreement.status}")
            if role not in PARTIES:
                raise DomainError(f"未知确认方：{role}")
            if role in agreement.confirmations:
                raise DomainError(f"{role} 已确认过协约 {agreement_id}")
            agreement.confirmations.append(role)
            self._emit("agreement_confirmed", agreement_id=agreement_id, role=role)
            if all(p in agreement.confirmations for p in PARTIES):
                # 生效前最终校验，防止确认期间出现新的占用
                self._assert_rooms_free(
                    agreement.courtyard_id,
                    self._agreement_rooms(agreement),
                    agreement.start,
                    agreement.end,
                    ignore=agreement.agreement_id,
                )
                rule = self.current_rule(today)
                agreement.rule_version = rule.version
                agreement.deposit_fen = (
                    agreement.nightly_price_fen
                    * nights(agreement.start, agreement.end)
                    * rule.deposit_bp
                    // 10000
                )
                agreement.status = STATUS_ACTIVE
                self._emit(
                    "agreement_activated",
                    agreement_id=agreement_id,
                    rule_version=rule.version,
                    deposit_fen=agreement.deposit_fen,
                )
            self._save()
            return agreement

    def add_leave(self, agreement_id, start, end, reason=""):
        """临时离村：记录离村区间，权益保留，容量不释放。"""
        with self.lock:
            agreement = self._agreement(agreement_id)
            if agreement.status != STATUS_ACTIVE:
                raise DomainError("仅生效中的协约可登记临时离村")
            if start < agreement.start or end > agreement.end or not start < end:
                raise DomainError("离村区间须落在协约租期内")
            agreement.leaves.append(Leave(start, end, reason))
            self._emit("leave_added", agreement_id=agreement_id, start=start, end=end)
            self._save()
            return agreement

    def checkout(self, agreement_id, on_date=None):
        """提前离村结账：租期截短到 on_date，释放后续权益。"""
        on_date = on_date or self.today()
        with self.lock:
            agreement = self._agreement(agreement_id)
            if agreement.status != STATUS_ACTIVE:
                raise DomainError("仅生效中的协约可结账")
            if not agreement.start < on_date <= agreement.end:
                raise DomainError("结账日期须位于租期内")
            agreement.end = on_date
            agreement.status = STATUS_COMPLETED
            self._emit("agreement_completed", agreement_id=agreement_id, end=on_date)
            self._save()
            return agreement

    def cancel_agreement(self, agreement_id):
        with self.lock:
            agreement = self._agreement(agreement_id)
            if agreement.status in (STATUS_COMPLETED, STATUS_CANCELLED):
                raise DomainError("协约已结束，不能取消")
            agreement.status = STATUS_CANCELLED
            self._emit("agreement_cancelled", agreement_id=agreement_id)
            self._save()
            return agreement

    # ---------- 维修封闭 ----------

    def add_closure(self, courtyard_id, room_ids, start, end, reason):
        """登记维修封闭。允许与在住协约重叠（封闭夜不计费），但会阻止新协约。"""
        with self.lock:
            courtyard = self._courtyard(courtyard_id)
            if not start < end:
                raise DomainError("封闭区间无效")
            if not reason:
                raise DomainError("维修封闭须登记理由")
            self._validate_rooms(courtyard, room_ids)
            closure = Closure(
                self._next_id("cls"), courtyard_id, list(room_ids), start, end, reason
            )
            self.closures[closure.closure_id] = closure
            self._emit(
                "closure_added",
                closure_id=closure.closure_id,
                courtyard_id=courtyard_id,
                start=start,
                end=end,
                reason=reason,
            )
            self._save()
            return closure

    # ---------- 使用权与公共资源查询 ----------

    def rights_on(self, courtyard_id, day):
        """某院落某天每个房间的使用权：空闲 / 占用（含持有人与方式）/ 维修封闭。"""
        courtyard = self._courtyard(courtyard_id)
        rows = []
        for room in courtyard.rooms:
            entry = {
                "room_id": room.room_id,
                "room": room.name,
                "status": "available",
            }
            for closure in self.closures.values():
                if closure.courtyard_id != courtyard_id:
                    continue
                closed = closure.room_ids or [r.room_id for r in courtyard.rooms]
                if room.room_id in closed and closure.start <= day < closure.end:
                    entry.update(
                        status="closed",
                        reason=closure.reason,
                        closure_id=closure.closure_id,
                    )
                    break
            for agreement in self.agreements.values():
                if agreement.courtyard_id != courtyard_id:
                    continue
                if agreement.status != STATUS_ACTIVE:
                    continue
                if not agreement.start <= day < agreement.end:
                    continue
                if room.room_id not in self._agreement_rooms(agreement):
                    continue
                on_leave = any(
                    lv.start <= day < lv.end for lv in agreement.leaves
                )
                if entry["status"] == "closed":
                    # 维修期间：协约保留但被封闭覆盖
                    entry["displaced_agreement"] = agreement.agreement_id
                    entry["holder"] = agreement.resident_id
                else:
                    entry = {
                        "room_id": room.room_id,
                        "room": room.name,
                        "status": "occupied",
                        "agreement_id": agreement.agreement_id,
                        "holder": agreement.resident_id,
                        "mode": agreement.mode,
                        "on_leave": on_leave,
                    }
            rows.append(entry)
        return rows

    def book_resource(self, resource_id, day, slots, reason, booked_by):
        """占用公共资源某日容量，必须登记占用理由。"""
        with self.lock:
            resource = self._resource(resource_id)
            if slots <= 0:
                raise DomainError("占用份数须为正数")
            if not reason:
                raise DomainError("占用公共资源须登记理由")
            used = sum(
                b.slots
                for b in self.bookings.values()
                if b.resource_id == resource_id and b.day == day
            )
            if used + slots > resource.capacity:
                raise ConflictError(
                    f"{resource.name} 在 {day} 剩余容量 {resource.capacity - used}，"
                    f"不足以占用 {slots} 份"
                )
            booking = Booking(
                self._next_id("bk"), resource_id, day, slots, reason, booked_by
            )
            self.bookings[booking.booking_id] = booking
            self._emit(
                "resource_booked",
                booking_id=booking.booking_id,
                resource_id=resource_id,
                day=day,
                slots=slots,
                reason=reason,
            )
            self._save()
            return booking

    def occupancy_on(self, day, village_id=None):
        """某天各村公共资源的占用明细（含占用理由）与剩余容量。"""
        rows = []
        for resource in self.resources.values():
            if village_id and resource.village_id != village_id:
                continue
            entries = [
                {
                    "booking_id": b.booking_id,
                    "by": b.booked_by,
                    "slots": b.slots,
                    "reason": b.reason,
                }
                for b in self.bookings.values()
                if b.resource_id == resource.resource_id and b.day == day
            ]
            used = sum(e["slots"] for e in entries)
            rows.append(
                {
                    "resource_id": resource.resource_id,
                    "village_id": resource.village_id,
                    "kind": resource.kind,
                    "name": resource.name,
                    "capacity": resource.capacity,
                    "used": used,
                    "remaining": resource.capacity - used,
                    "occupancy": entries,
                }
            )
        return rows

    # ---------- 公共资源 / 活动 / 提案 ----------

    def add_resource(self, village_id, kind, name, capacity):
        with self.lock:
            self._village(village_id)
            if capacity <= 0:
                raise DomainError("公共资源容量须为正数")
            resource = Resource(self._next_id("res"), village_id, kind, name, capacity)
            self.resources[resource.resource_id] = resource
            self._emit("resource_added", resource_id=resource.resource_id, kind=kind)
            self._save()
            return resource

    def create_activity(self, village_id, title, day, capacity):
        with self.lock:
            self._village(village_id)
            if capacity <= 0:
                raise DomainError("活动容量须为正数")
            activity = Activity(
                self._next_id("act"), village_id, title, day, capacity
            )
            self.activities[activity.activity_id] = activity
            self._emit("activity_created", activity_id=activity.activity_id)
            self._save()
            return activity

    def signup_activity(self, activity_id, resident_id):
        with self.lock:
            activity = self._activity(activity_id)
            if resident_id in activity.signups:
                raise DomainError("该旅居者已报名")
            if len(activity.signups) >= activity.capacity:
                raise ConflictError(f"活动 {activity.title} 已满员")
            activity.signups.append(resident_id)
            self._emit(
                "activity_signup", activity_id=activity_id, resident_id=resident_id
            )
            self._save()
            return activity

    def create_proposal(self, village_id, author, text):
        with self.lock:
            self._village(village_id)
            proposal = Proposal(self._next_id("prp"), village_id, author, text)
            self.proposals[proposal.proposal_id] = proposal
            self._emit("proposal_created", proposal_id=proposal.proposal_id)
            self._save()
            return proposal

    def vote_proposal(self, proposal_id, voter, approve):
        with self.lock:
            proposal = self._proposal(proposal_id)
            if proposal.status != "open":
                raise DomainError("提案已结案，不能投票")
            if voter in proposal.votes_for or voter in proposal.votes_against:
                raise DomainError("该成员已投过票")
            (proposal.votes_for if approve else proposal.votes_against).append(voter)
            self._emit("proposal_voted", proposal_id=proposal_id, approve=approve)
            self._save()
            return proposal

    def decide_proposal(self, proposal_id, approve):
        with self.lock:
            proposal = self._proposal(proposal_id)
            if proposal.status != "open":
                raise DomainError("提案已结案")
            proposal.status = "approved" if approve else "rejected"
            self._emit(
                "proposal_decided", proposal_id=proposal_id, status=proposal.status
            )
            self._save()
            return proposal

    # ---------- 身份/健康材料与限时授权 ----------

    def add_material(self, resident_id, kind, content, now=None):
        """登记敏感材料。材料不进入普通社区档案，仅可通过授权查看。"""
        now = now or self.clock()
        with self.lock:
            material = Material(
                self._next_id("mat"), resident_id, kind, content, now
            )
            self.materials[material.material_id] = material
            self._emit(
                "material_added", material_id=material.material_id, kind=kind
            )
            self._save()
            return material

    def grant_access(self, material_id, staff_id, expires_at):
        """授予服务人员限时查看权。"""
        with self.lock:
            self._material(material_id)
            grant = Grant(
                self._next_id("grt"), material_id, staff_id, expires_at
            )
            self.grants[grant.grant_id] = grant
            self._emit(
                "grant_created",
                grant_id=grant.grant_id,
                material_id=material_id,
                staff_id=staff_id,
                expires_at=expires_at,
            )
            self._save()
            return grant

    def _active_grant(self, material_id, staff_id, now):
        for grant in self.grants.values():
            if (
                grant.material_id == material_id
                and grant.staff_id == staff_id
                and not grant.revoked
                and grant.expires_at > now
            ):
                return grant
        return None

    def view_material(self, material_id, staff_id, now=None):
        """凭有效授权查看材料；查看行为留痕。"""
        now = now or self.clock()
        with self.lock:
            material = self._material(material_id)
            if self._active_grant(material_id, staff_id, now) is None:
                raise PermissionDenied(
                    f"服务人员 {staff_id} 对材料 {material_id} 无有效授权"
                )
            self._emit(
                "material_viewed", material_id=material_id, staff_id=staff_id
            )
            self._save()
            return material

    def sweep_grants(self, now=None):
        """收回所有到期授权（自动收回的兜底清扫），返回收回数量。"""
        now = now or self.clock()
        with self.lock:
            swept = 0
            for grant in self.grants.values():
                if not grant.revoked and grant.expires_at <= now:
                    grant.revoked = True
                    swept += 1
                    self._emit("grant_revoked", grant_id=grant.grant_id)
            if swept:
                self._save()
            return swept

    # ---------- 服务工时 ----------

    def add_worklog(self, agreement_id, worker, minutes, day, description):
        with self.lock:
            self._agreement(agreement_id)
            if minutes <= 0:
                raise DomainError("工时须为正数分钟")
            worklog = WorkLog(
                self._next_id("wrk"), agreement_id, worker, minutes, day, description
            )
            self.worklogs[worklog.work_id] = worklog
            self._emit("worklog_added", work_id=worklog.work_id)
            self._save()
            return worklog

    def confirm_worklog(self, work_id, role):
        """三方确认工时；齐后计入结算。"""
        with self.lock:
            worklog = self._worklog(work_id)
            if worklog.confirmed:
                raise DomainError("工时记录已确认完成")
            if role not in PARTIES:
                raise DomainError(f"未知确认方：{role}")
            if role in worklog.confirmations:
                raise DomainError(f"{role} 已确认过该工时")
            worklog.confirmations.append(role)
            if all(p in worklog.confirmations for p in PARTIES):
                worklog.confirmed = True
            self._emit("worklog_confirmed", work_id=work_id, role=role)
            self._save()
            return worklog

    # ---------- 结算 ----------

    def _closed_days(self, agreement):
        """协约占用房间被维修封闭覆盖的日期集合。"""
        rooms = set(self._agreement_rooms(agreement))
        courtyard = self.courtyards[agreement.courtyard_id]
        closed = set()
        for closure in self.closures.values():
            if closure.courtyard_id != agreement.courtyard_id:
                continue
            closed_rooms = set(closure.room_ids) or {
                r.room_id for r in courtyard.rooms
            }
            if not rooms & closed_rooms:
                continue
            start = max(closure.start, agreement.start)
            end = min(closure.end, agreement.end)
            if start < end:
                closed.update(each_day(start, end))
        return closed

    def settle(self, agreement_id, period):
        """按协约锁定的规则版本结算某月。同一 (协约, 月份) 幂等。

        维修封闭夜不计费；临时离村权益保留、照常计费。
        """
        key = f"{agreement_id}:{period}"
        with self.lock:
            if key in self.settlements:
                return self.settlements[key]
            agreement = self._agreement(agreement_id)
            if agreement.status not in (STATUS_ACTIVE, STATUS_COMPLETED):
                raise DomainError(f"协约 {agreement_id} 未生效，不能结算")
            if not agreement.rule_version:
                raise DomainError("协约未锁定规则版本，不能结算")
            rule = next(
                r for r in self.rules if r.version == agreement.rule_version
            )
            month_start, month_end = month_bounds(period)
            start = max(agreement.start, month_start)
            end = min(agreement.end, month_end)
            closed = self._closed_days(agreement)
            billable = closed_nights = 0
            if start < end:
                for day in each_day(start, end):
                    if day in closed:
                        closed_nights += 1
                    else:
                        billable += 1
            gross = billable * agreement.nightly_price_fen
            minutes = sum(
                w.minutes
                for w in self.worklogs.values()
                if w.agreement_id == agreement_id
                and w.confirmed
                and month_start <= w.day < month_end
            )
            service_fen = rule.service_hour_rate_fen * minutes // 60
            villager = gross * rule.villager_share_bp // 10000
            operator = gross * rule.operator_share_bp // 10000
            shares = [
                ShareLine("villager", villager, rule.version),
                ShareLine("operator", operator, rule.version),
                ShareLine("community", gross - villager - operator, rule.version),
            ]
            if service_fen:
                shares.append(ShareLine("service_pool", service_fen, rule.version))
            settlement = Settlement(
                settlement_id=self._next_id("stl"),
                agreement_id=agreement_id,
                period=period,
                billable_nights=billable,
                closed_nights=closed_nights,
                gross_fen=gross,
                service_fen=service_fen,
                rule_version=rule.version,
                shares=shares,
            )
            self.settlements[key] = settlement
            self._emit(
                "settlement_created",
                settlement_id=settlement.settlement_id,
                agreement_id=agreement_id,
                period=period,
                rule_version=rule.version,
                gross_fen=gross,
            )
            self._save()
            return settlement

    def settlements_of(self, agreement_id):
        return [
            s
            for s in self.settlements.values()
            if s.agreement_id == agreement_id
        ]
