"""Evaluate a local StackOrion inspection from normalized observations."""

from datetime import datetime, timedelta
from decimal import Decimal
from ipaddress import ip_network


def inspection_summary(report):
    if report is None:
        return {"status":"empty", "expected_accounts":0}
    return {"status":"observed", **{key:report[key] for key in ("observed_at", "expected_accounts", "coverage", "counts")},
            "accounts":[{key:row[key] for key in ("id", "label", "provider", "status", "data_source") if key in row}
                        for row in report["accounts"]],
            **({"selected_accounts":report["selected_accounts"]} if "selected_accounts" in report else {}),
            **({"simulated_at":report["simulated_at"]} if "simulated_at" in report else {})}


def balance_status(amount, threshold):
    if threshold is None:
        return "unconfigured"
    return "low" if Decimal(amount) < Decimal(threshold) else "normal"


def expiry_status(expires_at, billing, observed_at, window_days=7):
    if not expires_at:
        return "not_applicable" if billing == "postpaid" else "unknown"
    remaining = datetime.fromisoformat(expires_at) - observed_at
    if remaining <= timedelta(0):
        return "expired"
    return "due" if remaining <= timedelta(days=window_days) else "normal"


def is_public_ssh(direction, action, protocol, source, first_port, last_port):
    return (direction == "ingress" and action == "allow" and protocol in {"tcp", "all"}
            and ip_network(source, strict=False).prefixlen == 0
            and first_port <= 22 <= last_port)
