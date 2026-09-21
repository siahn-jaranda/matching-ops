"""운영 진단 — 사람이 기억해야 하는 확인 절차를 엔드포인트로 만든다.

- GET /api/diag/llm-keys — 주·보조 Anthropic 키가 실제로 인증되는지 최소 호출로 확인.

키를 바꾼 뒤 "동작 검증은 아직"이라고 넘기면 조용히 망가진다. 2026-09-18 주 키를
잘못 넣고 사흘을 흘려보냈다 — 자동 디스패치는 규칙 랭킹 폴백이 받아 겉으로는 돌았고,
로그에만 authentication_error 32건이 쌓였다. 키 교체 직후 이걸 한 번 호출하면 끝난다.

🚨 키 값은 어떤 경로로도 응답에 넣지 않는다. 끝 4자와 길이만 돌려준다 —
어느 키인지 식별하려면 그 정도로 충분하고, 로그·화면에 남아도 위험하지 않다.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends

from src.config import settings
from src.llm_client import is_failover_error
from src.routes.auto_dispatch import trigger_auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/diag", tags=["diag"])

_API = "https://api.anthropic.com/v1/messages"
_HINTS = ("usage limit", "credit balance", "quota", "billing", "spend limit")


def _verdict(code: int | None, msg: str) -> tuple[str, str]:
    """(판정, 설명). 400 은 프롬프트 오류와 한도 소진이 섞여 메시지로 가른다."""
    if code == 200:
        return "valid", "정상 인증·응답"
    if code in (401, 403):
        return "invalid", "키가 무효하거나 권한이 없음 — 교체 필요"
    if code == 400:
        if any(h in msg.lower() for h in _HINTS):
            return "limited", "키는 유효하나 한도·과금 제약 상태(리셋되면 동작)"
        return "unknown_400", "400 이지만 한도 문구가 아님 — 원문 확인 필요"
    if code == 429:
        return "rate_limited", "일시적 레이트 리밋 — 키는 유효"
    return "unknown", f"예상 못 한 상태({code})"


async def _probe(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    """max_tokens=1 최소 호출. 비용은 무시할 수준이고 부수효과가 없다."""
    try:
        r = await client.post(
            _API,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": settings.llm_recommend_model_id, "max_tokens": 1,
                  "messages": [{"role": "user", "content": "."}]},
        )
        code = r.status_code
        msg = ""
        if code != 200:
            try:
                msg = (r.json().get("error") or {}).get("message", "")[:300]
            except Exception:
                msg = r.text[:200]
    except Exception as e:  # noqa: BLE001
        logger.exception("llm-keys probe 실패 (graceful)")
        return {"http": None, "verdict": "probe_failed",
                "detail": f"{type(e).__name__}: {e!s}"[:200], "message": ""}

    verdict, detail = _verdict(code, msg)
    return {"http": code, "verdict": verdict, "detail": detail, "message": msg}


@router.get("/llm-keys")
async def llm_keys(_user: dict = Depends(trigger_auth)) -> dict[str, Any]:
    primary = settings.anthropic_api_key.strip()
    fallback = settings.anthropic_api_key_fallback.strip()

    out: dict[str, Any] = {"model": settings.llm_recommend_model_id, "keys": {}}
    async with httpx.AsyncClient(timeout=30) as client:
        for name, key in (("primary", primary), ("fallback", fallback)):
            if not key:
                out["keys"][name] = {"present": False, "verdict": "missing",
                                     "detail": "env 미설정"}
                continue
            res = await _probe(client, key)
            out["keys"][name] = {"present": True, "tail4": key[-4:],
                                 "length": len(key), **res}

    p, f = out["keys"]["primary"], out["keys"]["fallback"]
    out["same_key"] = bool(primary and fallback and primary == fallback)

    # 주 키가 죽었을 때 폴백이 실제로 받는지. is_failover_error 와 같은 판정을 쓴다 —
    # 진단이 서빙과 다른 규칙으로 답하면 진단이 거짓말을 한다.
    class _E(Exception):
        def __init__(self, code, m):
            super().__init__(m)
            self.status_code = code

    would_fail_over = (
        p.get("http") is not None
        and p.get("verdict") != "valid"
        and is_failover_error(_E(p["http"], p.get("message", "")))
    )
    out["fallback_would_engage"] = would_fail_over

    if p.get("verdict") == "valid":
        out["summary"] = "정상 — 주 키로 서비스 중"
    elif f.get("verdict") == "valid" and would_fail_over:
        out["summary"] = "주 키 이상, 보조 키가 받는 상태"
    else:
        out["summary"] = ("🚨 주 키 이상이고 보조 키도 받지 못함 — "
                          "LLM 랭킹 없이 규칙 랭킹 폴백으로만 동작")
    if out["same_key"]:
        out["summary"] += " / ⚠️ 주·보조가 같은 키라 폴백 무의미"

    logger.info("diag llm-keys primary=%s fallback=%s engage=%s",
                p.get("verdict"), f.get("verdict"), would_fail_over)
    return out
