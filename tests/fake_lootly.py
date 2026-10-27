"""In-memory имитация Lootly Seller API для тестов (через httpx.MockTransport)."""
from __future__ import annotations

import itertools
import json
import re
from datetime import datetime, timezone

import httpx

FILTERS = {
    "new": {"paid"}, "processing": {"processing"}, "awaiting": {"awaiting_confirmation"},
    "completed": {"completed"}, "cancelled": {"cancelled"},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class FakeLootly:
    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.products: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.idempotency: dict[str, httpx.Response] = {}
        self.fail_next: dict[str, list[int]] = {}  # "deliver" -> [503, 503]
        self._ids = itertools.count(1)

    # -- helpers for tests
    def add_product(self, pid: str, title: str, external: bool = True) -> dict:
        p = {"id": pid, "title": title, "external": external, "active": True, "archived": False,
             "price": 100, "priceRub": 100, "categoryId": "c"}
        self.products[pid] = p
        return p

    def add_order(self, oid: str, pid: str, quantity: int = 1, status: str = "paid", **extra) -> dict:
        o = {"id": oid, "status": status, "createdAt": now_iso(), "productId": pid,
             "productTitle": self.products[pid]["title"], "quantity": quantity, "amount": 100.0 * quantity,
             "fieldValues": [], "assigneeType": None, "deliveredValue": None, "deliveredValues": [], **extra}
        self.orders[oid] = o
        self.messages[oid] = []
        return o

    def add_message(self, oid: str, text: str, role: str = "customer", created_at: str | None = None) -> dict:
        m = {"id": f"m{next(self._ids)}", "senderRole": role, "text": text, "createdAt": created_at or now_iso()}
        self.messages[oid].append(m)
        return m

    def sent(self, oid: str) -> list[str]:
        return [m["text"] for m in self.messages[oid] if m["senderRole"] == "seller"]

    def count(self, action: str) -> int:
        return sum(1 for _, a, _ in self.calls if a == action)

    # -- transport
    @staticmethod
    def ok(data: dict, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json={"success": True, "data": data})

    @staticmethod
    def err(code: str, status: int) -> httpx.Response:
        return httpx.Response(status, json={"success": False, "error": {"code": code}})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        body = json.loads(request.content) if request.content and request.headers.get(
            "content-type", "").startswith("application/json") else None
        if not request.headers.get("authorization", "").startswith("Bearer lootly_sk_"):
            return self.err("API_KEY_INVALID", 401)

        m = re.fullmatch(r"/orders/([^/]+)/(take|deliver|cancel|messages)", path)
        action = m.group(2) if m and request.method == "POST" else request.method + " " + path
        self.calls.append((request.method, action, body))

        if action in self.fail_next and self.fail_next[action]:
            status = self.fail_next[action].pop(0)
            code = "RATE_LIMIT" if status == 429 else "SERVICE_UNAVAILABLE"
            return httpx.Response(status, json={"success": False, "error": {"code": code}},
                                  headers={"RateLimit-Reset": "0"} if status == 429 else {})

        key = request.headers.get("idempotency-key")
        if key and key in self.idempotency:
            return self.idempotency[key]
        response = self.route(request, path, body)
        if key and response.status_code < 500:
            self.idempotency[key] = response
        return response

    def route(self, request: httpx.Request, path: str, body: dict | None) -> httpx.Response:
        q = request.url.params
        if request.method == "GET" and path == "/products":
            items = list(self.products.values())
            return self.ok({"products": items, "total": len(items), "page": 1, "limit": 100})
        if request.method == "GET" and path == "/orders":
            allowed = FILTERS.get(q.get("status", "new"))
            items = [o for o in self.orders.values()
                     if (allowed is None or o["status"] in allowed)
                     and self.products[o["productId"]]["external"]]
            return self.ok({"orders": items, "total": len(items), "page": 1, "limit": 100})
        if m := re.fullmatch(r"/products/([^/]+)/archive", path):
            p = self.products[m.group(1)]
            p["archived"] = body["archived"]
            p["active"] = not body["archived"]
            return self.ok({"product": p})
        if m := re.fullmatch(r"/orders/([^/]+)(?:/(\w+))?", path):
            oid, action = m.groups()
            o = self.orders.get(oid)
            if o is None:
                return self.err("ORDER_NOT_FOUND", 404)
            if request.method == "GET" and action is None:
                return self.ok({"order": o})
            if request.method == "GET" and action == "messages":
                return self.ok({"messages": list(reversed(self.messages[oid])), "nextCursor": None})
            if action == "messages":
                if not body.get("text", "").strip():
                    return self.err("CHAT_EMPTY_MESSAGE", 400)
                msg = self.add_message(oid, body["text"], role="seller")
                return self.ok({"message": msg}, 201)
            if action == "take":
                if o["status"] != "paid":
                    return self.err("ORDER_INVALID_STATUS", 400)
                o.update(status="processing", assigneeType="api", takenAt=now_iso())
                return self.ok({"order": o})
            if action == "deliver":
                if o["status"] != "processing":
                    return self.err("ORDER_INVALID_STATUS", 400)
                values = body.get("deliveredValues") or []
                if values and len(values) != o["quantity"]:
                    return self.err("VALIDATION_ERROR", 400)
                o.update(status="awaiting_confirmation", deliveredValues=values,
                         deliveredValue=body.get("deliveredValue") or "\n".join(values), deliveredAt=now_iso())
                return self.ok({"order": o})
            if action == "cancel":
                if o["status"] in ("completed", "cancelled"):
                    return self.err("ORDER_INVALID_STATUS", 400)
                o["status"] = "cancelled"
                return self.ok({"order": o})
        return self.err("NOT_FOUND", 404)
