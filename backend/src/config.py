from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"
    jaranda_replica_url: str

    # matching-ops 전용 PostgreSQL (메모/인사이트 영속화). 미설정 시 메모 API 비활성.
    # 형식: postgresql+asyncpg://user:pw@/db?host=/cloudsql/PROJECT:REGION:INSTANCE
    matching_ops_db_url: str = ""

    # CORS — 페이지 도메인. 운영은 Cloud Run, 로컬은 file://·localhost
    allowed_origins: str = (
        "https://matching-ops-266295307740.asia-northeast3.run.app,"
        "http://localhost:8000,http://localhost:3000,http://127.0.0.1:8000"
    )

    # 인증: Google OAuth (@jaranda.kr 만 허용). auto-call 패턴 동일.
    google_client_id: str = ""
    auth_required: bool = True
    otp_secret_key: str = ""
    otp_session_hours: int = 8

    # 마감 임박 임계값(분). DeadlineTag urgent/soon 분기.
    urgent_threshold_min: int = 240  # 4시간
    soon_threshold_min: int = 1440  # 24시간

    # 신청서 조회 윈도우 (시간). 기본 7일.
    recent_window_hours: int = 168

    # LLM 인사이트 — Anthropic Claude Sonnet 4.6. 키 미설정 시 인사이트 API 비활성.
    anthropic_api_key: str = ""
    llm_model_id: str = "claude-sonnet-4-6"
    # 지원0 추천·지역 회수 전용 모델. 인사이트(llm_model_id)와 분리해 LLM_MODEL_ID 오버라이드 영향 안 받음 (WELL2-100).
    llm_recommend_model_id: str = "claude-sonnet-4-6"
    llm_max_tokens: int = 512
    llm_daily_limit: int = 200

    # Firestore (자란다 채팅방 직접 조회). 미설정 시 채팅방 신호 비활성 (graceful, 응답 필드 null).
    # 자란다 prod Firestore 프로젝트 = "platform-firebase-chat" (별도 GCP project).
    # matching-ops-api는 platform-jaranda-kr-standby에 떠있으므로 cross-project IAM 필요:
    #   gcloud projects add-iam-policy-binding platform-firebase-chat \
    #     --member="serviceAccount:<matching-ops-api SA>" --role="roles/datastore.viewer"
    firestore_project: str = ""
    firestore_enabled: bool = False

    # 자란다 콘솔 쓰기 API — 선생님 추가/메모/방문제안 발송.
    # vibe-cs와 동일 counselor 계정 재사용. 단, 쓰기 base는 prod 명시
    # (vibe-cs는 의도적으로 dev=japi-dev.jaranda.kr 사용 중 — 운영 변경 방지).
    console_api_base: str = "https://japi.jaranda.kr"        # 로그인용
    console_api_base_write: str = "https://japi.jaranda.kr"  # 쓰기용 — prod
    console_username: str = ""
    console_password: str = ""
    console_api_timeout: int = 30
    console_token_ttl_hours: int = 24 * 9  # 9일 (vibe-cs 동일)

    # 자동 디스패치 (KST 09-18 매시. cron은 마지막에 등록).
    # 운영 가드: kill switch + dry-run 기본 ON. live 전환은 명시적 env 변경 필요.
    auto_dispatch_enabled: bool = False         # 기본 OFF — 트리거 자체 차단
    auto_dispatch_dry_run: bool = True          # 기본 ON — 콘솔 쓰기 안 함 (필터·LLM만)
    auto_dispatch_daily_max_apps: int = 5       # 초기 5, 단계적 상향
    auto_dispatch_min_age_minutes: int = 180    # 생성 후 3시간 이상 경과 (운영자 수동 처리 여유)
    auto_dispatch_top_n: int = 20               # 상위 N명 추가·발송
    auto_dispatch_teacher_daily_cap: int = 3    # 선생님 일일 추천 알림 N건 이상 = 후보 풀 제외
    auto_dispatch_admin_emails: str = ""        # 수동 트리거 허용 운영자 (쉼표 구분). 빈 값=모두 허용
    auto_dispatch_slack_webhook: str = ""       # 슬랙 알림 URL (옵션)
    auto_dispatch_trigger_secret: str = ""      # X-Trigger-Secret 헤더 우회 (CLI·Cloud Scheduler용)

    # 배포 전/후 비교 일일 리포트 (Cloud Scheduler → Slack n8n 릴레이)
    ab_report_webhook: str = ""                 # n8n 릴레이 URL. 빈 값이면 발송 안 함(계산만)
    ab_report_slack_target: str = ""            # Slack 채널 ID 또는 사용자 ID(=DM)
    ab_report_anchor_kst: str = "2026-09-02 11:57"   # 배포 앵커 (KST, "YYYY-MM-DD HH:MM")
    ab_report_days: int = 30                    # 이 일수까지만 발송. 마지막 회차는 최종 요약

    @property
    def origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def auto_dispatch_admin_emails_list(self) -> list[str]:
        return [e.strip().lower() for e in self.auto_dispatch_admin_emails.split(",") if e.strip()]


settings = Settings()
