"""AI 판단 엔진: 원천 데이터를 모아 관련자 판정·한도 합산·리스크·현금·대안을 계산한다.

MVP는 설명 가능한 규칙 기반이다. 모든 결과에 근거 행(id)을 함께 돌려준다.
판정 기준은 data_dictionary.md(동일 관련자)와 approval_authority_policy.md(지급 한도)를 따른다.
"""
from datetime import date, timedelta

import pandas as pd

from config import RULES
from data import Data, period_months


def _city(addr: str) -> str:
    return (addr or "").split(" ")[0]


# ───────────────────────── ① 동일 관련자 판정 ─────────────────────────
def _deposit_links(d: Data):
    """다른 사업자 등록 계좌로 들어온 입금 → {frozenset(계좌 주인, 입금 대리점): [근거...]}"""
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
                    f"{r.deposit_id}({r.agency_id} 채권 {r.matched_ar_id} → {o} 등록 계좌 {r.bank_account_no})")
    return links


def pair_match(d: Data, a: dict, b: dict, links=None):
    """식별정보 3개(대표자·주소·정산계좌)의 일치 여부. 반환: (일치 개수, 신호 목록)

    신호 = (이름, 근거, 만점 1, 일치 1/불일치 0)
    """
    links = links if links is not None else _deposit_links(d)
    sig = []
    same_name = a["representative_name"] == b["representative_name"]
    sig.append(("대표자", f"{a['representative_name']} {'=' if same_name else '≠'} {b['representative_name']}",
                1, 1 if same_name else 0))
    if a["address"] == b["address"]:
        sig.append(("사업장 주소", f"{b['address']} 완전 일치", 1, 1))
    elif _city(a["address"]) == _city(b["address"]):
        sig.append(("사업장 주소", f"{b['address']} · 같은 {_city(b['address'])}이나 도로명이 달라 불일치", 1, 0))
    else:
        sig.append(("사업장 주소", f"{b['address']} · 불일치", 1, 0))
    key = frozenset((a["agency_id"], b["agency_id"]))
    if a["bank_account_no"] == b["bank_account_no"]:
        col = d.t("fin_collection_history_cv.csv")
        deps = col[(col.bank_account_no == a["bank_account_no"]) & col.agency_id.isin([a["agency_id"], b["agency_id"]])]
        detail = f"등록 계좌 동일 {a['bank_account_no']}"
        if len(deps):
            detail += " · 입금 " + ", ".join(deps.deposit_id)
        sig.append(("정산계좌", detail, 1, 1))
    elif key in links:
        sig.append(("정산계좌", "등록 계좌는 다르나 실제 입금 계좌 일치 · " + "; ".join(links[key]), 1, 1))
    else:
        sig.append(("정산계좌", f"{b['bank_account_no']} · 불일치", 1, 0))
    return sum(s[3] for s in sig), sig


def match(d: Data, anchor_id: str, extra_ids=()):
    """기준 거래처의 동일 관련자 그룹. 2개 이상 일치 관계를 전이적으로 연결한다."""
    ag = d.t("m_agency_cv.csv")
    recs = {r["agency_id"]: r for r in ag.to_dict("records")}
    links = _deposit_links(d)
    need = RULES["related_min_match"]
    ids = list(recs)
    pair = {}
    for i, x in enumerate(ids):
        for y in ids[i + 1:]:
            n, sig = pair_match(d, recs[x], recs[y], links)
            if n:
                pair[frozenset((x, y))] = n
    # 전이 연결: 기준 거래처에서 '2개 이상 일치' 관계를 따라 닿는 모든 거래처
    parent, queue = {anchor_id: None}, [anchor_id]
    while queue:
        cur = queue.pop(0)
        for other in ids:
            if other not in parent and pair.get(frozenset((cur, other)), 0) >= need:
                parent[other] = cur
                queue.append(other)
    cands = []
    for aid in ids:
        if aid == anchor_id:
            continue
        n, sig = pair_match(d, recs[anchor_id], recs[aid], links)
        in_group, proposed = aid in parent, aid in extra_ids
        if not (in_group or proposed):
            continue
        via = None
        if in_group and n < need:               # 기준 거래처와 직접은 부족하지만 다른 관련자를 거쳐 연결됨
            via = parent[aid]
            n, sig = pair_match(d, recs[via], recs[aid], links)
        decision = "auto" if in_group else ("review" if n >= 1 else "none")
        cands.append({"agency_id": aid, "agency_name": recs[aid]["agency_name"], "score": n, "max": 3,
                      "decision": decision, "signals": sig, "via": via, "proposed_by_sales": proposed})
    cands.sort(key=lambda c: (-c["score"], c["agency_id"]))
    # 대조군: 대표자 이름은 같지만 일치 개수가 모자라 묶지 않는 쌍
    controls = []
    for name, grp in ag.groupby("representative_name"):
        g = list(grp.agency_id)
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                if g[i] in parent and g[j] in parent:
                    continue
                n = pair.get(frozenset((g[i], g[j])), 0)
                if n < need:
                    controls.append({"pair": f"{g[i]} ↔ {g[j]}", "name": name, "score": n})
    return {"anchor": recs[anchor_id], "candidates": cands, "controls": controls, "need": need}


