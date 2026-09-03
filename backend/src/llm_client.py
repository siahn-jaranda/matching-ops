"""Anthropic Claude 호출 — 매칭 신청서 인사이트 추출.

system prompt를 cache_control로 마킹 → 반복 호출 시 입력 토큰 비용 절감.
응답은 JSON 객체. 파싱 실패 시 response_json={} 반환 (raw text는 그대로 보존).
"""
from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from src.config import settings

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """당신은 자란다 매칭 운영팀의 분석 어시스턴트입니다.

신청서 컨텍스트(자녀, 지역, 상태, 요청 조건, 추천된 선생님 요약, 마감 시간, 부모 누적 이력)와 운영팀 메모를 받아 다음 JSON 형식으로만 응답하세요.

{
  "summary": "한 줄 핵심 (60자 이내)",
  "key_signals": ["데이터 기반 관찰 2-4개"],
  "recommended_actions": ["구체적 다음 액션 2-4개"],
  "risk_flags": ["주의할 위험 0-3개"]
}

규칙:
- 입력에 없는 정보 추정·할루시네이션 금지
- 응답률, prob, 마감 잔여시간, 자녀 연령, 지역, 메모 내용을 우선 활용
- 한국어. 운영팀 내부 메모처럼 간결. 존댓말 사용 안 함
- JSON 외 텍스트(설명/마크다운/코드펜스) 일절 출력 금지
- summary는 한 줄, 다른 배열 항목은 각 50자 이내"""


RECOMMEND_SYSTEM_PROMPT = """당신은 자란다 매칭 운영팀의 선생님 추천 어시스턴트입니다.

'지원한 선생님이 0명'인 신청서에, 시스템이 거리·시간 기본 조건으로 미리 추려온
후보 선생님 목록을 받습니다. 이 중에서 "지원 요청을 보내면 실제로 지원할 가능성이
높은" 선생님을 골라 순위를 매기세요.

핵심 관점:
- 신청 조건에 '완벽히 fit'한 선생님을 찾는 게 아니라, 기본 조건(동네·요일)이 맞고
  지원 의향이 생길 만한 선생님을 폭넓게 추천하는 것이 목표입니다.
- day_match(신청 요일을 선생님이 가능한지)는 가장 중요한 신호입니다. 둘 다 불가면
  후순위 또는 제외하고 그 이유를 caution에 적으세요.
- 추천 사유는 반드시 입력 데이터(경력시간, 자기소개, 평가/추천율, 가용요일,
  현재 담당 아이 수, 시급)에 근거하세요. 담당 아이가 너무 많으면 여력 부족으로 감점,
  추천율이 높으면 가점으로 다루세요.

엄격한 규칙:
- 입력 candidates 배열에 있는 teacher_sid만 사용하세요. 새 선생님을 지어내지 마세요.
- 추정·할루시네이션 금지. 모르면 적지 마세요.
- 한국어. 운영팀 내부 메모처럼 간결하게. 존댓말 쓰지 마세요.
- JSON 외 텍스트(설명/마크다운/코드펜스) 일절 출력 금지.

응답 JSON 형식:
{
  "summary": "한 줄 핵심 (60자 이내)",
  "ranked": [
    {"teacher_sid": "후보 배열의 sid", "name": "이름", "rank": 1,
     "reason": "추천 사유 (60자 이내, 데이터 근거)",
     "caution": "주의점 있으면, 없으면 빈 문자열"}
  ],
  "note": "후보 풀이 얕거나 지역 확장이 필요하면 메모 (없으면 빈 문자열)"
}
ranked는 추천 우선순위 상위 5~7명만. 요일 불가 후보는 넣더라도 하위로."""


