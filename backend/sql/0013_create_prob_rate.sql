-- 예상 매칭확률 비율표 (2026-09-07 신진섭)
--
-- 배경: _compute_prob 휴리스틱이 실측 +48.6%p 과대평가이고 순위까지 역전됐다
--       (예측 55점 구간 실제 5.11% < 40점 구간 11.20%). 가산점 튜닝으로는
--       ±5%p 에 못 간다 — 기저 매칭률이 상품군·나이별로 0.2~58% 로 벌어지는데
--       base 30 단일 상수로 앵커하고 있었기 때문.
--
-- 대체: 셀별 과거 실측 매칭률을 그대로 예측값으로 쓴다. 셀 =
--       (상품군, 신청서 나이구간, 재이용 여부, 응답 선생님 유무).
--       홀드아웃(학습 2~6월 → 검증 7~8월) 가중 MAE 2.06%p.
--
-- 축소(shrinkage): 표본이 얇은 셀은 상위 셀(상품군×나이)로 당긴다.
--       rate = (matched + k*parent_rate) / (n + k), k=50.
--       raw_rate 는 축소 전 원값 — 표가 실제로 학습한 값을 감시하려고 같이 남긴다.
--
-- reuse/responder 의 -1 은 '무관'을 뜻하는 상위 셀 행이다.
-- Postgres PK 는 NULL 을 못 쓰므로 센티널로 표현한다.

CREATE TABLE IF NOT EXISTS matching_ops_prob_rate (
    seg         TEXT     NOT NULL,   -- reg2 | reg3 | urgent
    age_bucket  TEXT     NOT NULL,   -- lt6h | 6to48h | gte48h
    reuse       SMALLINT NOT NULL,   -- 0 | 1 | -1(무관)
    responder   SMALLINT NOT NULL,   -- 0 | 1 | -1(무관)
    n           INTEGER  NOT NULL,
    matched     INTEGER  NOT NULL,
    rate        NUMERIC(5,2) NOT NULL,
    raw_rate    NUMERIC(5,2) NOT NULL,
    window_days INTEGER  NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (seg, age_bucket, reuse, responder)
);

COMMENT ON TABLE matching_ops_prob_rate IS
  '예상 매칭확률 비율표. 롤링 윈도우 정착 코호트의 실측 매칭률. /api/prob-rate/refresh 가 갱신';
COMMENT ON COLUMN matching_ops_prob_rate.rate IS '축소 적용 후 예측값(%). 서빙은 이 값을 쓴다';
COMMENT ON COLUMN matching_ops_prob_rate.raw_rate IS '축소 전 원 실측률(%). 셀이 실제로 뭘 봤는지 감시용';
