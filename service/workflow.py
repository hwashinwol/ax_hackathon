"""상태 머신 + 저장소(SQLite). 모든 상태 변경은 감사 로그에 남는다(로그는 수정·삭제 불가)."""
import json
import sqlite3
from datetime import datetime

import agent
import engine
from config import APPROVAL_CHAIN, DB_PATH, REJECT_REASONS

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, anchor_id TEXT, period TEXT, memo TEXT,
  extra_ids TEXT, status TEXT, created_by TEXT, created_at TEXT,
  match_json TEXT, reviews_json TEXT, agg_json TEXT, risk_json TEXT, resolutions_json TEXT,
  cash_json TEXT, alt_json TEXT, alt_accept_json TEXT, ai_json TEXT);
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, step INTEGER, role TEXT,
  decision TEXT, reason TEXT, memo TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS threads(
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, role TEXT, kind TEXT, body TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS notices(
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, to_role TEXT, level TEXT, text TEXT, at TEXT, read INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS credits(
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, amount INTEGER, cash_amount INTEGER, channel TEXT,
  valid_to TEXT, skus TEXT, status TEXT, masil_po TEXT, settled_at TEXT,
  cash_status TEXT, cash_ref TEXT, cash_memo TEXT, cash_paid_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, actor TEXT, action TEXT,
  before TEXT, after TEXT, payload TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS claim_judgments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, agency_id TEXT, claim_date TEXT, claim_type TEXT,
  judgment TEXT, memo TEXT, source TEXT, by TEXT, at TEXT);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;
"""

# 허용되는 상태 전이
TRANSITIONS = {
    "REQUESTED": {"MATCHING"},
    "MATCHING": {"MANUAL_REVIEW", "WITHIN_LIMIT", "RISK_CHECK"},
    "MANUAL_REVIEW": {"MATCHING"},
    "RISK_CHECK": {"PENDING", "AWAITING_APPROVAL"},
    "PENDING": {"RISK_CHECK", "CLOSED"},
    "AWAITING_APPROVAL": {"PAID", "REJECTED"},
    "REJECTED": {"ALT_PROPOSED", "CLOSED"},
    "ALT_PROPOSED": {"CREDIT_SCHEDULED", "CLOSED"},
    "CREDIT_SCHEDULED": {"CREDIT_SETTLED", "CASH_PENDING"},
    "CASH_PENDING": {"CREDIT_SETTLED"},      # 분할안: 크레딧 차감 후 이연 현금분 지급 대기
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
    # 이전 버전 DB에는 이연 현금분 추적 컬럼이 없다
    have = {c[1] for c in con.execute("PRAGMA table_info(credits)")}
    for col in ("cash_status", "cash_ref", "cash_memo", "cash_paid_at"):
        if col not in have:
            con.execute(f"ALTER TABLE credits ADD COLUMN {col} TEXT")
    return con


def J(v):
    return json.dumps(v, ensure_ascii=False, default=str)


def L(s, default=None):
    return json.loads(s) if s else default


class Workflow:
    def __init__(self, data, con, ai_client=None):
        self.d = data
        self.con = con
        self.ai_client = ai_client      # 테스트에서 가짜 클라이언트를 넣는다. None이면 환경의 자격 증명으로 만든다.

    def _ai(self, rid, task):
        """AI 에이전트를 돌리고 결과·호출 내역을 요청과 감사 로그에 남긴다. LLM을 못 쓰면 None."""
        res, err = agent.safe_run(self, rid, task, self.ai_client)
        label = {"review": "결재 권고", "explain": "소명 검토", "alt": "대안 설명"}[task]
        if res:
            ai = self.get(rid)["ai"]
            ai[task] = {**res["output"], "model": res["model"], "tools": [t["tool"] for t in res["trace"]], "at": now()}
            self._set(rid, ai_json=J(ai))
            self.audit(rid, "AI", f"AI 에이전트 실행 · {label}",
                       payload={"model": res["model"], "tool_calls": res["trace"], "usage": res["usage"], "output": res["output"]})
            return res["output"]
        self.audit(rid, "AI", f"AI 에이전트 미실행 · {label}은 규칙 기반으로 대체",
                   payload={"reason": err or "LLM 자격 증명 없음 또는 CV_AI=off"})
        return None

    # ── 공통 ──
    def get(self, rid):
        r = self.con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r:
            raise WorkflowError("요청을 찾을 수 없습니다.")
        r = dict(r)
        for k in ("extra_ids", "match_json", "reviews_json", "agg_json", "risk_json", "resolutions_json",
                  "cash_json", "alt_json", "alt_accept_json", "ai_json"):
            r[k.replace("_json", "")] = L(r.pop(k), None)
        r["ai"] = r["ai"] or {}
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

    # 지급이 끝났거나 지급이 확정된 상태. 같은 월·같은 관련자 그룹의 요청이 이 상태면 중복 지급이다.
    SETTLED = ("PAID", "CREDIT_SCHEDULED", "CASH_PENDING", "CREDIT_SETTLED")

    def _block_duplicate(self, rid, members, months):
        """중복 지급 차단: 원천 데이터에서 이미 '승인'(지급)된 장려금, 또는 이 서비스에서 이미 지급 처리된 요청과 겹치면 막는다."""
        rb = self.d.t("sales_rebate_payment_cv.csv")
        if len(rb):
            paid = rb[rb.agency_id.isin(members) & rb.period_ym.isin(months) & (rb.approval_status == "승인")]
            if len(paid):
                raise WorkflowError(f"중복 지급 차단: {', '.join(sorted(set(paid.period_ym)))} 장려금은 이미 지급(승인)되었습니다 "
                                    f"({', '.join(paid.rebate_id)}).")
        for o in self.con.execute("SELECT id FROM requests WHERE id<>? AND status IN (%s)" % ",".join("?" * len(self.SETTLED)),
                                  (rid, *self.SETTLED)).fetchall():
            o = self.get(o["id"])
            if not (o["match"] and o["agg"]):
                continue
            same = sorted(set(members) & set(self.members(o)))
            same_m = sorted(set(months) & set(o["agg"]["months"]))
            if same and same_m:
                raise WorkflowError(f"중복 지급 차단: 같은 관련자 그룹({', '.join(same)})의 {', '.join(same_m)} 장려금은 "
                                    f"{o['code']}에서 이미 지급 처리되었습니다({o['status']}).")

    # ── 품질팀 클레임 판정 (원천 qc_claims_cv에 판정 필드와 대리점 연결이 없어 시스템에서 받는다) ──
    def judgments(self):
        return [dict(j) for j in self.con.execute("SELECT * FROM claim_judgments ORDER BY claim_date DESC, id DESC")]

    def add_judgment(self, actor, agency_id, claim_date, claim_type, memo="", source="품질팀 입력"):
        if actor != "품질팀":
            raise WorkflowError("클레임 판정 등록은 품질팀만 할 수 있습니다.")
        if not self.d.agency(agency_id):
            raise WorkflowError("대리점을 찾을 수 없습니다.")
        try:
            datetime.strptime(claim_date, "%Y-%m-%d")
        except ValueError:
            raise WorkflowError("클레임 일자는 YYYY-MM-DD 형식으로 입력하세요.")
        if not claim_type.strip():
            raise WorkflowError("클레임 유형을 입력하세요.")
        self.con.execute("INSERT INTO claim_judgments(agency_id,claim_date,claim_type,judgment,memo,source,by,at) VALUES(?,?,?,?,?,?,?,?)",
                         (agency_id, claim_date, claim_type.strip(), "악의적", memo, source, actor, now()))
        self.audit(None, actor, f"클레임 '악의적' 판정 등록 · {agency_id}",
                   payload={"claim_date": claim_date, "claim_type": claim_type, "memo": memo, "source": source})
        self.con.commit()

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
        """그룹 확정 → 관련자 합산 장려금을 월 한도와 비교 → (초과 시) 리스크 검사·임원 결재선."""
        r = self.get(rid)
        self._need(r, "MATCHING", "영업팀", actor)
        members = self.members(r)
        agg = engine.aggregate(self.d, members, r["period"])
        self._block_duplicate(rid, members, agg["months"])      # 상신 단계에서 중복 지급 차단
        self._set(rid, agg_json=J(agg))
        self.audit(rid, "AI", "관련자 합산·한도 비교", payload={"members": members, "amount": agg["amount"],
                                                            "limit": agg["limit"], "monthly": agg["monthly"],
                                                            "rate": round(agg["rate"], 4), "std_mean": round(agg["std_mean"], 4)})
        if agg["prior_over"]:
            self.notify(rid, ["내부감시팀"],
                        f"{r['code']} · 과거 {len(agg['prior_over'])}개월 합산 한도 초과 지급 {agg['prior_over_amount']:,}원 "
                        f"(결재 {agg['prior_approver']} 단독) 사후 검토 대상", "warn")
            self.audit(rid, "AI", "사후 검토 등록", payload={"prior_over": agg["prior_over"], "prior_paid": agg["prior_paid"],
                                                          "rate_excess": agg["prior_excess"]})
        if not agg["over_limit"]:
            self.move(rid, "AI", "WITHIN_LIMIT", "합산 한도 이하 · 영업지원팀 전결 범위", {"amount": agg["amount"], "limit": agg["limit"]})
            self.notify(rid, ["영업팀"], f"{r['code']} · 관련자 합산 {agg['amount']:,}원, 월 한도 이하라 영업지원팀 전결로 처리")
        else:
            self.move(rid, "AI", "RISK_CHECK", "합산 한도 초과 · 영업임원 승인 대상", {"amount": agg["amount"], "limit": agg["limit"]})
            self._run_risk(rid)
        self.con.commit()

    def _run_risk(self, rid):
        r = self.get(rid)
        members = self.members(r)
        rk = engine.risk(self.d, members, r["period"], r["agg"], r["resolutions"], self.judgments())
        cash = engine.cash(self.d)
        self._set(rid, risk_json=J(rk), cash_json=J(cash))
        if rk["blocking"]:
            self.move(rid, "AI", "PENDING", "리스크 감지 · 자동 보류", {"hold": rk["hold"]})
            roles = {"영업팀"}
            if "eligible" in rk["hold"]:
                roles.add("재무팀")
            if "malicious" in rk["hold"]:
                roles.add("품질팀")
            labels = [c["label"] for c in rk["checks"] if c["key"] in rk["hold"]]
            self.notify(rid, sorted(roles), f"{r['code']} · 지급 보류 및 사유 소명 요망: {', '.join(labels)}", "crit")
            self.con.execute("INSERT INTO threads(request_id,role,kind,body,at) VALUES(?,?,?,?,?)",
                             (rid, "AI", "system", "보류 사유: " + ", ".join(labels) + ". 영업팀 소명 후 담당 부서가 해제해야 결재가 재개됩니다.", now()))
        else:
            self.move(rid, "AI", "AWAITING_APPROVAL", "보류 사유 없음 · 결재 요청", {"checks": {c["key"]: c["result"] for c in rk["checks"]}})
            warn = [c["label"] for c in rk["checks"] if c["key"] == "overdue" and c["result"] == "warn"]
            self.notify(rid, [APPROVAL_CHAIN[0]], f"{r['code']} · 장려금 {r['agg']['amount']:,}원 결재 요청 (1차)"
                        + (f" · 경고: {warn[0]}" if warn else ""), "warn" if warn else "info")
            self._ai(rid, "review")

    def explain(self, rid, actor, body):
        r = self.get(rid)
        self._need(r, "PENDING", ["영업팀", "품질팀"], actor)
        if not body.strip():
            raise WorkflowError("소명 내용을 입력하세요.")
        self.con.execute("INSERT INTO threads(request_id,role,kind,body,at) VALUES(?,?,?,?,?)", (rid, actor, "explain", body, now()))
        self.audit(rid, actor, "소명 제출", payload={"body": body})
        owners = sorted({"malicious": "품질팀", "eligible": "재무팀"}[h] for h in r["risk"]["hold"])
        self.notify(rid, owners, f"{r['code']} · {actor} 소명 제출")
        out = self._ai(rid, "explain")
        if out:
            body = ("AI 소명 검토: " + out["assessment"]
                    + (" 추가로 필요한 것: " + "; ".join(out["missing"]) if out["missing"] else "")
                    + " (해제 여부는 담당 부서가 결정합니다)")
            self.con.execute("INSERT INTO threads(request_id,role,kind,body,at) VALUES(?,?,?,?,?)", (rid, "AI", "system", body, now()))
        self.con.commit()

    def resolve(self, rid, actor, key, kind, memo=""):
        """보류 해제: malicious(품질팀 해제), eligible(재무팀 확인)"""
        r = self.get(rid)
        owner = {"malicious": "품질팀", "eligible": "재무팀"}.get(key)
        if not owner:
            raise WorkflowError("해제할 수 있는 보류 사유가 아닙니다.")
        self._need(r, "PENDING", owner, actor)
        if key not in r["risk"]["hold"]:
            raise WorkflowError("해당 보류 사유가 없습니다.")
        has_explain = self.con.execute("SELECT 1 FROM threads WHERE request_id=? AND kind='explain' AND role='영업팀'", (rid,)).fetchone()
        if not has_explain:
            raise WorkflowError("영업팀 소명이 먼저 제출되어야 해제할 수 있습니다.")
        labels = {"released": "품질팀 해제", "confirmed": "재무팀 확인"}
        r["resolutions"][key] = {"kind": kind, "label": labels.get(kind, kind), "memo": memo, "by": actor, "at": now()}
        self._set(rid, resolutions_json=J(r["resolutions"]))
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

    def next_approver(self, rid):
        done = [a["role"] for a in self.con.execute("SELECT role FROM approvals WHERE request_id=? AND decision='approve'", (rid,))]
        return next((x for x in APPROVAL_CHAIN if x not in done), APPROVAL_CHAIN[-1])

    def approve(self, rid, actor, memo=""):
        r = self.get(rid)
        self._need(r, "AWAITING_APPROVAL", APPROVAL_CHAIN, actor)
        need = self.next_approver(rid)
        if actor != need:
            raise WorkflowError(f"지금은 {need} 결재 차례입니다.")
        step = APPROVAL_CHAIN.index(need) + 1
        self.con.execute("INSERT INTO approvals(request_id,step,role,decision,memo,at) VALUES(?,?,?,?,?,?)",
                         (rid, step, actor, "approve", memo, now()))
        self.audit(rid, actor, f"{step}차 결재 승인", payload={"memo": memo})
        if step < len(APPROVAL_CHAIN):
            nxt = APPROVAL_CHAIN[step]
            self.notify(rid, [nxt], f"{r['code']} · {actor} 승인, {step + 1}차 결재 요청")
        else:
            # 같은 그룹·같은 월 요청이 나란히 결재에 올라온 경우를 지급 직전에 한 번 더 막는다
            self._block_duplicate(rid, self.members(r), r["agg"]["months"])
            how = "현금 지급"
            pays = [{"rebate_ids": m["rebate_ids"], "agency_id": m["agency_id"], "amount": m["rebate"], "account": m["account"]}
                    for m in r["agg"]["members"]]
            self.move(rid, actor, "PAID", f"지급 완료 · {how}", {"payments": pays, "settlement": how})
            self.notify(rid, ["영업팀", "재무팀"], f"{r['code']} · {r['agg']['amount']:,}원 {how} 완료", "ok")
        self.con.commit()

    def reject(self, rid, actor, reason, memo=""):
        r = self.get(rid)
        self._need(r, "AWAITING_APPROVAL", APPROVAL_CHAIN, actor)
        if reason not in REJECT_REASONS:
            raise WorkflowError("거절 사유 코드를 선택하세요.")
        need = self.next_approver(rid)
        if actor != need:           # 거절도 승인과 같은 결재 순서를 따른다
            raise WorkflowError(f"지금은 {need} 결재 차례입니다.")
        step = APPROVAL_CHAIN.index(actor) + 1
        self.con.execute("INSERT INTO approvals(request_id,step,role,decision,reason,memo,at) VALUES(?,?,?,?,?,?,?)",
                         (rid, step, actor, "reject", reason, memo, now()))
        self.move(rid, actor, "REJECTED", f"결재 거절 · {reason}", {"memo": memo})
        if reason == "CASH_SHORTAGE":
            alt = engine.alternative(self.d, r["agg"]["amount"], self.members(r), r["cash"] or engine.cash(self.d))
            self._set(rid, alt_json=J(alt), alt_accept_json=J({"plan": "full"}))
            self.move(rid, "AI", "ALT_PROPOSED", "AI 대안 제안 · Masil 크레딧",
                      {"credit": alt["credit"], "channel": alt["channel"].get("channel_id"),
                       "skus_ok": alt["skus_ok"], "skus_excluded": alt["skus_excluded"],
                       "exclusion_basis": alt["exclusion_basis"],          # 품목 제외 판단 근거
                       "settlement": {k: v for k, v in (alt["settlement"] or {}).items() if k != "rows"}})
            self.notify(rid, ["영업팀", "재무팀"], f"{r['code']} · 현금 거절, Masil 재고 할인 대체안 검토 요청", "warn")
            self._ai(rid, "alt")
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
            self.con.execute("INSERT INTO credits(request_id,amount,cash_amount,channel,valid_to,skus,status,cash_status) VALUES(?,?,?,?,?,?,?,?)",
                             (rid, credit, cash_amt, alt["channel"].get("channel_id"), alt["valid_to"], J(alt["skus_ok"]), "SCHEDULED",
                              "WAITING" if cash_amt else None))
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
        cr = self.con.execute("SELECT cash_amount FROM credits WHERE request_id=?", (rid,)).fetchone()
        cash_amt = cr["cash_amount"] if cr else 0
        if cash_amt:    # 분할안: 크레딧은 끝났고 이연 현금분이 남았다
            self.move(rid, actor, "CASH_PENDING", "Masil 발주서 크레딧 차감 확인 · 이연 현금분 지급 대기",
                      {"po": po, "deferred_cash": cash_amt})
            self.notify(rid, ["재무팀"], f"{r['code']} · 크레딧 차감 완료({po}). 이연 현금 {cash_amt:,}원은 "
                        "이 그룹 연체 채권 회수 확인 후 지급하세요", "warn")
            self.notify(rid, ["영업팀"], f"{r['code']} · 크레딧 차감 완료 ({po}), 현금분 {cash_amt:,}원 지급 대기")
        else:
            self.move(rid, actor, "CREDIT_SETTLED", "Masil 발주서 크레딧 차감 확인", {"po": po})
            self.notify(rid, ["영업팀", "재무팀"], f"{r['code']} · 크레딧 정산 완료 ({po})", "ok")
        self.con.commit()

    def pay_deferred_cash(self, rid, actor, ref, memo=""):
        """분할안의 이연 현금분 지급. 재무팀이 이 그룹 연체 채권 회수를 확인하고 지급 전표 번호를 남긴다."""
        r = self.get(rid)
        self._need(r, "CASH_PENDING", "재무팀", actor)
        if not ref.strip():
            raise WorkflowError("지급 전표(또는 이체) 번호를 입력하세요.")
        cr = self.con.execute("SELECT cash_amount FROM credits WHERE request_id=?", (rid,)).fetchone()
        self.con.execute("UPDATE credits SET cash_status='PAID', cash_ref=?, cash_memo=?, cash_paid_at=? WHERE request_id=?",
                         (ref.strip(), memo, now(), rid))
        self.move(rid, actor, "CREDIT_SETTLED", "연체 회수 확인 · 이연 현금분 지급 · 분할 정산 완료",
                  {"cash": cr["cash_amount"], "ref": ref.strip(), "memo": memo})
        self.notify(rid, ["영업팀", "재무팀"], f"{r['code']} · 이연 현금 {cr['cash_amount']:,}원 지급, 분할 정산 완료 ({ref.strip()})", "ok")
        self.con.commit()
