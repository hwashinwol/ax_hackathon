"""서비스 설정과 판단 규칙 파라미터.

각 값 옆에 근거 문서를 적었다. '자체 설정'은 정책 문서에 근거가 없어 이 서비스가 정한 값이다.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# service/.env 가 있으면 읽는다(KEY=VALUE 줄). 이미 설정된 환경 변수는 덮어쓰지 않는다. .env 는 git 에 올리지 않는다.
_env = BASE_DIR / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# 운영 중 AI 에이전트 (agent.py). 자격 증명은 ANTHROPIC_API_KEY 환경 변수 또는 service/.env 로 준다.
AI_ENABLED = os.environ.get("CV_AI", "on").lower() not in ("off", "0", "false")
AI_MODEL = os.environ.get("CV_AI_MODEL", "claude-opus-5-5")
AI_EFFORT = os.environ.get("CV_AI_EFFORT", "medium")

DATA_DIR = Path(os.environ.get("CV_DATA_DIR", BASE_DIR.parent / "hackathon_data"))
DB_PATH = Path(os.environ.get("CV_DB_PATH", BASE_DIR / "instance" / "incentive.db"))

RULES = {
    # ① 동일 관련자 판정 — data_dictionary.md §4
    #    식별정보(대표자·주소·정산계좌) 중 이 개수 이상 일치하면 동일 관련자. 관계는 전이적으로 연결한다.
    #    정산계좌는 마스터 등록 계좌와 실제 입금 계좌(fin_collection_history_cv)를 함께 대조한다.
    "related_min_match": 2,
    # ② 지급 한도 — approval_authority_policy.md '판매장려금 지급 한도'
    #    거래처별 월 지급액이 이 금액 이하면 영업지원팀 전결, 초과하면 영업임원 승인. 동일 관련자는 합산한다.
    "limit_monthly": 2_000_000,
    # 연체 경고 — 자체 설정 (정책 문서에 연체 기준일수 없음. 대리점 채권 결제조건은 모두 60일)
    #   보류 사유가 아니라 결재 화면의 경고로만 쓴다.
    "overdue_days": 90,
    # 예외 A (악성 클레임 보류) — 워크플로우 명세 값
    "malicious_window_months": 6,
    "malicious_count": 3,
    # 예외 B — 자체 설정 (크레딧 비율·유효기간). 유효기간은 m_sku_ms 리드타임 최장 12주(84일)를 덮는 값
    "credit_ratio": 1.0,
    "credit_valid_days": 90,
    "split_cash_ratio": 0.5,
}

RULE_SOURCES = {
    "related_min_match": "data_dictionary.md §4 동일 관련자",
    "limit_monthly": "approval_authority_policy.md 판매장려금 지급 한도",
    "overdue_days": "자체 설정",
    "malicious": "자체 설정",
    "credit": "자체 설정",
}

ROLES = ["영업팀", "재무팀", "내부감시팀", "영업임원", "품질팀", "Masil"]

# 결재선: 재무팀·내부감시팀은 이 서비스가 더한 검토 단계, 영업임원은 정책상 한도 초과 시 필수 승인권자
APPROVAL_CHAIN = ["재무팀", "내부감시팀", "영업임원"]

REJECT_REASONS = {
    "CASH_SHORTAGE": "Corevia 현금 유출 억제 (현금 감소 · 이 그룹 미회수 채권)로 현금 지급 불가",
    "MATCH_DOUBT": "관련 거래처 근거 부족",
    "RATE_EXCESS": "우대 요율 근거 없음",
    "POLICY": "장려금 정책 위반",
}
