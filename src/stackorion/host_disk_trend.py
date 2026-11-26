"""Short-horizon projection from continuous root filesystem observations."""

from datetime import datetime, timedelta


MIN_SAMPLES = 5
MIN_SPAN_HOURS = 4
MAX_GAP_SECONDS = 2 * 3600


def disk_projection(history, threshold, now, *, truncated):
    threshold = threshold if threshold is not None else 100
    result = {"threshold": threshold, "samples": 0, "span_hours": 0}
    if not history:
        return {**result, "status": "insufficient_samples"}
    latest = history[0]["metrics"].get("disk_percent")
    if not latest:
        return {**result, "status": "latest_missing"}
    newest = datetime.fromisoformat(latest["observed_at"])
    age = (now - newest).total_seconds()
    if not 0 <= age <= MAX_GAP_SECONDS:
        return {**result, "status": "stale"}
    if latest["value"] >= threshold:
        return {**result, "status": "threshold_reached", "samples": 1}

    points = [(0.0, latest["value"])]
    stopped = "insufficient_samples"
    previous_run = history[0]
    for run in history[1:]:
        point = run["metrics"].get("disk_percent")
        intervals = [sample["interval_minutes"] for sample in (run, previous_run)
                     if sample.get("trigger") == "scheduled" and sample.get("interval_minutes")]
        gap_limit = min([MAX_GAP_SECONDS, *(90 * minutes for minutes in intervals)])
        if not point:
            stopped = "interrupted"
            break
        if point["source"] != latest["source"]:
            stopped = "source_changed"
            break
        hours = (datetime.fromisoformat(point["observed_at"]) - newest).total_seconds() / 3600
        if hours >= points[-1][0]:
            continue
        if (points[-1][0] - hours) * 3600 > gap_limit:
            stopped = "interrupted"
            break
        points.append((hours, point["value"]))
        previous_run = run

    span = -points[-1][0]
    result.update(samples=len(points), span_hours=round(span, 1))
    if len(points) < MIN_SAMPLES or span < MIN_SPAN_HOURS:
        return {**result, "status": "truncated" if truncated and stopped == "insufficient_samples" else stopped}

    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    xx = sum((x - mean_x) ** 2 for x, _ in points)
    yy = sum((y - mean_y) ** 2 for _, y in points)
    xy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    rate = xy / xx
    if rate * span < 0.5:
        return {**result, "status": "no_growth"}
    if xy * xy / (xx * yy) < 0.85:
        return {**result, "status": "unstable"}
    hours_left = (threshold - latest["value"]) / rate
    if hours_left > span:
        return {**result, "status": "outside_window"}
    projected_at = newest + timedelta(hours=hours_left)
    if projected_at <= now:
        return {**result, "status": "recheck"}
    return {**result, "status": "projected", "rate_per_hour": round(rate, 2),
            "projected_at": projected_at.isoformat()}