# ───────────────────────── ② 합산 한도·요율 ─────────────────────────
def aggregate(d: Data, members, period):
    rb = d.t("sales_rebate_payment_cv.csv").copy()
    rb["rate"] = rb.rebate_amount_krw / rb.sales_performance_krw
    months = period_months(period)
    limit = RULES["limit_monthly"]
    grp = rb[rb.agency_id.isin(members)]
    cur = grp[grp.period_ym.isin(months)]
    others = rb[~rb.agency_id.isin(members)]
    std_mean = float(others.rate.mean()) if len(others) else 0
    total = int(cur.sales_performance_krw.sum())
    amount = int(cur.rebate_amount_krw.sum())
    by_member = []
    for m in members:
        r = cur[cur.agency_id == m]
        a = d.agency(m) or {}
        by_member.append({"agency_id": m, "agency_name": a.get("agency_name", ""),
                          "sales": int(r.sales_performance_krw.sum()), "rebate": int(r.rebate_amount_krw.sum()),
                          "rebate_ids": list(r.rebate_id), "status": list(r.approval_status),
                          "eligible": a.get("rebate_eligible") == "True", "account": a.get("bank_account_no", "")})
    monthly = [{"ym": ym, "amount": int(g.rebate_amount_krw.sum()), "over": int(g.rebate_amount_krw.sum()) > limit}
               for ym, g in cur.groupby("period_ym")]
    # 이전 기간: 합산하면 한도를 넘는데 임원 승인 없이 지급된 달
    prior = grp[(grp.period_ym < months[0]) & (grp.approval_status == "승인")]
    over_rows = []
    for ym, g in prior.groupby("period_ym"):
        s = int(g.rebate_amount_krw.sum())
        if s > limit:
            over_rows.append({"ym": ym, "amount": s, "max_single": int(g.rebate_amount_krw.max()),
                              "approver": ", ".join(sorted(set(g.approver)))})
    prior_excess = int(round(prior.rebate_amount_krw.sum() - (prior.sales_performance_krw * std_mean).sum()))
    return {
        "period": period, "months": months, "members": by_member,
        "total_sales": total, "amount": amount,
        "rate": amount / total if total else 0,
        "limit": limit, "limit_total": limit * len(months), "monthly": monthly,
        "over_limit": any(m["over"] for m in monthly),
        "max_single": max((m["rebate"] for m in by_member), default=0),
        "file_over_single": int((rb.rebate_amount_krw > limit).sum()), "file_rows": int(len(rb)),
        "file_max_single": int(rb.rebate_amount_krw.max()) if len(rb) else 0,
        "std_mean": std_mean, "std_min": float(others.rate.min()) if len(others) else 0,
        "std_max": float(others.rate.max()) if len(others) else 0, "std_n": int(len(others)),
        "std_amount": int(round(total * std_mean)), "diff": amount - int(round(total * std_mean)),
        "prior_paid": int(prior.rebate_amount_krw.sum()), "prior_excess": prior_excess,
        "prior_approver": ", ".join(sorted(set(prior.approver))) or "-",
        "prior_over": over_rows, "prior_over_amount": sum(r["amount"] for r in over_rows),
        "rebate_ids": list(cur.rebate_id),
    }


