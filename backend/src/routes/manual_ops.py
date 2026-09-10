"""[수동관리 신청서] — 플래너가 우선순위 순서대로 처리하는 목록.

메뉴 2개. 우선순위는 필터가 아니라 **배경 정렬**이다 — 플래너는 위에서부터 처리한다.

- recommended — 선생님 추천 상태(20)가 된 지 48시간 지난 건. 오래 방치된 순.
  경과 기준은 created_at — suggested_at 은 추천할 때마다 갱신돼 못 쓴다(db.py 근거 참고).
- intake — 접수안내(10)를 두 그룹으로.
  · waiting — **응답한 선생님이 있는데 확정이 안 된 건**. 오래 기다린 순.
  · low — 나머지 중 예상 매칭확률 **하위 30% 분위**. 구조적으로 막힌 건.

  🚨 상위 그룹을 확률이 아니라 **상태**로 잡는 이유 — 확률 모델에서 나이가 지배
  변수라(lt6h 20% vs gte48h 1~2%) 확률 상위 30% 를 뽑으면 그대로 "방금 접수된 건"이
  된다(2026-09-07 실측: 상위 29건 전부 당일·전일 접수). 갓 들어온 건은 아직 밀 필요가
  없어 라벨과 행동이 어긋난다. 응답 선생님 유무는 "지금 밀면 닫히는가"를 직접 가리킨다.
  같은 나이대 안 상대분위로 바꿔도 해상도가 안 나온다 — 확률이 셀 값이라 같은
  나이구간에서 재이용×응답유무 4개 값뿐이다.

  🚨 하위가 절대값(20% 이하)이 아니라 **분위**인 이유 — 실측 분포에서 20% 이하가
  1건이라 화면이 빈다. 분위는 물량이 변해도 처리 가능한 크기를 유지한다.

  waiting 을 먼저 빼고 **나머지**에서 하위 분위를 자른다. 같은 신청서가
  "밀어줄 건"과 "막힌 건"에 동시에 뜨면 화면이 거짓말을 한다.

카드 스키마는 메인 목록과 동일하다(row_batch).
인증은 managed 와 동일한 trigger_auth.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from src.db import get_replica
from src.routes.auto_dispatch import trigger_auth
from src.row_batch import build_rows_for_sids

router = APIRouter(prefix="/api/manual-ops", tags=["manual-ops"])
logger = logging.getLogger(__name__)

MENUS = ("recommended", "intake")
# 하위 분위 — 30%
QUANTILE = 0.30


def _prob(row: dict[str, Any]) -> float:
    p = row.get("prob") or {}
    try:
        return float(p.get("value") or 0)
    except (TypeError, ValueError):
        return 0.0


def _has_responder(row: dict[str, Any]) -> bool:
    """지원·수락한 선생님이 1명 이상인가. applyCount 는 applied OR accepted 수."""
    try:
        return int(row.get("applyCount") or 0) > 0
    except (TypeError, ValueError):
        return False


def _bottom_quantile(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """확률 하위 30%. 낮은 것부터. 1건이라도 있으면 최소 1건은 낸다."""
    if not rows:
        return []
    ordered = sorted(rows, key=_prob)
    k = max(1, int(round(len(ordered) * QUANTILE)))
    return ordered[:k]


@router.get("")
async def list_manual_ops(
    menu: str = Query("recommended"),
    stale_hours: int = Query(48, ge=1, le=720),
    limit: int = Query(300, le=500),
    _user: dict = Depends(trigger_auth),
) -> dict[str, Any]:
    if menu not in MENUS:
        raise HTTPException(status_code=400, detail=f"menu 는 {MENUS} 중 하나")

    try:
        sids = await get_replica().list_manual_ops_sids(
            menu=menu, stale_hours=stale_hours, limit=limit
        )
    except Exception:
        logger.exception("manual_ops sid 조회 실패 menu=%s", menu)
        raise HTTPException(status_code=503, detail="replica query failed")

    if not sids:
        return {"menu": menu, "count": 0, "groups": [], "rows": []}

    row_map = await build_rows_for_sids(sids)
    # sid 조회 순서가 곧 우선순위다(recommended = 오래 방치된 순). 그 순서를 보존한다.
    rows = [row_map[s] for s in sids if s in row_map]

    if menu == "recommended":
        groups = [{
            "key": "stale",
            "label": f"{stale_hours}시간 경과",
            "hint": "선생님을 추천했는데 부모님 확정이 없는 건. 오래된 순.",
            "count": len(rows),
        }]
        logger.info("manual_ops menu=recommended stale_h=%d rows=%d", stale_hours, len(rows))
        return {"menu": menu, "count": len(rows), "groups": groups, "rows": rows}

    # rows 는 created_at DESC 순(최신 먼저). waiting 은 오래 기다린 순이 우선이라 뒤집는다.
    waiting = [r for r in reversed(rows) if _has_responder(r)]
    rest = [r for r in rows if not _has_responder(r)]
    low = _bottom_quantile(rest)
    for r in waiting:
        r["opsGroup"] = "waiting"
    for r in low:
        r["opsGroup"] = "low"
    merged = waiting + low
    groups = [
        {"key": "waiting", "label": "응답 선생님 대기", "count": len(waiting),
         "hint": "지원·수락한 선생님이 있는데 확정이 안 된 건. 오래 기다린 순."},
        {"key": "low", "label": "매칭확률 하위 30%", "count": len(low),
         "hint": "응답도 없고 확률도 바닥. 조건 조정·후보 확장 필요."},
    ]
    logger.info("manual_ops menu=intake pool=%d waiting=%d low=%d",
                len(rows), len(waiting), len(low))
    return {"menu": menu, "count": len(merged), "groups": groups, "rows": merged,
            "poolSize": len(rows)}
