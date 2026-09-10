"""자란다 read replica MySQL 클라이언트.

auto-call의 src/poller/jaranda_replica.py 패턴을 단순화. 읽기 전용 조회만 수행.

PRD: vibe-cs/auto-call과 동일하게 PoC 단계는 replica 직접 polling.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from src.config import settings

# teacher_specialties(=subject_wage_id) → 과목별 자기소개 컬럼 (화이트리스트, SQL 포맷용)
_SPECIALTY_INTRO = {
    1: "teacher_introduction_activity_tag_care",
    2: "teacher_introduction_activity_tag_stem",
    3: "teacher_introduction_activity_tag_sports",
    4: "teacher_introduction_activity_tag_art",
    5: "teacher_introduction_activity_tag_foreign_language",
    6: "teacher_introduction_activity_tag_korean",
}


class JarandaReplica:
    def __init__(self, url: str | None = None) -> None:
        self._engine: AsyncEngine = create_async_engine(
            url or settings.jaranda_replica_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def aclose(self) -> None:
        await self._engine.dispose()

    async def list_recent_recommendations(
        self,
        limit: int = 30,
        offset: int = 0,
        date_from: str | None = None,
        date_to: str | None = None,
        sids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """신청서 목록.

        - date_from/date_to (YYYY-MM-DD, KST) 주면 created_at 기준 [from 00:00, to 23:59:59] 범위.
          한쪽만 주면 그쪽만 조건으로 사용. 둘 다 없으면 최근 N시간 윈도우(settings).
        - sids 주면 그 sid 리스트만 조회 (윈도우 무관 — 관리 신청서 목록용).
        - status 101 (임시저장) 항상 제외
        - ORDER BY created_at DESC, sid (안정 정렬) — 페이지네이션용
        - offset/limit 지원 (페이지네이션)
        """
        where = ["r.status != 101"]
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        bindparams: list[Any] = []

        if sids is not None:
            if not sids:
                return []
            # sids 지정 시 윈도우/date 필터 무시 — 관리 신청서 목록은 오래된 신청서도 포함
            where.append("r.sid IN :sids")
            params["sids"] = sids
            bindparams.append(bindparam("sids", expanding=True))
        elif date_from or date_to:
            if date_from:
                where.append("r.created_at >= :date_from")
                params["date_from"] = f"{date_from} 00:00:00"
            if date_to:
                # to 일자의 끝(다음날 00:00 미만)까지 포함
                where.append("r.created_at < :date_to_excl")
                params["date_to_excl"] = f"{date_to} 23:59:59"
        else:
            where.append("r.created_at >= NOW() - INTERVAL :window_hours HOUR")
            params["window_hours"] = settings.recent_window_hours

        where_sql = " AND ".join(where)
        query = text(
            f"""
            SELECT
              r.sid,
              r.parent_account_sid,
              r.parent_name,
              r.parent_mobile,
              r.child_name,
              r.status,
              r.teacher_appliable,
              r.confirmed_at,
              r.cancelled_at,
              r.deadline_at,
              r.created_at,
              r.updated_at,
              r.new_parent,
              r.admin_account_sid,
              r.admin_name,
              r.is_urgent,
              r.auto_confirm,
              r.matched_teacher_name,
              r.estimated_charge,
              r.parent_request_to_teacher,
              r.biweekly,
              r.regular_visit_term,
              r.requested_first_visit_schedule,
              r.schedule,
              r.preferable_teacher_gender,
              r.preferable_teacher_characteristics,
              r.parent_address,
              r.requested_teacher_name,
              r.additional_children_num,
              r.regularity,
              r.cancelled_info,
              r.re_recommend,
              r.teacher_specialties,
              (
                SELECT COUNT(*)
                FROM recommendation_teachers rt
                WHERE rt.recommendation_sid = r.sid
                  AND (rt.applied = 1 OR rt.accepted = 1)
              ) AS applied_count,
              (
                SELECT COUNT(*)
                FROM recommendation_teachers rt
                WHERE rt.recommendation_sid = r.sid AND rt.requested = 1
              ) AS requested_count
            FROM recommendation r
            WHERE {where_sql}
            ORDER BY r.created_at DESC, r.sid DESC
            LIMIT :limit OFFSET :offset
            """
        )
        if bindparams:
            query = query.bindparams(*bindparams)
        async with self._session_factory() as session:
            result = await session.execute(query, params)
            return [dict(row._mapping) for row in result]

    async def get_recommendation(self, sid: str) -> dict[str, Any] | None:
        """단건 상세 조회."""
        query = text(
            """
            SELECT
              r.sid,
              r.parent_account_sid,
              r.parent_name,
              r.parent_mobile,
              r.child_name,
              r.status,
              r.teacher_appliable,
              r.confirmed_at,
              r.cancelled_at,
              r.deadline_at,
              r.created_at,
              r.updated_at,
              r.new_parent,
              r.admin_account_sid,
              r.admin_name,
              r.is_urgent,
              r.auto_confirm,
              r.matched_teacher_name,
              r.estimated_charge,
              r.parent_request_to_teacher,
              r.biweekly,
              r.regular_visit_term,
              r.requested_first_visit_schedule,
              r.schedule,
              r.preferable_teacher_gender,
              r.preferable_teacher_characteristics,
              r.parent_address,
              r.lat,
              r.lng,
              r.requested_teacher_name,
              r.additional_children_num,
              r.regularity,
              r.cancelled_info,
              r.re_recommend,
              r.teacher_specialties,
              (
                SELECT COUNT(*)
                FROM recommendation_teachers rt
                WHERE rt.recommendation_sid = r.sid
                  AND (rt.applied = 1 OR rt.accepted = 1)
              ) AS applied_count,
              (
                SELECT COUNT(*)
                FROM recommendation_teachers rt
                WHERE rt.recommendation_sid = r.sid AND rt.requested = 1
              ) AS requested_count,
              (
                SELECT GROUP_CONCAT(tg.name SEPARATOR ', ')
                FROM recommendation_tag rtag
                JOIN tag tg ON tg.id = rtag.tag_id
                WHERE rtag.recommendation_sid = r.sid AND rtag.deleted_at IS NULL
              ) AS subject_tag_names
            FROM recommendation r
            WHERE r.sid = :sid
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(query, {"sid": sid})
            row = result.first()
            return dict(row._mapping) if row else None

    async def get_status_overlay(self, sids: list[str]) -> dict[str, dict[str, Any]]:
        """관리 신청서 목록용 — sid 리스트의 현재 동적 상태를 윈도우 무관하게 조회.

        snapshot은 첫 메모 시점에 freeze되므로 status/매칭/취소/마감은 옛 값으로 굳는다.
        관리 목록 로드 시 이 메서드로 현재값을 가져와 동적 필드만 덮어쓴다. 최근
        N시간 윈도우를 적용하지 않으므로 이미 취소(99)·매칭(40)된 오래된 신청서도
        현재 상태를 그대로 반환. replica에 row가 없으면(삭제됨) 해당 sid는 누락 →
        호출부가 frozen 값을 유지. 리턴: {sid: recommendation row}.
        """
        if not sids:
            return {}
        query = text(
            """
            SELECT
              sid, status, matched_teacher_name, cancelled_info,
              deadline_at, confirmed_at, cancelled_at
            FROM recommendation
            WHERE sid IN :sids
            """
        ).bindparams(bindparam("sids", expanding=True))
        async with self._session_factory() as session:
            result = await session.execute(query, {"sids": sids})
            return {row._mapping["sid"]: dict(row._mapping) for row in result}

    async def get_parent_history_counts(
        self, parent_account_sids: list[str]
    ) -> dict[str, dict[str, int]]:
        """학부모별 누적 이력. {sid: {app_count, confirmed_count, lesson_count}}.

        - app_count: recommendation 누적 신청 건수
        - confirmed_count: status IN (40, 90) 매칭 확정 건수
        - lesson_count: visit_instance status = 90 (방문완료) 건수
        """
        if not parent_account_sids:
            return {}

        rec_query = text(
            """
            SELECT
              parent_account_sid,
              COUNT(*) AS app_count,
              SUM(CASE WHEN status IN (40, 90) THEN 1 ELSE 0 END) AS confirmed_count
            FROM recommendation
            WHERE parent_account_sid IN :sids
            GROUP BY parent_account_sid
            """
        ).bindparams(bindparam("sids", expanding=True))
        visit_query = text(
            """
            SELECT
              parent_account_sid,
              COUNT(*) AS lesson_count
            FROM visit_instance
            WHERE parent_account_sid IN :sids
              AND status = 90
            GROUP BY parent_account_sid
            """
        ).bindparams(bindparam("sids", expanding=True))

        result: dict[str, dict[str, int]] = {
            sid: {"app_count": 0, "confirmed_count": 0, "lesson_count": 0}
            for sid in parent_account_sids
        }
        async with self._session_factory() as session:
            rec_rows = await session.execute(rec_query, {"sids": parent_account_sids})
            for row in rec_rows:
                m = row._mapping
                sid = m["parent_account_sid"]
                if sid in result:
                    result[sid]["app_count"] = int(m["app_count"] or 0)
                    result[sid]["confirmed_count"] = int(m["confirmed_count"] or 0)

            visit_rows = await session.execute(visit_query, {"sids": parent_account_sids})
            for row in visit_rows:
                m = row._mapping
                sid = m["parent_account_sid"]
                if sid in result:
                    result[sid]["lesson_count"] = int(m["lesson_count"] or 0)

        return result

    async def list_recommendation_teachers(self, sid: str) -> list[dict[str, Any]]:
        """해당 신청서에 요청된 선생님 목록 + 응답 상태 + 부모님 열람 정보.

        teacher_profile_view: viewer_id=parent_account_sid, teacher_sid=teacher.account_sid.
        viewed_at >= r.created_at 으로 "이번 신청서 이후 열람"만 인정 (누적 이력 분리).
        """
        query = text(
            """
            SELECT
              rt.teacher_account_sid,
              rt.applied,
              rt.accepted,
              rt.requested,
              rt.rejected,
              rt.last_responded_at,
              rt._created_at AS created_at,
              t.name AS teacher_name,
              t.experience_hour AS experience_hour,
              t.experience_hour_for_play AS experience_hour_for_play,
              t.experience_hour_for_study AS experience_hour_for_study,
              t.thumbnail_profile_url AS thumbnail_profile_url,
              t.university AS university,
              t.major AS major,
              tpv.viewed_at AS viewed_at,
              tpv.viewed_count AS viewed_count
            FROM recommendation_teachers rt
            LEFT JOIN teacher t ON t.account_sid = rt.teacher_account_sid
            LEFT JOIN recommendation r ON r.sid = rt.recommendation_sid
            LEFT JOIN teacher_profile_view tpv
              ON tpv.viewer_id = r.parent_account_sid
             AND tpv.teacher_sid = rt.teacher_account_sid
             AND tpv.viewed_at >= r.created_at
            WHERE rt.recommendation_sid = :sid
              AND rt.is_deleted = 0
            ORDER BY (rt.applied OR rt.accepted) DESC, rt.last_responded_at ASC
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(query, {"sid": sid})
            return [dict(row._mapping) for row in result]

    async def list_recommendation_teachers_batch(
        self, sids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """여러 신청서의 선생님 목록을 한 번에 조회. {sid: [teacher,...]}.

        - list 응답에 t1/t2 미리 채워주기 위함 (N+1 회피)
        - 정렬 순서는 단건 list_recommendation_teachers와 동일
        """
        if not sids:
            return {}

        query = text(
            """
            SELECT
              rt.recommendation_sid,
              rt.teacher_account_sid,
              rt.applied,
              rt.accepted,
              rt.requested,
              rt.rejected,
              rt.last_responded_at,
              rt._created_at AS created_at,
              t.name AS teacher_name,
              t.experience_hour AS experience_hour,
              t.experience_hour_for_play AS experience_hour_for_play,
              t.experience_hour_for_study AS experience_hour_for_study,
              t.thumbnail_profile_url AS thumbnail_profile_url,
              t.university AS university,
              t.major AS major,
              tpv.viewed_at AS viewed_at,
              tpv.viewed_count AS viewed_count
            FROM recommendation_teachers rt
            LEFT JOIN teacher t ON t.account_sid = rt.teacher_account_sid
            LEFT JOIN recommendation r ON r.sid = rt.recommendation_sid
            LEFT JOIN teacher_profile_view tpv
              ON tpv.viewer_id = r.parent_account_sid
             AND tpv.teacher_sid = rt.teacher_account_sid
             AND tpv.viewed_at >= r.created_at
            WHERE rt.recommendation_sid IN :sids
              AND rt.is_deleted = 0
            ORDER BY (rt.applied OR rt.accepted) DESC, rt.last_responded_at ASC
            """
        ).bindparams(bindparam("sids", expanding=True))

        result: dict[str, list[dict[str, Any]]] = {sid: [] for sid in sids}
        async with self._session_factory() as session:
            rows = await session.execute(query, {"sids": sids})
            for row in rows:
                m = dict(row._mapping)
                rec_sid = str(m.pop("recommendation_sid"))
                if rec_sid in result:
                    result[rec_sid].append(m)
        return result

    async def get_teacher_feedback_summary(
        self, teacher_account_sids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """선생님별 부모님 평가 집계. {sid: {review_count, recommend_count, recommend_rate}}.

        - parent_feedback.status = 2 (완료된 리뷰)만 집계
        - recommend_rate = recommend_count / review_count * 100 (0~100)
        """
        if not teacher_account_sids:
            return {}

        query = text(
            """
            SELECT
              teacher_account_sid,
              COUNT(*) AS review_count,
              SUM(CASE WHEN recommend = 1 THEN 1 ELSE 0 END) AS recommend_count
            FROM parent_feedback
            WHERE teacher_account_sid IN :sids
              AND status = 2
            GROUP BY teacher_account_sid
            """
        ).bindparams(bindparam("sids", expanding=True))

        result: dict[str, dict[str, Any]] = {
            sid: {"review_count": 0, "recommend_count": 0, "recommend_rate": None}
            for sid in teacher_account_sids
        }
        async with self._session_factory() as session:
            rows = await session.execute(query, {"sids": teacher_account_sids})
            for row in rows:
                m = row._mapping
                sid = m["teacher_account_sid"]
                if sid not in result:
                    continue
                rc = int(m["review_count"] or 0)
                rec = int(m["recommend_count"] or 0)
                result[sid]["review_count"] = rc
                result[sid]["recommend_count"] = rec
                result[sid]["recommend_rate"] = round(rec / rc * 100, 1) if rc > 0 else None
        return result


    async def list_subject_wages(self) -> dict[int, str]:
        """jrdtbl_subject_wage id → name. recommendation.teacher_specialties(1~6)와 매핑.

        (1=돌봄, 2=수학/과학, 3=운동, 4=예능, 5=외국어, 6=한글/국어)
        request_form_category(1~27)와는 다른 차원 — 시급 기준 큰 과목군.
        """
        query = text("SELECT id, name FROM jrdtbl_subject_wage")
        async with self._session_factory() as session:
            result = await session.execute(query)
            return {int(row._mapping["id"]): row._mapping["name"] for row in result}

    async def list_manual_ops_sids(
        self, *, menu: str, stale_hours: int = 48, limit: int = 500
    ) -> list[str]:
        """[수동관리 신청서] 두 메뉴의 대상 sid.

        - recommended — 선생님 추천 상태(20)가 된 지 stale_hours 경과.
          🚨 경과 기준은 created_at 이다. suggested_at 은 추천이 나갈 때마다 **갱신**되어
          "상태에 진입한 시점"을 못 나타낸다(2026-09-07 실측: status=20 인 21건 전부
          suggested_at 이 최근 2일 내인데, 그중 15건은 접수 48시간 초과).
          같은 실측에서 첫 추천 시각(min recommendation_teachers._created_at where
          suggested=1)이 **21건 전부 created_at 과 동일**했다 — 플랫폼이 접수 즉시
          선생님을 붙이므로 접수 시각이 곧 추천 상태 진입 시각이다.
          접수와 추천 사이에 지연이 생기는 구조로 바뀌면 이 기준을 다시 봐야 한다.
          오래 방치된 순(오름차순)이 곧 처리 우선순위다.
        - intake — 접수안내(10) 전건. 매칭확률 분위는 라우트에서 자른다.
          확률 계산이 파이썬 쪽(_compute_prob)이라 SQL 로 자를 수 없고,
          접수안내는 상시 100건대라 전건을 가져와도 부담이 없다.

        sid 만 돌려주고 본문은 기존 목록 파이프라인(list_recent_recommendations +
        augment + _to_row)을 그대로 재사용한다 — 카드 스키마를 두 벌 만들지 않으려는 것.
        """
        if menu == "recommended":
            query = text(
                """
                SELECT r.sid
                  FROM recommendation r
                 WHERE r.status = 20
                   AND r.created_at <= DATE_SUB(NOW(), INTERVAL :stale_hours HOUR)
                 ORDER BY r.created_at ASC
                 LIMIT :limit
                """
            )
            params = {"stale_hours": stale_hours, "limit": limit}
        else:
            query = text(
                """
                SELECT r.sid
                  FROM recommendation r
                 WHERE r.status = 10
                 ORDER BY r.created_at DESC
                 LIMIT :limit
                """
            )
            params = {"limit": limit}
        async with self._session_factory() as session:
            rows = (await session.execute(query, params)).fetchall()
        return [str(r[0]) for r in rows]

    async def prob_rate_cohort(
        self, *, start_days_ago: int = 90, end_days_ago: int = 30,
        split_days_ago: int | None = None,
    ) -> list[dict]:
        """예상 매칭확률 비율표용 정착 코호트 집계.

        (seg, age_bucket, reuse, responder) 별 건수와 매칭 건수를 돌려준다.
        매칭 정의 = status IN (40, 90).

        코호트 = created_at 이 [now - start_days_ago, now - end_days_ago] 인 신청서.
        end_days_ago 가 정착(settle) 여유다 — 결과가 여물 시간을 안 주면 최근 건이
        전부 '미매칭'으로 잡혀 비율이 낮게 깎인다.
        회귀 감시(backtest)는 여기에 더 오래된 구간을 넣어 과거 표를 재현한다.

        각 구간은 대표 관측 시점(3h / 24h / 72h) 스냅샷으로 만든다.
        그 시점에 **아직 열려 있던** 건만 세는 게 핵심 — 이미 확정·취소된 건을 넣으면
        "지금 열려 있는 신청서가 앞으로 매칭될 확률"이라는 질문에 답하지 못한다.

        성능: 신청서당 팩트를 derived table 에서 **한 번만** 만들고 구간 3개를 교차
        조인한다. 구간마다 상관 서브쿼리를 다시 도는 형태로 짜면 평가 횟수가 3배가 된다.

        재이용·응답 신호는 상관 서브쿼리가 아니라 **미리 집계한 derived table 조인**이다.
        상관 서브쿼리 판은 코호트가 80일로 늘어나자 Cloud Run 300s 요청 타임아웃을
        넘겨 504 가 났다(2026-09-07, 정착 30→10일 변경 직후). 신청서 8천 건 × 2개
        서브쿼리 = 1.6만 회 평가가 집계 2회 + 조인으로 바뀐다.
        recommendation_teachers 는 rt_days 로 잘라 derived table 을 작게 유지한다 —
        rt 행은 신청서 생성 시각 이후에 생기므로 코호트 시작보다 앞선 행은 필요 없다.

        split_days_ago — 주면 결과에 period('older'|'newer') 가 붙는다.
        회귀 감시는 학습·채점 구간이 서로 **붙어 있으므로**, 두 번 호출하는 대신
        합친 구간을 한 번 뽑아 나눈다. 부모 이력 derived table 이 16만 행을 훑어
        4.9만 행을 만드는데 호출마다 다시 만들어져, 두 번 돌리면 요청 타임아웃에 걸린다.
        """
        query = text(
            """
            SELECT f.seg, b.bucket AS age_bucket, f.reuse,
                   (f.first_resp IS NOT NULL
                    AND f.first_resp <= DATE_ADD(f.created_at, INTERVAL b.h HOUR)) AS responder,
                   f.period,
                   COUNT(*) AS n,
                   SUM(f.m) AS matched
              FROM (
                    SELECT r.created_at, r.confirmed_at, r.cancelled_at,
                           CASE WHEN :split_days IS NULL THEN 'all'
                                WHEN r.created_at <
                                     DATE_SUB(NOW(), INTERVAL :split_days DAY)
                                THEN 'older' ELSE 'newer' END AS period,
                           CASE WHEN r.is_urgent = 1 THEN 'urgent'
                                WHEN r.regularity = 3 THEN 'reg3'
                                ELSE 'reg2' END AS seg,
                           (r.status IN (40, 90)) AS m,
                           (ph.first_matched_at IS NOT NULL
                            AND ph.first_matched_at < r.created_at) AS reuse,
                           rt.first_resp
                      FROM recommendation r
                      LEFT JOIN (
                            SELECT p.parent_account_sid,
                                   MIN(p.created_at) AS first_matched_at
                              FROM recommendation p
                             WHERE p.status IN (40, 90)
                             GROUP BY p.parent_account_sid
                           ) ph ON ph.parent_account_sid = r.parent_account_sid
                      LEFT JOIN (
                            SELECT t.recommendation_sid,
                                   MIN(COALESCE(
                                     NULLIF(t.last_responded_at, '0000-00-00 00:00:00'),
                                     t._created_at)) AS first_resp
                              FROM recommendation_teachers t
                             WHERE (t.applied = 1 OR t.accepted = 1)
                               AND t._created_at >=
                                   DATE_SUB(NOW(), INTERVAL :rt_days DAY)
                             GROUP BY t.recommendation_sid
                           ) rt ON rt.recommendation_sid = r.sid
                     WHERE r.created_at >= DATE_SUB(NOW(), INTERVAL :start_days DAY)
                       AND r.created_at <= DATE_SUB(NOW(), INTERVAL :end_days DAY)
                   ) f
              JOIN (
                        SELECT 'lt6h' AS bucket, 3 AS h
              UNION ALL SELECT '6to48h', 24
              UNION ALL SELECT 'gte48h', 72
                   ) b
             WHERE (f.confirmed_at <= '2000-01-01'
                    OR f.confirmed_at > DATE_ADD(f.created_at, INTERVAL b.h HOUR))
               AND (f.cancelled_at <= '2000-01-01'
                    OR f.cancelled_at > DATE_ADD(f.created_at, INTERVAL b.h HOUR))
             GROUP BY f.seg, b.bucket, f.reuse, responder, f.period
            """
        )
        async with self._session_factory() as session:
            rows = (await session.execute(
                query, {"start_days": start_days_ago, "end_days": end_days_ago,
                        "rt_days": start_days_ago + 10,
                        "split_days": split_days_ago}
            )).fetchall()
        return [
            {"seg": r[0], "age_bucket": r[1], "reuse": int(r[2]),
             "responder": int(r[3]), "period": r[4],
             "n": int(r[5]), "matched": int(r[6] or 0)}
            for r in rows
        ]

    async def outcome_stats(self, sids: list[str], until=None) -> dict[str, int]:
        """자동 디스패치가 처리한 신청서들의 성과 집계.

        분모는 **suggested=1 (방문 제안을 실제로 받은 선생님)** 이다.
        자발 지원(suggested=0, applied=1)은 봇 성과가 아니므로 분자·분모 모두에서 뺀다.

        2026-09-03 정정: 이전 구현은 신청서에 달린 **모든** 행의 accepted 를 세고
        분모로 PG succeed_count 를 썼다. 신청서 1건당 제안 외 행이 다수 섞여
        수락률이 15.94% 처럼 비현실적으로 부풀었다(실제는 1% 대).

        until 을 주면 그 시각 이전에 생성된 행만 센다. 전/후 구간의 관측 시간을
        같게 맞추기 위한 것 — 없으면 시점 무관 전량.
        """
        if not sids:
            return {"apps": 0, "offered": 0, "accepted": 0, "rejected": 0, "matched": 0}
        cond = "" if until is None else " AND rt._created_at < :until"
        params: dict[str, Any] = {"sids": sids}
        if until is not None:
            params["until"] = until
        q_t = text(
            f"""
            SELECT
              SUM(rt.suggested = 1) AS offered,
              SUM(rt.suggested = 1 AND rt.accepted = 1) AS accepted,
              SUM(rt.suggested = 1 AND rt.rejected = 1) AS rejected
            FROM recommendation_teachers rt
            WHERE rt.recommendation_sid IN :sids{cond}
            """
        ).bindparams(bindparam("sids", expanding=True))
        q_r = text(
            """
            SELECT
              COUNT(*) AS apps,
              SUM(CASE WHEN r.status IN (40, 90) THEN 1 ELSE 0 END) AS matched
            FROM recommendation r
            WHERE r.sid IN :sids
            """
        ).bindparams(bindparam("sids", expanding=True))
        async with self._session_factory() as session:
            t = (await session.execute(q_t, params)).first()
            r = (await session.execute(q_r, {"sids": sids})).first()
        return {
            "apps": int((r and r[0]) or 0),
            "matched": int((r and r[1]) or 0),
            "offered": int((t and t[0]) or 0),
            "accepted": int((t and t[1]) or 0),
            "rejected": int((t and t[2]) or 0),
        }

    async def list_wage_ranges(self, sids: list[str]) -> dict[str, list[str]]:
        """신청서 sid → 부모님이 선택한 wage_range_type 코드 리스트 (DesiredCost enum).

        한 신청서가 여러 범위를 가질 수 있어 list로 반환. is_deleted=0만.
        """
        if not sids:
            return {}
        query = text(
            """
            SELECT recommendation_sid, wage_range_type
            FROM recommendation_teacher_wage_range
            WHERE recommendation_sid IN :sids
              AND is_deleted = 0
            ORDER BY recommendation_sid, recommendation_teacher_wage_range_id
            """
        ).bindparams(bindparam("sids", expanding=True))
        result: dict[str, list[str]] = {sid: [] for sid in sids}
        async with self._session_factory() as session:
            rows = await session.execute(query, {"sids": sids})
            for row in rows:
                m = row._mapping
                sid = str(m["recommendation_sid"])
                t = str(m["wage_range_type"])
                if sid in result and t not in result[sid]:
                    result[sid].append(t)
        return result

    async def list_teacher_subject_wages(
        self, teacher_sids: list[str], subject_wage_ids: list[int]
    ) -> dict[str, dict[int, dict[str, int]]]:
        """(teacher_sid → {subject_wage_id → {teacher_wage, parent_charge}}).

        jrdtbl_subject_teacher_wage_info에서 선생님별 과목별 현재 시급 조회.
        """
        if not teacher_sids or not subject_wage_ids:
            return {}
        query = text(
            """
            SELECT
              teacher_account_sid,
              subject_wage_id,
              teacher_wage_amount,
              parent_charge_amount
            FROM jrdtbl_subject_teacher_wage_info
            WHERE teacher_account_sid IN :tsids
              AND subject_wage_id IN :wids
            """
        ).bindparams(
            bindparam("tsids", expanding=True),
            bindparam("wids", expanding=True),
        )
        result: dict[str, dict[int, dict[str, int]]] = {sid: {} for sid in teacher_sids}
        async with self._session_factory() as session:
            rows = await session.execute(
                query, {"tsids": teacher_sids, "wids": subject_wage_ids}
            )
            for row in rows:
                m = row._mapping
                tsid = str(m["teacher_account_sid"])
                if tsid not in result:
                    continue
                result[tsid][int(m["subject_wage_id"])] = {
                    "teacher_wage": int(m["teacher_wage_amount"] or 0),
                    "parent_charge": int(m["parent_charge_amount"] or 0),
                }
        return result


    async def list_push_to_teachers(
        self, recommendation_sids: list[str], teacher_sids: list[str]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """(recommendation_sid, teacher_account_sid) → {count, last_sent_at, read_count, last_push_name}.

        fcm_send_history (FCM PUSH 발송 이력). 신청서 선생님 추천 PUSH만 집계:
          - app_type='TEACHER'
          - push_name LIKE '선생님_수업요청%' (일반 + 플래너)
          - deep_link LIKE 'recommend/normal?requestFormId=%' + 후처리 set 필터
        receiver_id 인덱스(MUL)로 range scan. deep_link IN(N)으로 인한 row × N
        텍스트 비교 폭주를 회피 (rec_sids 수백 개에서 응답 1~2분 폭증 → ~수초).
        """
        if not recommendation_sids or not teacher_sids:
            return {}
        rec_set = set(recommendation_sids)
        query = text(
            """
            SELECT
              deep_link,
              receiver_id AS teacher_account_sid,
              COUNT(*) AS cnt,
              MAX(sent_at) AS last_sent,
              SUM(CASE WHEN read_at IS NOT NULL THEN 1 ELSE 0 END) AS read_cnt,
              SUBSTRING_INDEX(GROUP_CONCAT(push_name ORDER BY sent_at DESC), ',', 1) AS last_push_name
            FROM fcm_send_history
            WHERE app_type = 'TEACHER'
              AND push_name LIKE '선생님_수업요청%'
              AND receiver_id IN :tsids
              AND deep_link LIKE 'recommend/normal?requestFormId=%'
              AND sent_at > NOW() - INTERVAL 30 DAY
            GROUP BY deep_link, receiver_id
            """
        ).bindparams(
            bindparam("tsids", expanding=True),
        )
        result: dict[tuple[str, str], dict[str, Any]] = {}
        async with self._session_factory() as session:
            rows = await session.execute(query, {"tsids": teacher_sids})
            for row in rows:
                m = row._mapping
                rec_sid = str(m["deep_link"]).replace("recommend/normal?requestFormId=", "")
                if rec_sid not in rec_set:
                    continue
                key = (rec_sid, str(m["teacher_account_sid"]))
                result[key] = {
                    "count": int(m["cnt"] or 0),
                    "last_sent_at": m["last_sent"],
                    "read_count": int(m["read_cnt"] or 0),
                    "last_push_name": (m["last_push_name"] or "").split(",")[0],
                }
        return result


    async def list_scheduled_child_counts(
        self, teacher_sids: list[str]
    ) -> dict[str, int]:
        """선생님별 방문예정(visit_instance.status=1) 상태인 유니크 아이 수.

        visit.status=10(진행중)은 종료 처리되지 않은 잔여 계약이 다수 섞여
        실제 활성 수업을 과대 표시함(예: 진행중 206건인데 방문예정 0건). 따라서
        '현재 선생님이 담당 중인 수업'은 앞으로 방문이 예정된(status=1) 건에서
        유니크 아이(child_account_sid) 수로 집계한다.
        """
        if not teacher_sids:
            return {}
        query = text(
            """
            SELECT matched_teacher_account_sid AS teacher_sid,
                   COUNT(DISTINCT child_account_sid) AS cnt
            FROM visit_instance
            WHERE matched_teacher_account_sid IN :tsids
              AND status = 1
            GROUP BY matched_teacher_account_sid
            """
        ).bindparams(bindparam("tsids", expanding=True))
        result: dict[str, int] = {sid: 0 for sid in teacher_sids}
        async with self._session_factory() as session:
            rows = await session.execute(query, {"tsids": teacher_sids})
            for row in rows:
                m = row._mapping
                tsid = str(m["teacher_sid"])
                if tsid in result:
                    result[tsid] = int(m["cnt"] or 0)
        return result


    async def list_teacher_weekly_availability(
        self, teacher_sids: list[str]
    ) -> dict[str, set[str]]:
        """선생님별 가능 요일 집합 (DayOfWeek 영문 대문자).

        schedule 테이블의 mon~sun 비트마스크 != 0 인 요일을 가능으로 판단.
        비트마스크 30bit 정밀 시간 해석은 v2 — 일단 요일 단위 매칭만.
        """
        if not teacher_sids:
            return {}
        query = text(
            """
            SELECT account_sid, mon, tue, wed, thu, fri, sat, sun
            FROM schedule
            WHERE account_sid IN :tsids
            """
        ).bindparams(bindparam("tsids", expanding=True))
        result: dict[str, set[str]] = {sid: set() for sid in teacher_sids}
        cols = (
            ("mon", "MONDAY"), ("tue", "TUESDAY"), ("wed", "WEDNESDAY"),
            ("thu", "THURSDAY"), ("fri", "FRIDAY"),
            ("sat", "SATURDAY"), ("sun", "SUNDAY"),
        )
        async with self._session_factory() as session:
            rows = await session.execute(query, {"tsids": teacher_sids})
            for row in rows:
                m = row._mapping
                tsid = str(m["account_sid"])
                if tsid not in result:
                    continue
                for col, name in cols:
                    if int(m[col] or 0) != 0:
                        result[tsid].add(name)
        return result

    async def find_nearby_sigungu(
        self, lat: float, lng: float, n: int = 3
    ) -> list[str]:
        """부모 좌표에서 가까운 시군구 법정동코드 N개.

        service_area_geometry의 시군구 중심좌표(center_lat/lng)와 ST_Distance_Sphere
        거리순. 부모의 행정구역 코드 컬럼은 NULL이라 좌표만 신뢰 (WELL2-100 검증).
        """
        query = text(
            """
            SELECT g.legal_dong_code
            FROM service_area_geometry g
            ORDER BY ST_Distance_Sphere(
                POINT(:lng, :lat), POINT(g.center_lng, g.center_lat)
            ) ASC
            LIMIT :n
            """
        )
        async with self._session_factory() as session:
            rows = await session.execute(query, {"lat": lat, "lng": lng, "n": n})
            return [r._mapping["legal_dong_code"] for r in rows]

    async def list_candidate_teachers(
        self,
        recommendation_sid: str,
        gu_codes: list[str],
        subject_id: int,
        statuses: list[int],
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        """지원 0개 신청서용 선생님 후보 풀.

        teacher_preference_service_area(매일 갱신되는 선생님 선호 활동지)에서 인근
        시군구 + 활동상태 + 해당 과목 활동소개 보유 + 이미 추천된 선생님 제외.
        과목은 자기소개 태그 컬럼(intro_col) 보유 여부로 거른다 — 요일은 거르지 않고
        LLM이 day_match로 판단. 정렬은 선호우선순위(priority) → 진행중 수업 0개 우선
        (active_kids ASC) → 최근 로그인순(account.last_signed_in DESC). 부모 추천순·
        누적 수업시간은 제외해 실적 적은 신규도 들어오게 하고, 동순위는 여력·접속
        활성으로 가른다. 가용요일/평가/여력/시급/자기소개를 함께
        실어 LLM 입력으로 쓴다. udong_teacher_list는 갱신 중단되어 미사용.
        intro_col은 화이트리스트(_SPECIALTY_INTRO)에서만 와 SQL 포맷에 안전하다.
        """
        if not gu_codes:
            return []
        intro_col = _SPECIALTY_INTRO.get(subject_id, _SPECIALTY_INTRO[5])
        query = text(
            f"""
            SELECT
              t.account_sid AS teacher_sid, t.name,
              t.activity_status, t.activity_status_text,
              t.experience_hour, t.experience_hour_for_study,
              t.experience_hour_for_play, t.university, t.major,
              t.lateness, t.gender AS teacher_gender,
              MIN(tps.priority) AS pref_priority,
              MAX(sch.mon <> 0) AS mon, MAX(sch.tue <> 0) AS tue,
              MAX(sch.wed <> 0) AS wed, MAX(sch.thu <> 0) AS thu,
              MAX(sch.fri <> 0) AS fri, MAX(sch.sat <> 0) AS sat,
              MAX(sch.sun <> 0) AS sun,
              w.teacher_wage_amount AS subject_wage,
              (SELECT COUNT(*) FROM parent_feedback pf
                 WHERE pf.teacher_account_sid = t.account_sid AND pf.status = 2) AS reviews,
              (SELECT SUM(CASE WHEN pf.recommend = 1 THEN 1 ELSE 0 END) FROM parent_feedback pf
                 WHERE pf.teacher_account_sid = t.account_sid AND pf.status = 2) AS recommends,
              (SELECT COUNT(DISTINCT vi.child_account_sid) FROM visit_instance vi
                 WHERE vi.matched_teacher_account_sid = t.account_sid AND vi.status = 1) AS active_kids,
              LEFT(t.{intro_col}, 300) AS intro,
              MAX(a.last_signed_in) AS last_login
            FROM teacher_preference_service_area tps
            JOIN teacher t ON t.account_sid = tps.teacher_account_sid
            LEFT JOIN schedule sch ON sch.account_sid = t.account_sid
            LEFT JOIN jrdtbl_subject_teacher_wage_info w
              ON w.teacher_account_sid = t.account_sid AND w.subject_wage_id = :subject_id
            LEFT JOIN account a ON a.sid = t.account_sid
            WHERE tps.legal_dong_code IN :gu_codes
              AND t.activity_status IN :statuses
              AND t.searchable = 1
              AND t.{intro_col} IS NOT NULL AND TRIM(t.{intro_col}) <> ''
              AND (
                NOT EXISTS (
                  SELECT 1 FROM recommendation_tag rtag
                  WHERE rtag.recommendation_sid = :sid AND rtag.deleted_at IS NULL
                )
                OR EXISTS (
                  SELECT 1 FROM teacher_tags tt
                  WHERE tt.account_sid = t.account_sid AND tt.searchable = 1
                    AND tt.tag_id IN (
                      SELECT rtag.tag_id FROM recommendation_tag rtag
                      WHERE rtag.recommendation_sid = :sid AND rtag.deleted_at IS NULL
                    )
                )
              )
              AND NOT EXISTS (
                SELECT 1 FROM recommendation_teachers rt
                WHERE rt.recommendation_sid = :sid AND rt.teacher_account_sid = t.account_sid
              )
            GROUP BY t.account_sid
            ORDER BY pref_priority ASC, active_kids ASC, last_login DESC
            LIMIT :k
            """
        ).bindparams(
            bindparam("gu_codes", expanding=True),
            bindparam("statuses", expanding=True),
        )
        async with self._session_factory() as session:
            rows = await session.execute(
                query,
                {
                    "gu_codes": gu_codes,
                    "statuses": statuses,
                    "sid": recommendation_sid,
                    "subject_id": subject_id,
                    "k": limit,
                },
            )
            return [dict(row._mapping) for row in rows]

    async def list_recovery_candidates(
        self,
        recommendation_sid: str,
        lat: float,
        lng: float,
        subject_id: int,
        statuses: list[int],
        radius_m: int = 5000,
        apply_days: int = 30,
        close_days: int = 3,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """'지역 회수' 후보 — 부모 좌표 반경 내에서 둘 중 하나 이상인 선생님.

          (A) 최근 apply_days일 인근 신청서에 지원(applied)했으나 미선택
              (다른 선생님과 매칭됨 OR 마감 무산)
          (B) 최근 close_days일 인근 수업이 종료(visit.status=90 방문종료)

        지역은 recommendation.lat/lng로 매칭(visit은 recommendation_sid로 연결,
        recommendation의 area_code는 전부 NULL이라 좌표만 신뢰). 신호(미선택 횟수·
        최근시각, 종료 수업 수·최근시각)를 teacher 상세와 함께 반환해 LLM이 '연락하면
        받을 만한' 순으로 재정렬·사유 생성하게 한다. 과목은 거르지 않고(회수는 폭넓게)
        신청 과목 시급·자기소개만 부착. intro_col은 화이트리스트라 SQL 포맷에 안전.
        """
        intro_col = _SPECIALTY_INTRO.get(subject_id, _SPECIALTY_INTRO[5])
        sig_apply = text(
            """
            SELECT rt.teacher_account_sid AS tsid,
                   COUNT(*) AS unmatched_count,
                   MAX(rt._created_at) AS last_unmatched_at
            FROM recommendation_teachers rt
            JOIN recommendation r ON r.sid = rt.recommendation_sid
            WHERE rt.applied = 1 AND rt.is_deleted = b'0'
              AND rt._created_at >= DATE_SUB(NOW(), INTERVAL :apply_days DAY)
              AND r.lat <> 0
              AND ST_Distance_Sphere(POINT(:lng, :lat), POINT(r.lng, r.lat)) <= :radius_m
              AND (
                    (r.matched_teacher_account_sid IS NOT NULL
                     AND r.matched_teacher_account_sid <> rt.teacher_account_sid)
                 OR (r.matched_teacher_account_sid IS NULL
                     AND (r.status = 99 OR (r.deadline_at IS NOT NULL AND r.deadline_at < NOW())))
                  )
            GROUP BY rt.teacher_account_sid
            """
        )
        sig_closed = text(
            """
            SELECT v.matched_teacher_account_sid AS tsid,
                   COUNT(*) AS closed_count,
                   MAX(v.closed_at) AS last_closed_at
            FROM visit v
            JOIN recommendation r ON r.sid = v.recommendation_sid
            WHERE v.status = 90
              AND v.closed_at >= DATE_SUB(NOW(), INTERVAL :close_days DAY)
              AND r.lat <> 0
              AND ST_Distance_Sphere(POINT(:lng, :lat), POINT(r.lng, r.lat)) <= :radius_m
            GROUP BY v.matched_teacher_account_sid
            """
        )
        async with self._session_factory() as session:
            rows_a = (await session.execute(
                sig_apply,
                {"lat": lat, "lng": lng, "radius_m": radius_m, "apply_days": apply_days},
            )).all()
            rows_b = (await session.execute(
                sig_closed,
                {"lat": lat, "lng": lng, "radius_m": radius_m, "close_days": close_days},
            )).all()

        sig: dict[str, dict[str, Any]] = {}
        for row in rows_a:
            m = row._mapping
            sig[m["tsid"]] = {
                "unmatched_count": int(m["unmatched_count"] or 0),
                "last_unmatched_at": m["last_unmatched_at"],
                "closed_count": 0, "last_closed_at": None,
            }
        for row in rows_b:
            m = row._mapping
            d = sig.setdefault(m["tsid"], {
                "unmatched_count": 0, "last_unmatched_at": None,
                "closed_count": 0, "last_closed_at": None,
            })
            d["closed_count"] = int(m["closed_count"] or 0)
            d["last_closed_at"] = m["last_closed_at"]
        if not sig:
            return []

        detail = text(
            f"""
            SELECT
              t.account_sid AS teacher_sid, t.name,
              t.activity_status, t.activity_status_text,
              t.experience_hour, t.experience_hour_for_study,
              t.university, t.major, t.lateness,
              MAX(sch.mon <> 0) AS mon, MAX(sch.tue <> 0) AS tue,
              MAX(sch.wed <> 0) AS wed, MAX(sch.thu <> 0) AS thu,
              MAX(sch.fri <> 0) AS fri, MAX(sch.sat <> 0) AS sat,
              MAX(sch.sun <> 0) AS sun,
              w.teacher_wage_amount AS subject_wage,
              (SELECT COUNT(*) FROM parent_feedback pf
                 WHERE pf.teacher_account_sid = t.account_sid AND pf.status = 2) AS reviews,
              (SELECT SUM(CASE WHEN pf.recommend = 1 THEN 1 ELSE 0 END) FROM parent_feedback pf
                 WHERE pf.teacher_account_sid = t.account_sid AND pf.status = 2) AS recommends,
              (SELECT COUNT(DISTINCT vi.child_account_sid) FROM visit_instance vi
                 WHERE vi.matched_teacher_account_sid = t.account_sid AND vi.status = 1) AS active_kids,
              LEFT(t.{intro_col}, 300) AS intro
            FROM teacher t
            LEFT JOIN schedule sch ON sch.account_sid = t.account_sid
            LEFT JOIN jrdtbl_subject_teacher_wage_info w
              ON w.teacher_account_sid = t.account_sid AND w.subject_wage_id = :subject_id
            WHERE t.account_sid IN :tsids
              AND t.activity_status IN :statuses
              AND t.searchable = 1
              AND (
                NOT EXISTS (
                  SELECT 1 FROM recommendation_tag rtag
                  WHERE rtag.recommendation_sid = :sid AND rtag.deleted_at IS NULL
                )
                OR EXISTS (
                  SELECT 1 FROM teacher_tags tt
                  WHERE tt.account_sid = t.account_sid AND tt.searchable = 1
                    AND tt.tag_id IN (
                      SELECT rtag.tag_id FROM recommendation_tag rtag
                      WHERE rtag.recommendation_sid = :sid AND rtag.deleted_at IS NULL
                    )
                )
              )
            GROUP BY t.account_sid
            """
        ).bindparams(
            bindparam("tsids", expanding=True),
            bindparam("statuses", expanding=True),
        )
        async with self._session_factory() as session:
            rows = await session.execute(
                detail,
                {
                    "tsids": list(sig.keys()),
                    "statuses": statuses,
                    "subject_id": subject_id,
                    "sid": recommendation_sid,
                },
            )
            cands = []
            for row in rows:
                d = dict(row._mapping)
                s = sig.get(d["teacher_sid"], {})
                d["unmatched_count"] = s.get("unmatched_count", 0)
                d["last_unmatched_at"] = s.get("last_unmatched_at")
                d["closed_count"] = s.get("closed_count", 0)
                d["last_closed_at"] = s.get("last_closed_at")
                cands.append(d)
        # (B) 막 빈 슬롯(최근 종료) 우선 → 미선택 횟수 많은 순
        cands.sort(key=lambda c: (0 if c["closed_count"] > 0 else 1, -c["unmatched_count"]))
        return cands[:limit]

    async def list_auto_dispatch_candidates(
        self, min_age_minutes: int = 60, limit: int = 100
    ) -> list[dict[str, Any]]:
        """자동 디스패치 1차 필터 (replica MySQL).

        조건:
          - status = 10 (ACCEPTED)
          - created_at <= NOW() - INTERVAL N MINUTE  (생성 후 N분 이상 경과)
          - lat/lng NOT NULL (거리 매칭 필수)
          - **regularity = 2 (REGULAR, 정기수업)** — 긴급돌봄(1=ONE_TIME)·
            프로그램(3=MULTIPLE_TIMES) 제외. Regularity enum: 0=NONE/1=ONE_TIME/
            2=REGULAR/3=MULTIPLE_TIMES. 프로그램 신청서는 코드상 ONE_TIME 또는
            MULTIPLE_TIMES 로 생성됨 (CreateProgramRecommendationRequest:84).
          - is_urgent = 0 (이중 안전망: regularity=1과 100% 겹치지만 명시)
          - package_sid 비어있음 (프로그램 패키지 신청서 제외 — 이중 안전망)
          - **부모 지목 없음**: requested_teacher_name 컬럼 비어있음 AND
            recommendation_teachers 에 requested=1 row 없음
            (부모가 신청서에서 특정 선생님을 콕 찍은 케이스는 자동화 대상에서 제외 —
            의향이 분명한 신청서를 시스템이 흔들지 않음)
          - **수업 가능한 선생님 1명 이하**: recommendation_teachers 에서
            applied=1 OR accepted=1 인 선생님 수 <= 1
            (2026-09-02 변경: 이전에는 0명만 대상. 1명뿐이면 부모가 비교할 선택지가
            사실상 없어 자동화로 후보를 늘려준다. 이미 응답한 선생님은
            list_candidate_teachers 의 NOT EXISTS rt 로 중복 추가되지 않는다.)
            NOTE: 기존 지목 체크와 동일하게 is_deleted 는 보지 않는다.
          - **부모 주시 등급 제외**: account.observation_level IN (9, 90, 99)
            = 관리필요(ELEPHANT 9) · 추천제한(DOLPHIN 90) · 이용제한(TURTLE 99).
            관리필요는 매칭에 각별한 주의가 필요한 가정이라 사람이 봐야 하고,
            추천제한·이용제한은 정책상 추천 자체를 막은 고객.
            ObservationLevel enum (app-server domain/account/model/ObservationLevel.java).

        candidates.py 라우트와 동일한 필드 풀 셀렉트 → 호출자는 _parse_schedule /
        list_candidate_teachers 로 그대로 넘길 수 있음. list_candidate_teachers 가
        이미 신청서에 있는 선생님은 자동 제외(db.py 의 NOT EXISTS rt) — 중복 안전.

        matching-ops PG 의 auto_run / memo / handler 제외는 별도 단계
        (auto_run_store.get_excluded_sids).
        """
        query = text(
            """
            SELECT
              r.sid,
              r.parent_account_sid,
              r.parent_name,
              r.child_name,
              r.status,
              r.teacher_appliable,
              r.deadline_at,
              r.created_at,
              r.is_urgent,
              r.estimated_charge,
              r.parent_request_to_teacher,
              r.biweekly,
              r.regular_visit_term,
              r.schedule,
              r.preferable_teacher_gender,
              r.preferable_teacher_characteristics,
              r.parent_address,
              r.lat,
              r.lng,
              r.regularity,
              r.teacher_specialties,
              (
                SELECT GROUP_CONCAT(tg.name SEPARATOR ', ')
                FROM recommendation_tag rtag
                JOIN tag tg ON tg.id = rtag.tag_id
                WHERE rtag.recommendation_sid = r.sid AND rtag.deleted_at IS NULL
              ) AS subject_tag_names,
              0 AS applied_count,
              (
                SELECT COUNT(DISTINCT rt.teacher_account_sid)
                  FROM recommendation_teachers rt
                 WHERE rt.recommendation_sid = r.sid
                   AND (rt.applied = 1 OR rt.accepted = 1)
              ) AS pre_responder_count
            FROM recommendation r
            WHERE r.status = 10
              AND r.created_at <= NOW() - INTERVAL :min_age MINUTE
              AND r.lat IS NOT NULL
              AND r.lng IS NOT NULL
              AND r.regularity = 2
              AND r.is_urgent = 0
              AND (r.package_sid IS NULL OR r.package_sid = '')
              AND (r.requested_teacher_name IS NULL OR r.requested_teacher_name = '')
              AND NOT EXISTS (
                SELECT 1 FROM recommendation_teachers rt
                WHERE rt.recommendation_sid = r.sid
                  AND rt.requested = 1
              )
              AND (
                SELECT COUNT(DISTINCT rt.teacher_account_sid)
                  FROM recommendation_teachers rt
                 WHERE rt.recommendation_sid = r.sid
                   AND (rt.applied = 1 OR rt.accepted = 1)
              ) <= 1
              AND NOT EXISTS (
                SELECT 1 FROM account pa
                WHERE pa.sid = r.parent_account_sid
                  AND pa.observation_level IN (9, 90, 99)
              )
            ORDER BY r.created_at ASC
            LIMIT :limit
            """
        )
        async with self._session_factory() as session:
            rows = await session.execute(
                query, {"min_age": min_age_minutes, "limit": limit}
            )
            return [dict(row._mapping) for row in rows]

    async def count_today_teacher_recommendations(
        self, teacher_sids: list[str]
    ) -> dict[str, int]:
        """선생님별 KST 오늘(자정~현재) '수업 추천' 알림 발송 수.

        fcm_send_history.app_type='TEACHER' AND push_name LIKE '선생님_수업요청%'
        기준 — 일반(`선생님_수업요청_일반`) + 플래너(`선생님_수업요청_플래너`) 둘 다 포함.
        알림톡(긴급돌봄 teacher_urgent_request)은 별도 테이블이라 1차 미포함;
        FCM 이 비-긴급의 주 채널이라 cooldown 핵심 신호로 충분. 추후 보강 여지.

        MySQL 서버 시간대가 KST(jaranda prod)면 CURDATE()=KST 오늘 자정.
        """
        if not teacher_sids:
            return {}
        query = text(
            """
            SELECT
              receiver_id AS teacher_sid,
              COUNT(*) AS cnt
            FROM fcm_send_history
            WHERE app_type = 'TEACHER'
              AND push_name LIKE '선생님_수업요청%'
              AND receiver_id IN :tsids
              AND sent_at >= CURDATE()
            GROUP BY receiver_id
            """
        ).bindparams(bindparam("tsids", expanding=True))
        result: dict[str, int] = {}
        async with self._session_factory() as session:
            rows = await session.execute(query, {"tsids": teacher_sids})
            for row in rows:
                m = row._mapping
                result[str(m["teacher_sid"])] = int(m["cnt"] or 0)
        return result


_replica: JarandaReplica | None = None


def get_replica() -> JarandaReplica:
    global _replica
    if _replica is None:
        _replica = JarandaReplica()
    return _replica
