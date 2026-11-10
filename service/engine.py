"""AI 판단 엔진: 원천 데이터를 모아 매칭·합산·리스크·현금·대안을 계산한다.

MVP는 설명 가능한 규칙 기반이다. 모든 결과에 근거 행(id)을 함께 돌려준다.
"""
from datetime import date, timedelta

import pandas as pd

from config import RULES
from data import Data, period_months


def _city(addr: str) -> str:
    return (addr or "").split(" ")[0]


# ───────────────────────── ① 관련 거래처 매칭 ─────────────────────────
def _deposit_links(d: Data):
    """다른 사업자 등록 계좌에서 들어온 입금 → {(payer_owner, agency): [deposit_id...]}"""
    ag = d.t("m_agency_cv.csv")
    col = d.t("fin_collection_history_cv.csv")
    owner = {}
    for _, a in ag.iterrows():
        owner.setdefault(a.bank_account_no, []).append(a.agency_id)
    links = {}
    for _, r in col.iterrows():
        for o in owner.get(r.bank_account_no, []):
            if o != r.agency_id:
                links.setdefault(frozenset((o, r.agency_id)), []).append(
                    f"{r.deposit_id}({r.agency_id} 채권 {r.matched_ar_id}을 {o} 계좌로 입금)")
    return links


def pair_score(d: Data, a: dict, b: dict, links=None):
    links = links if links is not None else _deposit_links(d)
    col = d.t("fin_collection_history_cv.csv")
    sig = []
    # 자금 연결
    if a["bank_account_no"] == b["bank_account_no"]:
        deps = col[(col.bank_account_no == a["bank_account_no"]) & col.agency_id.isin([a["agency_id"], b["agency_id"]])]
        detail = f"정산계좌 동일 {a['bank_account_no']}"
        if len(deps):
            detail += " · 입금 " + ", ".join(deps.deposit_id)
        sig.append(("자금 연결", detail, RULES["w_account_same"], RULES["w_account_same"]))
    elif frozenset((a["agency_id"], b["agency_id"])) in links:
        sig.append(("자금 연결", "교차 입금 " + "; ".join(links[frozenset((a["agency_id"], b["agency_id"]))]),
                    RULES["w_account_same"], RULES["w_cross_deposit"]))
    else:
        sig.append(("자금 연결", "계좌·입금 연결 없음", RULES["w_account_same"], 0))
    # 대표자명
    same_name = a["representative_name"] == b["representative_name"]
    sig.append(("대표자명", f"{a['representative_name']} {'=' if same_name else '≠'} {b['representative_name']}",
                RULES["w_name"], RULES["w_name"] if same_name else 0))
    # 사업장 주소
    if a["address"] == b["address"]:
        sig.append(("사업장 주소", f"{b['address']} 완전 일치", RULES["w_addr_exact"], RULES["w_addr_exact"]))
    elif _city(a["address"]) == _city(b["address"]):
        sig.append(("사업장 주소", f"{b['address']} · 같은 {_city(b['address'])}, 도로명 다름",
                    RULES["w_addr_exact"], RULES["w_addr_city"]))
    else:
        sig.append(("사업장 주소", f"{b['address']} · 다른 지역", RULES["w_addr_exact"], 0))
    score = sum(s[3] for s in sig)
    unavailable = [c for c in ("연락처", "등록 IP") if not _has_col(d, c)]
    return score, sig, unavailable


def _has_col(d: Data, label):
    cols = set(d.t("m_agency_cv.csv").columns)
    return {"연락처": "phone", "등록 IP": "registered_ip"}[label] in cols


def _decision(score):
    if score >= RULES["auto_group"]:
        return "auto"
    if score >= RULES["manual_review"]:
        return "review"
    return "none"


def match(d: Data, anchor_id: str, extra_ids=()):
    ag = d.t("m_agency_cv.csv")
    anchor = d.agency(anchor_id)
    links = _deposit_links(d)
    cands = []
    for _, row in ag.iterrows():
        b = row.to_dict()
        if b["agency_id"] == anchor_id:
            continue
        score, sig, unavailable = pair_score(d, anchor, b, links)
        if score >= RULES["manual_review"] or b["agency_id"] in extra_ids:
            cands.append({"agency_id": b["agency_id"], "agency_name": b["agency_name"], "score": score,
                          "decision": _decision(score), "signals": sig, "unavailable": unavailable,
                          "proposed_by_sales": b["agency_id"] in extra_ids})
    cands.sort(key=lambda c: -c["score"])
    # 동명이인 대조군: 대표자 이름은 같지만 점수가 수동 검토 미만인 쌍
    controls = []
    group = {anchor_id, *(c["agency_id"] for c in cands if c["decision"] != "none")}
    for name, grp in ag.groupby("representative_name"):
        ids = list(grp.agency_id)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                if ids[i] in group and ids[j] in group:
                    continue   # 같은 그룹 안의 쌍은 대조군이 아님
                s, _, _ = pair_score(d, d.agency(ids[i]), d.agency(ids[j]), links)
                if s < RULES["manual_review"]:
                    controls.append({"pair": f"{ids[i]} ↔ {ids[j]}", "name": name, "score": s})
    return {"anchor": anchor, "candidates": cands, "controls": controls}


