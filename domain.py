"""旅居社区长期协约的领域模型。

设计要点
========
* 所有日期在模型边界内统一使用 ISO 字符串（``YYYY-MM-DD``），日期段一律为
  半开区间 ``[start, end)``；ISO 串可直接按字典序比较。
* 使用权（booking）与租约（agreement）分离：协约确定租金关系与规则版本，
  booking 确定某几天对具体房间/资源的占用。临时离村、跨村换住只改变
  booking 的“当日是否占用”，从不删除原协约。
* 规则（押金、分成比例、会员费、服务工时）按版本注册，协约在三方确认时
  冻结当时生效版本；封账分成永远读取冻结版本，新政策不追溯老合同。
* 身份/健康材料只登记元数据，不保存内容；授权均带到期时间，到期自动收回。
"""

from __future__ import annotations

import calendar
import threading
from datetime import date, datetime, timedelta, timezone

VALID_ROLES = ("village", "operator", "resident")
BOOKING_TYPES = ("ROOM", "WHOLE", "MEMBER", "SWAP")


class DomainError(Exception):
    """业务规则冲突（容量不足、状态不允许等），映射为 HTTP 409。"""


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"日期格式应为 YYYY-MM-DD：{value!r}") from exc


def parse_dt(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"时间格式应为 YYYY-MM-DDTHH:MM:SSZ：{value!r}") from exc


def require(payload: dict, *keys: str):
    missing = [key for key in keys if payload.get(key) in (None, "")]
    if missing:
        raise DomainError(f"缺少必填字段：{', '.join(missing)}")


def overlap(s1: str, e1: str, s2: str, e2: str) -> bool:
    return s1 < e2 and s2 < e1


def daterange(start: str, end: str):
    """遍历半开区间 [start, end) 内的每一天。"""
    cur, stop = parse_date(start), parse_date(end)
    if cur >= stop:
        raise DomainError(f"结束日期必须晚于开始日期：{start} ~ {end}")
    while cur < stop:
        yield cur
        cur += timedelta(days=1)


def month_segments(start: str, end: str):
    """把 [start,end) 切成按月相交的 (month, seg_start, seg_end)。"""
    cur = parse_date(start).replace(day=1)
    stop = parse_date(end)
    while True:
        last_day = calendar.monthrange(cur.year, cur.month)[1]
        month_end = min(date(cur.year, cur.month, last_day) + timedelta(days=1), stop)
        month_start = max(cur, parse_date(start))
        if month_start < month_end:
            yield f"{cur.year:04d}-{cur.month:02d}", month_start.isoformat(), month_end.isoformat()
        if month_end >= stop:
            return
        cur = (date(cur.year, cur.month, 28) + timedelta(days=10)).replace(day=1)


def _days(start: str, end: str) -> int:
    return (parse_date(end) - parse_date(start)).days


