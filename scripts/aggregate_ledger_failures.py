#!/usr/bin/env python3
"""Aggregate go-email-protocol ledger failures by code + transport pattern.

Usage:
  py -3.13 scripts/aggregate_ledger_failures.py --minutes 15
  py -3.13 scripts/aggregate_ledger_failures.py --start 2026-07-26T01:39:00 --end 2026-07-26T01:52:00
  py -3.13 scripts/aggregate_ledger_failures.py --summary output/cap_xxx_summary.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "data" / "go-email-protocol-ledger.db"


def _parse_iso(s: str) -> datetime:
    s = (s or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def aggregate(
    *,
    ledger: Path,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    start_s = start.strftime("%Y-%m-%dT%H:%M:%S")
    end_s = end.strftime("%Y-%m-%dT%H:%M:%S")
    out: dict[str, Any] = {
        "ledger": str(ledger),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "by_status": {},
        "by_failure_code": {},
        "error_patterns": {},
        "sample_errors": [],
        "failed_total": 0,
        "jobs_in_window": 0,
    }
    if not ledger.is_file():
        out["error"] = "ledger_missing"
        return out
    conn = sqlite3.connect(f"file:{ledger.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT status, COALESCE(failure_code, ''), COALESCE(result_json, ''),
                   COALESCE(stage, ''), created_at
            FROM jobs
            WHERE created_at >= ? AND created_at <= ?
            ORDER BY created_at
            """,
            (start_s, end_s + "Z"),
        ).fetchall()
        if not rows:
            rows = cur.execute(
                """
                SELECT status, COALESCE(failure_code, ''), COALESCE(result_json, ''),
                       COALESCE(stage, ''), created_at
                FROM jobs
                WHERE created_at >= ? AND created_at <= ?
                ORDER BY created_at
                """,
                (start_s, end_s),
            ).fetchall()
        status_c: Counter[str] = Counter()
        fail_c: Counter[str] = Counter()
        pat_c: Counter[str] = Counter()
        samples: list[dict[str, str]] = []
        for status, fcode, raw, stage, created in rows:
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
                pat = "http_to_https"
            elif "wsarecv" in low or "198.18." in low:
                pat = "wsarecv_or_fakeip"
            elif "goaway" in low or "http2:" in low:
                pat = "http2_goaway"
            elif "otp" in low or code.startswith("otp_"):
                pat = "otp"
            elif "already" in low or code == "email_already_used":
                pat = "email_used"
            elif "edge" in low or "challenge" in low:
                pat = "edge_challenge"
            else:
                pat = f"other:{code}"
            pat_c[pat] += 1
            if len(samples) < 20:
                samples.append(
                    {
                        "created_at": str(created or ""),
                        "failure_code": code,
                        "stage": str(stage or ""),
                        "pattern": pat,
                        "error": msg[:240],
                    }
                )
        out["jobs_in_window"] = sum(status_c.values())
        out["by_status"] = dict(status_c.most_common())
        out["by_failure_code"] = dict(fail_c.most_common())
        out["error_patterns"] = dict(pat_c.most_common())
        out["failed_total"] = sum(fail_c.values())
        out["sample_errors"] = samples
    finally:
        conn.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", type=str, default=str(DEFAULT_LEDGER))
    ap.add_argument("--minutes", type=float, default=0, help="lookback minutes from now (UTC)")
    ap.add_argument("--start", type=str, default="")
    ap.add_argument("--end", type=str, default="")
    ap.add_argument("--summary", type=str, default="", help="capacity summary json → window from at-elapsed")
    ap.add_argument("--out", type=str, default="")
    args = ap.parse_args()

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=15)
    if args.summary:
        p = Path(args.summary)
        if not p.is_file():
            print(f"summary missing: {p}", file=sys.stderr)
            return 2
        d = json.loads(p.read_text(encoding="utf-8"))
        end = _parse_iso(str(d.get("at") or end.isoformat()))
        elapsed = int(d.get("elapsed_s") or float(d.get("minutes") or 10) * 60)
        start = end - timedelta(seconds=max(60, elapsed + 30))
    if args.end:
        end = _parse_iso(args.end)
    if args.start:
        start = _parse_iso(args.start)
    elif float(args.minutes or 0) > 0:
        start = end - timedelta(minutes=float(args.minutes))

    report = aggregate(ledger=Path(args.ledger), start=start, end=end)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(out)
    print(text)
    print(
        f"[agg] jobs={report.get('jobs_in_window')} failed={report.get('failed_total')} "
        f"codes={report.get('by_failure_code')} patterns={report.get('error_patterns')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