# ───────────────────────── ② 실적 합산·요율 ─────────────────────────
def aggregate(d: Data, members, period):
    rb = d.t("sales_rebate_payment_cv.csv").copy()
    rb["rate"] = rb.rebate_amount_krw / rb.sales_performance_krw
    months = period_months(period)
    cur = rb[rb.agency_id.isin(members) & rb.period_ym.isin(months)]
    others = rb[~rb.agency_id.isin(members)]
    std_mean = float(others.rate.mean()) if len(others) else 0
    total = int(cur.sales_performance_krw.sum())
    amount = int(cur.rebate_amount_krw.sum())
    threshold = RULES["threshold_monthly"] * len(months)
    by_member = []
    for m in members:
        r = cur[cur.agency_id == m]
        a = d.agency(m) or {}
        by_member.append({"agency_id": m, "agency_name": a.get("agency_name", ""),
                          "sales": int(r.sales_performance_krw.sum()), "rebate": int(r.rebate_amount_krw.sum()),
                          "rebate_ids": list(r.rebate_id), "status": list(r.approval_status),
                          "eligible": a.get("rebate_eligible") == "True", "account": a.get("bank_account_no", "")})
    prior = rb[rb.agency_id.isin(members) & (rb.period_ym < months[0])]
    prior_paid = prior[prior.approval_status == "승인"]
    prior_excess = int(round(prior_paid.rebate_amount_krw.sum() - (prior_paid.sales_performance_krw * std_mean).sum()))
    single_max = rb.groupby(["agency_id", "period_ym"]).sales_performance_krw.sum()
    return {
        "period": period, "months": months, "members": by_member,
        "total_sales": total, "amount": amount,
        "rate": amount / total if total else 0,
        "threshold": threshold, "meets": total >= threshold,
        "std_mean": std_mean, "std_min": float(others.rate.min()) if len(others) else 0,
        "std_max": float(others.rate.max()) if len(others) else 0, "std_n": int(len(others)),
        "std_amount": int(round(total * std_mean)), "diff": amount - int(round(total * std_mean)),
        "prior_paid": int(prior_paid.rebate_amount_krw.sum()), "prior_excess": prior_excess,
        "prior_approver": ", ".join(sorted(set(prior_paid.approver))) or "-",
        "single_max": int(single_max.max()) if len(single_max) else 0,
        "rebate_ids": list(cur.rebate_id),
    }


