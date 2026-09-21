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

    async def model_split(self, start, end) -> dict:
        """[start, end) 구간을 **랭킹 방식별**로 가른 집계.

        2026-09-10 Anthropic 한도로 LLM 이 막히면서 규칙 랭킹 폴백이 돌기 시작했다.
        둘을 섞어 보면 폴백 도입 이후 지표 변화를 LLM 성과로 오독하게 된다.

        구분은 llm_model_id 로 한다 — 폴백은 FALLBACK_MODEL_ID('fallback-rule-v1')로
        기록되므로 접두사로 가른다. 모델명이 바뀌어도(sonnet→opus 등) llm 쪽은
        그대로 llm 으로 묶인다.

        반환 = {'llm': {..., 'sids': [...]}, 'fallback': {...}}
        """
        q = text(
            """
            SELECT CASE WHEN llm_model_id LIKE 'fallback-%' THEN 'fallback'
                        ELSE 'llm' END AS grp,
                   COUNT(*) AS runs,
                   COUNT(*) FILTER (WHERE succeed_count > 0) AS ok,
                   COALESCE(SUM(succeed_count), 0) AS sent,
                   ROUND(AVG(succeed_count) FILTER (WHERE succeed_count > 0), 1) AS avg_added,
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY pool_size)
                     FILTER (WHERE succeed_count > 0) AS pool_p50
              FROM matching_ops_auto_run
             WHERE dry_run = false AND run_at >= :s AND run_at < :e
             GROUP BY 1
            """
        )
        sids_q = text(
            """
            SELECT CASE WHEN llm_model_id LIKE 'fallback-%' THEN 'fallback'
                        ELSE 'llm' END AS grp,
                   recommendation_sid
              FROM matching_ops_auto_run
             WHERE dry_run = false AND run_at >= :s AND run_at < :e
               AND succeed_count > 0
            """
        )
        out: dict = {}
        async with self._session_factory() as session:
            for m in (await session.execute(q, {"s": start, "e": end})).mappings():
                d = dict(m)
                out[d.pop("grp")] = {**d, "sids": []}
            for grp, sid in (await session.execute(sids_q, {"s": start, "e": end})):
                if grp in out:
                    out[grp]["sids"].append(sid)
        return out

    async def get_excluded_sids(
        self, sids: list[str], *, max_attempts: int = 4, retry_after_minutes: int = 360
    ) -> set[str]:
        """주어진 sid 중 자동 디스패치 제외할 sid 집합.

        OR 신호:
          1) matching_ops_auto_run (dry_run=false) 중 아래 하나라도 해당
             a. succeed_count > 0        — 실제로 선생님을 추가함 (영구 제외)
             b. attempt_count >= max     — 재시도 소진 (영구 제외)
             c. run_at > NOW() - backoff — 방금 시도함 (백오프 중, 일시 제외)
             즉 **실패한 건은 백오프 뒤 재시도된다.**
             2026-09-04 이전에는 성공 여부를 보지 않고 행만 있으면 제외했다.
             그 탓에 Anthropic 한도 소진으로 실패한 6건이 영구 이탈했다(sql/0012).
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
               AND (
                     succeed_count > 0
                  OR attempt_count >= :max_attempts
                  OR run_at > NOW() - make_interval(mins => :retry_after)
               )
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
            rows = await session.execute(query, {
                "sids": sids,
                "max_attempts": max_attempts,
                "retry_after": retry_after_minutes,
            })
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
        pre_responder_count: int | None = None,
        added_teacher_sids: list[str] | None = None,
    ) -> None:
        """성공·실패 무관 무조건 UPSERT. dry-run row는 live run 시 갱신.

        variant: A/B 4-arm 식별 (0~3). NULL이면 A/B 미적용 (legacy).
        pre_responder_count: 처리 시점 기존 응답 선생님 수 (0 또는 1).
            매칭률을 '응답 0명' 그룹만으로 비교하기 위한 것 (sql/0011).
        added_teacher_sids: 콘솔에 추가 요청한 선생님 sid 배열.
            수락률을 봇 발송분에만 귀속시키기 위한 것 (sql/0011).
        """
        query = text(
            """
            INSERT INTO matching_ops_auto_run
                (recommendation_sid, run_at, pool_size, added_count, succeed_count,
                 denied_count, llm_model_id, dry_run, operator_email, error_message,
                 variant, pre_responder_count, added_teacher_sids, attempt_count)
            VALUES
                (:sid, NOW(), :pool, :added, :succeed, :denied, :model, :dry,
                 :email, :err, :variant, :pre_resp, :added_sids, 1)
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
                variant        = EXCLUDED.variant,
                pre_responder_count = EXCLUDED.pre_responder_count,
                added_teacher_sids  = EXCLUDED.added_teacher_sids,
                attempt_count       = matching_ops_auto_run.attempt_count + 1
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
                    "pre_resp": pre_responder_count,
                    "added_sids": added_teacher_sids,
                },
            )
            await session.commit()


    async def should_notify(self, key: str, *, steps=(0, 3600, 14400, 86400)) -> tuple[bool, int]:
        """알림을 지금 보낼지. (보낼지, 이번이 몇 번째인지) 반환.

        억제 상태를 **공유 저장소**에 둔다. 프로세스 메모리로는 Cloud Run 의
        다중 인스턴스·재기동을 못 넘어 억제가 무력해진다(2026-09-21 실측:
        폴백 알림이 "시간당 1회"인데도 계속 나갔고, 하루에 인스턴스 2개가 관여했다).

        같은 상태가 이어지면 간격을 늘린다 — steps[min(streak, last)] 초.
        기본값은 즉시 → 1시간 → 4시간 → 이후 24시간.
        사람이 이미 아는 사실을 반복하면 다음 진짜 경보까지 같이 무시된다.

        보낸다고 판정하면 그 자리에서 last_sent_at·streak 을 올린다 —
        호출부가 실패해도 재시도로 도배되지 않게 하려는 것(알림은 재시도 가치가 낮다).
        """
        sql = text(
            """
            INSERT INTO matching_ops_notice_state (notice_key, last_sent_at, streak, updated_at)
            VALUES (:k, NOW(), 1, NOW())
            ON CONFLICT (notice_key) DO UPDATE SET
                -- streak 은 **실제로 보낸 횟수**다. 확인할 때마다 올리면 간격이
                -- cron 주기에 끌려간다(10분 cron 이면 40분 만에 24시간 간격으로 점프).
                streak = matching_ops_notice_state.streak
                         + CASE WHEN :send THEN 1 ELSE 0 END,
                last_sent_at = CASE WHEN :send THEN NOW()
                                    ELSE matching_ops_notice_state.last_sent_at END,
                updated_at = NOW()
            RETURNING streak
            """
        )
        read = text("SELECT last_sent_at, streak FROM matching_ops_notice_state"
                    " WHERE notice_key = :k")
        try:
            async with self._session_factory() as session:
                row = (await session.execute(read, {"k": key})).first()
                if row is None:
                    streak, elapsed = 0, None
                else:
                    last, streak = row[0], int(row[1])
                    elapsed = None if last is None else (
                        await session.execute(
                            text("SELECT EXTRACT(EPOCH FROM (NOW() - :t))"), {"t": last})
                    ).scalar()
                gap = steps[min(streak, len(steps) - 1)]
                send = elapsed is None or float(elapsed) >= gap
                res = (await session.execute(sql, {"k": key, "send": send})).first()
                await session.commit()
                return send, int(res[0]) if res else streak
        except Exception:
            logger.exception("should_notify 조회 실패 — 이번 회차는 보내지 않는다 (graceful)")
            # 실패 시 **보내지 않는** 쪽으로 기운다. 억제 상태를 모르는데 보내면
            # 도배가 되고, 한 번 건너뛰는 손해가 훨씬 작다.
            return False, 0

    async def clear_notice(self, key: str) -> None:
        """상태가 해소되면 streak 을 0 으로. 다음 발생 때 다시 즉시 알린다."""
        try:
            async with self._session_factory() as session:
                await session.execute(
                    text("UPDATE matching_ops_notice_state SET streak = 0, updated_at = NOW()"
                         " WHERE notice_key = :k AND streak <> 0"), {"k": key})
                await session.commit()
        except Exception:
            logger.exception("clear_notice 실패 (graceful)")

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

    async def consecutive_llm_failures(self, *, lookback: int = 40) -> tuple[int, str]:
        """최근 라이브 실행부터 거슬러 올라가며 연속 llm_failed 건수와 가장 최근 오류 메시지.

        record_run 은 recommendation_sid UPSERT 라 run_at 이 갱신된다.
        따라서 run_at DESC 가 곧 '최근 처리 순서'다.
        llm_failed 가 아닌 행(성공·다른 오류·스킵)을 만나면 즉시 멈춘다.
        """
        query = text(
            """
            SELECT COALESCE(error_message, '') AS msg
              FROM matching_ops_auto_run
             WHERE dry_run = false
             ORDER BY run_at DESC
             LIMIT :lookback
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(query, {"lookback": lookback})
            rows = result.fetchall()
        count = 0
        latest = ""
        for (msg,) in rows:
            if not msg.startswith("llm_failed"):
                break
            if not latest:
                latest = msg
            count += 1
        return count, latest


_store: AutoRunStore | None = None


def get_auto_run_store() -> AutoRunStore:
    global _store
    if _store is None:
        _store = AutoRunStore()
    return _store


def auto_run_available() -> bool:
    return bool(settings.matching_ops_db_url)
