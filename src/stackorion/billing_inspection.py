"""Comparable, closed-month Tencent consumption evidence for the dashboard."""

from datetime import timedelta, timezone
from decimal import Decimal

from .catalog import CatalogError


MIN_INCREASE = Decimal("100")
MIN_GROWTH = Decimal("0.30")
BILLING_TIMEZONE = timezone(timedelta(hours=8))


def _period(adapter, month):
    try:
        bill = adapter.billing_summary(month)
    except (CatalogError, OSError):
        return {"month": month, "status": "unavailable", "amount": None}, {}
    if bill["status"] == "pending":
        return {"month": month, "status": "pending", "amount": None}, {}
    if bill["status"] != "complete" or bill["month"] != month or bill["unit"] != "CNY yuan":
        return {"month": month, "status": "unavailable", "amount": None}, {}

    amount = bill["summary"].get("RealTotalCost")
    period = {"month": month, "status": "complete" if amount is not None else "incomplete", "amount": amount}
    products = {}
    for row in bill["products"]:
        code = row.get("BusinessCode")
        if not code:
            period["status"] = "incomplete"
            continue
        total = row.get("RealTotalCost")
        if code in products or total is None:
            period["status"] = "incomplete"
            products[code] = None
        else:
            products[code] = total
    return period, products


def compare_closed_months(adapter, now):
    current = now.astimezone(BILLING_TIMEZONE).date().replace(day=1)
    last = current - timedelta(days=1)
    earlier = last.replace(day=1) - timedelta(days=1)
    recent, recent_products = _period(adapter, last.strftime("%Y-%m"))
    prior, prior_products = _period(adapter, earlier.strftime("%Y-%m"))
    evidence = {"recent": recent, "prior": prior, "compared": [], "unmatched_products": 0, "unmatched_examples": [],
                "granularity": "account_and_product"}
    findings = []

    def compare(key, name, before, after):
        if before is None or after is None:
            return False
        baseline, current_cost = Decimal(before), Decimal(after)
        if baseline < 0 or current_cost < 0:
            return False
        evidence["compared"].append(key)
        increase = current_cost - baseline
        if increase >= MIN_INCREASE and (baseline == 0 or increase >= baseline * MIN_GROWTH):
            findings.append(dict(kind="billing", id=key, name=name, status="increased", currency="CNY",
                                 month=recent["month"], prior_month=prior["month"], amount=after,
                                 prior_amount=before, increase=str(increase),
                                 growth_percent=str(round(increase * 100 / baseline, 1)) if baseline else None))
        return True

    total_compared = compare("account-total", "账号总费用", prior["amount"], recent["amount"])
    if all(period["status"] in {"complete", "incomplete"} for period in (recent, prior)):
        for code in sorted(recent_products.keys() | prior_products.keys()):
            if not compare("product:" + code, code, prior_products.get(code), recent_products.get(code)):
                evidence["unmatched_products"] += 1
                if len(evidence["unmatched_examples"]) < 5:
                    evidence["unmatched_examples"].append(code)
    if total_compared and all(p["status"] == "complete" for p in (recent, prior)) and not evidence["unmatched_products"]:
        status = "complete"
    elif any(p["status"] in {"complete", "incomplete", "pending"} for p in (recent, prior)):
        status = "partial"
    else:
        status = "unavailable"
    return status, evidence, findings