# ───────────────────────── 리스크 검사 ─────────────────────────
def risk(d: Data, members, period, agg, resolutions):
    as_of = date.fromisoformat(d.as_of)
    checks, hold = [], []
    # 장려금 대상 여부
    inel = [m["agency_id"] for m in agg["members"] if not m["eligible"]]
    checks.append({"key": "eligible", "label": "장려금 대상 여부(rebate_eligible)",
                   "result": "fail" if inel else "pass",
                   "detail": f"대상 아님: {', '.join(inel)}" if inel else f"{len(members)}곳 모두 대상"})
    if inel:
        hold.append("eligible")
    rb = d.t("sales_rebate_payment_cv.csv")
    rej = rb[rb.agency_id.isin(members) & (rb.approval_status == "반려")]
    checks.append({"key": "rejected", "label": "과거 반려 이력", "result": "warn" if len(rej) else "pass",
                   "detail": f"{len(rej)}건 " + ", ".join(rej.rebate_id) if len(rej) else "0건"})
    # 채권 연체
    ar = d.t("fin_ar_aging_cv.csv")
    od = ar[ar.agency_id.isin(members) & (ar.aging_days > RULES["overdue_days"]) & (ar.outstanding_krw > 0)]
    od = od.sort_values("aging_days", ascending=False)
    od_sum = int(od.outstanding_krw.sum())
    res = resolutions.get("overdue")
    if len(od) and not res:
        hold.append("overdue")
    checks.append({"key": "overdue", "label": f"채권 {RULES['overdue_days']}일 초과 연체",
                   "result": "pass" if not len(od) else ("resolved" if res else "fail"),
                   "detail": (f"{len(od)}건 {od_sum:,}원" + (f" → {res['label']}" if res else "")) if len(od) else "없음",
                   "rows": [[r.ar_id, r.agency_id, int(r.outstanding_krw), int(r.aging_days)] for r in od.itertuples()],
                   "amount": od_sum,
                   "total_outstanding": int(ar[ar.agency_id.isin(members)].outstanding_krw.sum())})
    # 악성 클레임 (품질팀 판정 필드가 있을 때만)
    qc = d.t("qc_claims_cv.csv")
    if "internal_judgment" in qc.columns:
        since = (as_of - timedelta(days=30 * RULES["malicious_window_months"])).isoformat()
        key = qc.agency_id if "agency_id" in qc.columns else qc.customer_id
        mal = qc[key.isin(members) & (qc.internal_judgment == "악의적") & (qc.claim_date >= since)]
        res_q = resolutions.get("malicious")
        bad = len(mal) >= RULES["malicious_count"]
        if bad and not res_q:
            hold.append("malicious")
        checks.append({"key": "malicious", "label": f"악성 클레임 (최근 {RULES['malicious_window_months']}개월 {RULES['malicious_count']}회 이상)",
                       "result": "pass" if not bad else ("resolved" if res_q else "fail"),
                       "detail": f"{len(mal)}건", "rows": [[r.claim_id, r.claim_date, r.claim_type] for r in mal.itertuples()]})
    else:
        checks.append({"key": "malicious", "label": "악성 클레임 (품질팀 '악의적' 판정)", "result": "nodata",
                       "detail": "qc_claims_cv에 internal_judgment 필드 없음 · 필드가 추가되면 자동 적용"})
    # 확정 결함 lot
    conf = qc[(qc.defect_confirmed == "True") & (qc.status == "확정")] if len(qc) else qc
    lots = []
    for lot, g in conf.groupby("lot_id"):
        lots.append({"lot": lot, "product": g.product_id.iloc[0], "n": len(g), "qty": int(g.qty_affected.sum()),
                     "from": g.claim_date.min(), "to": g.claim_date.max()})
    linked = "agency_id" in qc.columns
    claims_members = int(qc.customer_id.isin(members).sum()) if len(qc) else 0
    checks.append({"key": "quality", "label": "품질: 확정 결함 lot",
                   "result": "warn" if lots else "pass",
                   "detail": ("; ".join(f"{l['lot']}({l['product']}) {l['n']}건·{l['qty']:,}개" for l in lots) or "없음")
                   + f" · 그룹 직접 클레임 {claims_members}건" + ("" if linked else " · 대리점↔lot 연결 데이터 없음, 결재자 참고")})
    # 외부 보도
    news = d.docs.get("ext_news_quality_incident.md", "")
    checks.append({"key": "news", "label": "외부 품질 보도", "result": "info" if news else "pass",
                   "detail": "미확정 보도·제품 미지목 → 내부 확정 결함(defect_confirmed) 기준으로만 판단, 보류 근거 제외" if news else "없음"})
    # 판촉 중복
    camp = d.t("m_campaign_ms.csv")
    dup = camp[camp.sales_channel == "대리점"] if len(camp) else camp
    checks.append({"key": "promo", "label": "판촉비 중복 (Masil 캠페인)", "result": "warn" if len(dup) else "pass",
                   "detail": f"캠페인 {len(camp)}건 중 대리점 채널 {len(dup)}건"})
    return {"checks": checks, "hold": hold, "blocking": bool(hold)}


