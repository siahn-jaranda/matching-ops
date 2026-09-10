-- 실패한 자동 디스패치의 재시도 허용 (2026-09-04 신진섭)
--
-- 사고: 2026-09-04 09:40~11:50 KST Anthropic API 사용량 한도 소진으로 LLM 랭킹이
--       전면 실패(llm_failed 6건). 그런데 _process_one 은 실패해도
--       record_run(dry_run=false) 로 기록을 남기고, get_excluded_sids 는 성공 여부를
--       보지 않고 'dry_run=false 행이 있으면 제외' 했다.
--       → 일시적 API 장애 때문에 신청서 6건이 자동 처리 대상에서 영구 이탈.
--         그중 후보 풀 34·30·29명짜리도 있었다. 하루 방치했으면 최대 72건.
--
-- 고침: 실제로 선생님을 추가한 건(succeed_count > 0)만 영구 제외하고,
--       실패한 건은 백오프 후 재시도한다. 단 무한 재시도를 막기 위해 시도 횟수를 센다.
--
-- attempt_count — record_run UPSERT 때마다 +1. 기존 행은 1로 시작한다.
--   재시도 조건은 auto_run_store.get_excluded_sids 참고:
--     succeed_count > 0                         → 영구 제외 (처리 완료)
--     attempt_count >= AUTO_DISPATCH_MAX_ATTEMPTS → 영구 제외 (재시도 소진)
--     run_at > NOW() - RETRY_AFTER              → 일시 제외 (백오프 중)

ALTER TABLE matching_ops_auto_run
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 1;

COMMENT ON COLUMN matching_ops_auto_run.attempt_count IS
  '자동 디스패치 시도 횟수. record_run UPSERT 마다 +1. AUTO_DISPATCH_MAX_ATTEMPTS 도달 시 재시도 중단';

-- 재시도 후보를 빠르게 거르기 위한 인덱스
CREATE INDEX IF NOT EXISTS idx_auto_run_retry
    ON matching_ops_auto_run (dry_run, succeed_count, attempt_count, run_at DESC);
