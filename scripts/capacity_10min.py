#!/usr/bin/env python3
"""10-minute continuous capacity mode.

Keeps ~max_concurrent registrations in flight by submitting refill batches
whenever free slots appear. KPI = successes in the rolling 10-minute window
(not single-batch wall clock).

Usage:
  py -3.13 scripts/capacity_10min.py --minutes 10 --concurrent 200 --region JP,US,DE,GB,BR

Seat alignment: sets GO_EMAIL_PROTOCOL_MAX_ACTIVE to max(concurrent, existing env)
before ensure_go_worker so L1/L2 seats are not stuck at config.yaml 100.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _align_worker_max_active(concurrent: int) -> int:
    """Ensure worker admission seats >= target concurrent for this capacity run."""
    want = max(1, int(concurrent))
    raw = str(os.environ.get("GO_EMAIL_PROTOCOL_MAX_ACTIVE") or "").strip()
    if raw:
        try:
            want = max(want, int(raw))
        except ValueError:
            pass
    os.environ["GO_EMAIL_PROTOCOL_MAX_ACTIVE"] = str(want)
    return want


def _ledger_failure_breakdown(
    *,
    started_utc: datetime,
    ended_utc: datetime,
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Aggregate jobs.failure_code + transport pattern buckets for the run window."""
    path = ledger_path or (ROOT / "data" / "go-email-protocol-ledger.db")
    out: dict[str, Any] = {
        "ledger": str(path),
        "window_start": started_utc.isoformat(),
        "window_end": ended_utc.isoformat(),
        "by_status": {},
        "by_failure_code": {},
        "error_patterns": {},
        "failed_total": 0,
        "jobs_in_window": 0,
    }
    if not path.is_file():
        out["error"] = "ledger_missing"
        return out
    # SQLite created_at is RFC3339Nano UTC from Go.
    start_s = started_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    end_s = ended_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    # Widen end by 2s so late commits land in the window.
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    except Exception as exc:
        out["error"] = f"open_failed:{exc}"
        return out
    try:
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT status, COALESCE(failure_code, ''), COALESCE(result_json, '')
            FROM jobs
            WHERE created_at >= ? AND created_at <= ?
            """,
            (start_s, end_s + "Z"),
        ).fetchall()
        # Fallback without trailing Z if empty (created_at format variance).
        if not rows:
            rows = cur.execute(
                """
                SELECT status, COALESCE(failure_code, ''), COALESCE(result_json, '')
                FROM jobs
                WHERE created_at >= ? AND created_at <= ?
                """,
                (start_s, end_s),
            ).fetchall()
        status_c: Counter[str] = Counter()
        fail_c: Counter[str] = Counter()
        pat_c: Counter[str] = Counter()
        for status, fcode, raw in rows:
            st = str(status or "")
            status_c[st] += 1
            if st != "failed":
                continue
            code = str(fcode or "").strip() or "unknown"
            fail_c[code] += 1
            msg = ""
            try:
                if raw:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        msg = str(obj.get("error") or obj.get("message") or "")
            except Exception:
                msg = str(raw or "")
            low = msg.lower()
            if "http response to https" in low:
                pat_c["http_to_https"] += 1
            elif "wsarecv" in low or "198.18." in low:
                pat_c["wsarecv_or_fakeip"] += 1
            elif "goaway" in low or "http2:" in low:
                pat_c["http2_goaway"] += 1
            elif "otp" in low or code.startswith("otp_"):
                pat_c["otp"] += 1
            elif "already" in low or code == "email_already_used":
                pat_c["email_used"] += 1
            elif "edge" in low or "challenge" in low:
                pat_c["edge_challenge"] += 1
            else:
                pat_c[f"other:{code}"] += 1
        out["jobs_in_window"] = sum(status_c.values())
        out["by_status"] = dict(status_c.most_common())
        out["by_failure_code"] = dict(fail_c.most_common())
        out["error_patterns"] = dict(pat_c.most_common())
        out["failed_total"] = sum(fail_c.values())
    except Exception as exc:
        out["error"] = f"query_failed:{exc}"
    finally:
        conn.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--concurrent", type=int, default=100, help="target in-flight; 200 needs staged prime+breaker")
    ap.add_argument("--region", type=str, default="JP,US,DE,GB,BR", help="single or comma multi-region")
    ap.add_argument("--otp-timeout", type=int, default=120)
    ap.add_argument("--refill-chunk", type=int, default=0, help="0 = auto min(20, free)")
    ap.add_argument("--styles", type=str, default="bestgo", help="proxy_seed_styles CSV, e.g. bestgo or bestgo,1024")
    ap.add_argument("--out", type=str, default="")
    ap.add_argument(
        "--max-active",
        type=int,
        default=0,
        help="override worker max_active (0 = max(concurrent, GO_EMAIL_PROTOCOL_MAX_ACTIVE/env))",
    )
    args = ap.parse_args()

    import start
    from services.go_email_protocol_runner import check_go_email_protocol_health
    from services.go_registration_batch import (
        get_go_registration_batch,
        start_go_registration_batch,
        worker_supports_batches,
    )

    concurrent = max(1, int(args.concurrent))
    minutes = max(1.0, float(args.minutes))
    region = str(args.region or "JP,US,DE,GB,BR").strip().upper() or "JP,US,DE,GB,BR"
    otp_timeout = max(60, min(240, int(args.otp_timeout or 120)))
    window_s = minutes * 60.0

    if int(args.max_active or 0) > 0:
        os.environ["GO_EMAIL_PROTOCOL_MAX_ACTIVE"] = str(int(args.max_active))
        seat_want = int(args.max_active)
    else:
        seat_want = _align_worker_max_active(concurrent)

    print(f"[cap] ensure go worker… (max_active want={seat_want})")
    start.ensure_go_worker()
    health = check_go_email_protocol_health(
        {"go_email_protocol_mode": "pure", "go_email_protocol_transport": "tls"}
    )
    print(
        f"[cap] worker runner={health.get('runner')} mode={health.get('protocol_mode')} "
        f"transport={health.get('transport')} active={health.get('active_count')}/{health.get('max_active')}"
    )
    try:
        running_max = int(health.get("max_active") or 0)
    except (TypeError, ValueError):
        running_max = 0
    if running_max and running_max < concurrent:
        print(
            f"[cap] WARN: worker max_active={running_max} < concurrent={concurrent}; "
            f"admission will queue. Set GO_EMAIL_PROTOCOL_MAX_ACTIVE>={concurrent}."
        )
    if str(health.get("runner") or "") != "protocol":
        print("[cap] FAIL: worker not pure-go protocol")
        return 2
    if not worker_supports_batches({}):
        print("[cap] FAIL: worker missing email-register-batches")
        return 2

    # Keep multi-region CSV intact (Go bulk spreads + remint rotates).
    primary_region = region.split(",")[0].strip() or "JP"
    config: dict[str, Any] = {
        "email_protocol_backend": "go",
        "go_email_protocol_mode": "pure",
        "go_email_protocol_transport": "tls",
        "mailbox_provider": "outlook_token",
        "mailat_protocol_skip_phone": True,
        "email_otp_timeout": otp_timeout,
        "go_batch_timeout_seconds": otp_timeout + 90,
        "proxy_seed_styles": str(args.styles or "bestgo").strip() or "bestgo",
        "proxy_region": region,
        "proxy_regions": region,
        "lajiao_proxy_expected_country": primary_region,
        "lajiao_proxy_regions": region,
        "max_register_tasks": concurrent,
        "email_tries": 5,
    }
    run_id = f"cap_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    started = time.time()
    started_utc = datetime.now(timezone.utc)
    deadline = started + window_s

    # Track open batches: batch_id -> last snapshot
    batches: dict[str, dict[str, Any]] = {}
    # Cumulative terminals attributed to this run (from batch snapshots deltas)
    prev_ok: dict[str, int] = {}
    prev_fail: dict[str, int] = {}
    total_ok = 0
    total_fail = 0
    total_submitted = 0
    success_times: deque[float] = deque()
    peak_in_flight = 0
    peak_go_active = 0
    samples: list[dict[str, Any]] = []
    last_print = 0.0
    wave = 0

    def in_flight() -> int:
        n = 0
        for bid, snap in batches.items():
            c = int(snap.get("count") or 0)
            ok = int(snap.get("succeeded") or 0)
            fail = int(snap.get("failed") or 0)
            canc = int(snap.get("cancelled") or 0)
            n += max(0, c - ok - fail - canc)
        return n

    def submit(count: int) -> None:
        nonlocal wave, total_submitted
        if count <= 0:
            return
        wave += 1
        bid = f"{run_id}_w{wave}"
        view = start_go_registration_batch(
            count=count,
            config=config,
            batch_id=bid,
            max_concurrent=min(concurrent, count),
        )
        batches[bid] = view
        prev_ok[bid] = int(view.get("succeeded") or 0)
        prev_fail[bid] = int(view.get("failed") or 0)
        total_submitted += int(view.get("count") or count)
        print(
            f"[cap] submit wave={wave} batch={bid} count={view.get('count')} "
            f"max_c={view.get('max_concurrent')} total_submitted={total_submitted}"
        )

    # Staged prime: never dump full concurrent in one shot at 100+.
    # 200 simultaneous S0 under TUN/fake-ip collapses proxy (go_active→0, fail storm).
    prime_left = concurrent
    if concurrent > 80:
        first = min(60, concurrent)
    elif concurrent > 40:
        first = min(40, concurrent)
    else:
        first = concurrent
    submit(first)
    prime_left -= first
    pause_refill_until = 0.0
    last_fail_snapshot = 0
    last_ok_snapshot = 0

    while time.time() < deadline:
        now = time.time()
        # Refresh batch snapshots and accumulate new terminals.
        for bid in list(batches.keys()):
            try:
                snap = get_go_registration_batch(bid, config)
            except Exception as exc:
                print(f"[cap] poll warn {bid}: {exc}")
                continue
            batches[bid] = snap
            ok = int(snap.get("succeeded") or 0)
            fail = int(snap.get("failed") or 0)
            d_ok = max(0, ok - prev_ok.get(bid, 0))
            d_fail = max(0, fail - prev_fail.get(bid, 0))
            if d_ok:
                total_ok += d_ok
                for _ in range(d_ok):
                    success_times.append(now)
            if d_fail:
                total_fail += d_fail
            prev_ok[bid] = ok
            prev_fail[bid] = fail

        while success_times and now - success_times[0] > 600:
            success_times.popleft()
        ok_1m = sum(1 for t in success_times if now - t <= 60)
        flight = in_flight()
        peak_in_flight = max(peak_in_flight, flight)
        free = max(0, concurrent - flight)

        active = -1
        try:
            h2 = check_go_email_protocol_health(
                {"go_email_protocol_mode": "pure", "go_email_protocol_transport": "tls"}
            )
            active = int(h2.get("active_count") or 0)
            peak_go_active = max(peak_go_active, active)
            go_line = f"go_active={active}/{h2.get('max_active')}"
        except Exception as exc:
            go_line = f"go_health_err={exc}"

        # Circuit breaker: stop feeding when proxy path is dead or fail storm.
        # Window = since last print sample (~15s) using cumulative deltas.
        d_fail_win = total_fail - last_fail_snapshot
        d_ok_win = total_ok - last_ok_snapshot
        storm = False
        if now - started > 45:
            if d_fail_win >= 40 and d_ok_win <= 5:
                storm = True
            if active == 0 and free >= max(20, concurrent // 4) and d_fail_win >= 20:
                storm = True
            if total_fail > 0 and total_ok > 0 and total_fail > total_ok * 3 and d_fail_win >= 30:
                storm = True
            if total_ok == 0 and total_fail >= 50:
                storm = True
        if storm:
            pause_refill_until = max(pause_refill_until, now + 45)
            prime_left = 0
            if now - last_print >= 5:
                print(
                    f"[cap] BREAKER pause_refill 45s "
                    f"d_ok={d_ok_win} d_fail={d_fail_win} active={active} free={free} "
                    f"total_ok={total_ok} total_fail={total_fail}"
                )

        # Finish staged prime after first wave has some protocol activity.
        if prime_left > 0 and now >= pause_refill_until and now - started >= 20:
            if active >= min(20, concurrent // 4) or total_ok + total_fail >= first // 3:
                chunk = min(prime_left, max(40, concurrent // 4))
                try:
                    submit(chunk)
                    prime_left -= chunk
                except Exception as exc:
                    print(f"[cap] prime refill failed: {exc}")

        # Continuous refill (only when not in breaker).
        if free > 0 and time.time() < deadline - 5 and now >= pause_refill_until and prime_left <= 0:
            chunk = int(args.refill_chunk or 0)
            if chunk <= 0:
                # Cap auto refill so we don't re-burst 100 after a collapse.
                chunk = min(free, max(20, min(40, concurrent // 5)))
            else:
                chunk = min(chunk, free)
            if free >= concurrent // 2 and not storm:
                chunk = min(free, max(chunk, min(60, concurrent // 3)))
            try:
                submit(chunk)
            except Exception as exc:
                print(f"[cap] refill failed: {exc}")

        elapsed = now - started
        if now - last_print >= 15 or elapsed < 2:
            last_print = now
            last_fail_snapshot = total_fail
            last_ok_snapshot = total_ok
            rate_per_min = total_ok * 60.0 / max(1.0, elapsed)
            proj_10m = total_ok * 600.0 / max(1.0, elapsed)
            print(
                f"[cap] t={int(elapsed)}s ok={total_ok} fail={total_fail} "
                f"in_flight={flight}/{concurrent} free={free} "
                f"rate_1m={ok_1m}/min avg={rate_per_min:.1f}/min proj_10m={proj_10m:.0f} "
                f"submitted={total_submitted} waves={wave} prime_left={prime_left} {go_line}"
            )
            samples.append(
                {
                    "t": int(elapsed),
                    "ok": total_ok,
                    "fail": total_fail,
                    "in_flight": flight,
                    "rate_1m": ok_1m,
                    "avg_per_min": round(rate_per_min, 2),
                    "proj_10m": round(proj_10m, 1),
                    "go_active": active,
                }
            )
        time.sleep(1)

    # Final poll
    now = time.time()
    for bid in list(batches.keys()):
        try:
            snap = get_go_registration_batch(bid, config)
            batches[bid] = snap
            ok = int(snap.get("succeeded") or 0)
            fail = int(snap.get("failed") or 0)
            d_ok = max(0, ok - prev_ok.get(bid, 0))
            d_fail = max(0, fail - prev_fail.get(bid, 0))
            total_ok += d_ok
            total_fail += d_fail
            for _ in range(d_ok):
                success_times.append(now)
            prev_ok[bid] = ok
            prev_fail[bid] = fail
        except Exception:
            pass

    ended_utc = datetime.now(timezone.utc)
    fail_breakdown = _ledger_failure_breakdown(started_utc=started_utc, ended_utc=ended_utc)

    elapsed = max(1.0, time.time() - started)
    summary = {
        "at": ended_utc.isoformat(),
        "run_id": run_id,
        "minutes": minutes,
        "concurrent": concurrent,
        "worker_max_active_want": seat_want,
        "worker_max_active_got": running_max,
        "proxy_region": region,
        "otp_timeout": otp_timeout,
        "elapsed_s": int(elapsed),
        "submitted": total_submitted,
        "ok": total_ok,
        "fail": total_fail,
        "in_flight_end": in_flight(),
        "peak_in_flight": peak_in_flight,
        "peak_go_active": peak_go_active,
        "throughput_per_min": round(total_ok * 60.0 / elapsed, 2),
        "throughput_per_10min": round(total_ok * 600.0 / elapsed, 1),
        "successes_in_last_10min_window": len([t for t in success_times if now - t <= 600]),
        "waves": wave,
        "failure_breakdown": fail_breakdown,
        "batches": {
            bid: {
                k: snap.get(k)
                for k in (
                    "count",
                    "succeeded",
                    "failed",
                    "cancelled",
                    "running",
                    "waiting_for_otp",
                    "protocol_active",
                    "done",
                )
            }
            for bid, snap in batches.items()
        },
        "samples": samples,
        "worker": health,
        "path": "capacity:continuous_refill_10min",
    }
    out = Path(args.out) if args.out else ROOT / "output" / f"{run_id}_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[cap] summary {out}")
    if fail_breakdown.get("by_failure_code"):
        print(f"[cap] failure_codes {fail_breakdown['by_failure_code']}")
    if fail_breakdown.get("error_patterns"):
        print(f"[cap] error_patterns {fail_breakdown['error_patterns']}")
    print(
        f"[cap] RESULT ok={total_ok} fail={total_fail} elapsed={int(elapsed)}s "
        f"rate={summary['throughput_per_min']}/min ~{summary['throughput_per_10min']}/10min "
        f"region={region} concurrent={concurrent} max_active={running_max}"
    )
    return 0 if total_ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
