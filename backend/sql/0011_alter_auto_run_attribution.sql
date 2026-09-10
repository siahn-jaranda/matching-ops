-- 자동 디스패치 성과 귀속용 2개 컬럼 (2026-09-03 신진섭)
--
-- 배경: 배포 전/후 비교 리포트(routes/reports.py)에서 두 가지를 계산할 수 없었다.
--
-- 1) pre_responder_count — 처리 시점의 기존 응답 선생님 수
--    2026-09-02 대상 조건을 '응답 0명' → '1명 이하' 로 완화하면서, 배포 후 구간에만
--    이미 1명이 반응한 신청서가 섞이게 됐다. 이런 신청서는 매칭에 이미 가까워서
--    모수 변화만으로 매칭률이 오른다(실측: 매칭 5건 중 2건이 이 그룹).
--    이 값을 남겨야 '응답 0명' 그룹만으로 사과 대 사과 비교가 가능하다.
--
-- 2) added_teacher_sids — 콘솔 add_teachers 로 실제 추가 요청한 선생님 sid
--    수락률 분모로 recommendation_teachers.suggested=1 을 쓰는데, 여기에는 봇 발송 외
--    플랫폼 확장추천·플래너 제안이 섞인다(실측 37건 기준 봇 264 / 전체 1,132).
--    봇이 붙인 선생님을 특정할 수 있어야 순수 봇 성과를 뗄 수 있다.
--
-- 둘 다 NULL 허용 — 이 마이그레이션 이전 row 는 값이 없다. 분석 시
-- pre_responder_count IS NULL 인 구간은 '구 규칙(응답 0명)' 으로 해석하면 된다.

ALTER TABLE matching_ops_auto_run
    ADD COLUMN IF NOT EXISTS pre_responder_count INTEGER,
    ADD COLUMN IF NOT EXISTS added_teacher_sids  TEXT[];

COMMENT ON COLUMN matching_ops_auto_run.pre_responder_count IS
  '처리 시점 기존 응답(applied 또는 accepted) 선생님 수. 2026-09-02 조건 완화로 0 또는 1. NULL=마이그레이션 이전 row(구 규칙이므로 사실상 0)';

COMMENT ON COLUMN matching_ops_auto_run.added_teacher_sids IS
  '콘솔 add_teachers 에 요청한 선생님 account_sid 배열(LLM 랭킹 상위 N). 수락률을 봇 발송분에만 귀속시키기 위함. NULL=마이그레이션 이전 row';

CREATE INDEX IF NOT EXISTS idx_auto_run_pre_responder
    ON matching_ops_auto_run (pre_responder_count, run_at DESC);
