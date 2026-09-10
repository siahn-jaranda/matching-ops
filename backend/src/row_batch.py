"""sid 목록 → 카드 행 배치 생성.

신청서 카드는 메인 목록·관리 목록·수동관리 목록이 모두 같은 스키마를 쓴다.
augment(선생님·부모이력·시급·평가·가용요일·채팅·메모)를 배치로 모아 _to_row 에 넘기는
과정이 길어서, 목록이 늘 때마다 복사되면 스키마가 갈라진다. 여기 한 곳에 둔다.

모든 augment 는 graceful — 실패해도 그 항목만 비고 행 자체는 나온다.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.db import get_replica
from src.firestore_chat import find_chat_status, firestore_available
from src.handler_store import get_handler_store, handler_store_available
from src.memo_store import get_memo_store, memo_store_available
from src.routes.applications import _load_rates, _to_row, get_subject_map

logger = logging.getLogger(__name__)


async def _handler_batch(sids: list[str]) -> dict[str, dict[str, Any]]:
    if not handler_store_available() or not sids:
        return {}
    try:
        return await get_handler_store().list_by_sids(sids)
    except Exception:
        logger.exception("handler batch fetch failed (graceful)")
        return {}


async def build_rows_for_sids(sids: list[str]) -> dict[str, dict[str, Any]]:
    """{sid: 카드 행}. 리플리카에 없는 sid 는 결과에서 빠진다(호출부가 폴백 결정)."""
    if not sids:
        return {}

    replica = get_replica()
    raw_rows: list[dict[str, Any]] = []
    try:
        raw_rows = await replica.list_recent_recommendations(limit=len(sids), sids=sids)
    except Exception:
        logger.exception("row_batch replica fetch failed (graceful)")

    raw_map = {str(r["sid"]): r for r in raw_rows}
    rec_sids = list(raw_map.keys())
    parent_sids = list({r["parent_account_sid"] for r in raw_rows if r.get("parent_account_sid")})

    history_map: dict[str, dict[str, int]] = {}
    teachers_map: dict[str, list[dict[str, Any]]] = {}
    wage_range_map: dict[str, list[str]] = {}
    subject_map: dict[int, str] = {}
    handler_map: dict[str, dict[str, Any]] = {}
    if rec_sids:
        try:
            history_map, teachers_map, wage_range_map, subject_map, handler_map = (
                await asyncio.gather(
                    replica.get_parent_history_counts(parent_sids),
                    replica.list_recommendation_teachers_batch(rec_sids),
                    replica.list_wage_ranges(rec_sids),
                    get_subject_map(),
                    _handler_batch(sids),
                )
            )
        except Exception:
            logger.exception("row_batch augment failed (graceful)")
    if not subject_map:
        try:
            subject_map = await get_subject_map()
        except Exception:
            subject_map = {}
    if not handler_map:
        handler_map = await _handler_batch(sids)

    teacher_sids = list({
        str(t.get("teacher_account_sid"))
        for ts in teachers_map.values()
        for t in ts
        if t.get("teacher_account_sid")
    })
    all_subject_ids: set[int] = set()
    for r in raw_rows:
        for tok in str(r.get("teacher_specialties") or "").split("|"):
            tok = tok.strip()
            if tok.isdigit():
                all_subject_ids.add(int(tok))

    feedback_map: dict[str, dict[str, Any]] = {}
    teacher_wages_map: dict[str, Any] = {}
    visit_counts_map: dict[str, int] = {}
    teacher_availability_map: dict[str, Any] = {}
    if teacher_sids:
        try:
            (feedback_map, teacher_wages_map, visit_counts_map,
             teacher_availability_map) = await asyncio.gather(
                replica.get_teacher_feedback_summary(teacher_sids),
                replica.list_teacher_subject_wages(teacher_sids, sorted(all_subject_ids)),
                replica.list_scheduled_child_counts(teacher_sids),
                replica.list_teacher_weekly_availability(teacher_sids),
            )
        except Exception:
            logger.exception("row_batch teacher augment failed (graceful)")

    chat_room_map: dict[tuple[str, str], dict[str, Any]] = {}
    if firestore_available() and teachers_map:
        pairs: list[tuple[str, str]] = []
        for r in raw_rows:
            psid = r.get("parent_account_sid")
            if not psid:
                continue
            for t in teachers_map.get(str(r.get("sid")), []) or []:
                tsid = t.get("teacher_account_sid")
                if tsid:
                    pairs.append((str(psid), str(tsid)))
        if pairs:
            try:
                chat_room_map = await find_chat_status(list(set(pairs)))
            except Exception:
                logger.exception("row_batch chat_room fetch failed (graceful)")

    memo_meta_map: dict[str, dict[str, Any]] = {}
    if memo_store_available():
        try:
            memo_meta_map = await get_memo_store().counts_by_sids(sids)
        except Exception:
            logger.exception("row_batch memo fetch failed (graceful)")

    rates = await _load_rates()

    out: dict[str, dict[str, Any]] = {}
    for sid, raw in raw_map.items():
        try:
            out[sid] = _to_row(
                raw,
                subject_map,
                history_map.get(raw.get("parent_account_sid")),
                teachers_map.get(sid),
                handler_map.get(sid),
                feedback_map,
                wage_range_map.get(sid),
                teacher_wages_map,
                {},  # push_map — 상세 패널에서 lazy load
                visit_counts_map,
                teacher_availability_map,
                chat_room_map,
                memo_meta_map.get(sid),
                rates=rates,
            )
        except Exception:
            logger.exception("row_batch _to_row failed sid=%s (skip)", sid)
    return out
