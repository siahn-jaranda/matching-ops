"""배포 전/후 대칭 구간 비교 리포트.

- POST /api/reports/ab-daily — 앵커(배포 시각) 기준 같은 길이의 앞/뒤 구간을 비교해
  Slack(n8n 릴레이)으로 발송. Cloud Scheduler 가 매일 13:00 KST 호출한다.
  인증은 auto_dispatch 와 동일 (X-Trigger-Secret 또는 세션).

비교 구간 = min(앵커 이후 경과시간, AB_REPORT_DAYS 일).
앞뒤 길이를 항상 같게 맞춰야 1:1 비교가 된다. D+1 처럼 경과가 하루 미만이면
양쪽 모두 그 경과시간만큼만 본다.

AB_REPORT_DAYS 를 넘기면 마지막 1회만 '최종 요약'으로 보내고 이후는 무발송.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends

from src.auto_run_store import auto_run_available, get_auto_run_store
from src.config import settings
from src.db import get_replica
from src.routes.auto_dispatch import trigger_auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reports", tags=["reports"])

KST = timezone(timedelta(hours=9))

def _anchor() -> datetime:
    raw = settings.ab_report_anchor_kst.strip()
    return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


async def _window_stats(store, replica, start: datetime, end: datetime) -> dict[str, Any]:
    """구간 [start, end) 의 실행 지표 + 성과.

    성과는 end 시점까지 생성된 행만 센다 — 전/후 구간의 관측 시간을 동일하게 맞추려는 것.
    """
    metrics, sids = await store.window_stats(start, end)
    metrics["sids"] = len(sids)
    metrics.update(await replica.outcome_stats(sids, until=end.replace(tzinfo=None)))
    return metrics


def _pct(a: float | int | None, b: float | int | None) -> float | None:
    if not b:
        return None
    return round(100.0 * (a or 0) / b, 2)


def _fmt(v: Any) -> str:
    return "-" if v is None else str(v)


def _delta(after: Any, before: Any, unit: str, higher_better: bool) -> str:
    if after is None or before is None:
        return "   -"
    d = float(after) - float(before)
    if abs(d) < 1e-9:
        return "   = 0"
    good = (d > 0) == higher_better
    return "%s %+.1f%s%s" % ("▲" if d > 0 else "▼", d, unit, "" if good else " ⚠")


def _build_text(day_n: int, window: timedelta, bs: datetime, be: datetime,
                as_: datetime, ae: datetime, b: dict, a: dict, final: bool) -> str:
    hrs = window.total_seconds() / 3600
    win = ("%.1f시간" % hrs) if hrs < 48 else ("%.1f일" % (hrs / 24))

    rows = [
        ("실행 건수", b["runs"], a["runs"], "", True),
        ("성공 건수", b["ok"], a["ok"], "", True),
        ("성공률(%)", _pct(b["ok"], b["runs"]), _pct(a["ok"], a["runs"]), "%p", True),
        ("풀 중앙값", b["pool_p50"], a["pool_p50"], "명", True),
        ("건당 추가", b["avg_added"], a["avg_added"], "명", True),
        ("제안 발송", b["offered"], a["offered"], "명", True),
        ("수락률(%)", _pct(b["accepted"], b["offered"]), _pct(a["accepted"], a["offered"]), "%p", True),
        ("거절률(%)", _pct(b["rejected"], b["offered"]), _pct(a["rejected"], a["offered"]), "%p", False),
        ("매칭률(%)", _pct(b["matched"], b["apps"]), _pct(a["matched"], a["apps"]), "%p", True),
        ("필터전멸(%)", _pct(b["empty_filter"], b["runs"]), _pct(a["empty_filter"], a["runs"]), "%p", False),
    ]
    lines = ["%-12s %9s %9s   %s" % ("지표", "배포 전", "배포 후", "변화"), "-" * 50]
    for name, bv, av, unit, hb in rows:
        lines.append("%-12s %9s %9s   %s" % (name, _fmt(bv), _fmt(av), _delta(av, bv, unit, hb)))

    n = min(a["runs"], b["runs"])
    if n < 30:
        note = ("⚠️ 표본 %d건 — 일별 풀 중앙값이 원래 2~13으로 흔들려 아직 노이즈와 구분 불가." % n)
    elif n < 100:
        note = "표본 %d건 — 방향은 보이나 확정은 이릅니다." % n
    else:
        note = "표본 %d건 — 판단 가능한 수준입니다." % n

    head = "*matching-ops 배포 전/후 비교 — %s*" % ("최종 (D+%d)" % day_n if final else "D+%d" % day_n)
    body = (
        "%s\n"
        "_앵커 %s KST · rev 00069 조건완화 + 00070 A1제거_\n"
        "_비교 구간 각 %s — 전 %s~%s / 후 %s~%s_\n\n"
        "```\n%s\n```\n%s"
    ) % (head, _anchor().strftime("%Y-%m-%d %H:%M"), win,
         bs.strftime("%m-%d %H:%M"), be.strftime("%m-%d %H:%M"),
         as_.strftime("%m-%d %H:%M"), ae.strftime("%m-%d %H:%M"),
         "\n".join(lines), note)
    if final:
        body += "\n\n_%d일 관찰 종료. 이후 자동 발송은 없습니다._" % settings.ab_report_days
    body += (
        "\n\n_수락률·거절률은 방문제안을 받은 선생님(suggested=1) 대비, 매칭률은 처리 신청서 대비._"
        " _최근 구간일수록 매칭 결과가 아직 확정되지 않아 낮게 나올 수 있습니다._"
    )
    return body


@router.post("/ab-daily")
async def ab_daily(user: dict = Depends(trigger_auth)) -> dict[str, Any]:
    if not auto_run_available():
        return {"status": "skipped", "reason": "MATCHING_OPS_DB_URL 미설정"}

    anchor = _anchor()
    now = datetime.now(KST)
    elapsed = now - anchor
    if elapsed.total_seconds() <= 0:
        return {"status": "skipped", "reason": "anchor_in_future"}

    limit = timedelta(days=settings.ab_report_days)
    day_n = elapsed.days + 1
    over = elapsed > limit + timedelta(days=1)
    if over:
        return {"status": "skipped", "reason": "past_report_window", "day": day_n}
    final = elapsed > limit

    window = min(elapsed, limit)
    bs, be = anchor - window, anchor
    as_, ae = anchor, min(anchor + window, now)

    store, replica = get_auto_run_store(), get_replica()
    b = await _window_stats(store, replica, bs, be)
    a = await _window_stats(store, replica, as_, ae)
    body = _build_text(day_n, window, bs, be, as_, ae, b, a, final)

    sent = False
    url = settings.ab_report_webhook.strip()
    target = settings.ab_report_slack_target.strip()
    if url:
        payload: dict[str, Any] = {"text": body}
        if target:
            payload["channel"] = target
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.post(url, json=payload)
            sent = res.status_code < 400
            if not sent:
                logger.error("ab_daily webhook %s %s", res.status_code, res.text[:200])
        except Exception:
            logger.exception("ab_daily webhook post failed")

    logger.info("ab_daily day=%d final=%s sent=%s before=%s after=%s",
                day_n, final, sent, json.dumps(b, default=str), json.dumps(a, default=str))
    return {"status": "ok", "day": day_n, "final": final, "sent": sent,
            "window_hours": round(window.total_seconds() / 3600, 2),
            "before": b, "after": a, "text": body}