# ───────────────────────── 리스크 검사 ─────────────────────────
def risk(d: Data, members, period, agg, resolutions, judgments=()):
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
    # 과거 한도 우회 지급 (사후 검토 대상, 보류 사유는 아님)
    po = agg.get("prior_over") or []
    checks.append({"key": "prior_over", "label": "과거 합산 한도 초과 지급 (임원 승인 없음)",
                   "result": "warn" if po else "pass",
                   "detail": (f"{len(po)}개월 {agg['prior_over_amount']:,}원 · 결재 {agg['prior_approver']} 단독 → 내부감시팀 사후 검토"
                              if po else "없음")})
    # 채권 연체
    ar = d.t("fin_ar_aging_cv.csv")
    od = ar[ar.agency_id.isin(members) & (ar.aging_days > RULES["overdue_days"]) & (ar.outstanding_krw > 0)]
    od = od.sort_values("aging_days", ascending=False)
    od_sum = int(od.outstanding_krw.sum())
    # 연체는 보류 사유가 아니라 결재자에게 보여 주는 경고다(지급 여부는 결재자가 판단).
    checks.append({"key": "overdue", "label": f"채권 {RULES['overdue_days']}일 초과 연체",
                   "result": "warn" if len(od) else "pass",
                   "detail": f"{len(od)}건 {od_sum:,}원 · 결재 화면에 경고로 표시" if len(od) else "없음",
                   "rows": [[r.ar_id, r.agency_id, int(r.outstanding_krw), int(r.aging_days)] for r in od.itertuples()],
                   "amount": od_sum,
                   "total_outstanding": int(ar[ar.agency_id.isin(members)].outstanding_krw.sum())})
    # 악성 클레임: 원천 파일의 판정 필드(있을 때) + 품질팀이 시스템에 등록한 판정
    qc = d.t("qc_claims_cv.csv")
    has_field = "internal_judgment" in qc.columns
    since = (as_of - timedelta(days=30 * RULES["malicious_window_months"])).isoformat()
    mal_rows = []
    if has_field:
        key = qc.agency_id if "agency_id" in qc.columns else qc.customer_id
        mal = qc[key.isin(members) & (qc.internal_judgment == "악의적") & (qc.claim_date >= since)]
        mal_rows += [[r.claim_id, r.claim_date, r.claim_type, "qc_claims_cv"] for r in mal.itertuples()]
    mal_rows += [[f"QJ-{j['id']:04d}", j["claim_date"], f"{j['agency_id']} {j['claim_type']}", j["source"]]
                 for j in judgments if j["agency_id"] in members and since <= j["claim_date"] <= d.as_of]
    mal_label = f"악성 클레임 (최근 {RULES['malicious_window_months']}개월 '악의적' 판정 {RULES['malicious_count']}회 이상)"
    if has_field or judgments:
        res_q = resolutions.get("malicious")
        bad = len(mal_rows) >= RULES["malicious_count"]
        if bad and not res_q:
            hold.append("malicious")
        checks.append({"key": "malicious", "label": mal_label,
                       "result": "pass" if not bad else ("resolved" if res_q else "fail"),
                       "detail": f"{len(mal_rows)}건" + (f" → {res_q['label']}" if bad and res_q else ""), "rows": mal_rows})
    else:
        checks.append({"key": "malicious", "label": mal_label, "result": "nodata",
                       "detail": "qc_claims_cv에 판정 필드가 없고 품질팀이 등록한 판정도 없음 · '품질 판정' 화면에서 등록하면 적용"})
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
def _pick(doc, keyword):
    """문서에서 키워드가 든 첫 줄을 마크다운 기호 없이 돌려준다(결재 화면 근거 인용용)."""
    for line in (doc or "").splitlines():
        if keyword in line:
            return line.strip().lstrip("-> ").replace("**", "").replace("`", "")
    return None


