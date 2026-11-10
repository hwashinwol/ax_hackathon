"""상태 머신 + 저장소(SQLite). 모든 상태 변경은 감사 로그에 남는다(로그는 수정·삭제 불가)."""
import json
import sqlite3
from datetime import datetime

import engine
from config import DB_PATH, REJECT_REASONS, RULES

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, anchor_id TEXT, period TEXT, memo TEXT,
  extra_ids TEXT, status TEXT, created_by TEXT, created_at TEXT,
  match_json TEXT, reviews_json TEXT, agg_json TEXT, risk_json TEXT, resolutions_json TEXT,
  cash_json TEXT, alt_json TEXT, alt_accept_json TEXT, settlement TEXT);
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, step INTEGER, role TEXT,
  decision TEXT, reason TEXT, memo TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS threads(
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, role TEXT, kind TEXT, body TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS notices(
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, to_role TEXT, level TEXT, text TEXT, at TEXT, read INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS credits(
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, amount INTEGER, cash_amount INTEGER, channel TEXT,
  valid_to TEXT, skus TEXT, status TEXT, masil_po TEXT, settled_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, actor TEXT, action TEXT,
  before TEXT, after TEXT, payload TEXT, at TEXT);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;
"""

# 허용되는 상태 전이
TRANSITIONS = {
    "REQUESTED": {"MATCHING"},
    "MATCHING": {"MANUAL_REVIEW", "NOT_ELIGIBLE", "RISK_CHECK"},
    "MANUAL_REVIEW": {"MATCHING"},
    "RISK_CHECK": {"PENDING", "AWAITING_APPROVAL"},
    "PENDING": {"RISK_CHECK", "CLOSED"},
    "AWAITING_APPROVAL": {"PAID", "REJECTED"},
    "REJECTED": {"ALT_PROPOSED", "CLOSED"},
    "ALT_PROPOSED": {"CREDIT_SCHEDULED", "CLOSED"},
    "CREDIT_SCHEDULED": {"CREDIT_SETTLED"},
}


class WorkflowError(Exception):
    pass


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path=None):
    p = path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def J(v):
    return json.dumps(v, ensure_ascii=False, default=str)


def L(s, default=None):
    return json.loads(s) if s else default


class Workflow:
    def __init__(self, data, con):
        self.d = data
        self.con = con

    # ── 공통 ──
    def get(self, rid):
        r = self.con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r:
            raise WorkflowError("요청을 찾을 수 없습니다.")
        r = dict(r)
        for k in ("extra_ids", "match_json", "reviews_json", "agg_json", "risk_json", "resolutions_json",
                  "cash_json", "alt_json", "alt_accept_json"):
            r[k.replace("_json", "")] = L(r.pop(k), None)
        r["resolutions"] = r["resolutions"] or {}
        r["reviews"] = r["reviews"] or {}
        r["alt_accept"] = r["alt_accept"] or {}
        return r

    def _set(self, rid, **cols):
        sets = ", ".join(f"{k}=?" for k in cols)
        self.con.execute(f"UPDATE requests SET {sets} WHERE id=?", (*cols.values(), rid))

    def audit(self, rid, actor, action, before=None, after=None, payload=None):
        self.con.execute("INSERT INTO audit_log(request_id,actor,action,before,after,payload,at) VALUES(?,?,?,?,?,?,?)",
                         (rid, actor, action, before, after, J(payload or {}), now()))

    def notify(self, rid, roles, text, level="info"):
        for role in roles:
            self.con.execute("INSERT INTO notices(request_id,to_role,level,text,at) VALUES(?,?,?,?,?)",
                             (rid, role, level, text, now()))

    def move(self, rid, actor, to, action, payload=None):
        cur = self.get(rid)["status"]
        if to not in TRANSITIONS.get(cur, set()):
            raise WorkflowError(f"{cur} → {to} 전이는 허용되지 않습니다.")
        self._set(rid, status=to)
        self.audit(rid, actor, action, cur, to, payload)

    def _need(self, r, status, role, actor):
        if r["status"] not in (status if isinstance(status, (list, tuple, set)) else [status]):
            raise WorkflowError(f"현재 상태({r['status']})에서는 할 수 없는 작업입니다.")
        if role and actor not in (role if isinstance(role, (list, tuple, set)) else [role]):
            raise WorkflowError(f"{actor}은(는) 이 작업 권한이 없습니다. 필요 역할: {role}")

    def members(self, r):
        m = r["match"]
        ids = [m["anchor"]["agency_id"]]
        for c in m["candidates"]:
            if c["decision"] == "auto" or r["reviews"].get(c["agency_id"], {}).get("decision") == "confirm":
                ids.append(c["agency_id"])
        return ids

    # ── 단계별 액션 ──
    def create(self, actor, anchor_id, period, extra_ids=(), memo=""):
        if actor != "영업팀":
            raise WorkflowError("요청 생성은 영업팀만 할 수 있습니다.")
        if not self.d.agency(anchor_id):
            raise WorkflowError("대리점을 찾을 수 없습니다.")
        extra = [e for e in extra_ids if e and e != anchor_id]
        cur = self.con.execute(
            "INSERT INTO requests(anchor_id,period,memo,extra_ids,status,created_by,created_at,resolutions_json,reviews_json)"
            " VALUES(?,?,?,?,?,?,?,?,?)", (anchor_id, period, memo, J(extra), "REQUESTED", actor, now(), "{}", "{}"))
        rid = cur.lastrowid
        code = f"INC-{period.replace('-', '')}-{rid:04d}"
        self._set(rid, code=code)
        self.audit(rid, actor, "요청 생성", None, "REQUESTED", {"anchor": anchor_id, "period": period, "extra": extra, "memo": memo})
        self.con.commit()
        return rid

    def run_matching(self, rid, actor):
        r = self.get(rid)
        self._need(r, "REQUESTED", "영업팀", actor)
        m = engine.match(self.d, r["anchor_id"], r["extra_ids"] or [])
        self._set(rid, match_json=J(m))
        self.move(rid, "AI", "MATCHING", "AI 관련 거래처 매칭",
                  {c["agency_id"]: {"score": c["score"], "decision": c["decision"]} for c in m["candidates"]})
        review = [c for c in m["candidates"] if c["decision"] == "review"]
        if review:
            self.move(rid, "AI", "MANUAL_REVIEW", "수동 검토 요청", {"targets": [c["agency_id"] for c in review]})
            self.notify(rid, ["내부감시팀"], f"{r['code']} · {', '.join(c['agency_id'] for c in review)} 매칭 수동 검토 필요", "warn")
        self.con.commit()

    def review(self, rid, actor, agency_id, decision, memo=""):
        r = self.get(rid)
        self._need(r, "MANUAL_REVIEW", "내부감시팀", actor)
        if decision not in ("confirm", "exclude"):
            raise WorkflowError("결정 값이 올바르지 않습니다.")
        r["reviews"][agency_id] = {"decision": decision, "memo": memo, "by": actor, "at": now()}
        self._set(rid, reviews_json=J(r["reviews"]))
        self.audit(rid, actor, f"수동 검토 {'그룹 확정' if decision == 'confirm' else '제외'} · {agency_id}", payload={"memo": memo})
        pending = [c for c in r["match"]["candidates"] if c["decision"] == "review" and c["agency_id"] not in r["reviews"]]
        if not pending:
            self.move(rid, actor, "MATCHING", "수동 검토 완료")
            self.notify(rid, ["영업팀"], f"{r['code']} · 내부감시팀 그룹 검토 완료")
        self.con.commit()

    def evaluate(self, rid, actor):
        """그룹 확정 → 실적 합산 → (충족 시) 리스크 검사."""
        r = self.get(rid)
        self._need(r, "MATCHING", "영업팀", actor)
        members = self.members(r)
        agg = engine.aggregate(self.d, members, r["period"])
        self._set(rid, agg_json=J(agg))
        self.audit(rid, "AI", "실적 합산·요율 계산", payload={"members": members, "total": agg["total_sales"],
                                                            "threshold": agg["threshold"], "amount": agg["amount"],
                                                            "rate": round(agg["rate"], 4), "std_mean": round(agg["std_mean"], 4)})
        if agg["prior_excess"] > 0:
            self.notify(rid, ["내부감시팀"], f"{r['code']} · 과거 우대 요율 초과 지급분 {agg['prior_excess']:,}원 사후 검토 대상", "warn")
            self.audit(rid, "AI", "사후 검토 등록", payload={"prior_paid": agg["prior_paid"], "prior_excess": agg["prior_excess"]})
        if not agg["meets"]:
            self.move(rid, "AI", "NOT_ELIGIBLE", "합산 실적 기준 미달", {"total": agg["total_sales"], "threshold": agg["threshold"]})
            self.notify(rid, ["영업팀"], f"{r['code']} · 합산 실적 기준 미달로 종결")
        else:
            self.move(rid, "AI", "RISK_CHECK", "합산 실적 기준 충족")
            self._run_risk(rid)
        self.con.commit()

    def _run_risk(self, rid):
        r = self.get(rid)
        members = self.members(r)
        rk = engine.risk(self.d, members, r["period"], r["agg"], r["resolutions"])
        cash = engine.cash(self.d)
        self._set(rid, risk_json=J(rk), cash_json=J(cash))
        if rk["blocking"]:
            self.move(rid, "AI", "PENDING", "리스크 감지 · 자동 보류", {"hold": rk["hold"]})
            roles = {"영업팀"}
            if "overdue" in rk["hold"] or "eligible" in rk["hold"]:
                roles.add("재무팀")
            if "malicious" in rk["hold"]:
                roles.add("품질팀")
            labels = [c["label"] for c in rk["checks"] if c["key"] in rk["hold"]]
            self.notify(rid, sorted(roles), f"{r['code']} · 지급 보류 및 사유 소명 요망: {', '.join(labels)}", "crit")
            self.con.execute("INSERT INTO threads(request_id,role,kind,body,at) VALUES(?,?,?,?,?)",
                             (rid, "AI", "system", "보류 사유: " + ", ".join(labels) + ". 영업팀 소명 후 담당 부서가 해제해야 결재가 재개됩니다.", now()))
        else:
            self.move(rid, "AI", "AWAITING_APPROVAL", "리스크 없음 · 결재 요청", {"checks": {c["key"]: c["result"] for c in rk["checks"]}})
            self.notify(rid, ["재무팀"], f"{r['code']} · 장려금 {r['agg']['amount']:,}원 결재 요청 (1차)")

    def explain(self, rid, actor, body):
        r = self.get(rid)
        self._need(r, "PENDING", ["영업팀", "품질팀"], actor)
        if not body.strip():
            raise WorkflowError("소명 내용을 입력하세요.")
        self.con.execute("INSERT INTO threads(request_id,role,kind,body,at) VALUES(?,?,?,?,?)", (rid, actor, "explain", body, now()))
        self.audit(rid, actor, "소명 제출", payload={"body": body})
        self.notify(rid, ["재무팀", "품질팀"], f"{r['code']} · {actor} 소명 제출")
        self.con.commit()

    def resolve(self, rid, actor, key, kind, memo=""):
        """보류 해제: overdue(재무팀: 회수 확인/상계), malicious(품질팀: 해제)"""
        r = self.get(rid)
        owner = {"overdue": "재무팀", "malicious": "품질팀", "eligible": "재무팀"}[key]
        self._need(r, "PENDING", owner, actor)
        if key not in r["risk"]["hold"]:
            raise WorkflowError("해당 보류 사유가 없습니다.")
        has_explain = self.con.execute("SELECT 1 FROM threads WHERE request_id=? AND kind='explain' AND role='영업팀'", (rid,)).fetchone()
        if not has_explain:
            raise WorkflowError("영업팀 소명이 먼저 제출되어야 해제할 수 있습니다.")
        labels = {"collected": "회수 확인", "offset": "장려금 상계", "released": "품질팀 해제"}
        r["resolutions"][key] = {"kind": kind, "label": labels.get(kind, kind), "memo": memo, "by": actor, "at": now()}
        self._set(rid, resolutions_json=J(r["resolutions"]))
        if kind == "offset":
            self._set(rid, settlement="OFFSET")
        self.audit(rid, actor, f"보류 해제 · {labels.get(kind, kind)}", payload={"key": key, "memo": memo})
        remaining = [h for h in r["risk"]["hold"] if h not in r["resolutions"]]
        if not remaining:
            self.move(rid, actor, "RISK_CHECK", "보류 해제 · 리스크 재검사")
            self._run_risk(rid)
        self.con.commit()

    def dismiss(self, rid, actor, memo=""):
        r = self.get(rid)
        self._need(r, "PENDING", ["재무팀", "품질팀"], actor)
        self.move(rid, actor, "CLOSED", "소명 기각 · 종결", {"memo": memo})
        self.notify(rid, ["영업팀"], f"{r['code']} · 소명 기각으로 종결", "crit")
        self.con.commit()

    def approve(self, rid, actor, memo=""):
        r = self.get(rid)
        self._need(r, "AWAITING_APPROVAL", ["재무팀", "내부감시팀"], actor)
        done = [a["role"] for a in self.con.execute("SELECT role FROM approvals WHERE request_id=? AND decision='approve'", (rid,))]
        need = "재무팀" if "재무팀" not in done else "내부감시팀"
        if actor != need:
            raise WorkflowError(f"지금은 {need} 결재 차례입니다.")
        step = 1 if need == "재무팀" else 2
        self.con.execute("INSERT INTO approvals(request_id,step,role,decision,memo,at) VALUES(?,?,?,?,?,?)",
                         (rid, step, actor, "approve", memo, now()))
        self.audit(rid, actor, f"{step}차 결재 승인", payload={"memo": memo})
        if step == 1:
            self.notify(rid, ["내부감시팀"], f"{r['code']} · 재무팀 승인, 2차 결재 요청")
        else:
            how = "연체 채권 상계" if r["settlement"] == "OFFSET" else "현금 지급"
            pays = [{"rebate_ids": m["rebate_ids"], "agency_id": m["agency_id"], "amount": m["rebate"], "account": m["account"]}
                    for m in r["agg"]["members"]]
            self.move(rid, actor, "PAID", f"지급 완료 · {how}", {"payments": pays, "settlement": how})
            self.notify(rid, ["영업팀", "재무팀"], f"{r['code']} · {r['agg']['amount']:,}원 {how} 완료", "ok")
        self.con.commit()

    def reject(self, rid, actor, reason, memo=""):
        r = self.get(rid)
        self._need(r, "AWAITING_APPROVAL", ["재무팀", "내부감시팀"], actor)
        if reason not in REJECT_REASONS:
            raise WorkflowError("거절 사유 코드를 선택하세요.")
        step = 1 if actor == "재무팀" else 2
        self.con.execute("INSERT INTO approvals(request_id,step,role,decision,reason,memo,at) VALUES(?,?,?,?,?,?,?)",
                         (rid, step, actor, "reject", reason, memo, now()))
        self.move(rid, actor, "REJECTED", f"결재 거절 · {reason}", {"memo": memo})
        if reason == "CASH_SHORTAGE":
            alt = engine.alternative(self.d, r["agg"]["amount"], self.members(r), r["cash"] or engine.cash(self.d))
            self._set(rid, alt_json=J(alt), alt_accept_json=J({"plan": "full"}))
            self.move(rid, "AI", "ALT_PROPOSED", "AI 대안 제안 · Masil 크레딧",
                      {"credit": alt["credit"], "channel": alt["channel"].get("channel_id"), "masil_impact_pct": alt["masil_impact_pct"]})
            self.notify(rid, ["영업팀", "재무팀"], f"{r['code']} · 현금 거절, Masil 재고 할인 대체안 검토 요청", "warn")
        else:
            self.move(rid, "AI", "CLOSED", "거절 종결")
            self.notify(rid, ["영업팀"], f"{r['code']} · {actor} 거절({reason})로 종결", "crit")
        self.con.commit()

    def alt_plan(self, rid, actor, plan):
        r = self.get(rid)
        self._need(r, "ALT_PROPOSED", ["영업팀", "재무팀"], actor)
        if plan not in ("full", "split"):
            raise WorkflowError("대안 유형이 올바르지 않습니다.")
        self._set(rid, alt_accept_json=J({"plan": plan}))   # 안이 바뀌면 수락 초기화
        self.audit(rid, actor, f"대안 변경 · {'크레딧 100%' if plan == 'full' else '분할(현금+크레딧)'}")
        self.con.commit()

    def alt_accept(self, rid, actor):
        r = self.get(rid)
        self._need(r, "ALT_PROPOSED", ["영업팀", "재무팀"], actor)
        acc = r["alt_accept"]
        acc[actor] = now()
        self._set(rid, alt_accept_json=J(acc))
        self.audit(rid, actor, "대안 수락", payload={"plan": acc.get("plan")})
        if "영업팀" in acc and "재무팀" in acc:
            alt = r["alt"]
            split = acc.get("plan") == "split"
            credit = alt["split"]["credit"] if split else alt["credit"]
            cash_amt = alt["split"]["cash"] if split else 0
            self.con.execute("INSERT INTO credits(request_id,amount,cash_amount,channel,valid_to,skus,status) VALUES(?,?,?,?,?,?,?)",
                             (rid, credit, cash_amt, alt["channel"].get("channel_id"), alt["valid_to"], J(alt["skus_ok"]), "SCHEDULED"))
            self.move(rid, actor, "CREDIT_SCHEDULED", "지급 계획 변경 · 크레딧 예약", {"credit": credit, "deferred_cash": cash_amt})
            self.notify(rid, ["Masil"], f"{r['code']} · {alt['channel'].get('channel_name')} 다음 발주에 {credit:,}원 크레딧 차감 예약", "warn")
            self.notify(rid, ["영업팀", "재무팀"], f"{r['code']} · 지급 계획 변경 완료", "ok")
        self.con.commit()

    def alt_decline(self, rid, actor, memo=""):
        r = self.get(rid)
        self._need(r, "ALT_PROPOSED", ["영업팀", "재무팀"], actor)
        self.move(rid, actor, "CLOSED", "대안 거절 · 종결", {"memo": memo})
        self.con.commit()

    def settle_credit(self, rid, actor, po):
        r = self.get(rid)
        self._need(r, "CREDIT_SCHEDULED", "Masil", actor)
        if not po.strip():
            raise WorkflowError("반영된 발주서(PO) 번호를 입력하세요.")
        self.con.execute("UPDATE credits SET status='SETTLED', masil_po=?, settled_at=? WHERE request_id=?", (po, now(), rid))
        self.move(rid, actor, "CREDIT_SETTLED", "Masil 발주서 크레딧 차감 확인", {"po": po})
        self.notify(rid, ["영업팀", "재무팀"], f"{r['code']} · 크레딧 정산 완료 ({po})", "ok")
        self.con.commit()
