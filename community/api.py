"""HTTP API 路由：把 /api/* 请求映射到 CommunityStore 的领域操作。"""

from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass


def _to_jsonable(obj):
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    return obj


def _compile(pattern):
    regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
    return re.compile(f"^{regex}$")


class Api:
    """无状态路由层；状态全部在 store 中。"""

    def __init__(self, store):
        self.store = store
        self.routes = []
        add = self._add
        add("POST", "/api/villages", lambda b, q: store.add_village(b["name"], b.get("region", "")))
        add("POST", "/api/courtyards", lambda b, q: store.add_courtyard(
            b["village_id"], b["name"], b["whole_price_fen"], b["rooms"]))
        add("POST", "/api/rules", lambda b, q: store.add_rule(
            b["effective_from"], b["deposit_bp"], b["villager_share_bp"],
            b["operator_share_bp"], b["service_hour_rate_fen"], b.get("note", "")))
        add("POST", "/api/agreements", lambda b, q: store.create_agreement(
            b["village_id"], b["courtyard_id"], b["mode"], b.get("room_ids", []),
            b["resident_id"], b["start"], b["end"]))
        add("GET", "/api/agreements/{aid}", lambda b, q, aid: store._agreement(aid))
        add("POST", "/api/agreements/{aid}/confirm",
            lambda b, q, aid: store.confirm_agreement(aid, b["role"], b.get("today")))
        add("POST", "/api/agreements/{aid}/leave", lambda b, q, aid: store.add_leave(
            aid, b["start"], b["end"], b.get("reason", "")))
        add("POST", "/api/agreements/{aid}/checkout",
            lambda b, q, aid: store.checkout(aid, b.get("on_date")))
        add("POST", "/api/agreements/{aid}/cancel",
            lambda b, q, aid: store.cancel_agreement(aid))
        add("POST", "/api/swaps", lambda b, q: store.create_swap(
            b["side_a"], b["side_b"], b["start"], b["end"]))
        add("POST", "/api/closures", lambda b, q: store.add_closure(
            b["courtyard_id"], b.get("room_ids", []), b["start"], b["end"], b["reason"]))
        add("POST", "/api/resources", lambda b, q: store.add_resource(
            b["village_id"], b["kind"], b["name"], b["capacity"]))
        add("POST", "/api/resources/{rid}/bookings", lambda b, q, rid: store.book_resource(
            rid, b["day"], b["slots"], b["reason"], b["booked_by"]))
        add("GET", "/api/occupancy", lambda b, q: store.occupancy_on(
            q["day"], q.get("village_id")))
        add("GET", "/api/rights", lambda b, q: store.rights_on(
            q["courtyard_id"], q["day"]))
        add("POST", "/api/activities", lambda b, q: store.create_activity(
            b["village_id"], b["title"], b["day"], b["capacity"]))
        add("POST", "/api/activities/{aid}/signups",
            lambda b, q, aid: store.signup_activity(aid, b["resident_id"]))
        add("POST", "/api/proposals", lambda b, q: store.create_proposal(
            b["village_id"], b["author"], b["text"]))
        add("POST", "/api/proposals/{pid}/votes", lambda b, q, pid: store.vote_proposal(
            pid, b["voter"], b["approve"]))
        add("POST", "/api/materials", lambda b, q: store.add_material(
            b["resident_id"], b["kind"], b["content"]))
        add("POST", "/api/materials/{mid}/grants", lambda b, q, mid: store.grant_access(
            mid, b["staff_id"], b["expires_at"]))
        add("GET", "/api/materials/{mid}",
            lambda b, q, mid: store.view_material(mid, q["staff_id"]))
        add("POST", "/api/grants/sweep", lambda b, q: {"swept": store.sweep_grants()})
        add("POST", "/api/worklogs", lambda b, q: store.add_worklog(
            b["agreement_id"], b["worker"], b["minutes"], b["day"], b["description"]))
        add("POST", "/api/worklogs/{wid}/confirm",
            lambda b, q, wid: store.confirm_worklog(wid, b["role"]))
        add("POST", "/api/settlements", lambda b, q: store.settle(
            b["agreement_id"], b["period"]))
        add("GET", "/api/settlements", lambda b, q: store.settlements_of(
            q["agreement_id"]))
        add("GET", "/api/events", lambda b, q: store.events)

    def _add(self, method, pattern, func):
        self.routes.append((method, _compile(pattern), func))

    def handle(self, method, path, query, body):
        """返回 (status, payload)；领域异常由调用方映射为 HTTP 状态。"""
        for route_method, regex, func in self.routes:
            if route_method != method:
                continue
            match = regex.match(path)
            if match:
                result = func(body, query, **match.groupdict())
                return 200, _to_jsonable(result)
        return 404, {"error": f"未知接口：{method} {path}"}
