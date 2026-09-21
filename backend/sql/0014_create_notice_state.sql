-- 운영 알림 발송 상태 (2026-09-21 신진섭)
--
-- 배경: LLM 폴백 알림이 "1시간에 한 번"이라고 적혀 있는데도 계속 온다는 제보.
--       원인 두 가지가 겹쳤다.
--
-- 1) 억제 상태가 **프로세스 메모리**(module-level float)에 있었다.
--    Cloud Run 은 인스턴스가 여럿이고 수시로 재기동된다 — 인스턴스마다 따로 세니
--    억제가 사실상 무력하다. 실측: 2026-09-21 하루에 인스턴스 2개가 관여.
--
-- 2) 더 근본적으로 **주기가 틀렸다**. 시간당 1회는 짧은 장애용이다.
--    이번엔 9/18 주 키 무효 이후 사흘 넘게 같은 상태가 이어졌고,
--    보조 키도 10/1 까지 한도라 앞으로 열흘 더 간다. 그동안 240회가 온다.
--    사람이 이미 아는 사실을 240번 알리면 다음 진짜 경보도 같이 무시된다.
--
-- 그래서 상태를 공유 저장소에 두고, 같은 상태가 지속되면 간격을 늘린다.
--   streak 1회차 → 즉시, 2회차 → 1시간, 3회차 → 4시간, 4회차 이후 → 24시간
-- 상태가 바뀌면(정상 복귀) streak 를 0 으로 되돌려 다음 발생 때 다시 즉시 알린다.

CREATE TABLE IF NOT EXISTS matching_ops_notice_state (
    notice_key   TEXT        PRIMARY KEY,
    last_sent_at TIMESTAMPTZ,
    streak       INTEGER     NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE matching_ops_notice_state IS
  '운영 알림 발송 억제 상태. 프로세스 메모리로는 Cloud Run 다중 인스턴스·재기동을 못 넘는다';
COMMENT ON COLUMN matching_ops_notice_state.streak IS
  '같은 상태가 연속으로 보고된 횟수. 클수록 알림 간격을 늘린다. 상태 해소 시 0 으로 리셋';
