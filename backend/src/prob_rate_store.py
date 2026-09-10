"""예상 매칭확률 비율표 — 셀별 과거 실측 매칭률.

셀 = (상품군 seg, 신청서 나이구간 age_bucket, 재이용 reuse, 응답 선생님 유무 responder).
서빙은 이 표를 조회만 한다. 축소(shrinkage)는 갱신 시점에 이미 반영돼 있다.

배경과 검증은 sql/0013_create_prob_rate.sql 및
옵시디언 `수동매칭 대시보드/matching-ops 예상 매칭확률 캘리브레이션.md` 참고.

표가 없거나 조회에 실패해도 서빙은 멈추지 않는다 — _DEFAULT_RATES 로 폴백한다.
이 기본값은 2026-09-07 기준 롤링 90일(정착 10일) 실측이며, 갱신 배치가 돌면 덮인다.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from src.config import settings

logger = logging.getLogger(__name__)

SEGS = ("reg2", "reg3", "urgent")
AGE_BUCKETS = ("lt6h", "6to48h", "gte48h")
# 구간별 대표 관측 시점(시간). 갱신 배치가 이 시점 스냅샷으로 비율을 만든다.
BUCKET_SNAPSHOT_H = {"lt6h": 3, "6to48h": 24, "gte48h": 72}
# 축소 강도. 셀 표본이 K 와 같아지면 상위 셀과 반반 섞인다.
SHRINK_K = 100
# 이보다 얇은 셀은 아예 상위 셀 값을 쓴다.
# 올리면 오히려 나빠진다(실측: 250 으로 올리면 MAE 1.59 → 1.83%p) — 얇은 셀도
# 축소해서 쓰는 편이 상위 셀 값으로 뭉개는 것보다 낫다.
MIN_CELL_N = 30

# 응답유무로 쪼개지 않는 세그먼트. reg2 는 셀당 표본이 150~330건이라 4셀을 감당하지만
# reg3·urgent 는 30~110건이라 쪼개면 노이즈가 오차를 지배한다.
# 실측: reg3/urgent 를 통합하면 최대 셀오차 15.4%p → 10.5%p, 12%p 초과 셀 1개 → 0개.
# (전체 평균 MAE 는 1.59 → 1.58%p 로 사실상 동일 — 정확도를 잃지 않고 편차만 줄인다)
COLLAPSE_RESPONDER_SEGS = frozenset({"reg3", "urgent"})


def cell_key(seg: str, age_bucket: str, reuse: int, responder: int) -> tuple[str, str, int, int]:
    """잎 셀 키. 학습·서빙·채점이 반드시 이 함수를 거쳐야 한다.

    셋 중 하나라도 다른 규칙을 쓰면 표를 잘못된 칸에서 읽거나 채점이 무의미해진다.
    통합 세그먼트는 responder 를 -1(무관)로 접는다 — 상위 셀 (-1, -1) 과는 구분된다.
    """
    if seg in COLLAPSE_RESPONDER_SEGS:
        return (seg, age_bucket, reuse, -1)
    return (seg, age_bucket, reuse, responder)

_DEFAULT_RATES: dict[tuple[str, str, int, int], tuple[float, int]] = {
    ("reg2", "lt6h", -1, -1): (14.7, 2530),
    ("reg2", "lt6h", 0, 0): (8.0, 721),
    ("reg2", "lt6h", 0, 1): (13.2, 359),
    ("reg2", "lt6h", 1, 0): (17.1, 835),
    ("reg2", "lt6h", 1, 1): (20.1, 615),
    ("reg2", "6to48h", -1, -1): (8.4, 2106),
    ("reg2", "6to48h", 0, 0): (4.5, 376),
    ("reg2", "6to48h", 0, 1): (6.0, 569),
    ("reg2", "6to48h", 1, 0): (11.2, 361),
    ("reg2", "6to48h", 1, 1): (10.8, 800),
    ("reg2", "gte48h", -1, -1): (1.4, 1583),
    ("reg2", "gte48h", 0, 0): (0.8, 195),
    ("reg2", "gte48h", 0, 1): (1.0, 552),
    ("reg2", "gte48h", 1, 0): (1.6, 170),
    ("reg2", "gte48h", 1, 1): (2.0, 666),
    ("reg3", "lt6h", -1, -1): (26.3, 885),
    ("reg3", "lt6h", 0, -1): (18.9, 246),
    ("reg3", "lt6h", 1, -1): (29.8, 639),
    ("reg3", "6to48h", -1, -1): (16.2, 647),
    ("reg3", "6to48h", 0, -1): (11.9, 195),
    ("reg3", "6to48h", 1, -1): (18.5, 452),
    ("reg3", "gte48h", -1, -1): (6.0, 401),
    ("reg3", "gte48h", 0, -1): (5.0, 138),
    ("reg3", "gte48h", 1, -1): (6.6, 263),
    ("urgent", "lt6h", -1, -1): (34.3, 239),
    ("urgent", "lt6h", 0, -1): (30.3, 53),
    ("urgent", "lt6h", 1, -1): (36.5, 186),
    ("urgent", "6to48h", -1, -1): (18.5, 54),
    ("urgent", "6to48h", 0, -1): (18.5, 11),
    ("urgent", "6to48h", 1, -1): (19.2, 43),
    ("urgent", "gte48h", -1, -1): (13.3, 15),
    ("urgent", "gte48h", 0, -1): (13.3, 3),
    ("urgent", "gte48h", 1, -1): (13.3, 12),
}


def age_bucket(age_minutes: float | None) -> str:
    """신청서 나이(분) → 구간. 나이를 모르면 가장 흔한 구간으로 둔다."""
    if age_minutes is None:
        return "6to48h"
    if age_minutes < 6 * 60:
        return "lt6h"
    if age_minutes < 48 * 60:
        return "6to48h"
    return "gte48h"


def segment(regularity: Any, is_urgent: Any) -> str:
    """상품군. 긴급이 최우선 — regularity=1 과 100% 겹치지만 명시적으로 가른다."""
    if is_urgent:
        return "urgent"
    try:
        if int(regularity) == 3:
            return "reg3"
    except (TypeError, ValueError):
        pass
    return "reg2"


class ProbRateStore:
    """비율표 저장소. 조회는 프로세스 캐시(TTL)를 태운다 — 목록 요청마다 PG 를 때리지 않게."""

    def __init__(self, cache_ttl_sec: int = 600) -> None:
        self._engine: AsyncEngine = create_async_engine(
            settings.matching_ops_db_url, pool_pre_ping=True, pool_size=2, max_overflow=2
        )
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._cache: dict[tuple[str, str, int, int], tuple[float, int]] | None = None
        self._cache_at = 0.0
        self._ttl = cache_ttl_sec

    async def aclose(self) -> None:
        await self._engine.dispose()

    def invalidate(self) -> None:
        self._cache = None

    async def load(self) -> dict[tuple[str, str, int, int], tuple[float, int]]:
        """{(seg, bucket, reuse, responder): (rate, n)}. 실패하면 기본표."""
        now = time.monotonic()
        if self._cache is not None and now - self._cache_at < self._ttl:
            return self._cache
        try:
            async with self._session_factory() as session:
                rows = (await session.execute(text(
                    "SELECT seg, age_bucket, reuse, responder, rate, n"
                    "  FROM matching_ops_prob_rate"
                ))).fetchall()
            table = {(r[0], r[1], int(r[2]), int(r[3])): (float(r[4]), int(r[5])) for r in rows}
            if not table:
                logger.info("prob_rate 표가 비어 있음 — 기본표 사용")
                table = dict(_DEFAULT_RATES)
        except Exception:
            logger.exception("prob_rate 조회 실패 — 기본표로 폴백 (graceful)")
            table = dict(_DEFAULT_RATES)
        self._cache, self._cache_at = table, now
        return table

    async def replace_all(self, cells: list[dict[str, Any]], window_days: int) -> int:
        """표를 이번 계산 결과로 **통째 교체**. 반환 = 기록한 행 수.

        upsert 만 하면 셀 정의가 바뀐 뒤 옛 키의 행이 그대로 남는다
        (2026-09-07 reg3·urgent 응답통합 후 죽은 행 24개가 잔류했다).
        서빙은 cell_key 를 거치므로 잘못 읽히진 않지만, 정의를 되돌리면 낡은 값이
        되살아나고 조회·감사 집계도 오염된다. 한 트랜잭션에서 비우고 다시 넣는다 —
        중간에 죽으면 롤백되어 직전 표가 그대로 남는다.
        """
        if not cells:
            return 0
        stmt = text(
            """
            INSERT INTO matching_ops_prob_rate
                (seg, age_bucket, reuse, responder, n, matched, rate, raw_rate,
                 window_days, computed_at)
            VALUES (:seg, :age_bucket, :reuse, :responder, :n, :matched, :rate, :raw_rate,
                    :window_days, NOW())
            ON CONFLICT (seg, age_bucket, reuse, responder) DO UPDATE SET
                n = EXCLUDED.n, matched = EXCLUDED.matched, rate = EXCLUDED.rate,
                raw_rate = EXCLUDED.raw_rate, window_days = EXCLUDED.window_days,
                computed_at = EXCLUDED.computed_at
            """
        )
        async with self._session_factory() as session:
            await session.execute(text("DELETE FROM matching_ops_prob_rate"))
            for c in cells:
                await session.execute(stmt, {**c, "window_days": window_days})
            await session.commit()
        self.invalidate()
        return len(cells)


def build_cells(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """리플리카 집계(seg, age_bucket, reuse, responder, n, matched) → 축소 적용 셀 목록.

    상위 셀(reuse=-1, responder=-1)을 먼저 만들고, 잎 셀을 그쪽으로 당긴다.
    표본이 MIN_CELL_N 미만이면 상위 셀 값을 그대로 쓴다(rate 만; n·matched 는 원값 보존).
    """
    parent: dict[tuple[str, str], list[int]] = {}
    leaf: dict[tuple[str, str, int, int], list[int]] = {}
    for r in raw:
        n, m = int(r["n"]), int(r["matched"])
        if n <= 0:
            continue
        acc = parent.setdefault((r["seg"], r["age_bucket"]), [0, 0])
        acc[0] += n
        acc[1] += m
        # 통합 세그먼트는 여러 raw 행이 한 잎 셀로 합쳐진다
        k = cell_key(r["seg"], r["age_bucket"], int(r["reuse"]), int(r["responder"]))
        lacc = leaf.setdefault(k, [0, 0])
        lacc[0] += n
        lacc[1] += m

    out: list[dict[str, Any]] = []
    for key, (pn, pm) in parent.items():
        if pn <= 0:
            continue
        rate = round(100.0 * pm / pn, 2)
        out.append({"seg": key[0], "age_bucket": key[1], "reuse": -1, "responder": -1,
                    "n": pn, "matched": pm, "rate": rate, "raw_rate": rate})
    for (seg, bucket, reuse, responder), (n, m) in leaf.items():
        pn, pm = parent[(seg, bucket)]
        p_rate = 100.0 * pm / pn
        raw_rate = 100.0 * m / n
        rate = p_rate if n < MIN_CELL_N else 100.0 * (m + SHRINK_K * p_rate / 100.0) / (n + SHRINK_K)
        out.append({"seg": seg, "age_bucket": bucket,
                    "reuse": reuse, "responder": responder,
                    "n": n, "matched": m,
                    "rate": round(rate, 2), "raw_rate": round(raw_rate, 2)})
    return out


def lookup(table: dict[tuple[str, str, int, int], tuple[float, int]],
           seg: str, bucket: str, reuse: int, responder: int) -> tuple[float, bool] | None:
    """서빙과 **같은** 조회 규칙. (rate, is_leaf). 없으면 None.

    감시가 서빙과 다른 규칙으로 조회하면 채점이 무의미해지므로 한 곳에 둔다.
    """
    hit = table.get(cell_key(seg, bucket, reuse, responder))
    if hit:
        return hit[0], True
    hit = table.get((seg, bucket, -1, -1))
    if hit:
        return hit[0], False
    return None


def score_table(fit_cells: list[dict[str, Any]], test_raw: list[dict[str, Any]],
                min_cell_n: int) -> dict[str, Any]:
    """fit_cells 로 만든 표를 test_raw 실측으로 채점.

    반환 = {rows, mae, n, by_seg, skipped}. mae 는 채점 표본 수로 가중한 절대오차(%p).
    표본이 min_cell_n 미만인 셀은 뺀다 — n=4 짜리 셀의 0% 를 오차로 세면 노이즈만 커진다.
    """
    table = {(c["seg"], c["age_bucket"], c["reuse"], c["responder"]): (c["rate"], c["n"])
             for c in fit_cells}
    rows: list[dict[str, Any]] = []
    skipped = 0
    for t in test_raw:
        n, m = int(t["n"]), int(t["matched"])
        if n < min_cell_n:
            skipped += 1
            continue
        got = lookup(table, t["seg"], t["age_bucket"], int(t["reuse"]), int(t["responder"]))
        if got is None:
            skipped += 1
            continue
        pred, is_leaf = got
        actual = 100.0 * m / n
        # 채점 셀 자체의 95% 표본오차 반폭. 오차가 이 안이면 모델 탓인지 알 수 없다.
        # 이걸 같이 안 보여주면 n 이 작은 셀의 '큰 오차'를 회귀로 오독하게 된다.
        q = actual / 100.0
        noise = 1.96 * math.sqrt(max(q * (1.0 - q), 1e-9) / n) * 100.0
        err = abs(pred - actual)
        rows.append({"seg": t["seg"], "age_bucket": t["age_bucket"],
                     "reuse": int(t["reuse"]), "responder": int(t["responder"]),
                     "n": n, "predicted": round(pred, 2), "actual": round(actual, 2),
                     "err": round(err, 2), "noise": round(noise, 2),
                     "signal": err > noise, "leaf": is_leaf})

    def _mae(rs: list[dict[str, Any]]) -> tuple[float | None, int]:
        tot = sum(r["n"] for r in rs)
        if not tot:
            return None, 0
        return round(sum(r["err"] * r["n"] for r in rs) / tot, 2), tot

    mae, total_n = _mae(rows)
    by_seg = {}
    for seg in SEGS:
        sub = [r for r in rows if r["seg"] == seg]
        if sub:
            v, sn = _mae(sub)
            by_seg[seg] = {"mae": v, "n": sn, "cells": len(sub)}
    rows.sort(key=lambda r: -r["err"])
    return {"rows": rows, "mae": mae, "n": total_n, "cells": len(rows),
            "by_seg": by_seg, "skipped": skipped,
            "signal_cells": sum(1 for r in rows if r["signal"])}


_store: ProbRateStore | None = None


def get_prob_rate_store() -> ProbRateStore:
    global _store
    if _store is None:
        _store = ProbRateStore()
    return _store


def prob_rate_available() -> bool:
    return bool(settings.matching_ops_db_url)
