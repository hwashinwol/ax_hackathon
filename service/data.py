"""hackathon_data 로더. 하위 폴더까지 모든 파일을 찾아 읽는다."""
from pathlib import Path

import pandas as pd

# 서비스가 쓰는 파일 (없으면 기능 일부가 '데이터 없음'으로 동작)
CORE_FILES = {
    "m_agency_cv.csv": "대리점 마스터",
    "sales_rebate_payment_cv.csv": "장려금 지급 이력",
    "fin_collection_history_cv.csv": "대리점 수금(입금) 이력",
    "fin_ar_aging_cv.csv": "Corevia 채권 연령",
    "qc_claims_cv.csv": "품질 클레임",
    "fin_cash_daily_cv.csv": "Corevia 일별 현금",
    "fin_cash_daily_ms.csv": "Masil 일별 현금 (약정 하한 포함)",
    "fin_ar_aging_ms.csv": "Masil 채권 연령",
    "fin_collection_history_ms.csv": "Masil 채널별 회수율",
    "m_channel_ms.csv": "Masil 판매 채널",
    "m_sku_ms.csv": "Masil SKU 마스터",
    "m_campaign_ms.csv": "Masil 인플루언서 캠페인",
    "m_influencer_ms.csv": "Masil 인플루언서",
    "board_minutes.md": "이사회 회의록",
    "ext_news_quality_incident.md": "외부 품질 보도",
    "approval_authority_policy.md": "의사결정 권한 · 판매장려금 지급 한도",
    "data_dictionary.md": "공통 데이터 정의 · 동일 관련자 판정 기준",
}
# 없어도 서비스는 돌지만, 있으면 Masil 현금 재전망과 크레딧 정산에 쓴다.
SPEC_OPTIONAL = {
    "covenant_terms_ms.md": "Masil 대출 약정 조건 (없으면 fin_cash_daily_ms.covenant_min_krw 사용)",
    "lc0001_payment_term_notice.md": "LC-0001 결제조건 변경 공지 (없으면 회수율 추이로 대체)",
    "sc_purchase_orders_ms.csv": "Masil 발주(PO) 내역 (없으면 크레딧 정산 대상 발주를 지정할 수 없음)",
    "fin_ap_aging_ms.csv": "Masil 매입채무 (없으면 현금 재전망을 하지 않음)",
}

NUMERIC = {
    "sales_rebate_payment_cv.csv": ["sales_performance_krw", "rebate_amount_krw"],
    "fin_collection_history_cv.csv": ["amount_krw"],
    "fin_ar_aging_cv.csv": ["amount_krw", "outstanding_krw", "aging_days"],
    "qc_claims_cv.csv": ["qty_affected"],
    "fin_cash_daily_cv.csv": ["opening_balance", "inflow_krw", "outflow_krw", "closing_balance"],
    "fin_cash_daily_ms.csv": ["opening_balance", "inflow_krw", "outflow_krw", "closing_balance", "covenant_min_krw"],
    "fin_ar_aging_ms.csv": ["amount_krw", "outstanding_krw", "aging_days"],
    "fin_collection_history_ms.csv": ["expected_amount_krw", "collected_amount_krw", "collection_rate_pct"],
    "sc_purchase_orders_ms.csv": ["amount_krw"],
    "fin_ap_aging_ms.csv": ["amount_krw"],
}


class Data:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.paths = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".csv", ".md"):
                self.paths.setdefault(p.name, p)   # 같은 이름(중복 사본)은 첫 파일만
        self.tables = {}
        self.docs = {}
        for name, path in self.paths.items():
            if name.endswith(".csv"):
                df = pd.read_csv(path, sep="|", dtype=str, encoding="utf-8", keep_default_na=False)
                for col in NUMERIC.get(name, []):
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
                self.tables[name] = df
            else:
                self.docs[name] = path.read_text(encoding="utf-8")
        cash = self.t("fin_cash_daily_cv.csv")
        self.as_of = cash["date"].max() if len(cash) else "2026-06-30"

    def t(self, name) -> pd.DataFrame:
        return self.tables.get(name, pd.DataFrame())

    def has(self, name) -> bool:
        return name in self.paths

    def inventory(self):
        rows = []
        for name, desc in {**CORE_FILES, **SPEC_OPTIONAL}.items():
            p = self.paths.get(name)
            df = self.tables.get(name)
            rows.append({
                "name": name, "desc": desc, "found": p is not None,
                "optional": name in SPEC_OPTIONAL,
                "path": str(p.relative_to(self.root)) if p else "",
                "rows": len(df) if df is not None else None,
                "cols": list(df.columns) if df is not None else [],
            })
        extra = [n for n in self.paths if n not in CORE_FILES and n not in SPEC_OPTIONAL]
        return rows, extra

    # 편의 조회
    def agency(self, aid):
        df = self.t("m_agency_cv.csv")
        r = df[df.agency_id == aid]
        return r.iloc[0].to_dict() if len(r) else None

    def periods(self):
        df = self.t("sales_rebate_payment_cv.csv")
        months = sorted(df.period_ym.unique()) if len(df) else []
        quarters = sorted({f"{m[:4]}-Q{(int(m[5:7]) - 1) // 3 + 1}" for m in months})
        return months, quarters


def period_months(period: str):
    if "-Q" in period:
        y, q = period.split("-Q")
        start = (int(q) - 1) * 3 + 1
        return [f"{y}-{m:02d}" for m in range(start, start + 3)]
    return [period]
