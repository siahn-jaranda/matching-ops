"""자동 디스패치 — 지원 0개 신청서에 LLM 추천 선생님 추가 + 방문 제안 발송.

흐름:
  1) 신청서 1차 필터 (replica: status=10, age≥min_age(운영 1h), 지목 0명,
     수업 가능한 선생님 1명 이하, 좌표 있음,
     부모 observation_level 관리필요/추천제한/이용제한 제외)
  2) PG 제외 (auto_run.live / memo / handler 어느 하나라도 있으면 skip)
  3) 일일 cap (live만): KST 오늘 처리한 신청서 수가 daily_max_apps 이상이면 중단
  4) 신청서별 처리:
     a. 후보 풀 (인접 시군구·활동중·과목 매칭) 50명 확보
     b. cooldown — 오늘 추천 알림 ≥ cap 받은 선생님 사전 제외
     c. LLM 랭킹 (AUTO_DISPATCH_SYSTEM_PROMPT, 상위 20명)
     d. live: console add_teachers → write_memo → send_visit_offers
        dry-run: 호출 안 함, 결과만 로그
     e. matching_ops_auto_run UPSERT (성공·실패 무관)
  5) 슬랙 요약 알림 (옵션) + 결과 dict 반환

신청서 1건 실패가 전체를 멈추지 않도록 per-recommendation try/except.
race: replica polling 기반이라 atomic하지 않음. console succeedCount=0 = 충돌 신호.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.auto_run_store import auto_run_available, get_auto_run_store
from src.config import settings
from src.console_client import ConsoleApiError, console_available, get_console_client
from src.db import get_replica
from src.handler_store import get_handler_store
from src.llm_client import AUTO_DISPATCH_SYSTEM_PROMPT, get_llm_client
from src.llm_insight_store import get_llm_insight_store, llm_insight_available
from src.memo_store import get_memo_store
from src.routes.candidates import (
    SPECIALTY_NAME,
    _build_input,
    _candidate_view,
    _parse_schedule,
)
from src.snapshot_store import get_snapshot_store

# 자동 디스패치가 matching-ops 대시보드에 신청서를 노출할 때 사용하는 system identity.
# 운영자 release/추가 메모는 별도 식별자(@matching-ops 도메인)로 구분된다.
AUTO_BOT_EMAIL = "auto_bot@matching-ops"
AUTO_BOT_NAME = "Auto_bot"
AUTO_BOT_TAG = "auto-dispatch"

logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

# 신청서 1건당 LLM 입력 후보 풀 상한. cooldown·variant 필터 후 top_n 채울 여유.
_RAW_POOL_LIMIT = 100
# LLM 응답 max_tokens — 상위 20명 ranking은 RECOMMEND(5~7) 대비 더 필요.
# 한 item ~80 token × 20 + summary/note → 약 1700-2000. 안전하게 4096.
_LLM_MAX_TOKENS = 4096

# =====================================================================
# V0 전체 승격 (2026-07-02) — A/B v2 종료
# =====================================================================
# 3주 관찰(16일·172 신청서) 결과 V0(R1+R2+A1시급+A2요일+A6성별)이 챔피언 확정.
#   V0 매칭률 14.89% (W1·W3 각각 22.22% 재현), 수락률 W3 0.91%
#   V1 매칭률 13.24% (시급 A1 제외) — V0 근소 열등
#   V2 매칭률 3.51% (같은 구 A4) — 확연히 열등, 폐기
# 실험 결과 docs/AB_TEST_v2_RESULT.md.
#
# 이번 릴리스: _assign_variant 항상 0 반환 → 전체 신청서 V0 로직 적용.
# variant 컬럼은 그대로 유지 (모두 0으로 기록). 다음 세대 A/B 도입 시 재사용.

_VARIANT_NGU = {0: 3, 1: 3, 2: 1}  # 미사용 (variant 항상 0). 참고용 유지.

# A1 시급 하드필터는 2026-09-02 제거됨. 시급대 정본(_WAGE_RANGES)은
# routes/candidates.py 로 옮겨 LLM 입력 신호로만 쓴다.

_DAY_KEY = {
    "MONDAY": "mon", "TUESDAY": "tue", "WEDNESDAY": "wed",
    "THURSDAY": "thu", "FRIDAY": "fri", "SATURDAY": "sat", "SUNDAY": "sun",
}


def _assign_variant(sid: str) -> int:
    """V0 100% 승격 (2026-07-02). 모든 신청서에 V0 로직만 적용.

    A/B v2 종료. docs/AB_TEST_v2_RESULT.md 참고.
    다음 세대 A/B 재개 시 md5 hash 분배 복원.
    """
    return 0


def _vet_pass(c: dict[str, Any]) -> bool:
    """R1+R2: 신참(리뷰 0)·저성과·경력 부족 제외. 모든 arm 공통."""
    exp = float(c.get("experience_hour") or 0)
    reviews = int(c.get("reviews") or 0)
    recommends = int(c.get("recommends") or 0)
    if exp < 200:
        return False
    if reviews < 10:
        return False
    if recommends / max(reviews, 1) < 0.80:
        return False
    return True


def _a2_day_match(c: dict[str, Any], schedule_json: Any) -> bool:
    """A2: 신청서 possible_day_of_weeks ∩ 선생님 가용요일 ≥ 1.

    '요일 정보 없음'은 양쪽 모두 판단 불가로 보고 통과시킨다.
      - 신청서에 요일이 없으면 통과 (기존)
      - 선생님 요일 데이터가 없어도 통과 (2026-08-31 추가)
        schedule row 자체가 없으면 mon~sun 이 전부 None,
        row 는 있으나 미입력이면 전부 0 — 둘 다 '미설정'으로 취급.

    근거: 최근 5일 11,379 (신청서×선생님) 쌍 실측
      요일 일치      9,045쌍 수락률 3.17%
      요일 불일치    1,241쌍 수락률 1.77%
      요일 미설정    1,093쌍 수락률 4.12%  ← 세 그룹 중 최고
    미설정은 '시간이 없다'가 아니라 '폼을 안 채웠다'는 뜻이라 배제 근거가 없다.
    R1+R2 통과 256명 중 33명(12.9%)이 여기 해당하며, 이들의 90일 평균 지원은
    4.52건으로 요일 설정자(3.20건)보다 오히려 활발하다.
    요일이 실제로 안 맞는 후보는 LLM 이 day_match 를 최우선 신호로 보고 거른다.
    """
    try:
        sched = json.loads(schedule_json) if isinstance(schedule_json, str) else schedule_json
    except (ValueError, TypeError):
        return True  # 파싱 실패 → 무관 통과
    if not isinstance(sched, dict):
        return True
    days = sched.get("possible_day_of_weeks") or []
    if not days:
        return True  # 신청서에 요일 정보 없으면 통과
    if not any(bool(c.get(k)) for k in _DAY_KEY.values()):
        return True  # 선생님 요일 미설정(None 또는 전부 0) → 판단 불가 → 통과
    for d in days:
        key = _DAY_KEY.get(d)
        if key and bool(c.get(key)):
            return True
    return False


def _a6_gender_match(c: dict[str, Any], preferred_gender: int | None) -> bool:
    """A6: 부모 선호 성별과 선생님 성별 매칭. 모든 variant 공통.

    규칙 (자란다 도메인):
    - 부모 1 (여성 선호) → teacher.gender=1 (여성)만 통과
    - 부모 2 (남성 선호) → 무관 (모두 통과). 남자 선생님 풀 작아 hard 시 풀 0 위험
    - 부모 3 (무관) → 모두 통과
    - 부모 NULL/0 → 무관
    """
    if preferred_gender != 1:
        return True  # 남성 선호·무관·NULL 모두 통과
    t_gender = c.get("teacher_gender")
    return t_gender == 1


def _apply_variant_filter(
    c: dict[str, Any],
    variant: int,
    schedule_json: Any,
    preferred_gender: int | None,
) -> bool:
    """V0 로직 hard filter.

    R1+R2 + A2(요일) + A6(성별) — 전 신청서 공통.
    variant 파라미터는 하위호환 위해 유지 (_assign_variant가 항상 0).

    A1(시급) 은 2026-09-02 제거. 참조하던 _WAGE_RANGES 가 2026-04~05 코드 이관을
    반영하지 못해 신청서의 82.4% 에서 무력화돼 있었고, 작동하는 나머지에서는
    희망 상한 초과 선생님의 수락률이 2.4~3.4배 높아 역방향으로 동작했다.
    정본 범위로 정렬하면 오히려 실제 성사 매칭의 31% 를 차단한다.
    시급대는 이제 LLM 입력(parent_wage_preference)의 소프트 신호로만 쓴다.
    """
    if not _vet_pass(c):
        return False
    if not _a6_gender_match(c, preferred_gender):
        return False
    if not _a2_day_match(c, schedule_json):
        return False
    return True


class AutoDispatchUnavailable(RuntimeError):
    """필수 설정 누락 — 호출자가 503으로 변환."""


async def run_once(
    *,
    dry_run: bool,
    max_apps: int | None,
    operator_email: str,
) -> dict[str, Any]:
    """자동 디스패치 1회 실행. 결과 요약 dict 반환.

    max_apps=None이면 settings.auto_dispatch_daily_max_apps 사용.
    """
    started_at = datetime.now(KST)
    requested_cap = max_apps if max_apps is not None else settings.auto_dispatch_daily_max_apps

    if not auto_run_available():
        raise AutoDispatchUnavailable("MATCHING_OPS_DB_URL 미설정")
    if not llm_insight_available():
        raise AutoDispatchUnavailable("ANTHROPIC_API_KEY 미설정")
    if not dry_run and not console_available():
        raise AutoDispatchUnavailable("CONSOLE_USERNAME / CONSOLE_PASSWORD 미설정")

    replica = get_replica()
    store = get_auto_run_store()
    llm = get_llm_client()
    console = get_console_client()

    # 1) replica 1차 필터 — 항상 충분히 큰 LIMIT.
    # 이전 max(cap*5, 10) 은 max_apps=1·daily_cap=30 같이 가속된 환경에서
    # raw=10건 모두 이전 라이브 처리로 excluded → eligible=0 으로 매 cron 스킵
    # (2026-06-01 KST 17:00~ 발견). 모수가 20-30건 수준이라 200 으로 여유 확보.
    raw = await replica.list_auto_dispatch_candidates(
        min_age_minutes=settings.auto_dispatch_min_age_minutes,
        limit=max(requested_cap * 50, 200),
    )
    sids_pre = [r["sid"] for r in raw]
    logger.info("auto_dispatch step1 raw_candidates=%d", len(sids_pre))

    # 2) PG 제외
    excluded = await store.get_excluded_sids(sids_pre)
    eligible = [r for r in raw if r["sid"] not in excluded]
    logger.info(
        "auto_dispatch step2 excluded=%d eligible=%d",
        len(excluded), len(eligible),
    )

    # 3) 일일 cap (live만)
    skipped_reason: str | None = None
    if not dry_run:
        today_live = await store.count_today_runs(dry_run=False)
        remaining = max(0, settings.auto_dispatch_daily_max_apps - today_live)
        effective_cap = min(requested_cap, remaining)
        if effective_cap <= 0:
            skipped_reason = (
                f"daily_cap_reached today_live={today_live} "
                f"max={settings.auto_dispatch_daily_max_apps}"
            )
            logger.warning("auto_dispatch %s", skipped_reason)
            targets = []
        else:
            targets = eligible[:effective_cap]
    else:
        targets = eligible[:requested_cap]

    # 4) 신청서별 처리
    processed: list[dict[str, Any]] = []
    for app in targets:
        sid = str(app["sid"])
        try:
            res = await _process_one(
                app, dry_run=dry_run, operator_email=operator_email,
                replica=replica, llm=llm, console=console, store=store,
            )
        except AutoDispatchUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("auto_dispatch process_one failed sid=%s", sid)
            try:
                await store.record_run(
                    recommendation_sid=sid,
                    dry_run=dry_run,
                    pool_size=0,
                    added_count=0,
                    succeed_count=0,
                    denied_count=0,
                    llm_model_id=settings.llm_recommend_model_id,
                    operator_email=operator_email,
                    error_message=str(e)[:1000],
                    variant=_assign_variant(sid),
                )
            except Exception:
                logger.exception("auto_dispatch record_run on error failed sid=%s", sid)
            res = {"sid": sid, "status": "error", "error": str(e)[:300],
                   "variant": _assign_variant(sid)}
        processed.append(res)

    summary = _make_summary(
        dry_run=dry_run,
        requested_cap=requested_cap,
        effective_targets=len(targets),
        eligible=len(eligible),
        raw=len(raw),
        excluded=len(excluded),
        processed=processed,
        skipped_reason=skipped_reason,
        started_at=started_at,
        operator_email=operator_email,
    )

    # 5) 슬랙 (옵션)
    await _post_slack_summary(summary)

    return summary


async def _process_one(
    app: dict[str, Any],
    *,
    dry_run: bool,
    operator_email: str,
    replica,
    llm,
    console,
    store,
) -> dict[str, Any]:
    sid = str(app["sid"])
    spec = int(app.get("teacher_specialties") or 5)
    statuses = [2]  # 활동중만
    lat = float(app["lat"])
    lng = float(app["lng"])

    # A/B 4-arm variant 할당 (sid hash % 4)
    variant = _assign_variant(sid)
    n_gu = _VARIANT_NGU[variant]

    # a) 후보 풀 — variant별 n_gu 조절 (V2/V3는 같은 시군구만 = A4)
    gu_codes = await replica.find_nearby_sigungu(lat, lng, n_gu)
    cands = await replica.list_candidate_teachers(
        sid, gu_codes, spec, statuses, _RAW_POOL_LIMIT
    )
    raw_pool_size = len(cands)
    if not cands:
        await store.record_run(
            recommendation_sid=sid, dry_run=dry_run,
            pool_size=0, added_count=0, succeed_count=0, denied_count=0,
            llm_model_id=settings.llm_recommend_model_id,
            operator_email=operator_email,
            error_message="no_candidates_in_pool",
            variant=variant,
        )
        return {"sid": sid, "status": "skipped", "reason": "no_candidates_in_pool",
                "variant": variant}

    # b) cooldown — 오늘 추천 알림 ≥ cap 받은 선생님 사전 제외
    teacher_sids_in_pool = [str(c["teacher_sid"]) for c in cands]
    today_counts = await replica.count_today_teacher_recommendations(teacher_sids_in_pool)
    cap = settings.auto_dispatch_teacher_daily_cap
    filtered = [
        c for c in cands if today_counts.get(str(c["teacher_sid"]), 0) < cap
    ]
    cooldown_removed = raw_pool_size - len(filtered)
    if not filtered:
        await store.record_run(
            recommendation_sid=sid, dry_run=dry_run,
            pool_size=raw_pool_size, added_count=0, succeed_count=0, denied_count=0,
            llm_model_id=settings.llm_recommend_model_id,
            operator_email=operator_email,
            error_message=f"all_in_cooldown removed={cooldown_removed}",
            variant=variant,
        )
        return {"sid": sid, "status": "skipped", "reason": "all_in_cooldown",
                "pool_size": raw_pool_size, "cooldown_removed": cooldown_removed,
                "variant": variant}

    # b-2) A/B variant hard filter (R1+R2+A1 공통, V1/V3 +A2, V3 +A5)
    pre_variant_size = len(filtered)
    try:
        wage_map = await replica.list_wage_ranges([sid])
        wage_types = wage_map.get(sid) or []
    except Exception:
        logger.exception("auto_dispatch wage_ranges fetch failed sid=%s", sid)
        wage_types = []
    schedule_json = app.get("schedule")
    preferred_gender = app.get("preferable_teacher_gender")
    filtered = [
        c for c in filtered
        if _apply_variant_filter(c, variant, schedule_json, preferred_gender)
    ]
    variant_removed = pre_variant_size - len(filtered)
    if not filtered:
        await store.record_run(
            recommendation_sid=sid, dry_run=dry_run,
            pool_size=pre_variant_size, added_count=0, succeed_count=0, denied_count=0,
            llm_model_id=settings.llm_recommend_model_id,
            operator_email=operator_email,
            error_message=f"empty_after_variant_filter v={variant} removed={variant_removed}",
            variant=variant,
        )
        return {"sid": sid, "status": "skipped", "reason": "empty_after_variant_filter",
                "variant": variant, "variant_removed": variant_removed,
                "pool_size": pre_variant_size}

    # c) LLM 랭킹
    sched = _parse_schedule(app.get("schedule"))
    want_days = sched.get("days", [])
    cand_views = [_candidate_view(c, want_days) for c in filtered]

    # 일일 LLM 호출 한도 가드 (인사이트와 공유)
    ok, current = await get_llm_insight_store().check_and_increment_daily(
        limit=settings.llm_daily_limit
    )
    if not ok:
        await store.record_run(
            recommendation_sid=sid, dry_run=dry_run,
            pool_size=len(filtered), added_count=0, succeed_count=0, denied_count=0,
            llm_model_id=settings.llm_recommend_model_id,
            operator_email=operator_email,
            error_message=f"llm_daily_limit_exceeded current={current}",
            variant=variant,
        )
        return {"sid": sid, "status": "skipped", "reason": "llm_daily_limit_exceeded",
                "current": current, "variant": variant}

    payload = _build_input(app, cand_views, wage_types)
    try:
        raw_text, parsed, in_tok, out_tok = await llm.generate_recommendation(
            payload,
            max_tokens=_LLM_MAX_TOKENS,
            system_prompt=AUTO_DISPATCH_SYSTEM_PROMPT,
        )
        # JSON parse 실패 시 raw_text 앞 일부를 로그에 남겨 디버깅
        if not parsed and raw_text:
            logger.warning(
                "auto_dispatch LLM parse empty sid=%s len=%d head=%r tail=%r",
                sid, len(raw_text), raw_text[:200], raw_text[-200:],
            )
    except Exception as e:
        logger.exception("auto_dispatch LLM call failed sid=%s", sid)
        await store.record_run(
            recommendation_sid=sid, dry_run=dry_run,
            pool_size=len(filtered), added_count=0, succeed_count=0, denied_count=0,
            llm_model_id=settings.llm_recommend_model_id,
            operator_email=operator_email,
            error_message=f"llm_failed: {e!s}"[:1000],
            variant=variant,
        )
        return {"sid": sid, "status": "error", "error": "llm_failed", "variant": variant}

    try:
        await get_llm_insight_store().add_token_usage(in_tok, out_tok)
    except Exception:
        logger.exception("auto_dispatch token_usage persist failed sid=%s (graceful)", sid)

    ranked = parsed.get("ranked") or []
    summary = (parsed.get("summary") or "").strip()
    # LLM이 추천 풀에 없는 sid를 끼워넣을 위험 방지
    valid_sids = {c["teacher_sid"] for c in cand_views}
    top_n = settings.auto_dispatch_top_n
    top_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in ranked:
        ts = str(r.get("teacher_sid") or "")
        if not ts or ts not in valid_sids or ts in seen:
            continue
        seen.add(ts)
        top_items.append({
            "teacher_sid": ts,
            "name": str(r.get("name") or ""),
            "rank": r.get("rank"),
            "reason": (r.get("reason") or "").strip(),
            "caution": (r.get("caution") or "").strip(),
        })
        if len(top_items) >= top_n:
            break
    top_sids = [t["teacher_sid"] for t in top_items]
    top_names = [t["name"] for t in top_items]

    if not top_sids:
        await store.record_run(
            recommendation_sid=sid, dry_run=dry_run,
            pool_size=len(filtered), added_count=0, succeed_count=0, denied_count=0,
            llm_model_id=settings.llm_recommend_model_id,
            operator_email=operator_email,
            error_message="llm_returned_empty_ranked",
            variant=variant,
        )
        return {"sid": sid, "status": "skipped", "reason": "llm_returned_empty_ranked",
                "pool_size": len(filtered), "variant": variant}

    # d) 콘솔 호출 (live) 또는 스킵 (dry-run)
    if dry_run:
        logger.info(
            "auto_dispatch DRY_RUN sid=%s pool=%d cooldown_removed=%d top=%d",
            sid, len(filtered), cooldown_removed, len(top_sids),
        )
        await store.record_run(
            recommendation_sid=sid, dry_run=True,
            pool_size=len(filtered), added_count=len(top_sids),
            succeed_count=0, denied_count=0,
            llm_model_id=settings.llm_recommend_model_id,
            operator_email=operator_email,
            variant=variant,
        )
        return {
            "sid": sid, "status": "dry_run",
            "variant": variant,
            "pool_size": len(filtered),
            "cooldown_removed": cooldown_removed,
            "top": [{"teacher_sid": s, "name": n} for s, n in zip(top_sids, top_names)],
        }

    # live: add → memo → visit-offers (각 단계 graceful)
    err: str | None = None
    add_result: dict[str, Any] = {}
    denied: list[dict[str, Any]] = []
    try:
        add_result = await console.add_teachers(sid, top_sids)
    except ConsoleApiError as e:
        logger.error("auto_dispatch add_teachers failed sid=%s %s", sid, e)
        err = f"add_teachers_failed: status={e.status} body={e.body!r}"[:1000]
        await store.record_run(
            recommendation_sid=sid, dry_run=False,
            pool_size=len(filtered), added_count=len(top_sids),
            succeed_count=0, denied_count=0,
            llm_model_id=settings.llm_recommend_model_id,
            operator_email=operator_email,
            error_message=err,
            variant=variant,
        )
        return {"sid": sid, "status": "error", "error": err[:300], "variant": variant}

    # 자란다 콘솔 응답이 snake_case (application.yaml: property-naming-strategy: SNAKE_CASE).
    # 안전하게 두 표기 모두 호환.
    succeed_count = int(
        add_result.get("succeed_count") or add_result.get("succeedCount") or 0
    )

    # 메모 (graceful — 실패해도 visit-offers 진행)
    memo_ok = False
    if succeed_count > 0:
        memo_content = (
            f"[AI매칭 자동] LLM 추천 {succeed_count}명 추가 후 방문제안 발송"
        )
        try:
            await console.write_recommendation_memo([sid], memo_content)
            memo_ok = True
        except ConsoleApiError as e:
            logger.warning("auto_dispatch write_memo failed sid=%s %s", sid, e)
            err = f"memo_failed: status={e.status}"

    # visit-offers — 실제 알림 발송
    visit_offers_called = False
    if succeed_count > 0:
        try:
            denied = await console.send_visit_offers(sid, top_sids)
            visit_offers_called = True
        except ConsoleApiError as e:
            logger.error("auto_dispatch visit_offers failed sid=%s %s", sid, e)
            err = (err + " | " if err else "") + (
                f"visit_offers_failed: status={e.status} body={e.body!r}"[:600]
            )

    # matching-ops 대시보드에 노출 (memo·handler·snapshot) — 부수효과 발생한 경우만
    dashboard_ok: dict[str, bool] = {"memo": False, "handler": False, "snapshot": False}
    if succeed_count > 0:
        dashboard_ok = await _record_dashboard(
            sid=sid,
            spec=spec,
            summary=summary,
            top_items=top_items,
            pool_size=len(filtered),
            cooldown_removed=cooldown_removed,
            replica=replica,
        )

    await store.record_run(
        recommendation_sid=sid, dry_run=False,
        pool_size=len(filtered), added_count=len(top_sids),
        succeed_count=succeed_count, denied_count=len(denied),
        llm_model_id=settings.llm_recommend_model_id,
        operator_email=operator_email,
        error_message=err,
        variant=variant,
    )

    return {
        "sid": sid,
        "status": "live" if visit_offers_called and not err else "partial",
        "variant": variant,
        "pool_size": len(filtered),
        "cooldown_removed": cooldown_removed,
        "requested": len(top_sids),
        "succeed_count": succeed_count,
        "denied_count": len(denied),
        "memo_ok": memo_ok,
        "visit_offers_called": visit_offers_called,
        "dashboard": dashboard_ok,
        "error": err,
    }


async def _record_dashboard(
    *,
    sid: str,
    spec: int,
    summary: str,
    top_items: list[dict[str, Any]],
    pool_size: int,
    cooldown_removed: int,
    replica,
) -> dict[str, bool]:
    """라이브 처리 후 matching-ops 대시보드에 신청서를 노출.

    - matching_ops_memo: 자동 처리 메모 (Auto_bot 작성, LLM 선정 사유 포함)
    - matching_ops_handler: Auto_bot 으로 claim (운영자 release 가능)
    - matching_ops_application_snapshot: 메모 라우트의 _ensure_snapshot 패턴 차용

    각 단계 graceful — 실패해도 본 호출 흐름은 진행. 반환 {memo, handler, snapshot}.
    """
    out = {"memo": False, "handler": False, "snapshot": False}
    subject_name = SPECIALTY_NAME.get(spec, "?")
    cap = settings.auto_dispatch_teacher_daily_cap

    lines = [
        f"[AI매칭 자동] LLM 추천 {len(top_items)}명 추가 + 방문제안 발송",
        "",
        "선정 기준:",
        "• 신청서: 정기수업(regularity=2) · 수업 가능 선생님 1명 이하 · 부모 지목 없음 · 생성 후 1시간 이상",
        "• 고객: 관리필요·추천제한·이용제한 등급 제외",
        f"• 후보 풀: 부모 좌표 인근 시군구 3개 · 과목={subject_name} · 활동중(2) 선생님만",
        f"• cooldown: 오늘 추천 알림 ≥{cap}건 받은 선생님 사전 제외",
        f"• 풀 규모: {pool_size}명 (cooldown {cooldown_removed}명 제외 후)",
        f"• 모델: {settings.llm_recommend_model_id} (요일 매치·경력·추천율·여력 기준)",
    ]
    if summary:
        lines += ["", f"LLM 요약: {summary}"]
    if top_items:
        lines += ["", "선정 사유:"]
        for t in top_items:
            head = f"{t.get('rank') or '?'}. {t.get('name') or '?'}"
            reason = t.get("reason") or ""
            caution = t.get("caution") or ""
            line = head + (f" — {reason}" if reason else "")
            if caution:
                line += f" (주의: {caution})"
            lines.append(line)
    content = "\n".join(lines)

    try:
        await get_memo_store().create_memo(
            application_sid=sid,
            author_email=AUTO_BOT_EMAIL,
            author_name=AUTO_BOT_NAME,
            content=content,
            tags=[AUTO_BOT_TAG],
        )
        out["memo"] = True
    except Exception:
        logger.exception("auto_dispatch dashboard memo failed sid=%s (graceful)", sid)

    try:
        await get_handler_store().claim(
            application_sid=sid,
            handler_email=AUTO_BOT_EMAIL,
            handler_name=AUTO_BOT_NAME,
        )
        out["handler"] = True
    except Exception:
        logger.exception("auto_dispatch dashboard handler failed sid=%s (graceful)", sid)

    try:
        # memo 라우트의 _ensure_snapshot 패턴: replica 신청서 + subject/wage 머지 → freeze
        from src.routes.applications import get_subject_map, to_snapshot_fields

        rec = await replica.get_recommendation(sid)
        if rec is not None:
            subject_map = await get_subject_map()
            wage_types = (await replica.list_wage_ranges([sid])).get(sid)
            fields = to_snapshot_fields(rec, subject_map, wage_types)
            await get_snapshot_store().insert_if_absent(sid, fields)
            out["snapshot"] = True
    except Exception:
        logger.exception("auto_dispatch dashboard snapshot failed sid=%s (graceful)", sid)

    return out


def _make_summary(
    *,
    dry_run: bool,
    requested_cap: int,
    effective_targets: int,
    eligible: int,
    raw: int,
    excluded: int,
    processed: list[dict[str, Any]],
    skipped_reason: str | None,
    started_at: datetime,
    operator_email: str,
) -> dict[str, Any]:
    finished_at = datetime.now(KST)
    by_status: dict[str, int] = {}
    for p in processed:
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
    total_succeed = sum(int(p.get("succeed_count") or 0) for p in processed)
    total_denied = sum(int(p.get("denied_count") or 0) for p in processed)
    return {
        "dry_run": dry_run,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_sec": round((finished_at - started_at).total_seconds(), 2),
        "operator_email": operator_email,
        "requested_cap": requested_cap,
        "raw_candidates": raw,
        "excluded": excluded,
        "eligible": eligible,
        "effective_targets": effective_targets,
        "skipped_reason": skipped_reason,
        "by_status": by_status,
        "total_succeed_teachers": total_succeed,
        "total_denied_teachers": total_denied,
        "details": processed,
    }


async def _post_slack_summary(summary: dict[str, Any]) -> None:
    url = settings.auto_dispatch_slack_webhook.strip()
    if not url:
        return
    mode = "DRY_RUN" if summary["dry_run"] else "LIVE"
    lines = [
        f"*[AI매칭 자동] {mode}* — {summary['operator_email']}",
        f"raw={summary['raw_candidates']} excluded={summary['excluded']} "
        f"eligible={summary['eligible']} targets={summary['effective_targets']}",
        f"by_status={summary['by_status']} "
        f"succeed_teachers={summary['total_succeed_teachers']} "
        f"denied_teachers={summary['total_denied_teachers']} "
        f"elapsed={summary['elapsed_sec']}s",
    ]
    if summary.get("skipped_reason"):
        lines.append(f"skipped: {summary['skipped_reason']}")
    text = "\n".join(lines)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={"text": text})
    except Exception:
        logger.exception("auto_dispatch slack post failed (graceful)")