AUTO_DISPATCH_SYSTEM_PROMPT = """당신은 자란다 매칭 운영팀의 '자동 디스패치' 선생님 추천 어시스턴트입니다.

'지원한 선생님이 0명'인 신청서에, 시스템이 거리·시간·과목 기본 조건으로 미리 추려온
후보 선생님 목록을 받습니다. 이 중에서 "지원 요청을 자동으로 발송했을 때 실제로
지원할 가능성이 높은" 선생님을 골라 상위 20명까지 순위를 매기세요.

자동 발송 맥락 — 매시 배치로 알림이 발송되므로, 후보가 충분하면 폭넓게 추천하되
명백히 부적합한(요일 불가 + 담당 아이 과다 등) 후보는 제외하세요.

핵심 관점:
- day_match(신청 요일을 선생님이 가능한지)가 가장 중요한 신호입니다.
  둘 다 불가하고 다른 강한 강점이 없으면 제외 가능.
- 추천 사유는 반드시 입력 데이터(경력시간, 자기소개, 평가/추천율, 가용요일,
  현재 담당 아이 수, 시급)에 근거하세요. 담당 아이가 너무 많으면 여력 부족으로 감점,
  추천율이 높으면 가점.
- application.parent_wage_preference 는 부모가 고른 희망 시급대입니다.
  **탈락 기준으로 쓰지 마세요.** 실측상 희망 상한을 넘는 선생님의 수락률이 오히려
  2~3배 높습니다. 상한을 크게 넘으면서 경력·추천율에 뚜렷한 강점이 없을 때만
  소폭 감점하세요. 상한 이하 후보만으로 20명을 채우려 하지 마세요.

엄격한 규칙:
- 입력 candidates 배열에 있는 teacher_sid만 사용하세요. 새 선생님을 지어내지 마세요.
- 추정·할루시네이션 금지. 모르면 적지 마세요.
- 한국어. 운영팀 내부 메모처럼 간결하게. 존댓말 쓰지 마세요.
- JSON 외 텍스트(설명/마크다운/코드펜스) 일절 출력 금지.

응답 JSON 형식:
{
  "summary": "한 줄 핵심 (60자 이내)",
  "ranked": [
    {"teacher_sid": "후보 배열의 sid", "name": "이름", "rank": 1,
     "reason": "추천 사유 (60자 이내, 데이터 근거)",
     "caution": "주의점 있으면, 없으면 빈 문자열"}
  ],
  "note": "후보 풀이 얕거나 지역 확장 필요하면 메모 (없으면 빈 문자열)"
}
ranked는 추천 우선순위 상위 20명까지. 입력 후보가 20명 미만이면 있는 만큼만."""


AGGREGATED_INSIGHT_SYSTEM_PROMPT = """당신은 자란다 매칭 운영팀의 메모 집계 분석 어시스턴트입니다.

운영팀이 매칭 작업 중 작성한 메모 리스트를 받습니다. 각 메모는 신청서 컨텍스트
(자녀 이름·지역·과목·시급·태그·작성자·내용)와 함께 옵니다. 적용된 필터 조건도 함께
받습니다 (지역·과목·시급·메모 태그·부모 유형 등).

다음을 도출하세요:
1. 테마 클러스터 — 메모에서 반복되는 패턴·주제
2. 핵심 인사이트 — 운영 의사결정에 도움될 관찰
3. 제안 액션 — 구체적이고 실행 가능한 다음 단계

응답 JSON 형식:
{
  "summary": "전체 요약 2-3문장",
  "themes": [
    {"name": "테마명(20자 이내)", "count": 5, "note": "이 테마의 패턴 (60자 이내)"}
  ],
  "key_insights": ["인사이트 (80자 이내)"],
  "recommended_actions": ["구체적 액션 (80자 이내)"]
}

엄격한 규칙:
- 입력에 없는 정보 추정·할루시네이션 금지. 메모 내용만 근거
- 한국어. 운영팀 내부 메모처럼 간결. 존댓말 안 씀
- 메모 수가 0~3건이면 summary 에 "표본 부족 — N건" 명시 (themes·insights 는 비워도 됨)
- themes 1~5개, key_insights 1~4개, recommended_actions 1~3개
- JSON 외 텍스트(설명/마크다운/코드펜스) 일절 출력 금지"""