# ───────────────────────── 그룹 현금 ─────────────────────────
def cash(d: Data):
    out = {}
    cv = d.t("fin_cash_daily_cv.csv").copy()
    cv["date"] = pd.to_datetime(cv["date"])
    ecv = cv.set_index("date").closing_balance.resample("ME").last()
    out["cv_close"] = int(ecv.iloc[-1])
    out["cv_month_net"] = int(ecv.iloc[-1] - ecv.iloc[-2])
    ms = d.t("fin_cash_daily_ms.csv").copy()
    ms["date"] = pd.to_datetime(ms["date"])
    ems = ms.set_index("date").closing_balance.resample("ME").last()
    cov = float(ms.covenant_min_krw.iloc[-1])
    close = float(ems.iloc[-1])
    avg = (close - float(ems.iloc[-7])) / 6
    out.update(ms_close=int(close), covenant=int(cov), headroom=int(close - cov), ms_avg_change=int(avg),
               ms_month_net=int(ems.iloc[-1] - ems.iloc[-2]),
               ms_series=[(k.strftime("%Y-%m"), int(v)) for k, v in ems.iloc[-7:].items()],
               breach=bool((ms.breach_flag == "True").any()))
    if avg < 0:
        months = (close - cov) / -avg
        out["months_to_breach"] = round(months, 1)
        out["breach_date"] = (date.fromisoformat(d.as_of) + timedelta(days=int(months * 30.4))).isoformat()
    # 회수율 급락 채널 (LC-0001 결제 지연 근거)
    col = d.t("fin_collection_history_ms.csv")
    ch = d.t("m_channel_ms.csv").set_index("channel_id")
    drops = []
    for c, g in col.groupby("channel"):
        g = g.sort_values("period_ym")
        recent, prior = g.collection_rate_pct.iloc[-4:].mean(), g.collection_rate_pct.iloc[:-4].mean()
        if prior - recent > 10:
            drops.append({"channel": c, "name": ch.loc[c, "channel_name"] if c in ch.index else c,
                          "partner": ch.loc[c, "partner_code"] if c in ch.index else "",
                          "prior": round(prior, 1), "recent": round(recent, 1),
                          "shortfall": int((g.expected_amount_krw - g.collected_amount_krw).iloc[-4:].sum())})
    out["collection_drops"] = drops
    arm = d.t("fin_ar_aging_ms.csv")
    out["ms_ar_total"] = int(arm.outstanding_krw.sum()) if len(arm) else 0
    out["covenant_doc"] = d.docs.get("covenant_terms_ms.md")
    out["lc_notice"] = d.docs.get("lc0001_payment_term_notice.md")
    board = d.docs.get("board_minutes.md", "")
    out["board"] = [l.lstrip("- ").replace("**", "") for l in board.splitlines() if l.startswith("- ")]
    return out


# ───────────────────────── 예외 B: 대안 탐색 ─────────────────────────
def alternative(d: Data, amount: int, members, cashinfo):
    ch = d.t("m_channel_ms.csv")
    agency_ch = ch[ch.channel_type == "대리점"]
    channel = agency_ch.iloc[0].to_dict() if len(agency_ch) else {}
    qc = d.t("qc_claims_cv.csv")
    blocked_pl = sorted(set(qc[(qc.defect_confirmed == "True") & (qc.status == "확정")].product_id))
    sku = d.t("m_sku_ms.csv")
    sku = sku[sku.source_company == "Corevia"] if len(sku) else sku
    ok_sku = sku[~sku.product_id.isin(blocked_pl)]
    ex_sku = sku[sku.product_id.isin(blocked_pl)]
    # 다음 발주: PO 파일이 있으면 그것을, 없으면 채널 청구일 간격으로 추정
    next_order, basis = None, ""
    if d.has("sc_purchase_orders_ms.csv"):
        po = d.t("sc_purchase_orders_ms.csv")
        date_col = next((c for c in po.columns if "date" in c), None)
        if date_col:
            fut = po[po[date_col] > d.as_of].sort_values(date_col)
            if len(fut):
                next_order, basis = fut.iloc[0][date_col], f"sc_purchase_orders_ms {fut.iloc[0].iloc[0]}"
    if not next_order and channel:
        arm = d.t("fin_ar_aging_ms.csv")
        inv = sorted(set(arm[arm.channel_id == channel["channel_id"]].invoice_date))
        if len(inv) >= 2:
            gaps = sorted((date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(inv, inv[1:]))
            gap = max(gaps[len(gaps) // 2], 7)
            next_order = (date.fromisoformat(inv[-1]) + timedelta(days=gap)).isoformat()
            basis = f"{channel['channel_id']} 청구일 {', '.join(inv[-2:])} 간격으로 추정 (PO 파일 미제공)"
    credit = int(round(amount * RULES["credit_ratio"]))
    valid_to = (date.fromisoformat(d.as_of) + timedelta(days=RULES["credit_valid_days"])).isoformat()
    split_cash = int(round(amount * RULES["split_cash_ratio"]))
    return {
        "credit": credit, "channel": channel, "valid_to": valid_to,
        "skus_ok": [f"{r.sku_id} {r.sku_name}" for r in ok_sku.itertuples()],
        "skus_excluded": [f"{r.sku_id} {r.sku_name}" for r in ex_sku.itertuples()],
        "blocked_pl": blocked_pl, "next_order": next_order, "next_order_basis": basis,
        "masil_impact_pct": round(credit / cashinfo["headroom"] * 100, 2) if cashinfo.get("headroom") else None,
        "split": {"cash": split_cash, "credit": amount - split_cash},
    }
