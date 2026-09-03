"""matching-ops 자동 디스패치 처리 이력 + 제외 sid 셋 조회 (PostgreSQL).

핵심:
  - get_excluded_sids(sids): 주어진 sid 중 자동 디스패치 제외 대상 셋 반환.
    제외 신호 3종 OR — auto_run(dry_run=false) / memo / handler 중 하나라도 있으면 제외.
  - record_run(...): UPSERT. dry-run row는 live run 시 자연스럽게 덮어씀.

matching_ops_memo / matching_ops_handler 와 같은 인스턴스(matching-ops-db).
llm_insight_store 패턴 그대로.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from src.config import settings

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


class AutoRunStore:
    def __init__(self, url: str | None = None) -> None:
        target = url or settings.matching_ops_db_url
        if not target:
            raise RuntimeError("MATCHING_OPS_DB_URL 미설정 — auto_dispatch 비활성")
        self._engine: AsyncEngine = create_async_engine(
            target,
            pool_size=2,
            max_overflow=3,
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def aclose(self) -> None:
        await self._engine.dispose()

    async def window_stats(self, start, end) -> tuple[dict, list[str]]:
        """[start, end) 구간의 live run 집계 + 성공한 신청서 sid 목록.

        배포 전/후 비교 리포트(routes/reports.py) 전용.
        """
        metrics = text(
            """
            SELECT COUNT(*) AS runs,
                   COUNT(*) FILTER (WHERE succeed_count > 0) AS ok,
                   COALESCE(SUM(succeed_count), 0) AS sent,
                   COALESCE(SUM(denied_count), 0) AS denied_at_send,
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY pool_size)
                     FILTER (WHERE succeed_count > 0) AS pool_p50,
                   ROUND(AVG(pool_size) FILTER (WHERE succeed_count > 0), 1) AS pool_avg,
                   ROUND(AVG(succeed_count) FILTER (WHERE succeed_count > 0), 1) AS avg_added,
                   COUNT(*) FILTER (WHERE error_message LIKE 'empty_after_variant%') AS empty_filter,
                   COUNT(*) FILTER (WHERE error_message LIKE 'no_candidates%') AS no_pool
            FROM matching_ops_auto_run
            WHERE dry_run = false AND run_at >= :s AND run_at < :e
            """
        )
        sids_q = text(
            """
            SELECT recommendation_sid FROM matching_ops_auto_run
            WHERE dry_run = false AND run_at >= :s AND run_at < :e AND succeed_count > 0
            """
        )
        async with self._session_factory() as session:
            m = (await session.execute(metrics, {"s": start, "e": end})).mappings().first()
            sids = [r[0] for r in (await session.execute(sids_q, {"s": start, "e": end}))]
        return dict(m or {}), sids

    async def get_excluded_sids(self, sids: list[str]) -> set[str]:
        """주어진 sid 중 자동 디스패치 제외할 sid 집합.

        OR 신호:
          1) matching_ops_auto_run.recommendation_sid IN sids AND dry_run=false
             — 이 자동화가 처리한 이력 (live)
          2) matching_ops_memo.recommendation_sid IN sids
             — 운영자가 매칭-ops 대시보드에서 메모 작성
          3) matching_ops_handler.application_sid IN sids
             — 운영자가 처리담당 claim

        PG ANY(:sids) 사용 (SQLAlchemy expanding bindparam을 UNION 안 3번 재사용 시
        SQLAlchemy 가 한 placeholder만 expand 하고 나머지가 비어 NotSupportedError).
        """
        if not sids:
            return set()
        # PG의 = ANY(text[]) — asyncpg가 Python list를 native array로 직렬화.
        # 컬럼명 주의: matching_ops_memo / handler 는 `application_sid` 컨벤션
        # (자란다 prod recommendation.sid 값을 의미), auto_run 만 신규로 recommendation_sid.
        query = text(
            """
            SELECT recommendation_sid AS sid
              FROM matching_ops_auto_run
             WHERE recommendation_sid = ANY(:sids)
               AND dry_run = false
            UNION
            SELECT application_sid AS sid
              FROM matching_ops_memo
             WHERE application_sid = ANY(:sids)
            UNION
            SELECT application_sid AS sid
              FROM matching_ops_handler
             WHERE application_sid = ANY(:sids)
            """
        )
        async with self._session_factory() as session:
            rows = await session.execute(query, {"sids": sids})
            return {str(row._mapping["sid"]) for row in rows}

    async def record_run(
        self,
        *,
        recommendation_sid: str,
        dry_run: bool,
        pool_size: int,
        added_count: int,
        succeed_count: int,
        denied_count: int,
        llm_model_id: str,
        operator_email: str,
        error_message: str | None = None,
        variant: int | None = None,
    ) -> None:
        """성공·실패 무관 무조건 UPSERT. dry-run row는 live run 시 갱신.

        variant: A/B 4-arm 식별 (0~3). NULL이면 A/B 미적용 (legacy).
        """
        query = text(
            """
            INSERT INTO matching_ops_auto_run
                (recommendation_sid, run_at, pool_size, added_count, succeed_count,
                 denied_count, llm_model_id, dry_run, operator_email, error_message,
                 variant)
            VALUES
                (:sid, NOW(), :pool, :added, :succeed, :denied, :model, :dry,
                 :email, :err, :variant)
            ON CONFLICT (recommendation_sid) DO UPDATE SET
                run_at         = NOW(),
                pool_size      = EXCLUDED.pool_size,
                added_count    = EXCLUDED.added_count,
                succeed_count  = EXCLUDED.succeed_count,
                denied_count   = EXCLUDED.denied_count,
                llm_model_id   = EXCLUDED.llm_model_id,
                dry_run        = EXCLUDED.dry_run,
                operator_email = EXCLUDED.operator_email,
                error_message  = EXCLUDED.error_message,
                variant        = EXCLUDED.variant
            """
        )
        async with self._session_factory() as session:
            await session.execute(
                query,
                {
                    "sid": recommendation_sid,
                    "pool": pool_size,
                    "added": added_count,
                    "succeed": succeed_count,
                    "denied": denied_count,
                    "model": llm_model_id,
                    "dry": dry_run,
                    "email": operator_email,
                    "err": error_message,
                    "variant": variant,
                },
            )
            await session.commit()


    async def count_today_runs(self, *, dry_run: bool = False) -> int:
        """KST 오늘 자정 이후 처리된 신청서 수. 일일 cap 강제용."""
        kst_midnight = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
        since_utc = kst_midnight.astimezone(timezone.utc)
        query = text(
            """
            SELECT COUNT(*) AS cnt
              FROM matching_ops_auto_run
             WHERE run_at >= :since
               AND dry_run = :dry
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(query, {"since": since_utc, "dry": dry_run})
            row = result.first()
            return int(row[0]) if row else 0


_store: AutoRunStore | None = None


def get_auto_run_store() -> AutoRunStore:
    global _store
    if _store is None:
        _store = AutoRunStore()
    return _store


def auto_run_available() -> bool:
    return bool(settings.matching_ops_db_url)
