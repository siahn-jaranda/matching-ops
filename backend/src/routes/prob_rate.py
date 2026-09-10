"""예상 매칭확률 비율표 조회·갱신·회귀 감시.

- POST /api/prob-rate/refresh — 리플리카 정착 코호트로 표를 다시 계산해 PG 에 기록.
  Cloud Scheduler 가 매일 04:20 KST 호출한다. 인증은 auto_dispatch 와 동일
  (X-Trigger-Secret 또는 세션).
- POST /api/prob-rate/audit — 회귀 감시 백테스트. 월 1회.
  슬랙 발송은 body {"notify": true} 일 때만 — 수동 호출이 팀 채널을 울리지 않게.
- GET  /api/prob-rate — 현재 표. 플래너·운영자가 "이 확률의 근거 표본이 몇 건인지"를
  확인할 수 있어야 해서 n·raw_rate 를 같이 노출한다.

갱신 주기가 중요하다. 2026-07-02 자동 디스패치 V0 100% 승격 직후
`재이용·응답0` 셀의 실제 매칭률이 12.97% → 19.23% 로 뛰었는데, 학습창을 6개월로
고정하면 이런 개입을 몇 달 동안 못 따라간다. 롤링 90일 + 일 1회 갱신이 기본값인 이유.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends

from src.config import settings
from src.db import get_replica
from src.prob_rate_store import (
    SEGS,
    build_cells,
    get_prob_rate_store,
    prob_rate_available,
    score_table,
)
from src.routes.auto_dispatch import trigger_auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/prob-rate", tags=["prob-rate"])


@router.post("/refresh")
async def refresh(user: dict = Depends(trigger_auth)) -> dict[str, Any]:
    if not prob_rate_available():
        return {"status": "skipped", "reason": "MATCHING_OPS_DB_URL 미설정"}

    window = settings.prob_rate_window_days
    settle = settings.prob_rate_settle_days
    raw = await get_replica().prob_rate_cohort(start_days_ago=window, end_days_ago=settle)
    if not raw:
        # 표를 비우지 않는다 — 코호트가 비면 직전 표를 그대로 두는 편이 안전하다.
        logger.warning("prob_rate 코호트가 비어 표를 갱신하지 않음 window=%d settle=%d",
                       window, settle)
        return {"status": "skipped", "reason": "empty_cohort",
                "window_days": window, "settle_days": settle}

    cells = build_cells(raw)
    written = await get_prob_rate_store().replace_all(cells, window)
    total_n = sum(c["n"] for c in cells if c["reuse"] == -1)
    total_m = sum(c["matched"] for c in cells if c["reuse"] == -1)
    thin = [f'{c["seg"]}/{c["age_bucket"]}/{c["reuse"]}{c["responder"]}'
            for c in cells if c["reuse"] != -1 and c["n"] < 100]
    logger.info("prob_rate refreshed cells=%d cohort_n=%d matched=%d thin=%d",
                written, total_n, total_m, len(thin))
    return {"status": "ok", "cells": written, "window_days": window,
            "settle_days": settle, "cohort_n": total_n, "cohort_matched": total_m,
            "thin_cells": thin}


@router.get("")
async def get_table(user: dict = Depends(trigger_auth)) -> dict[str, Any]:
    if not prob_rate_available():
        return {"status": "skipped", "reason": "MATCHING_OPS_DB_URL 미설정"}
    table = await get_prob_rate_store().load()
    rows = [
        {"seg": k[0], "age_bucket": k[1], "reuse": k[2], "responder": k[3],
         "rate": v[0], "n": v[1]}
        for k, v in sorted(table.items())
    ]
    return {"status": "ok", "count": len(rows), "rows": rows}


_CELL_LABEL = {(0, 0): "신규·응답0", (0, 1): "신규·응답1↑",
               (1, 0): "재이용·응답0", (1, 1): "재이용·응답1↑"}


def _audit_text(res: dict[str, Any], fit: tuple[int, int], test: tuple[int, int],
                breached: bool) -> str:
    thr = settings.prob_rate_audit_mae_threshold
    head = ("🚨 *matching-ops 매칭확률 회귀 감지*" if breached
            else "✅ *matching-ops 매칭확률 회귀 감시 — 정상*")

    lines = ["%-22s %7s %7s %8s %8s %5s"
             % ("셀", "예측", "실제", "오차", "노이즈±", "n"), "-" * 62]
    for r in res["rows"][:12]:
        tag = "%s/%s/%s" % (r["seg"], r["age_bucket"],
                            _CELL_LABEL.get((r["reuse"], r["responder"]), "?"))
        # 오차가 그 셀의 표본오차 안이면 모델 탓으로 볼 수 없다
        flag = " ⚠" if (r["err"] > thr and r["signal"]) else ("" if r["signal"] else " ~")
        lines.append("%-22s %6.1f%% %6.1f%% %6.1f%%p %6.1f%%p %5d%s"
                     % (tag[:22], r["predicted"], r["actual"], r["err"],
                        r["noise"], r["n"], flag))

    over = [r for r in res["rows"] if r["err"] > thr and r["signal"]]
    by_seg = " · ".join("%s %s%%p(n=%d)" % (k, v["mae"], v["n"])
                        for k, v in res["by_seg"].items())
    # 채점 셀이 하나도 없는 세그먼트를 침묵시키지 않는다 — 감시 공백은 드러나야 한다
    uncovered = [g for g in SEGS if g not in res["by_seg"]]
    if uncovered:
        by_seg += "\n⚠️ *미채점 세그먼트 — %s* (채점 표본 %d건 미만이라 이번 회차 감시 밖)" % (
            ", ".join(uncovered), settings.prob_rate_audit_min_cell_n)

    body = (
        "%s\n"
        "_백테스트 — 학습 %d~%d일 전 구간으로 표를 재현해, 채점 %d~%d일 전 구간 실제값과 대조_\n"
        "*가중 MAE %s%%p* (임계 %s%%p) · 셀 %d개 · 표본 %s건\n"
        "세그먼트별 — %s\n"
        "임계 초과 셀 %d개(노이즈 제외) / 노이즈 초과 %d개 / 표본 부족 제외 %d개\n\n"
        "```\n%s\n```"
    ) % (head, fit[0], fit[1], test[0], test[1], res["mae"], thr,
         res["cells"], format(res["n"], ","), by_seg,
         len(over), res["signal_cells"], res["skipped"],
         "\n".join(lines))

    if breached:
        body += ("\n_표가 최근 실제와 벌어졌습니다. 셀 정의나 학습창(현행 %d일)을 재검토하세요._"
                 % settings.prob_rate_window_days)
    else:
        body += ("\n_오차 상위 12셀. `~` = 그 셀의 95%% 표본오차(노이즈±) 안이라"
                 " 모델 오차로 볼 수 없음. 전체 표는 `GET /api/prob-rate`._")
    return body


@router.post("/audit")
async def audit(
    options: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(trigger_auth),
) -> dict[str, Any]:
    """회귀 감시 백테스트. Cloud Scheduler 가 월 1회 호출한다.

    현행 표를 그대로 채점하면 학습 구간과 채점 구간이 같아 오차가 0에 가깝게 나온다.
    그래서 **채점 구간 직전**의 window 길이 구간으로 표를 다시 만들어(=그 시점의 표를
    재현) 채점 구간 실제값과 대조한다 — "한 달 전 방식으로 만든 표가 지금 얼마나
    틀렸나". 이게 오늘 표의 향후 오차 추정치다.

    두 구간은 반드시 **겹치지 않아야** 한다. 겹치면 학습에 쓴 데이터로 채점하는 꼴이라
    오차가 실제보다 낮게 나온다. settle·window 에서 구간을 유도해 구조적으로 막는다.

    경보 여부와 무관하게 항상 보낸다. 침묵과 정상이 구분되지 않으면 감시가 아니다
    (2026-09-04 LLM 연속 실패를 아무도 모른 채 지나간 사고와 같은 실패 양식).

    단 슬랙 발송은 body 에 notify=true 를 준 호출에서만 한다. 기본이 발송이면
    개발·검증용 수동 호출이 그대로 팀 채널에 나간다 — 실제로 2026-09-07 오차를
    잡는 동안 #1-팀-운영 에 4번 나갔다. 스케줄러만 notify=true 를 보낸다.
    """
    # 요청 옵션은 맨 위에서 뽑는다 — 아래에서 지역변수 body(슬랙 본문)를 쓰기 때문에
    # 요청 파라미터를 그 이름으로 두면 섀도잉된다(실제로 그래서 500 이 났다).
    notify = bool(options.get("notify"))

    if not prob_rate_available():
        return {"status": "skipped", "reason": "MATCHING_OPS_DB_URL 미설정"}

    win, settle = settings.prob_rate_window_days, settings.prob_rate_settle_days
    t = settings.prob_rate_audit_test_days
    # 채점은 최신 정착 구간, 학습은 그 직전 window 길이 구간. 붙어 있을 뿐 겹치지 않는다.
    test_span = (settle + t, settle)
    fit_span = (settle + t + win, settle + t)

    # 두 구간은 붙어 있으므로 합쳐서 **한 번만** 뽑고 period 로 나눈다.
    # 따로 두 번 부르면 부모 이력 derived table(16만 행 스캔 → 4.9만 행)이 두 번
    # 만들어져 요청 타임아웃을 넘긴다. 동시 실행(gather)으로도 해결되지 않았다.
    rows = await get_replica().prob_rate_cohort(
        start_days_ago=fit_span[0], end_days_ago=test_span[1],
        split_days_ago=test_span[0],
    )
    fit_raw = [r for r in rows if r["period"] == "older"]
    test_raw = [r for r in rows if r["period"] == "newer"]
    if not fit_raw or not test_raw:
        return {"status": "skipped", "reason": "empty_cohort",
                "fit_cells": len(fit_raw), "test_cells": len(test_raw)}

    res = score_table(build_cells(fit_raw), test_raw, settings.prob_rate_audit_min_cell_n)
    if res["mae"] is None:
        return {"status": "skipped", "reason": "no_scorable_cells", "skipped": res["skipped"]}

    # MAE 가 임계를 넘고 **그 원인이 노이즈가 아닌** 셀이 있을 때만 경보.
    # 표본이 얇아 흔들린 것까지 경보하면 매달 늑대가 나타난다.
    breached = (res["mae"] > settings.prob_rate_audit_mae_threshold
                and res["signal_cells"] > 0)
    body = _audit_text(res, fit_span, test_span, breached)

    sent = False
    url = settings.ab_report_webhook.strip()
    if notify and url:
        payload: dict[str, Any] = {"text": body}
        target = settings.prob_rate_audit_slack_target.strip()
        if target:
            payload["channel"] = target
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, json=payload)
            sent = r.status_code < 400
            if not sent:
                logger.error("prob_rate audit webhook %s %s", r.status_code, r.text[:200])
        except Exception:
            logger.exception("prob_rate audit webhook post failed")

    logger.info("prob_rate audit mae=%s breached=%s cells=%d n=%d notify=%s sent=%s",
                res["mae"], breached, res["cells"], res["n"], notify, sent)
    return {"status": "ok", "mae": res["mae"], "breached": breached, "sent": sent,
            "cells": res["cells"], "n": res["n"], "by_seg": res["by_seg"],
            "skipped": res["skipped"], "signal_cells": res["signal_cells"],
            "fit_span": fit_span, "test_span": test_span,
            "rows": res["rows"], "text": body}