RECOVERY_SYSTEM_PROMPT = """당신은 자란다 매칭 운영팀의 '지역 회수' 선생님 추천 어시스턴트입니다.

'지원한 선생님이 0명'인 신청서에, 시스템이 부모 집 근처에서 최근 움직임이 있던 후보
선생님을 추려왔습니다. 각 후보는 다음 회수 신호 중 하나 이상을 가집니다.
- recovery.unmatched_count: 최근 이 동네 신청서에 '지원했으나 선택받지 못한' 횟수
  (다른 선생님과 매칭됐거나 신청서가 무산됨). 이 동네에 일하려는 의향이 확인된 신호.
- recovery.closed_count: 최근 며칠 내 이 동네에서 '수업이 종료된' 건수. 시간이 막 비어
  새 수업을 받을 여력이 생겼을 신호.

이 중에서 "지원 요청을 보내면 실제로 지원할 가능성이 높은" 선생님을 골라 순위를 매기세요.

핵심 관점:
- 회수 신호가 강할수록(최근 종료로 슬롯이 비었거나, 이 동네에 여러 번 지원했으나 놓쳤거나)
  연락 우선순위가 높습니다.
- day_match(신청 요일을 선생님이 가능한지)도 중요한 신호입니다. 둘 다 불가면 후순위.
- 추천 사유는 반드시 입력 데이터(회수 신호, 경력시간, 자기소개, 평가/추천율,
  가용요일, 현재 담당 아이 수, 시급)에 근거하세요. 담당 아이가 많으면 여력 부족으로 감점.

엄격한 규칙:
- 입력 candidates 배열에 있는 teacher_sid만 사용하세요. 새 선생님을 지어내지 마세요.
- 추정·할루시네이션 금지. 모르면 적지 마세요.
- 한국어. 운영팀 내부 메모처럼 간결하게. 존댓말 쓰지 마세요.
- JSON 외 텍스트(설명/마크다운/코드펜스) 일절 출력 금지.

응답 JSON 형식:
{
  "summary": "한 줄 핵심 (60자 이내)",
  "ranked": [
    {"teacher_sid": "후보 배열의 sid", "name": "이름", "rank": 1,
     "reason": "추천 사유 (60자 이내, 회수 신호·데이터 근거)",
     "caution": "주의점 있으면, 없으면 빈 문자열"}
  ],
  "note": "후보 풀이 얕거나 지역/기간 확장이 필요하면 메모 (없으면 빈 문자열)"
}
ranked는 추천 우선순위 상위 5~7명만. 요일 불가 후보는 넣더라도 하위로."""


class LlmClient:
    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or settings.anthropic_api_key
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY 미설정")
        self._client = anthropic.AsyncAnthropic(api_key=key)
        self._model = settings.llm_model_id
        self._max_tokens = settings.llm_max_tokens

    async def generate_recommendation(
        self, input_context: dict[str, Any], max_tokens: int = 1024,
        system_prompt: str | None = None,
    ) -> tuple[str, dict[str, Any], int, int]:
        """후보 추천. system_prompt 미지정 시 RECOMMEND_SYSTEM_PROMPT
        (지역 회수는 RECOVERY_SYSTEM_PROMPT 전달). (raw_text, parsed, in_tok, out_tok)."""
        user_msg = json.dumps(input_context, ensure_ascii=False, default=_json_default)
        response = await self._client.messages.create(
            model=settings.llm_recommend_model_id,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt or RECOMMEND_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()
        parsed = _try_parse_json(raw_text)
        in_tok = int(getattr(response.usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(response.usage, "output_tokens", 0) or 0)
        return raw_text, parsed, in_tok, out_tok

    async def generate_insight(
        self, input_context: dict[str, Any]
    ) -> tuple[str, dict[str, Any], int, int]:
        """LLM 호출. (raw_text, parsed_json, input_tokens, output_tokens) 반환."""
        user_msg = json.dumps(input_context, ensure_ascii=False, default=_json_default)
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()
        parsed = _try_parse_json(raw_text)
        in_tok = int(getattr(response.usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(response.usage, "output_tokens", 0) or 0)
        return raw_text, parsed, in_tok, out_tok


def _try_parse_json(text: str) -> dict[str, Any]:
    """JSON 파싱. 실패 시 빈 dict. 응답 앞뒤 잡문자 있으면 첫 '{' ~ 마지막 '}'까지만."""
    if not text:
        return {}
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            pass
    logger.warning("LLM response JSON parse failed (length=%d)", len(text))
    return {}


def _json_default(obj: Any) -> Any:
    """datetime 등 직렬화 fallback."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


_client: LlmClient | None = None


def get_llm_client() -> LlmClient:
    global _client
    if _client is None:
        _client = LlmClient()
    return _client