class Community:
    """线程安全的旅居社区聚合根，状态可整体序列化为 JSON 快照。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._next_id = 1
        self.villages = {}
        self.courtyards = {}          # 院落（含维修封闭段）
        self.rooms = {}
        self.resources = {}           # 菜地、书屋、活动空间等公共资源
        self.rules = {}               # version -> 规则
        self.agreements = {}
        self.bookings = {}
        self.absences = {}            # 临时离村
        self.swaps = {}               # 跨村换住单
        self.resource_bookings = {}
        self.proposals = {}
        self.activities = {}
        self.documents = {}           # 仅元数据，无内容
        self.grants = {}
        self.confirmations = {}       # agreement|period -> 三方确认
        self.periods = {}             # village|period -> 封账记录
        self.events = []

    # ----- 基础设置 -------------------------------------------------------

    def _new_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _event(self, kind: str, detail: dict):
        self.events.append({"seq": len(self.events) + 1, "at": _now(), "kind": kind, "detail": detail})

    def register_village(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "name")
            vid = self._new_id()
            self.villages[vid] = {"id": vid, "name": payload["name"]}
            self._event("village.registered", {"village_id": vid, "name": payload["name"]})
            return dict(self.villages[vid])

    def register_courtyard(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "village_id", "name")
            village_id = int(payload["village_id"])
            if village_id not in self.villages:
                raise DomainError("村落不存在")
            cid = self._new_id()
            self.courtyards[cid] = {
                "id": cid,
                "village_id": village_id,
                "name": payload["name"],
                "member_capacity": int(payload.get("member_capacity", 0)),
                "closures": [],
            }
            self._event("courtyard.registered", {"courtyard_id": cid, "name": payload["name"]})
            return dict(self.courtyards[cid])

    def register_room(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "courtyard_id", "name")
            courtyard_id = int(payload["courtyard_id"])
            if courtyard_id not in self.courtyards:
                raise DomainError("院落不存在")
            rid = self._new_id()
            self.rooms[rid] = {"id": rid, "courtyard_id": courtyard_id, "name": payload["name"]}
            self._event("room.registered", {"room_id": rid, "name": payload["name"]})
            return dict(self.rooms[rid])

    def register_resource(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "village_id", "name", "capacity")
            village_id = int(payload["village_id"])
            if village_id not in self.villages:
                raise DomainError("村落不存在")
            capacity = int(payload["capacity"])
            if capacity <= 0:
                raise DomainError("公共资源容量必须为正整数")
            rid = self._new_id()
            self.resources[rid] = {
                "id": rid,
                "village_id": village_id,
                "name": payload["name"],
                "capacity": capacity,
            }
            self._event("resource.registered", {"resource_id": rid, "name": payload["name"]})
            return dict(self.resources[rid])

    # ----- 规则版本 -------------------------------------------------------

    def register_rule(self, payload: dict) -> dict:
        """登记一版规则。规则只对生效日之后新确认的协约起作用。"""
        with self._lock:
            require(payload, "version", "effective_on", "deposit_per_room",
                    "member_fee_per_month", "service_hour_per_month")
            version = int(payload["version"])
            if version in self.rules:
                raise DomainError(f"规则版本 {version} 已存在，规则不可覆盖")
            effective_on = payload["effective_on"]
            parse_date(effective_on)
            shares = {}
            for role in VALID_ROLES:
                share = float(payload.get(f"{role}_share"))
                if not 0 <= share <= 1:
                    raise DomainError(f"{role}_share 必须在 0~1 之间")
                shares[role] = share
            if abs(sum(shares.values()) - 1.0) > 1e-9:
                raise DomainError("三方分成比例之和必须为 1")
            rule = {
                "version": version,
                "effective_on": effective_on,
                "deposit_per_room": float(payload["deposit_per_room"]),
                "member_fee_per_month": float(payload["member_fee_per_month"]),
                "service_hour_per_month": float(payload["service_hour_per_month"]),
                "shares": shares,
                "note": payload.get("note", ""),
            }
            self.rules[version] = rule
            self._event("rule.registered", {"version": version, "effective_on": effective_on})
            return dict(rule)

    def effective_rule(self, on: str) -> dict:
        candidates = [r for r in self.rules.values() if r["effective_on"] <= on]
        if not candidates:
            raise DomainError(f"{on} 尚无生效规则")
        rule = max(candidates, key=lambda r: r["version"])
        return dict(rule)

    # ----- 协约与三方确认 -------------------------------------------------

    def confirm_agreement(self, payload: dict) -> dict:
        """村民、运营方、旅居者确认协约，冻结当日生效规则版本与押金。"""
        with self._lock:
            require(payload, "village_id", "person_id", "person_name", "kind", "start", "end")
            vid = int(payload["village_id"])
            if vid not in self.villages:
                raise DomainError("村落不存在")
            kind = payload["kind"]
            if kind not in ("ROOM", "WHOLE", "MEMBER"):
                raise DomainError("协约类型只能是 ROOM / WHOLE / MEMBER")
            start, end = payload["start"], payload["end"]
            list(daterange(start, end))  # 校验

            room_id = payload.get("room_id")
            courtyard_id = payload.get("courtyard_id")
            if kind == "ROOM":
                if not room_id:
                    raise DomainError("按房间协约需指定 room_id")
                courtyard_id = self._room_courtyard(int(room_id))
            elif kind == "WHOLE":
                if not courtyard_id:
                    raise DomainError("整院协约需指定 courtyard_id")
                courtyard_id = int(courtyard_id)
            else:
                courtyard_id = int(courtyard_id) if courtyard_id else None

            if kind in ("ROOM", "WHOLE") and courtyard_id not in self.courtyards:
                raise DomainError("院落不存在")
            if kind == "MEMBER" and courtyard_id is not None and courtyard_id not in self.courtyards:
                raise DomainError("院落不存在")

            rule_on = payload.get("confirmed_on") or _today()
            parse_date(rule_on)
            rule = self.effective_rule(rule_on)
            units = len(self._rooms_of_courtyard(courtyard_id)) if kind == "WHOLE" else 1
            monthly_rent = float(payload.get("monthly_rent", 0))
            if kind != "MEMBER" and monthly_rent <= 0:
                raise DomainError("长期协约需约定 monthly_rent")

            aid = self._new_id()
            agreement = {
                "id": aid,
                "village_id": vid,
                "person_id": payload["person_id"],
                "person_name": payload["person_name"],
                "kind": kind,
                "start": start,
                "end": end,
                "room_id": int(room_id) if room_id else None,
                "courtyard_id": courtyard_id,
                "monthly_rent": monthly_rent,
                "deposit": round(rule["deposit_per_room"] * units, 2),
                "deposit_units": units,
                "rule_version": rule["version"],
                "confirmed_on": rule_on,
                "status": "ACTIVE",
                "cancelled_on": None,
                "created_at": _now(),
            }
            self.agreements[aid] = agreement
            self._event("agreement.confirmed", {
                "agreement_id": aid, "person_id": agreement["person_id"],
                "rule_version": rule["version"], "deposit": agreement["deposit"],
            })
            return dict(agreement)

    def cancel_agreement(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "agreement_id", "cancelled_on")
            agreement_id = int(payload["agreement_id"])
            cancelled_on = payload["cancelled_on"]
            agreement = self._get_agreement(agreement_id)
            if not (agreement["start"] <= cancelled_on <= agreement["end"]):
                raise DomainError("退租日期必须在协约期内")
            agreement["status"] = "CANCELLED"
            agreement["cancelled_on"] = cancelled_on
            for booking in self.bookings.values():
                if booking["agreement_id"] == agreement_id and booking["status"] == "CONFIRMED":
                    if booking["start"] < cancelled_on < booking["end"]:
                        raise DomainError("存在跨越退租日的入住预约，请先调整或换住后再退租")
                    if booking["start"] >= cancelled_on:
                        booking["status"] = "CANCELLED"
            self._event("agreement.cancelled", {"agreement_id": agreement_id, "cancelled_on": cancelled_on})
            return dict(agreement)

    def _get_agreement(self, agreement_id: int) -> dict:
        agreement = self.agreements.get(int(agreement_id))
        if not agreement:
            raise DomainError("协约不存在")
        return agreement

    def _effective_end(self, agreement: dict) -> str:
        return agreement["cancelled_on"] or agreement["end"]

    # ----- 容量判断 -------------------------------------------------------

    def _room_courtyard(self, room_id: int) -> int:
        room = self.rooms.get(room_id)
        if not room:
            raise DomainError("房间不存在")
        return room["courtyard_id"]

    def _rooms_of_courtyard(self, courtyard_id: int) -> list[int]:
        return [rid for rid, room in self.rooms.items() if room["courtyard_id"] == courtyard_id]

    def _right_active(self, booking: dict, day: str) -> bool:
        """booking 在当天是否仍持有房间/院落权益（离村仍保留，换出才释放）。"""
        if booking["status"] != "CONFIRMED" or not (booking["start"] <= day < booking["end"]):
            return False
        if booking["type"] == "SWAP":
            swap = self.swaps.get(booking.get("swap_id"))
            return bool(swap and swap["status"] == "AGREED")
        for swap in self.swaps.values():
            if (swap["status"] == "AGREED" and swap["out_booking_id"] == booking["id"]
                    and swap["start"] <= day < swap["end"]):
                return False
        return True

    def _absent(self, booking: dict, day: str) -> bool:
        return any(
            absence["booking_id"] == booking["id"] and absence["start"] <= day < absence["end"]
            for absence in self.absences.values()
        )

    def _present(self, booking: dict, day: str) -> bool:
        """当天人是否在村实际使用房间（离村期间为 False）。"""
        return self._right_active(booking, day) and not self._absent(booking, day)

    def _room_claims(self, room_id: int, day: str, exclude_ids: set | None = None) -> int:
        """当天该房间的权益占用数：按房间预约 + 整院覆盖；离村仍计数，换出不计数。"""
        excluded = set(exclude_ids or ())
        court_id = self.rooms[room_id]["courtyard_id"]
        used = 0
        for booking in self.bookings.values():
            if booking["id"] in excluded or not self._right_active(booking, day):
                continue
            if booking["type"] == "WHOLE":
                if booking["courtyard_id"] == court_id:
                    used += 1
            elif booking.get("room_id") == room_id:
                used += 1
        return used

    def _member_used(self, courtyard_id: int, day: str, exclude_ids: set | None = None) -> int:
        excluded = set(exclude_ids or ())
        return sum(
            1 for booking in self.bookings.values()
            if booking["type"] == "MEMBER" and booking["id"] not in excluded
            and self._right_active(booking, day)
            and self._room_courtyard(booking["room_id"]) == courtyard_id
        )

    def _assert_courtyard_open(self, courtyard_id: int, start: str, end: str):
        for closure in self.courtyards[courtyard_id]["closures"]:
            if overlap(start, end, closure["start"], closure["end"]):
                raise DomainError(
                    f"院落于 {closure['start']}~{closure['end']} 维修封闭：{closure['reason']}")

    def _assert_room_fits(self, room_id: int, start: str, end: str,
                          exclude_ids: set | None = None, is_member: bool = False):
        courtyard_id = self._room_courtyard(room_id)
        self._assert_courtyard_open(courtyard_id, start, end)
        court = self.courtyards[courtyard_id]
        excluded = set(exclude_ids or ())
        for day in (d.isoformat() for d in daterange(start, end)):
            if is_member:
                cap = court["member_capacity"]
                if cap <= 0:
                    raise DomainError("该院落不接待会员换住")
                if self._member_used(courtyard_id, day, excluded) + 1 > cap:
                    raise DomainError(f"{day} 该院落会员名额已满")
            if self._room_claims(room_id, day, excluded) >= 1:
                raise DomainError(f"{day} 房间权益已被占用，防止超卖")

    def _assert_whole_fits(self, courtyard_id: int, start: str, end: str,
                           exclude_ids: set | None = None):
        if courtyard_id not in self.courtyards:
            raise DomainError("院落不存在")
        self._assert_courtyard_open(courtyard_id, start, end)
        room_ids = self._rooms_of_courtyard(courtyard_id)
        for day in (d.isoformat() for d in daterange(start, end)):
            for room_id in room_ids:
                if self._room_claims(room_id, day, exclude_ids) >= 1:
                    raise DomainError(f"{day} 院内仍有旅居者持有权益，不能整院超卖")

    # ----- 入住预约 / 离村 / 换住 / 封闭 ----------------------------------

    def create_booking(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "agreement_id", "type", "start", "end")
            agreement = self._get_agreement(payload["agreement_id"])
            if agreement["status"] != "ACTIVE":
                raise DomainError("协约已终止，不能新增入住预约")
            kind = payload["type"]
            if kind not in ("ROOM", "WHOLE", "MEMBER"):
                raise DomainError("预约类型只能是 ROOM / WHOLE / MEMBER")
            if agreement["kind"] == "MEMBER" and kind != "MEMBER":
                raise DomainError("会员协约只能登记会员预约")
            if agreement["kind"] != "MEMBER" and kind == "MEMBER":
                raise DomainError("会员预约需要 MEMBER 协约")
            start, end = payload["start"], payload["end"]
            list(daterange(start, end))
            if not (agreement["start"] <= start and end <= self._effective_end(agreement)):
                raise DomainError("入住时段必须在协约有效期内")

            room_id = int(payload["room_id"]) if payload.get("room_id") else None
            courtyard_id = int(payload["courtyard_id"]) if payload.get("courtyard_id") else None

            if kind == "WHOLE":
                courtyard_id = courtyard_id or agreement["courtyard_id"]
                if courtyard_id != agreement["courtyard_id"]:
                    raise DomainError("整院预约只能使用协约院落")
                self._assert_whole_fits(courtyard_id, start, end)
            else:
                if kind == "ROOM":
                    room_id = room_id or agreement["room_id"]
                    if agreement["kind"] == "ROOM" and room_id != agreement["room_id"]:
                        raise DomainError("按房间协约只能预约协约房间，跨院请走换住")
                if not room_id:
                    raise DomainError("该预约类型需指定 room_id")
                target_village = self.courtyards[self._room_courtyard(room_id)]["village_id"]
                if target_village != agreement["village_id"]:
                    raise DomainError("跨村入住必须办理换住单，不能直接预约外村房间")
                self._assert_room_fits(room_id, start, end, is_member=(kind == "MEMBER"))
                courtyard_id = self._room_courtyard(room_id)

            bid = self._new_id()
            booking = {
                "id": bid,
                "agreement_id": agreement["id"],
                "type": kind,
                "start": start,
                "end": end,
                "room_id": room_id,
                "courtyard_id": courtyard_id,
                "status": "CONFIRMED",
                "swap_id": None,
                "created_at": _now(),
            }
            self.bookings[bid] = booking
            self._event("booking.created", {"booking_id": bid, "type": kind, "start": start, "end": end})
            return dict(booking)

    def cancel_booking(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "booking_id")
            booking = self.bookings.get(int(payload["booking_id"]))
            if not booking:
                raise DomainError("预约不存在")
            if booking["type"] == "SWAP":
                raise DomainError("换住预约需通过换住单取消")
            for swap in self.swaps.values():
                if (swap["out_booking_id"] == booking["id"]
                        and swap["status"] in ("PROPOSED", "AGREED")):
                    raise DomainError("存在换住申请，请先取消换住单再取消预约")
            booking["status"] = "CANCELLED"
            self._event("booking.cancelled", {"booking_id": booking_id})
            return dict(booking)

    def register_absence(self, payload: dict) -> dict:
        """临时离村：登记后该时段不占容量，但协约与租期不变。"""
        with self._lock:
            require(payload, "booking_id", "start", "end", "reason")
            booking = self.bookings.get(int(payload["booking_id"]))
            if not booking or booking["status"] != "CONFIRMED" or booking["type"] == "SWAP":
                raise DomainError("只能对有效的本人入住预约登记离村")
            start, end = payload["start"], payload["end"]
            list(daterange(start, end))
            if not overlap(start, end, booking["start"], booking["end"]):
                raise DomainError("离村时段必须在入住期内")
            start = max(start, booking["start"])
            end = min(end, booking["end"])
            for swap in self.swaps.values():
                if (swap["out_booking_id"] == booking["id"] and swap["status"] == "AGREED"
                        and overlap(start, end, swap["start"], swap["end"])):
                    raise DomainError("跨村换住期间不能再登记离村")
            for absence in self.absences.values():
                if absence["booking_id"] == booking["id"] and overlap(start, end, absence["start"], absence["end"]):
                    raise DomainError("离村时段不能重叠登记")
            aid = self._new_id()
            record = {"id": aid, "booking_id": booking["id"], "start": start, "end": end,
                      "reason": payload["reason"], "created_at": _now()}
            self.absences[aid] = record
            self._event("absence.registered", {"absence_id": aid, "start": start, "end": end})
            return dict(record)

    def request_swap(self, payload: dict) -> dict:
        """旅居者申请把一段使用权换到另一个村的房间。

        两个村的对换需各自提交一份 PROPOSED 换住单，再由 accept_swap 原子配对
        达成；换往对方空闲房间的单向换住可直接 accept。
        """
        with self._lock:
            require(payload, "agreement_id", "booking_id", "to_room_id", "start", "end")
            agreement = self._get_agreement(payload["agreement_id"])
            booking = self.bookings.get(int(payload["booking_id"]))
            if not booking or booking["agreement_id"] != agreement["id"]:
                raise DomainError("换住预约不属于该协约")
            if booking["status"] != "CONFIRMED" or booking["type"] not in ("ROOM", "MEMBER"):
                raise DomainError("只能对有效的按房间/会员入住预约发起换住")
            start, end = payload["start"], payload["end"]
            list(daterange(start, end))
            if not (booking["start"] <= start and end <= booking["end"]):
                raise DomainError("换住时段必须在原入住期内")
            for absence in self.absences.values():
                if absence["booking_id"] == booking["id"] and overlap(start, end, absence["start"], absence["end"]):
                    raise DomainError("离村期间不能办理换住")
            for other in self.swaps.values():
                if (other["out_booking_id"] == booking["id"] and other["status"] != "CANCELLED"
                        and overlap(start, end, other["start"], other["end"])):
                    raise DomainError("该时段已有换住安排，换住时段不能重叠")
            to_room_id = int(payload["to_room_id"])
            to_court = self._room_courtyard(to_room_id)
            to_village = self.courtyards[to_court]["village_id"]
            if to_village == agreement["village_id"]:
                raise DomainError("跨村换住必须指向其他村，同村调房请变更预约")
            sid = self._new_id()
            record = {
                "id": sid,
                "agreement_id": agreement["id"],
                "out_booking_id": booking["id"],
                "from_village_id": agreement["village_id"],
                "to_village_id": to_village,
                "to_room_id": to_room_id,
                "pair_swap_id": None,
                "in_booking_id": None,
                "start": start,
                "end": end,
                "status": "PROPOSED",
                "created_at": _now(),
            }
            self.swaps[sid] = record
            self._event("swap.proposed", {"swap_id": sid, "to_village_id": to_village})
            return dict(record)

    def accept_swap(self, payload: dict) -> dict:
        """接受换住。

        * 仅给 ``swap_id``：换往当前空闲房间，直接达成；
        * 同时给 ``pair_swap_id``：两个村的换住单原子配对达成，容量校验时
          互相排除双方将释放的原预约，杜绝“互为前提导致谁都换不成”。
        """
        with self._lock:
            require(payload, "swap_id")
            swap = self.swaps.get(int(payload["swap_id"]))
            if not swap:
                raise DomainError("换住单不存在")
            if swap["status"] == "AGREED":
                return self._swap_result(swap)  # 重复接受按幂等处理
            if swap["status"] != "PROPOSED":
                raise DomainError("换住单已取消")

            pair_id = payload.get("pair_swap_id")
            pair = None
            if pair_id is not None:
                pair = self.swaps.get(int(pair_id))
                if not pair or pair["id"] == swap["id"]:
                    raise DomainError("配对换住单不存在")
                self._validate_pair(swap, pair)
                # 原子校验：双方目标房间在排除双方原预约后均为空
                self._assert_room_fits(
                    swap["to_room_id"], swap["start"], swap["end"],
                    exclude_ids={swap["out_booking_id"], pair["out_booking_id"]})
                self._assert_room_fits(
                    pair["to_room_id"], pair["start"], pair["end"],
                    exclude_ids={swap["out_booking_id"], pair["out_booking_id"]})
                swap["pair_swap_id"] = pair["id"]
                pair["pair_swap_id"] = swap["id"]
                self._activate_swap(pair)
            else:
                # 单向：目标房间必须当前无权益占用
                self._assert_room_fits(swap["to_room_id"], swap["start"], swap["end"])
            self._activate_swap(swap)
            return self._swap_result(swap, pair)

    def _validate_pair(self, swap: dict, pair: dict):
        if pair["status"] != "PROPOSED":
            raise DomainError("配对换住单不在待处理状态")
        if (swap["start"], swap["end"]) != (pair["start"], pair["end"]):
            raise DomainError("两份换住单的时段必须一致才能配对")
        if swap["to_village_id"] != pair["from_village_id"] \
                or swap["from_village_id"] != pair["to_village_id"]:
            raise DomainError("两份换住单必须互为跨村方向")
        out_room = self.bookings[pair["out_booking_id"]].get("room_id")
        if swap["to_room_id"] != out_room:
            raise DomainError("换入房间必须正好是对方换出的房间")
        out_room_rev = self.bookings[swap["out_booking_id"]].get("room_id")
        if pair["to_room_id"] != out_room_rev:
            raise DomainError("对方换入房间必须正好是本方换出的房间")

    def _activate_swap(self, swap: dict):
        bid = self._new_id()
        in_booking = {
            "id": bid,
            "agreement_id": swap["agreement_id"],
            "type": "SWAP",
            "start": swap["start"],
            "end": swap["end"],
            "room_id": swap["to_room_id"],
            "courtyard_id": self._room_courtyard(swap["to_room_id"]),
            "status": "CONFIRMED",
            "swap_id": swap["id"],
            "created_at": _now(),
        }
        self.bookings[bid] = in_booking
        swap["status"] = "AGREED"
        swap["in_booking_id"] = bid
        self._event("swap.agreed", {"swap_id": swap["id"], "in_booking_id": bid,
                                    "pair_swap_id": swap["pair_swap_id"]})

    def _swap_result(self, swap: dict, pair: dict | None = None) -> dict:
        result = {"swap": dict(swap), "in_booking": dict(self.bookings[swap["in_booking_id"]])}
        if pair is not None:
            result["paired_swap"] = dict(pair)
            result["paired_in_booking"] = dict(self.bookings[pair["in_booking_id"]])
        return result

    def cancel_swap(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "swap_id")
            swap = self.swaps.get(int(payload["swap_id"]))
            if not swap:
                raise DomainError("换住单不存在")
            if swap["status"] == "CANCELLED":
                raise DomainError("换住单已取消")
            pair = self.swaps.get(swap["pair_swap_id"]) if swap["pair_swap_id"] else None
            for record in [swap, pair]:
                if record is None:
                    continue
                record["status"] = "CANCELLED"
                if record["in_booking_id"]:
                    self.bookings[record["in_booking_id"]]["status"] = "CANCELLED"
            self._event("swap.cancelled", {"swap_id": swap["id"],
                                           "pair_swap_id": swap["pair_swap_id"]})
            return {"swap": dict(swap),
                    "paired_swap": dict(pair) if pair else None}

    def register_closure(self, payload: dict) -> dict:
        """维修封闭：与现存使用权冲突则拒绝，需先换住或调整预约。

        逐日判定该院落房间是否仍有持有权益的预约：临时离村期间权益保留（仍
        冲突，否则旅居者回村会面对封闭院落），只有跨村换出释放的时段可封闭。
        """
        with self._lock:
            require(payload, "courtyard_id", "start", "end", "reason")
            courtyard_id = int(payload["courtyard_id"])
            if courtyard_id not in self.courtyards:
                raise DomainError("院落不存在")
            start, end = payload["start"], payload["end"]
            list(daterange(start, end))
            room_ids = self._rooms_of_courtyard(courtyard_id)
            for day in (d.isoformat() for d in daterange(start, end)):
                for room_id in room_ids:
                    if self._room_claims(room_id, day) >= 1:
                        raise DomainError(
                            f"{day} 院落房间仍有旅居者持有使用权，请先办理换住或调整预约")
            closure = {"start": start, "end": end, "reason": payload["reason"], "created_at": _now()}
            self.courtyards[courtyard_id]["closures"].append(closure)
            self._event("courtyard.closed", {"courtyard_id": courtyard_id, "start": start, "end": end})
            return dict(closure)

    # ----- 公共资源：预约、提案、活动 --------------------------------------

    def book_resource(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "resource_id", "agreement_id", "start", "end", "reason")
            rid = int(payload["resource_id"])
            resource = self.resources.get(rid)
            if not resource:
                raise DomainError("公共资源不存在")
            agreement = self._get_agreement(payload["agreement_id"])
            start, end = payload["start"], payload["end"]
            list(daterange(start, end))
            for day in (d.isoformat() for d in daterange(start, end)):
                if self._resource_used(rid, day) >= resource["capacity"]:
                    raise DomainError(f"{day} {resource['name']}容量已满")
            bid = self._new_id()
            record = {
                "id": bid,
                "resource_id": rid,
                "agreement_id": agreement["id"],
                "village_id": resource["village_id"],
                "start": start,
                "end": end,
                "reason": payload["reason"],
                "status": "CONFIRMED",
                "created_at": _now(),
            }
            self.resource_bookings[bid] = record
            self._event("resource.booked", {"resource_booking_id": bid, "resource_id": rid})
            return dict(record)

    def _resource_used(self, resource_id: int, day: str, exclude: int | None = None) -> int:
        used = 0
        for record in self.resource_bookings.values():
            if record["id"] != exclude and record["status"] == "CONFIRMED" \
                    and record["resource_id"] == resource_id \
                    and record["start"] <= day < record["end"]:
                used += 1
        for activity in self.activities.values():
            if activity["status"] == "OPEN" and activity["resource_id"] == resource_id \
                    and activity["start"] <= day < activity["end"]:
                used += 1  # 一个活动占一个档期
        return used

    def create_proposal(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "village_id", "creator_id", "title")
            vid = int(payload["village_id"])
            if vid not in self.villages:
                raise DomainError("村落不存在")
            pid = self._new_id()
            record = {
                "id": pid,
                "village_id": vid,
                "creator_id": payload["creator_id"],
                "title": payload["title"],
                "body": payload.get("body", ""),
                "votes": {},
                "status": "OPEN",
                "created_at": _now(),
            }
            self.proposals[pid] = record
            self._event("proposal.created", {"proposal_id": pid})
            return self._proposal_view(record)

    def vote_proposal(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "proposal_id", "person_id", "stance")
            record = self.proposals.get(int(payload["proposal_id"]))
            if not record:
                raise DomainError("提案不存在")
            if record["status"] != "OPEN":
                raise DomainError("提案已关闭，不能再投票")
            if payload["stance"] not in ("support", "against"):
                raise DomainError("态度只能是 support / against")
            record["votes"][payload["person_id"]] = payload["stance"]
            self._event("proposal.voted", {"proposal_id": record["id"], "person_id": payload["person_id"]})
            return self._proposal_view(record)

    def close_proposal(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "proposal_id")
            record = self.proposals.get(int(payload["proposal_id"]))
            if not record:
                raise DomainError("提案不存在")
            record["status"] = "CLOSED"
            return self._proposal_view(record)

    def _proposal_view(self, record: dict) -> dict:
        view = dict(record)
        view["support"] = sum(1 for v in record["votes"].values() if v == "support")
        view["against"] = sum(1 for v in record["votes"].values() if v == "against")
        return view

    def create_activity(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "village_id", "resource_id", "start", "end", "title", "capacity")
            vid, rid = int(payload["village_id"]), int(payload["resource_id"])
            resource = self.resources.get(rid)
            if not resource or resource["village_id"] != vid:
                raise DomainError("公共资源不属于该村")
            start, end = payload["start"], payload["end"]
            list(daterange(start, end))
            capacity = int(payload["capacity"])
            if capacity <= 0:
                raise DomainError("活动容量必须为正整数")
            for day in (d.isoformat() for d in daterange(start, end)):
                if self._resource_used(rid, day) >= resource["capacity"]:
                    raise DomainError(f"{day} {resource['name']}档期已满，无法安排活动")
            aid = self._new_id()
            record = {
                "id": aid,
                "village_id": vid,
                "resource_id": rid,
                "title": payload["title"],
                "start": start,
                "end": end,
                "capacity": capacity,
                "signups": [],
                "status": "OPEN",
                "created_at": _now(),
            }
            self.activities[aid] = record
            self._event("activity.created", {"activity_id": aid, "capacity": capacity})
            return dict(record)

    def signup_activity(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "activity_id", "person_id")
            activity = self.activities.get(int(payload["activity_id"]))
            if not activity:
                raise DomainError("活动不存在")
            if activity["status"] != "OPEN":
                raise DomainError("活动已结束")
            person_id = payload["person_id"]
            if person_id in activity["signups"]:
                raise DomainError("已报名，请勿重复")
            if len(activity["signups"]) >= activity["capacity"]:
                raise DomainError("活动名额已满")
            activity["signups"].append(person_id)
            self._event("activity.signup", {"activity_id": activity["id"], "person_id": person_id})
            return {"activity_id": activity["id"], "signed_up": len(activity["signups"]),
                    "capacity": activity["capacity"]}

    def cancel_signup(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "activity_id", "person_id")
            activity = self.activities.get(int(payload["activity_id"]))
            if not activity:
                raise DomainError("活动不存在")
            if payload["person_id"] not in activity["signups"]:
                raise DomainError("未报名该活动")
            activity["signups"].remove(payload["person_id"])
            return {"activity_id": activity["id"], "signed_up": len(activity["signups"])}

    # ----- 身份/健康材料：限时授权 ----------------------------------------

    def register_document(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "person_id", "kind", "label")
            if payload["kind"] not in ("identity", "health"):
                raise DomainError("材料类型只能是 identity / health")
            did = self._new_id()
            record = {
                "id": did,
                "person_id": payload["person_id"],
                "kind": payload["kind"],
                "label": payload["label"],
                # 刻意不保存材料内容，服务端无法泄露自己没有的数据
                "created_at": _now(),
            }
            self.documents[did] = record
            self._event("document.registered", {"document_id": did, "kind": payload["kind"]})
            return dict(record)

    def grant_document(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "document_id", "grantee_id", "grantee_role", "ttl_seconds")
            did = int(payload["document_id"])
            if did not in self.documents:
                raise DomainError("材料不存在")
            if payload["grantee_role"] not in ("village", "operator", "medical"):
                raise DomainError("grantee_role 只能是 village / operator / medical")
            ttl = int(payload["ttl_seconds"])
            if ttl <= 0:
                raise DomainError("授权时长必须为正整数秒")
            base = parse_dt(payload["now"]) if payload.get("now") else datetime.now(timezone.utc)
            expires = base + timedelta(seconds=ttl)
            expires_iso = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
            # 同一服务人员对同一材料的重复授权按续期处理
            existing = next(
                (g for g in self.grants.values()
                 if g["document_id"] == did and g["grantee_id"] == payload["grantee_id"]
                 and g["status"] == "ACTIVE"),
                None,
            )
            if existing:
                existing["expires_at"] = expires_iso
                self._event("document.grant_renewed", {"grant_id": existing["id"], "expires_at": expires_iso})
                return dict(existing)
            gid = self._new_id()
            record = {
                "id": gid,
                "document_id": did,
                "grantee_id": payload["grantee_id"],
                "grantee_role": payload["grantee_role"],
                "expires_at": expires_iso,
                "status": "ACTIVE",
                "created_at": _now(),
            }
            self.grants[gid] = record
            self._event("document.granted", {"grant_id": gid, "ttl_seconds": ttl})
            return dict(record)

    def _live_grant(self, document_id: int, grantee_id: str, now: datetime) -> dict | None:
        for grant in self.grants.values():
            if (grant["status"] == "ACTIVE" and grant["document_id"] == document_id
                    and grant["grantee_id"] == grantee_id
                    and parse_dt(grant["expires_at"]) > now):
                return grant
        return None

    def view_document(self, payload: dict, now_iso: str | None = None) -> dict:
        with self._lock:
            require(payload, "document_id", "grantee_id")
            did = int(payload["document_id"])
            document = self.documents.get(did)
            if not document:
                raise DomainError("材料不存在")
            now = parse_dt(now_iso) if now_iso else datetime.now(timezone.utc)
            grant = self._live_grant(did, payload["grantee_id"], now)
            if not grant:
                self._event("document.view_denied", {"document_id": did, "grantee_id": payload["grantee_id"]})
                raise DomainError("未获授权或授权已到期收回")
            self._event("document.viewed", {
                "document_id": did, "grantee_id": payload["grantee_id"],
                "grant_id": grant["id"], "grantee_role": grant["grantee_role"],
            })
            # 返回的只是元数据；真实材料由线下/加密通道凭授权单查看
            return {
                "document_id": did,
                "person_id": document["person_id"],
                "kind": document["kind"],
                "label": document["label"],
                "grant_id": grant["id"],
                "expires_at": grant["expires_at"],
            }

    def revoke_grant(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "grant_id")
            grant = self.grants.get(int(payload["grant_id"]))
            if not grant:
                raise DomainError("授权不存在")
            grant["status"] = "REVOKED"
            self._event("document.grant_revoked", {"grant_id": int(payload["grant_id"])})
            return dict(grant)

    def list_grants(self) -> list[dict]:
        with self._lock:
            now = datetime.now(timezone.utc)
            result = []
            for grant in self.grants.values():
                view = dict(grant)
                view["document_kind"] = self.documents[grant["document_id"]]["kind"]
                view["person_id"] = self.documents[grant["document_id"]]["person_id"]
                if grant["status"] == "ACTIVE" and parse_dt(grant["expires_at"]) <= now:
                    view["status"] = "EXPIRED"  # 到期状态实时计算，不改底单
                result.append(view)
            return result

    # ----- 三方确认与按月封账 ---------------------------------------------

    def confirm_settlement_party(self, payload: dict) -> dict:
        with self._lock:
            require(payload, "agreement_id", "period", "role")
            agreement = self._get_agreement(payload["agreement_id"])
            role = payload["role"]
            if role not in VALID_ROLES:
                raise DomainError("确认方只能是 village / operator / resident")
            period = payload["period"]
            self._validate_period(period)
            if not self._agreement_touches_period(agreement, period):
                raise DomainError("协约在该月没有有效时段")
            key = f"{agreement['id']}|{period}"
            record = self.confirmations.setdefault(key, {
                "agreement_id": agreement["id"], "period": period,
                "village": False, "operator": False, "resident": False,
                "actors": {},
            })
            record[role] = True
            record["actors"][role] = payload.get("actor_id", "")
            record["rule_version"] = agreement["rule_version"]
            self._event("settlement.confirmed", {"agreement_id": agreement["id"], "period": period, "role": role})
            return dict(record)

    def _validate_period(self, period: str):
        try:
            datetime.strptime(period, "%Y-%m")
        except (TypeError, ValueError) as exc:
            raise DomainError("月份格式应为 YYYY-MM") from exc

    def _agreement_touches_period(self, agreement: dict, period: str) -> bool:
        month_start = f"{period}-01"
        year, month = int(period[:4]), int(period[5:7])
        last = calendar.monthrange(year, month)[1]
        next_day = (date(year, month, last) + timedelta(days=1)).isoformat()
        return agreement["start"] < next_day and self._effective_end(agreement) > month_start

    def _agreements_for_period(self, village_id: int, period: str) -> list[dict]:
        return [a for a in self.agreements.values()
                if a["village_id"] == village_id and self._agreement_touches_period(a, period)]

    def close_period(self, payload: dict) -> dict:
        """封账：三方确认齐备后，按协约冻结的规则版本生成本月分成。"""
        with self._lock:
            require(payload, "village_id", "period")
            village_id = int(payload["village_id"])
            period = payload["period"]
            self._validate_period(period)
            if village_id not in self.villages:
                raise DomainError("村落不存在")
            key = f"{village_id}|{period}"
            if self.periods.get(key, {}).get("status") == "CLOSED":
                raise DomainError(f"{period} 已封账，封账结果不可直接改写")
            agreements = self._agreements_for_period(village_id, period)
            if not agreements:
                raise DomainError("该村该月没有可结算协约")
            for agreement in agreements:
                conf = self.confirmations.get(f"{agreement['id']}|{period}")
                missing = [role for role in VALID_ROLES if not conf or not conf.get(role)]
                if missing:
                    raise DomainError(
                        f"协约 #{agreement['id']} 尚缺 {'/'.join(missing)} 确认，不能封账")

            settlements = []
            for agreement in agreements:
                end_eff = self._effective_end(agreement)
                seg_start, seg_end = None, None
                for month, ms, me in month_segments(agreement["start"], end_eff):
                    if month == period:
                        seg_start, seg_end = ms, me
                        break
                if seg_start is None:
                    continue
                days = _days(seg_start, seg_end)
                year, month = int(period[:4]), int(period[5:7])
                month_total = calendar.monthrange(year, month)[1]
                rule = self.rules[agreement["rule_version"]]
                if agreement["kind"] == "MEMBER":
                    base = round(rule["member_fee_per_month"] * days / month_total, 2)
                    base_kind = "member_fee"
                else:
                    base = round(agreement["monthly_rent"] * days / month_total, 2)
                    base_kind = "rent"
                amounts = {role: round(base * rule["shares"][role], 2) for role in VALID_ROLES}
                amounts["operator"] = round(base - amounts["village"] - amounts["resident"], 2)
                splits = [{
                    "party": role,
                    "amount": amounts[role],
                    "rule_version": rule["version"],
                } for role in VALID_ROLES]
                settlements.append({
                    "agreement_id": agreement["id"],
                    "person_id": agreement["person_id"],
                    "kind": agreement["kind"],
                    "rule_version": rule["version"],
                    "base_kind": base_kind,
                    "days": days,
                    "base": base,
                    "deposit": agreement["deposit"],
                    "service_hours": round(rule["service_hour_per_month"] * days / month_total, 2),
                    "splits": splits,
                })

            record = {
                "village_id": village_id,
                "period": period,
                "status": "CLOSED",
                "closed_at": _now(),
                "settlements": settlements,
            }
            self.periods[key] = record
            self._event("period.closed", {"village_id": village_id, "period": period,
                                          "settlements": len(settlements)})
            return dict(record)

    def reopen_period(self, payload: dict) -> dict:
        """纠错重开：全程留痕；重算时仍读取协约冻结版本，新规则依旧不追溯。"""
        with self._lock:
            require(payload, "village_id", "period")
            village_id = int(payload["village_id"])
            period = payload["period"]
            reason = payload.get("reason", "")
            key = f"{village_id}|{period}"
            record = self.periods.get(key)
            if not record or record["status"] != "CLOSED":
                raise DomainError("该账期未封账")
            record["status"] = "OPEN"
            self._event("period.reopened", {"village_id": village_id, "period": period, "reason": reason})
            return dict(record)

    def get_period(self, village_id: int, period: str) -> dict | None:
        with self._lock:
            record = self.periods.get(f"{village_id}|{period}")
            return dict(record) if record else None

    # ----- 查询：使用权与占用理由 -----------------------------------------

    def usage_on(self, village_id: int, day: str) -> dict:
        with self._lock:
            parse_date(day)
            courtyards = []
            for cid, court in sorted(self.courtyards.items()):
                if court["village_id"] != village_id:
                    continue
                rooms = []
                active_closures = [c for c in court["closures"] if c["start"] <= day < c["end"]]
                for rid, room in sorted(self.rooms.items()):
                    if room["courtyard_id"] != cid:
                        continue
                    occupants = []
                    for booking in self.bookings.values():
                        if not self._right_active(booking, day):
                            continue
                        hit = False
                        if booking["type"] == "WHOLE":
                            hit = booking["courtyard_id"] == cid
                        elif booking.get("room_id") == rid:
                            hit = True
                        if hit:
                            view = self._occupant_view(booking)
                            view["present"] = self._present(booking, day)
                            view["state"] = "temporarily_away" if not view["present"] else "in_village"
                            occupants.append(view)
                    rooms.append({"room_id": rid, "name": room["name"], "occupants": occupants})
                courtyards.append({
                    "courtyard_id": cid,
                    "name": court["name"],
                    "closed": active_closures,
                    "rooms": rooms,
                })

            resources = []
            for rid, resource in sorted(self.resources.items()):
                if resource["village_id"] != village_id:
                    continue
                occupants = []
                for record in self.resource_bookings.values():
                    if record["status"] == "CONFIRMED" and record["resource_id"] == rid \
                            and record["start"] <= day < record["end"]:
                        occupants.append({
                            "kind": "booking",
                            "resource_booking_id": record["id"],
                            "agreement_id": record["agreement_id"],
                            "reason": record["reason"],
                            "start": record["start"],
                            "end": record["end"],
                        })
                for activity in self.activities.values():
                    if activity["status"] == "OPEN" and activity["resource_id"] == rid \
                            and activity["start"] <= day < activity["end"]:
                        occupants.append({
                            "kind": "activity",
                            "activity_id": activity["id"],
                            "reason": f"社区活动：{activity['title']}",
                            "start": activity["start"],
                            "end": activity["end"],
                        })
                resources.append({
                    "resource_id": rid,
                    "name": resource["name"],
                    "capacity": resource["capacity"],
                    "used": len(occupants),
                    "remaining": resource["capacity"] - len(occupants),
                    "occupants": occupants,
                })

            return {"day": day, "village_id": village_id,
                    "courtyards": courtyards, "resources": resources}

    def _occupant_view(self, booking: dict) -> dict:
        agreement = self.agreements.get(booking["agreement_id"], {})
        if booking["type"] == "SWAP":
            swap = self.swaps.get(booking["swap_id"], {})
            label = f"跨村换入（换住单 #{booking['swap_id']}，来自 {swap.get('from_village_id')} 村）"
        else:
            label = {
                "ROOM": "协约入住（按房间）",
                "WHOLE": "协约入住（整院）",
                "MEMBER": "会员预约入住",
            }[booking["type"]]
        return {
            "booking_id": booking["id"],
            "agreement_id": booking["agreement_id"],
            "person_id": agreement.get("person_id"),
            "type": booking["type"],
            "reason": label,
            "start": booking["start"],
            "end": booking["end"],
            "village_id": agreement.get("village_id"),
        }

    def rights_of(self, person_id: str, day: str) -> dict:
        with self._lock:
            parse_date(day)
            agreements = []
            rights = []
            for agreement in self.agreements.values():
                if agreement["person_id"] != person_id:
                    continue
                agreements.append({k: agreement[k] for k in (
                    "id", "village_id", "kind", "status", "start", "end",
                    "room_id", "courtyard_id", "rule_version", "deposit")})
            for booking in self.bookings.values():
                agreement = self.agreements.get(booking["agreement_id"])
                if not agreement or agreement["person_id"] != person_id:
                    continue
                if booking["status"] != "CONFIRMED" or not (booking["start"] <= day < booking["end"]):
                    continue
                holds_right = self._right_active(booking, day)
                if holds_right:
                    state = "in_village" if self._present(booking, day) else "temporarily_away"
                elif booking["type"] == "SWAP":
                    state = "swap_cancelled"
                elif any(sw["out_booking_id"] == booking["id"] and sw["start"] <= day < sw["end"]
                         for sw in self.swaps.values() if sw["status"] == "AGREED"):
                    state = "swapped_out"
                else:
                    continue  # 非当日权益，且不是换出，不列入
                rights.append({
                    "booking_id": booking["id"],
                    "type": booking["type"],
                    "village_id": self.courtyards[booking["courtyard_id"]]["village_id"],
                    "courtyard_id": booking["courtyard_id"],
                    "room_id": booking["room_id"],
                    "start": booking["start"],
                    "end": booking["end"],
                    "holds_right": holds_right,
                    "state": state,
                    "swap_id": booking["swap_id"],
                })
            return {"person_id": person_id, "day": day,
                    "agreements": agreements, "rights": rights}

    # ----- 快照 -----------------------------------------------------------

    def to_snapshot(self) -> dict:
        with self._lock:
            return {
                "format": 1,
                "next_id": self._next_id,
                "villages": self.villages,
                "courtyards": self.courtyards,
                "rooms": self.rooms,
                "resources": self.resources,
                "rules": self.rules,
                "agreements": self.agreements,
                "bookings": self.bookings,
                "absences": self.absences,
                "swaps": self.swaps,
                "resource_bookings": self.resource_bookings,
                "proposals": self.proposals,
                "activities": self.activities,
                "documents": self.documents,
                "grants": self.grants,
                "confirmations": self.confirmations,
                "periods": self.periods,
                "events": self.events,
            }

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> "Community":
        community = cls()
        if snapshot.get("format") != 1:
            raise DomainError("不支持的快照格式")
        community._next_id = int(snapshot["next_id"])
        for key in (
            "villages", "courtyards", "rooms", "resources", "rules", "agreements",
            "bookings", "absences", "swaps", "resource_bookings", "proposals",
            "activities", "documents", "grants", "confirmations", "periods", "events",
        ):
            setattr(community, key, _restored(snapshot.get(key, {} if key != "events" else [])))
        return community


def _restored(value):
    """从 JSON 结构恢复字典键为 int 的表（兼容直接传入内存快照时的 int 键）。"""
    if isinstance(value, list):
        return [_restored(v) for v in value]
    if isinstance(value, dict):
        if value and all(str(k).isdigit() for k in value):
            return {int(k): _restored(v) for k, v in value.items()}
        return {k: _restored(v) for k, v in value.items()}
    return value
