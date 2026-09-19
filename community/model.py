"""旅居社区领域模型。

约定：
- 日期/时间一律保存为 ISO 字符串（"YYYY-MM-DD" / ISO8601 日期时间）；
- 金额一律为整数，单位分（fen）；
- 比例一律为基点（bp，万分比），分成剩余部分归社区公共基金；
- 居住区间一律为半开区间 [start, end)，end 为离村/退房当日。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta


def nights(start: str, end: str) -> int:
    """半开区间 [start, end) 的天数。"""
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    """两个半开区间是否相交。"""
    return a_start < b_end and b_start < a_end


def month_bounds(period: str) -> tuple[str, str]:
    """"YYYY-MM" → 该月的半开区间 [首日, 次月首日)。"""
    year, month = int(period[:4]), int(period[5:7])
    first = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return first.isoformat(), nxt.isoformat()


def each_day(start: str, end: str):
    """逐日生成半开区间内的日期字符串。"""
    day = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while day < stop:
        yield day.isoformat()
        day += timedelta(days=1)


# 协约使用方式
MODE_ROOM = "room"    # 按房间
MODE_WHOLE = "whole"  # 整院
MODE_SWAP = "swap"    # 会员换住

# 协约状态
STATUS_PENDING = "pending"      # 待三方确认
STATUS_ACTIVE = "active"        # 已生效（占用权益）
STATUS_COMPLETED = "completed"  # 已结束（提前离村或到期结账）
STATUS_CANCELLED = "cancelled"  # 已取消

# 需要共同确认押金/工时/分成的三方
PARTIES = ("villager", "operator", "resident")


@dataclass
class Village:
    village_id: str
    name: str
    region: str = ""


@dataclass
class Room:
    room_id: str
    name: str
    nightly_price_fen: int


@dataclass
class Courtyard:
    courtyard_id: str
    village_id: str
    name: str
    whole_price_fen: int
    rooms: list = field(default_factory=list)  # list[Room]

    @classmethod
    def from_dict(cls, data):
        return cls(**{**data, "rooms": [Room(**r) for r in data["rooms"]]})


@dataclass
class RuleVersion:
    """结算规则的一个生效版本。老协约锁定确认时的版本，不被新政策追溯改价。"""

    version: int
    effective_from: str          # 生效日期（含当日）
    deposit_bp: int              # 押金 = 总价 × deposit_bp / 10000
    villager_share_bp: int       # 村民分成
    operator_share_bp: int       # 运营方分成
    service_hour_rate_fen: int   # 服务工时单价（分/小时）
    note: str = ""


@dataclass
class Leave:
    """临时离村：权益保留，不释放容量。"""

    start: str
    end: str
    reason: str = ""


@dataclass
class Agreement:
    agreement_id: str
    village_id: str
    courtyard_id: str
    mode: str                    # room / whole / swap
    room_ids: list               # whole 模式为院落全部房间
    resident_id: str
    start: str
    end: str
    nightly_price_fen: int
    status: str = STATUS_PENDING
    confirmations: list = field(default_factory=list)  # 已确认的角色
    rule_version: int = 0        # 生效时锁定的规则版本，0 = 未锁定
    deposit_fen: int = 0
    leaves: list = field(default_factory=list)  # list[Leave]
    swap_group_id: str = ""      # 换住组号，同一组两边同时生效

    @classmethod
    def from_dict(cls, data):
        return cls(**{**data, "leaves": [Leave(**x) for x in data["leaves"]]})


@dataclass
class Closure:
    """维修封闭：封闭期间不可新签协约；已在住的协约保留但封闭夜不计费。"""

    closure_id: str
    courtyard_id: str
    room_ids: list               # 空列表 = 整个院落
    start: str
    end: str
    reason: str


@dataclass
class Resource:
    """公共资源：菜地、书屋、快递代收、活动空间、创业服务等，按日容量控制。"""

    resource_id: str
    village_id: str
    kind: str
    name: str
    capacity: int                # 每日可占用的份数


@dataclass
class Booking:
    """公共资源某日占用记录，必须登记占用理由。"""

    booking_id: str
    resource_id: str
    day: str
    slots: int
    reason: str
    booked_by: str


@dataclass
class Activity:
    """容量有限的共享活动。"""

    activity_id: str
    village_id: str
    title: str
    day: str
    capacity: int
    signups: list = field(default_factory=list)


@dataclass
class Proposal:
    """邻里提案。"""

    proposal_id: str
    village_id: str
    author: str
    text: str
    status: str = "open"         # open / approved / rejected
    votes_for: list = field(default_factory=list)
    votes_against: list = field(default_factory=list)


@dataclass
class Material:
    """身份/健康材料：不进入普通社区档案，仅获授权人员可短期查看。"""

    material_id: str
    resident_id: str
    kind: str
    content: str
    created_at: str


@dataclass
class Grant:
    """材料查看授权：限时，到期自动收回。"""

    grant_id: str
    material_id: str
    staff_id: str
    expires_at: str
    revoked: bool = False


@dataclass
class WorkLog:
    """服务工时：三方确认后进入结算。"""

    work_id: str
    agreement_id: str
    worker: str
    minutes: int
    day: str
    description: str
    confirmations: list = field(default_factory=list)
    confirmed: bool = False


@dataclass
class ShareLine:
    """一条分成记录，标注结算采用的规则版本。"""

    party: str                   # villager / operator / community / service_pool
    amount_fen: int
    rule_version: int


@dataclass
class Settlement:
    """某协约某月的结算单。同一 (agreement_id, period) 幂等，重算返回原单。"""

    settlement_id: str
    agreement_id: str
    period: str                  # "YYYY-MM"
    billable_nights: int
    closed_nights: int           # 维修封闭导致未计费的天数
    gross_fen: int
    service_fen: int
    rule_version: int
    shares: list = field(default_factory=list)  # list[ShareLine]

    @classmethod
    def from_dict(cls, data):
        return cls(**{**data, "shares": [ShareLine(**s) for s in data["shares"]]})
