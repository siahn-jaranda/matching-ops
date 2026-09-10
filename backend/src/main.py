"""matching-ops-api FastAPI 엔트리포인트.

자란다 매칭 운영 대시보드(matching-ops Cloud Run)의 데이터 소스 백엔드.
read replica MySQL 직접 조회 — 쓰기 작업 없음.
인증: Google OAuth (@jaranda.kr).
"""
from __future__ import annotations

import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from src import auth
from src.config import settings
from src.routes import (
    aggregated_insights,
    applications,
    auto_dispatch,
    candidates,
    handlers,
    insights,
    managed,
    manual_ops,
    memos,
    prob_rate,
    reports,
)

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="matching-ops-api",
    description="자란다 매칭 운영 대시보드 백엔드 (read-only)",
    version="0.1.0",
)

# /api/applications 응답이 1000건 기준 ~5MB. gzip으로 ~10x 단축 → network 시간 큰 절감.
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# 인증 routes (로그인은 토큰 없이 호출)
app.include_router(auth.router)

# 데이터 routes — Google OAuth 세션 토큰 필수 (auth_required=true 일 때)
app.include_router(applications.router, dependencies=[Depends(auth.require_auth)])

# 메모 CRUD — 라우트 내부에서 require_auth_full 의존성으로 author 식별
app.include_router(memos.router, dependencies=[Depends(auth.require_auth)])

# 처리 담당 claim/release — 라우트 내부에서 require_auth_full로 본인 식별
app.include_router(handlers.router, dependencies=[Depends(auth.require_auth)])

# 관리 신청서 목록 — 라우트 내부에서 trigger_auth 사용 (Google 세션 OR X-Trigger-Secret)
app.include_router(managed.router)

# 수동관리 신청서 — 인증은 라우터 내부 trigger_auth
app.include_router(manual_ops.router)

# LLM 인사이트 — POST는 호출당 비용. 라우트 내부에서 일일 한도 가드.
app.include_router(insights.router, dependencies=[Depends(auth.require_auth)])

# 지원 0개 신청서 선생님 추천 (WELL2-100) — POST 호출당 비용, 일일 한도 공유.
app.include_router(candidates.router, dependencies=[Depends(auth.require_auth)])

# 자동 디스패치 — 지원 0 신청서에 LLM 추천 N명 자동 추가 + 방문 제안 발송.
# 라우트 내부에서 require_auth_full(운영자 email 기록용) + kill switch 가드.
app.include_router(auto_dispatch.router)

# 메모 집계 인사이트 — 필터 5종 + LLM. 라우트 내부 trigger_auth.
app.include_router(aggregated_insights.router)

# 배포 전/후 비교 일일 리포트 — 자체 인증(X-Trigger-Secret 또는 세션)
app.include_router(reports.router)

# 예상 매칭확률 비율표 — 조회·갱신 모두 trigger_auth (세션 또는 X-Trigger-Secret)
app.include_router(prob_rate.router)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "matching-ops-api", "status": "ok"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
