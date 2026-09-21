"""Inspect delivery evidence without model calls, SMTP, or remote state writes."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .models import digest
from .news_quality import metrics


def snapshot(conn, config, now=None):
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(config.timezone))
    deadline = local.replace(hour=config.send_hour, minute=0, second=0, microsecond=0)
    deadline += timedelta(minutes=50)
    run_id = local.date().isoformat() + "-" + digest(config.recipient)[:12]
    row = conn.execute("SELECT status,report,updated FROM outbox WHERE id=?", (run_id,)).fetchone()
    uncertain = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM outbox WHERE status IN ('sending','unknown') ORDER BY day LIMIT 30"
        )
    ]
    errors = []
    if uncertain:
        errors.append("delivery_uncertain_check_mailbox")
    if local >= deadline and (not row or row["status"] != "sent"):
        errors.append("daily_delivery_deadline_missed")
    result = {
        "status": "unhealthy" if errors else "healthy" if row and row[0] == "sent" else "not_due",
        "checked_at": local.isoformat(),
        "deadline": deadline.isoformat(),
        "delivery_id": run_id,
        "delivery_state": row[0] if row else "missing",
        "uncertain_deliveries": uncertain,
        "errors": errors,
        "receipt_verified": False,  # SMTP accepted / operator reconciliation is not inbox telemetry.
    }
    if row:
        report = json.loads(row["report"])
        result.update(
            state_updated_at=row["updated"],
            degraded=report.get("degraded", False),
            companies=metrics(report),
        )
    return result, 2 if errors else 0
