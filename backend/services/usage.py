"""Usage tracking + subscription architecture (no payments in V1).

Every AI call records usage_events tagged with BOTH device_id and account_id, so we can
later bill/limit per parent account (user-level) while still reporting per toy (device-level).
Quota enforcement is off unless ENFORCE_QUOTAS=true.
"""
from __future__ import annotations


from dataclasses import dataclass
from datetime import datetime

from backend.storage.db import Database, now

PLANS = {
    # plan -> monthly quotas. Placeholder values for the future subscription product.
    "free": {"monthly_minutes_quota": 60, "monthly_llm_token_quota": 200_000},
    "family": {"monthly_minutes_quota": 600, "monthly_llm_token_quota": 2_000_000},
}


@dataclass
class QuotaCheck:
    allowed: bool
    reason: str = ""


class UsageService:
    def __init__(self, db: Database):
        self.db = db

    def record(self, device_id: str, account_id: str | None, kind: str, amount: float, provider: str = "") -> None:
        if amount:
            self.db.execute(
                "INSERT INTO usage_events (device_id, account_id, kind, amount, provider, created_at) VALUES (?,?,?,?,?,?)",
                (device_id, account_id, kind, float(amount), provider, now()),
            )

    @staticmethod
    def _start_of_today() -> float:
        d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()

    @staticmethod
    def _start_of_month() -> float:
        d = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()

    def _sum(self, where: str, params: tuple) -> dict[str, float]:
        rows = self.db.query(f"SELECT kind, SUM(amount) AS total FROM usage_events WHERE {where} GROUP BY kind", params)
        return {r["kind"]: r["total"] or 0 for r in rows}

    def device_stats(self, device_id: str) -> dict:
        today = self._sum("device_id = ? AND created_at >= ?", (device_id, self._start_of_today()))
        month = self._sum("device_id = ? AND created_at >= ?", (device_id, self._start_of_month()))
        total = self._sum("device_id = ?", (device_id,))

        def shape(d):
            return {
                "turns": int(d.get("turn", 0)),
                "conversation_minutes": round(d.get("conversation_seconds", 0) / 60, 2),
                "stt_seconds": round(d.get("stt_seconds", 0), 1),
                "llm_input_tokens": int(d.get("llm_input_tokens", 0)),
                "llm_output_tokens": int(d.get("llm_output_tokens", 0)),
                "tts_chars": int(d.get("tts_chars", 0)),
            }

        return {"today": shape(today), "month": shape(month), "all_time": shape(total)}

    def account_stats(self, account_id: str) -> dict:
        month = self._sum("account_id = ? AND created_at >= ?", (account_id, self._start_of_month()))
        return {"conversation_minutes": round(month.get("conversation_seconds", 0) / 60, 2),
                "llm_tokens": int(month.get("llm_input_tokens", 0) + month.get("llm_output_tokens", 0))}

    def today_minutes_and_turns(self, device_id: str) -> tuple[float, int]:
        t = self.device_stats(device_id)["today"]
        return t["conversation_minutes"], t["turns"]

    # --- accounts & subscriptions (architecture for later) ---
    def create_account(self, account_id: str, name: str, email: str | None = None, plan: str = "free") -> dict:
        self.db.execute("INSERT OR IGNORE INTO accounts (id, name, email, created_at) VALUES (?,?,?,?)",
                        (account_id, name, email, now()))
        if not self.db.query_one("SELECT id FROM subscriptions WHERE account_id = ?", (account_id,)):
            q = PLANS.get(plan, PLANS["free"])
            self.db.execute(
                "INSERT INTO subscriptions (account_id, plan, status, monthly_minutes_quota, monthly_llm_token_quota, period_start, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (account_id, plan, "active", q["monthly_minutes_quota"], q["monthly_llm_token_quota"], self._start_of_month(), now()),
            )
        return self.get_account(account_id)

    def get_account(self, account_id: str) -> dict | None:
        acc = self.db.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        if acc:
            acc["subscription"] = self.db.query_one("SELECT * FROM subscriptions WHERE account_id = ? ORDER BY id DESC", (account_id,))
            acc["usage_this_month"] = self.account_stats(account_id)
            acc["devices"] = [r["id"] for r in self.db.query("SELECT id FROM devices WHERE account_id = ?", (account_id,))]
        return acc

    def check_quota(self, account_id: str | None, enforce: bool) -> QuotaCheck:
        if not enforce or not account_id:
            return QuotaCheck(True)
        sub = self.db.query_one("SELECT * FROM subscriptions WHERE account_id = ? ORDER BY id DESC", (account_id,))
        if not sub or sub["status"] not in ("active", "trial"):
            return QuotaCheck(False, "subscription_inactive")
        used = self.account_stats(account_id)
        if used["conversation_minutes"] >= sub["monthly_minutes_quota"]:
            return QuotaCheck(False, "monthly_minutes_exhausted")
        if used["llm_tokens"] >= sub["monthly_llm_token_quota"]:
            return QuotaCheck(False, "monthly_tokens_exhausted")
        return QuotaCheck(True)


