"""서비스 설정과 판단 규칙 파라미터.

값 중 데이터에 근거가 없는 것(기준선, 가중치, 요율표 등)은 기획서에서 '입력 예정'으로 표시한 예시값이다.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CV_DATA_DIR", BASE_DIR.parent / "hackathon_data"))
DB_PATH = Path(os.environ.get("CV_DB_PATH", BASE_DIR / "instance" / "incentive.db"))

RULES = {
    # ① 매칭 신호 가중치 (100점 만점)
    "w_account_same": 40,      # 등록 정산계좌 동일
    "w_cross_deposit": 30,     # 다른 사업자 계좌에서 대금 입금
    "w_name": 30,              # 대표자명 동일
    "w_addr_exact": 30,        # 사업장 주소 완전 일치
    "w_addr_city": 6,          # 같은 시
    "auto_group": 90,          # 이 점수 이상 자동 그룹
    "manual_review": 70,       # 이 점수 이상 수동 검토
    # ② 실적
    "threshold_monthly": 20_000_000,   # 월 합산 기준선 (예시)
    # 예외 A
    "overdue_days": 90,
    "malicious_window_months": 6,
    "malicious_count": 3,
    # 예외 B
    "credit_ratio": 1.0,
    "credit_valid_days": 90,
    "split_cash_ratio": 0.5,
}

ROLES = ["영업팀", "재무팀", "내부감시팀", "품질팀", "Masil"]

REJECT_REASONS = {
    "CASH_SHORTAGE": "그룹 유동성 방어로 현금 지급 불가",
    "MATCH_DOUBT": "관련 거래처 근거 부족",
    "RATE_EXCESS": "우대 요율 근거 없음",
    "POLICY": "장려금 정책 위반",
}