def _reforecast(d: Data, opening: float, covenant: float, horizon_days=90):
    """covenant_terms_ms.md의 재전망 방식: 기말 현금 + 채권 회수예정 − 운영 매입채무 − 재고 발주 지급예정.

    가정(보수적): 이미 기한이 지난 채권은 회수에 넣지 않고, 기한이 지난 매입채무는 기준일 다음 날 지급한다.
    발주 대금은 sc_purchase_orders_ms에서만 잡는다(fin_ap_aging_ms의 po_id 보유 행은 이중 계상이라 제외).
    """
    need = ("fin_ar_aging_ms.csv", "fin_ap_aging_ms.csv", "sc_purchase_orders_ms.csv")
    if not all(d.has(n) for n in need):
        return None
    as_of = date.fromisoformat(d.as_of)
    flows = {}

    def add(day, amt):
        day = date.fromisoformat(day)
        if day <= as_of:
            if amt > 0:
                return                      # 기한 지난 채권: 회수 시점을 알 수 없어 제외
            day = as_of + timedelta(days=1)  # 기한 지난 매입채무: 즉시 지급
        if day <= as_of + timedelta(days=horizon_days):
            flows[day] = flows.get(day, 0) + amt

    for r in d.t("fin_ar_aging_ms.csv").itertuples():
        add(r.due_date, float(r.outstanding_krw))
    ap = d.t("fin_ap_aging_ms.csv")
    for r in ap[ap.po_id == "NONE"].itertuples():
        add(r.payment_due_date, -float(r.amount_krw))
    po = d.t("sc_purchase_orders_ms.csv")
    for r in po.itertuples():
        add(r.payment_due_date, -float(r.amount_krw))
    bal, low, low_day, breach = opening, opening, as_of, None
    for day in sorted(flows):
        bal += flows[day]
        if bal < low:
            low, low_day = bal, day
        if breach is None and bal < covenant:
            breach = day
    return {"breach_date": breach.isoformat() if breach else None, "min_balance": int(low),
            "min_date": low_day.isoformat(), "horizon_days": horizon_days,
            "po_total": int(po.amount_krw.sum()), "po_n": int(len(po)),
            "po_due": ", ".join(sorted(set(po.payment_due_date)))}


def cash(d: Data):
    out = {}
    cv = d.t("fin_cash_daily_cv.csv").copy()
    cv["date"] = pd.to_datetime(cv["date"])
    ecv = cv.set_index("date").closing_balance.resample("ME").last()
    out["cv_close"] = int(ecv.iloc[-1])
    out["cv_month_net"] = int(ecv.iloc[-1] - ecv.iloc[-2])
    # Corevia 자체 현금 근거: 월말 잔액 추이, 연속 감소 개월 수, 미회수 채권
    out["cv_series"] = [(k.strftime("%Y-%m"), int(v)) for k, v in ecv.iloc[-4:].items()]
    diffs = list(ecv.diff().iloc[1:])
    streak = 0
    for x in reversed(diffs):
        if x >= 0:
            break
        streak += 1
    out["cv_decline_months"] = streak
    out["cv_decline_amount"] = int(ecv.iloc[-1] - ecv.iloc[-1 - streak]) if streak else 0
    arc = d.t("fin_ar_aging_cv.csv")
    if len(arc):
        over = arc[(arc.aging_days > RULES["overdue_days"]) & (arc.outstanding_krw > 0)]
        out["cv_ar_total"] = int(arc.outstanding_krw.sum())
        out["cv_ar_over"] = int(over.outstanding_krw.sum())
        out["cv_ar_over_n"] = int(len(over))
        out["cv_ar_over_agency"] = int(over[over.agency_id != "NONE"].outstanding_krw.sum())
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
        out["trend_breach_date"] = (date.fromisoformat(d.as_of) + timedelta(days=int(months * 30.4))).isoformat()
    # 스케줄 기반 재전망이 가능하면 그것을 하한 도달 시점으로 쓴다(추세 추정보다 우선)
    out["reforecast"] = _reforecast(d, close, cov)
    out["breach_date"] = (out["reforecast"] or {}).get("breach_date") or out.get("trend_breach_date")
    out["breach_basis"] = "스케줄 재전망" if (out["reforecast"] or {}).get("breach_date") else "6개월 추세"
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
    out["covenant_note"] = _pick(out["covenant_doc"], "신규 약정 제한")
    out["lc_note"] = _pick(out["lc_notice"], "90일")
    board = d.docs.get("board_minutes.md", "")
    out["board"] = [l.lstrip("- ").replace("**", "") for l in board.splitlines() if l.startswith("- ")]
    return out


