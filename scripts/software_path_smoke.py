#!/usr/bin/env python3
"""Software-path smoke: thin Python control plane → Go registration batch.

Prefers POST /v2/email-register-batches (no N Python inline pipelines).

Usage:
  py -3.13 scripts/software_path_smoke.py --n 100 --region JP,US,DE,GB,BR --timeout 900
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--out", type=str, default="")
    ap.add_argument("--region", type=str, default="JP,US,DE,GB,BR", help="proxy region(s), comma multi-region ok")
    ap.add_argument("--otp-timeout", type=int, default=120, help="OTP total wait seconds (default 120)")
    ap.add_argument("--max-concurrent", type=int, default=0, help="0 = min(n, max_register_tasks)")
    args = ap.parse_args()

    import start
    from application.tasks_service import TasksService
    from services.go_email_protocol_runner import check_go_email_protocol_health

    print("[smoke] ensure go worker…")
    start.ensure_go_worker()
    health = check_go_email_protocol_health(
        {"go_email_protocol_mode": "pure", "go_email_protocol_transport": "tls"}
    )
    print(
        f"[smoke] worker runner={health.get('runner')} mode={health.get('protocol_mode')} "
        f"transport={health.get('transport')} active={health.get('active_count')}/{health.get('max_active')}"
    )
    if str(health.get("runner") or "") != "protocol":
        print("[smoke] FAIL: worker not pure-go protocol")
        return 2

    ts = TasksService()
    ts.reload_limits()
    ts.ensure_reconcile_loop()

    n = max(1, int(args.n))
    region = str(args.region or "JP,US,DE,GB,BR").strip().upper() or "JP,US,DE,GB,BR"
    otp_timeout = max(60, min(360, int(args.otp_timeout or 180)))
    max_c = int(args.max_concurrent or 0)
    if max_c <= 0:
        try:
            max_c = int(ts.bucket_limits.get("register") or 200)
        except Exception:
            max_c = 200
    max_c = max(1, min(max_c, n, 200))

    batch_id = f"software_smoke_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    overrides = {
        "batch_id": batch_id,
        "email_protocol_backend": "go",
        "go_email_protocol_mode": "pure",
        "go_email_protocol_transport": "tls",
        "email_protocol_spawn_mode": "inline",
        "mailbox_provider": "outlook_token",
        "mailat_protocol_skip_phone": True,
        "email_otp_timeout": otp_timeout,
        "go_batch_timeout_seconds": otp_timeout + 90,
        "go_batch_required": "1",
        "proxy_seed_styles": "bestgo,1024",
        "proxy_region": region,
        "proxy_regions": region,
        "lajiao_proxy_expected_country": region.split(",")[0].strip() or "JP",
        "lajiao_proxy_regions": region,
        "max_register_tasks": max_c,
        "email_tries": 5,
    }
    print(
        f"[smoke] create n={n} concurrent={max_c} region={region} otp={otp_timeout}s "
        f"batch_id={batch_id} (Go continuous pipeline)"
    )
    started = time.time()
    created = ts.start_email_protocol_register_many({"config": "config.yaml"}, overrides, n)
    print(f"[smoke] create_many returned {created}")

    from services.go_registration_batch import get_go_registration_batch

    task_ids: list[str] = []
    try:
        view0 = get_go_registration_batch(batch_id, overrides)
        raw_ids = view0.get("task_ids") if isinstance(view0, dict) else None
        if isinstance(raw_ids, list):
            task_ids = [str(x) for x in raw_ids if str(x or "").strip()]
        print(
            f"[smoke] go batch snapshot count={view0.get('count')} "
            f"queued={view0.get('queued')} running={view0.get('running')} "
            f"otp={view0.get('waiting_for_otp')} proto={view0.get('protocol_active')} "
            f"ok={view0.get('succeeded')} fail={view0.get('failed')} done={view0.get('done')}"
        )
    except Exception as exc:
        print(f"[smoke] go batch status warn: {exc}")

    if len(task_ids) < min(n, int(created or n)):
        for _ in range(15):
            try:
                view = get_go_registration_batch(batch_id, overrides)
                raw = view.get("task_ids") if isinstance(view, dict) else None
                if isinstance(raw, list) and raw:
                    task_ids = [str(x) for x in raw if str(x or "").strip()]
            except Exception:
                pass
            if len(task_ids) >= min(n, int(created or n)):
                break
            time.sleep(0.25)
    print(f"[smoke] tracking {len(task_ids)} task ids")
    for tid in task_ids[:8]:
        print(f"  - {tid}")

    terminal = {"succeeded", "failed", "cancelled", "interrupted"}
    deadline = started + max(60, int(args.timeout))
    last_print = 0.0
    # Rolling success timestamps for 1min / 10min rate (KPI, not batch wall).
    success_times: deque[float] = deque()
    last_ok = 0
    peak_go_active = 0
    samples: list[dict] = []

    while time.time() < deadline:
        status_ctr: Counter[str] = Counter()
        done = 0
        batch_done = False
        go_ok = go_fail = 0
        try:
            bview = get_go_registration_batch(batch_id, overrides)
            batch_done = bool(bview.get("done"))
            go_ok = int(bview.get("succeeded") or 0)
            go_fail = int(bview.get("failed") or 0)
            status_ctr["go_succeeded"] = go_ok
            status_ctr["go_failed"] = go_fail
            status_ctr["go_running"] = int(bview.get("running") or 0)
            status_ctr["go_waiting_otp"] = int(bview.get("waiting_for_otp") or 0)
            status_ctr["go_protocol"] = int(bview.get("protocol_active") or 0)
            status_ctr["go_queued"] = int(bview.get("queued") or 0)
            status_ctr["go_cancelled"] = int(bview.get("cancelled") or 0)
            if not task_ids:
                raw = bview.get("task_ids") if isinstance(bview.get("task_ids"), list) else []
                task_ids = [str(x) for x in raw if str(x or "").strip()]
        except Exception as exc:
            status_ctr["go_status_err"] = 1
            print(f"[smoke] go batch poll warn: {exc}")

        now = time.time()
        if go_ok > last_ok:
            for _ in range(go_ok - last_ok):
                success_times.append(now)
            last_ok = go_ok
        while success_times and now - success_times[0] > 600:
            success_times.popleft()
        ok_1m = sum(1 for t in success_times if now - t <= 60)
        ok_10m = len(success_times)
        rate_1m = ok_1m  # successes in last 60s
        rate_10m_proj = ok_10m * (600.0 / max(1.0, min(600.0, now - started))) if now > started else 0.0

        for tid in task_ids:
            try:
                t = ts.get_task(tid)
                st = str(t.get("status") or "")
            except Exception:
                st = "missing"
            status_ctr[st] += 1
            if st in terminal:
                done += 1

        try:
            h2 = check_go_email_protocol_health(
                {"go_email_protocol_mode": "pure", "go_email_protocol_transport": "tls"}
            )
            active = int(h2.get("active_count") or 0)
            peak_go_active = max(peak_go_active, active)
            go_line = f"go_active={active}/{h2.get('max_active')}"
        except Exception as exc:
            go_line = f"go_health_err={exc}"

        if now - last_print >= 15 or batch_done:
            last_print = now
            elapsed = max(1.0, now - started)
            print(
                f"[smoke] t={int(elapsed)}s batch_done={batch_done} "
                f"ok={go_ok} fail={go_fail} terminal={done}/{len(task_ids)} "
                f"rate_1m={rate_1m}/min proj_10m={rate_10m_proj:.0f} "
                f"status={dict(status_ctr)} {go_line}"
            )
            samples.append(
                {
                    "t": int(elapsed),
                    "ok": go_ok,
                    "fail": go_fail,
                    "waiting_otp": status_ctr.get("go_waiting_otp", 0),
                    "protocol": status_ctr.get("go_protocol", 0),
                    "go_active": peak_go_active,
                    "rate_1m": rate_1m,
                }
            )
        if batch_done:
            break
        time.sleep(3)

    outcomes = []
    for tid in task_ids:
        try:
            t = ts.get_task(tid)
        except Exception as exc:
            outcomes.append({"id": tid, "status": "missing", "error": str(exc)})
            continue
        result = t.get("result") if isinstance(t.get("result"), dict) else {}
        outcomes.append(
            {
                "id": tid,
                "status": t.get("status"),
                "message": str(t.get("error") or result.get("message") or result.get("error") or "")[:240],
            }
        )

    ledger_stats: dict = {}
    ledger_path = ROOT / "data" / "go-email-protocol-ledger.db"
    if ledger_path.is_file():
        c = sqlite3.connect(str(ledger_path))
        since = datetime.fromtimestamp(started - 5, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        stages = list(
            c.execute(
                "select stage, failure_code, count(*) from jobs "
                "where updated_at>=? group by 1,2 order by 3 desc",
                (since,),
            )
        )
        ledger_stats = {"since": since, "by_stage_failure": stages}
        c.close()

    ok = sum(1 for o in outcomes if o.get("status") == "succeeded")
    fail = sum(1 for o in outcomes if o.get("status") == "failed")
    try:
        final_batch = get_go_registration_batch(batch_id, overrides)
        if final_batch.get("done"):
            ok = int(final_batch.get("succeeded") or ok)
            fail = int(final_batch.get("failed") or fail)
    except Exception:
        final_batch = {}

    elapsed = max(1, int(time.time() - started))
    summary = {
        "at": datetime.now(timezone.utc).isoformat(),
        "batch_id": batch_id,
        "n": n,
        "max_concurrent": max_c,
        "proxy_region": region,
        "otp_timeout": otp_timeout,
        "tracked": len(task_ids),
        "ok": ok,
        "fail": fail,
        "other": max(0, len(task_ids) - ok - fail),
        "throughput_per_min": round(ok * 60.0 / elapsed, 2),
        "throughput_per_10min": round(ok * 600.0 / elapsed, 1),
        "peak_go_active": peak_go_active,
        "samples": samples,
        "worker": health,
        "go_batch": {
            k: final_batch.get(k)
            for k in (
                "count",
                "max_concurrent",
                "succeeded",
                "failed",
                "cancelled",
                "waiting_for_otp",
                "protocol_active",
                "done",
                "created_at",
                "finished_at",
            )
        }
        if final_batch
        else {},
        "outcomes": outcomes,
        "ledger": ledger_stats,
        "elapsed_s": elapsed,
        "path": "software:go_batch_continuous_pipeline",
    }
    out_path = Path(args.out) if args.out else ROOT / "output" / f"{batch_id}_summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[smoke] summary {out_path}")
    print(
        f"[smoke] RESULT ok={ok} fail={fail} other={summary['other']} elapsed={elapsed}s "
        f"rate={summary['throughput_per_min']}/min ~{summary['throughput_per_10min']}/10min "
        f"region={region} otp={otp_timeout}s"
    )
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