# ───────────────────────── 결재자용 AI 권고 ─────────────────────────
def recommend(match_result, members, agg, rk, cashinfo):
    """결재자에게 보여 줄 권고안. 근거를 모아 방향만 제시하고 결정은 사람이 한다."""
    reasons = []
    cands = {c["agency_id"]: c for c in (match_result or {}).get("candidates", [])}
    grp = [f"{m} {cands[m]['score']}/{cands[m]['max']}개 일치" for m in members if m in cands]
    if grp:
        reasons.append("동일 관련자 근거: " + ", ".join(grp))
    reasons.append(f"합산 장려금 {agg['amount']:,}원은 월 한도 {agg['limit_total']:,}원의 {agg['amount'] / agg['limit_total']:.1f}배 → 영업임원 승인 필수")
    if agg["diff"] > 0:
        reasons.append(f"요율 {agg['rate'] * 100:.1f}%는 일반 평균 {agg['std_mean'] * 100:.1f}%보다 높음 (차액 {agg['diff']:,}원) · 근거 정책 없음, 소명 확인 필요")
    if agg.get("prior_over"):
        reasons.append(f"과거 {len(agg['prior_over'])}개월 한도 초과 지급 {agg['prior_over_amount']:,}원은 사후 검토 진행 중")
    resolved = [c["label"] + " → " + c["detail"].split("→")[-1].strip() for c in rk["checks"] if c["result"] == "resolved"]
    if resolved:
        reasons.append("해제된 보류 사유: " + "; ".join(resolved))
    c = cashinfo or {}
    od = next((x for x in rk["checks"] if x["key"] == "overdue"), {})
    caution = False
    if od.get("amount"):
        caution = True
        reasons.append(f"이 그룹의 {RULES['overdue_days']}일 초과 미회수 채권 {od['amount']:,}원 ({len(od.get('rows', []))}건) · "
                       f"이번 장려금의 {od['amount'] / max(agg['amount'], 1):.1f}배")
    if c.get("cv_decline_months", 0) >= 2:
        caution = True
        reasons.append(f"Corevia 월말 현금이 {c['cv_decline_months']}개월 연속 감소 ({c['cv_decline_amount'] / 1e8:,.1f}억 원), "
                       f"현재 {c['cv_close'] / 1e8:,.1f}억 원")
    if c.get("cv_ar_over"):
        reasons.append(f"Corevia 전체 {RULES['overdue_days']}일 초과 미회수 채권 {c['cv_ar_over'] / 1e8:,.1f}억 원 ({c['cv_ar_over_n']}건)")
    if caution:
        return {"level": "warn", "title": "조건부 승인 권고 · 현금 지급 여부는 재무팀 판단",
                "text": "관련자·한도 검사는 통과했습니다. 다만 Corevia가 이 그룹에서 받을 돈이 밀려 있고 현금이 줄고 있어, "
                        "현금 지급 여부는 재무팀이 판단해야 합니다. 현금 지급을 거절하면 현금이 나가지 않는 크레딧 대체안을 바로 제안합니다.",
                "reasons": reasons, "source": "규칙 기반"}
    return {"level": "ok", "title": "승인 권고", "text": "관련자·한도·리스크 검사를 모두 통과했습니다.",
            "reasons": reasons, "source": "규칙 기반"}


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
    # 대리점의 다음 발주: 대리점 채널 청구일 간격으로 추정
    next_order, order_basis = None, ""
    if channel:
        arm = d.t("fin_ar_aging_ms.csv")
        inv = sorted(set(arm[arm.channel_id == channel["channel_id"]].invoice_date))
        if len(inv) >= 2:
            gaps = sorted((date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(inv, inv[1:]))
            gap = max(gaps[len(gaps) // 2], 7)
            next_order = (date.fromisoformat(inv[-1]) + timedelta(days=gap)).isoformat()
            order_basis = f"{channel['channel_id']} 청구일 {', '.join(inv[-2:])} 간격으로 추정"
    credit = int(round(amount * RULES["credit_ratio"]))
    valid_to = (date.fromisoformat(d.as_of) + timedelta(days=RULES["credit_valid_days"])).isoformat()
    split_cash = int(round(amount * RULES["split_cash_ratio"]))
    # Corevia↔Masil 정산: Masil이 Corevia에 지급할 발주 대금에서 같은 금액을 차감 (제안)
    settle = None
    if d.has("sc_purchase_orders_ms.csv"):
        po = d.t("sc_purchase_orders_ms.csv")
        po = po[(po.supplier_id == "Corevia") & (po.payment_due_date > d.as_of)]
        if len(po):
            total = int(po.amount_krw.sum())
            ex = po[po.sku_id.isin(ex_sku.sku_id)] if len(ex_sku) else po.iloc[0:0]   # 확정 결함 품목의 발주
            ok = po[~po.po_id.isin(ex.po_id)]
            base = ok if len(ok) else po
            settle = {"all_n": int(len(po)), "all_total": total,
                      "excluded_n": int(len(ex)), "excluded_total": int(ex.amount_krw.sum()),
                      "excluded_ids": ", ".join(ex.po_id),
                      "po_n": int(len(base)), "po_total": int(base.amount_krw.sum()),
                      "due": ", ".join(sorted(set(base.payment_due_date))),
                      "po_ids": f"{base.po_id.iloc[0]}~{base.po_id.iloc[-1]}",
                      "rows": [[r.po_id, r.sku_id, r.order_date, int(r.amount_krw), r.payment_due_date,
                                r.po_id in set(ex.po_id)] for r in po.itertuples()],
                      "pct": round(credit / int(base.amount_krw.sum()) * 100, 2)}
    lead = pd.to_numeric(sku.lead_time_weeks, errors="coerce").max() if len(sku) else None
    # 품목 제외 판단 근거 (감사 로그에 남긴다)
    basis = None
    if blocked_pl:
        open_conf = qc[(qc.defect_confirmed == "True") & (qc.status == "확정")]
        all_pl = sorted(set(sku.product_id)) if len(sku) else []
        clean = [p for p in all_pl if p not in blocked_pl]
        basis = {
            "rule": "미종결 확정 결함(defect_confirmed=True, status=확정)이 있는 제품군은 크레딧 적용 품목에서 제외",
            "excluded": [{"product_id": p, "lots": sorted(set(g.lot_id)), "claims": int(len(g)), "qty": int(g.qty_affected.sum())}
                         for p, g in open_conf.groupby("product_id")],
            "other_products_open_defects": 0,
            "discrimination": f"미종결 확정 결함은 {len(all_pl)}개 제품군 중 {', '.join(blocked_pl)}에만 있음 "
                              f"({', '.join(clean)}는 0건) → SKU {len(sku)}종 중 {len(ex_sku)}종만 제외되고 {len(ok_sku)}종이 남음",
            "why_product_level": "발주(sc_purchase_orders_ms)와 클레임 lot을 잇는 키가 없어 lot 단위로는 제외할 수 없음. "
                                 "결함 품목에 할인을 얹어 판매를 늘리는 것을 피하려고 제품군 단위로 제외",
        }
    return {
        "exclusion_basis": basis,
        "credit": credit, "channel": channel, "valid_to": valid_to,
        "skus_ok": [f"{r.sku_id} {r.sku_name}" for r in ok_sku.itertuples()],
        "skus_excluded": [f"{r.sku_id} {r.sku_name}" for r in ex_sku.itertuples()],
        "blocked_pl": blocked_pl, "next_order": next_order, "next_order_basis": order_basis,
        "masil_impact_pct": round(credit / cashinfo["headroom"] * 100, 2) if cashinfo.get("headroom") else None,
        "settlement": settle, "lead_time_weeks_max": int(lead) if lead == lead and lead is not None else None,
        "split": {"cash": split_cash, "credit": amount - split_cash},
    }
