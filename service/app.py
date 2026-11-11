"""Corevia 관련 거래처 장려금 결재 서비스 (Flask)."""
import csv
import io
import os

from flask import Flask, Response, flash, g, redirect, render_template, request, url_for

import agent
import engine
from config import AI_MODEL, APPROVAL_CHAIN, DATA_DIR, DB_PATH, REJECT_REASONS, ROLES, RULES, RULE_SOURCES
from data import Data
from workflow import Workflow, WorkflowError, connect, L

STATUS_STYLE = {
    "REQUESTED": "neutral", "MATCHING": "info", "MANUAL_REVIEW": "warn", "WITHIN_LIMIT": "neutral",
    "RISK_CHECK": "info", "PENDING": "warn", "AWAITING_APPROVAL": "info", "PAID": "ok", "REJECTED": "crit",
    "ALT_PROPOSED": "info", "CREDIT_SCHEDULED": "info", "CASH_PENDING": "warn", "CREDIT_SETTLED": "ok", "CLOSED": "neutral",
}
STEPS = ["REQUESTED", "MATCHING", "RISK_CHECK", "AWAITING_APPROVAL", "처리"]


def create_app(data_dir=DATA_DIR, db_path=DB_PATH, ai_client=None):
    app = Flask(__name__)
    app.secret_key = os.environ.get("CV_SECRET", "corevia-demo")
    app.config["DATA"] = Data(data_dir)
    app.config["DB_PATH"] = db_path
    app.config["AI_CLIENT"] = ai_client     # 테스트용 주입. 평소에는 None(환경의 자격 증명 사용)

    @app.template_filter("won")
    def won(n):
        return f"{int(n):,}원" if n is not None else "-"

    @app.template_filter("man")
    def man(n):
        return f"{n / 1e4:,.1f}만" if n is not None else "-"

    @app.template_filter("eok")
    def eok(n):
        return f"{n / 1e8:,.1f}억" if n is not None else "-"

    @app.template_filter("pct")
    def pct(x, digits=1):
        return f"{x * 100:.{digits}f}%"

    def wf():
        if "wf" not in g:
            g.con = connect(app.config["DB_PATH"])
            g.wf = Workflow(app.config["DATA"], g.con, app.config["AI_CLIENT"])
        return g.wf

    @app.teardown_appcontext
    def close(_):
        con = g.pop("con", None)
        if con:
            con.close()

    def role():
        r = request.cookies.get("role", "영업팀")
        return r if r in ROLES else "영업팀"

    @app.context_processor
    def ctx():
        w = wf()
        unread = w.con.execute("SELECT COUNT(*) FROM notices WHERE to_role=? AND read=0", (role(),)).fetchone()[0]
        return {"role": role(), "roles": ROLES, "unread": unread, "S": STATUS_STYLE, "as_of": app.config["DATA"].as_of,
                "RULES": RULES, "SRC": RULE_SOURCES, "ai_model": AI_MODEL,
                "ai_on": bool(app.config["AI_CLIENT"]) or agent.available()}

    def act(fn, rid, *args):
        try:
            fn(rid, role(), *args)
        except WorkflowError as e:
            wf().con.rollback()
            flash(str(e), "crit")
        return redirect(url_for("detail", rid=rid))

    # ── 화면 ──
    @app.post("/role")
    def set_role():
        resp = redirect(request.form.get("next") or url_for("inbox"))
        if request.form.get("role") in ROLES:
            resp.set_cookie("role", request.form["role"], samesite="Lax")
        return resp

    @app.get("/")
    def inbox():
        w, d = wf(), app.config["DATA"]
        f = request.args.get("status", "")
        q = "SELECT * FROM requests" + (" WHERE status=?" if f else "") + " ORDER BY id DESC"
        rows = [dict(r) for r in w.con.execute(q, (f,) if f else ())]
        for r in rows:
            agg = L(r["agg_json"])
            r["amount"] = agg["amount"] if agg else None
            m = L(r["match_json"])
            r["n_members"] = len(w.members(w.get(r["id"]))) if m else 1
        counts = dict(w.con.execute("SELECT status, COUNT(*) FROM requests GROUP BY status").fetchall())
        rb = d.t("sales_rebate_payment_cv.csv")
        src = rb.approval_status.value_counts().to_dict()
        last = rb.period_ym.max()
        pending = rb[(rb.period_ym == last) & (rb.approval_status == "미지급")]
        return render_template("inbox.html", rows=rows, counts=counts, f=f, src=src, n_src=len(rb), last=last,
                               pending_n=len(pending), pending_sum=int(pending.rebate_amount_krw.sum()),
                               approvers=rb[rb.approver != "NONE"].approver.value_counts().to_dict())

    @app.route("/new", methods=["GET", "POST"])
    def new():
        d = app.config["DATA"]
        if request.method == "POST":
            try:
                rid = wf().create(role(), request.form["anchor_id"], request.form["period"],
                                  [x.strip() for x in request.form.get("extra_ids", "").split(",")],
                                  request.form.get("memo", ""))
                return redirect(url_for("detail", rid=rid))
            except WorkflowError as e:
                flash(str(e), "crit")
        ag = d.t("m_agency_cv.csv").to_dict("records")
        months, quarters = d.periods()
        return render_template("new.html", agencies=ag, months=months, quarters=quarters)

    @app.get("/requests/<int:rid>")
    def detail(rid):
        w = wf()
        try:
            r = w.get(rid)
        except WorkflowError:
            return redirect(url_for("inbox"))
        members = w.members(r) if r["match"] else [r["anchor_id"]]
        approvals = [dict(a) for a in w.con.execute("SELECT * FROM approvals WHERE request_id=? ORDER BY id", (rid,))]
        threads = [dict(t) for t in w.con.execute("SELECT * FROM threads WHERE request_id=? ORDER BY id", (rid,))]
        credit = w.con.execute("SELECT * FROM credits WHERE request_id=?", (rid,)).fetchone()
        audit = [dict(a) for a in w.con.execute("SELECT * FROM audit_log WHERE request_id=? ORDER BY id DESC", (rid,))]
        rec = None
        if r["status"] == "AWAITING_APPROVAL" and r["agg"] and r["risk"]:
            ai = r["ai"].get("review")
            if ai:      # Claude가 쓴 권고
                rec = {"title": ai["title"], "text": ai["summary"], "reasons": ai["reasons"], "ask_sales": ai["ask_sales"],
                       "source": f"Claude · {ai['model']}", "tools": ai.get("tools", [])}
            else:       # LLM 미연결 시 규칙 기반 문구
                rec = engine.recommend(r["match"], members, r["agg"], r["risk"], r["cash"])
                rec["source"] = "규칙 기반 (AI 에이전트 미연결)"
        return render_template("detail.html", r=r, members=members, approvals=approvals, threads=threads, rec=rec,
                               credit=dict(credit) if credit else None, audit=audit,
                               next_approver=w.next_approver(rid), chain=APPROVAL_CHAIN,
                               reasons=REJECT_REASONS, anchor=app.config["DATA"].agency(r["anchor_id"]))

    # ── 액션 ──
    @app.post("/requests/<int:rid>/match")
    def do_match(rid):
        return act(wf().run_matching, rid)

    @app.post("/requests/<int:rid>/review")
    def do_review(rid):
        return act(wf().review, rid, request.form["agency_id"], request.form["decision"], request.form.get("memo", ""))

    @app.post("/requests/<int:rid>/evaluate")
    def do_eval(rid):
        return act(wf().evaluate, rid)

    @app.post("/requests/<int:rid>/explain")
    def do_explain(rid):
        return act(wf().explain, rid, request.form.get("body", ""))

    @app.post("/requests/<int:rid>/resolve")
    def do_resolve(rid):
        return act(wf().resolve, rid, request.form["key"], request.form["kind"], request.form.get("memo", ""))

    @app.post("/requests/<int:rid>/dismiss")
    def do_dismiss(rid):
        return act(wf().dismiss, rid, request.form.get("memo", ""))

    @app.post("/requests/<int:rid>/approve")
    def do_approve(rid):
        return act(wf().approve, rid, request.form.get("memo", ""))

    @app.post("/requests/<int:rid>/reject")
    def do_reject(rid):
        return act(wf().reject, rid, request.form.get("reason", ""), request.form.get("memo", ""))

    @app.post("/requests/<int:rid>/alt/plan")
    def do_alt_plan(rid):
        return act(wf().alt_plan, rid, request.form.get("plan", "full"))

    @app.post("/requests/<int:rid>/alt/accept")
    def do_alt_accept(rid):
        return act(wf().alt_accept, rid)

    @app.post("/requests/<int:rid>/alt/decline")
    def do_alt_decline(rid):
        return act(wf().alt_decline, rid, request.form.get("memo", ""))

    @app.post("/requests/<int:rid>/credit/settle")
    def do_settle(rid):
        return act(wf().settle_credit, rid, request.form.get("po", ""))

    @app.post("/requests/<int:rid>/credit/cash")
    def do_pay_cash(rid):
        return act(wf().pay_deferred_cash, rid, request.form.get("ref", ""), request.form.get("memo", ""))

    # 명세 시나리오 2의 예시 클레임 3건. 원천 데이터에 없는 값이라 '시연용 입력'으로 표시해 넣는다.
    DEMO_CLAIMS = [("2026-02-10", "포장 파손"), ("2026-04-03", "성분 이상"), ("2026-05-21", "변질 주장")]

    @app.route("/quality", methods=["GET", "POST"])
    def quality():
        w, d = wf(), app.config["DATA"]
        if request.method == "POST":
            try:
                aid = request.form.get("agency_id", "")
                if request.form.get("demo"):
                    for day, kind in DEMO_CLAIMS:
                        w.add_judgment(role(), aid, day, kind, "명세 시나리오 2 예시", "시연용 입력")
                    flash(f"{aid}에 시연용 '악의적' 판정 {len(DEMO_CLAIMS)}건을 등록했습니다.", "ok")
                else:
                    w.add_judgment(role(), aid, request.form.get("claim_date", ""), request.form.get("claim_type", ""),
                                   request.form.get("memo", ""))
                    flash(f"{aid} 클레임 판정을 등록했습니다.", "ok")
            except WorkflowError as e:
                w.con.rollback()
                flash(str(e), "crit")
            return redirect(url_for("quality"))
        qc = d.t("qc_claims_cv.csv")
        return render_template("quality.html", rows=w.judgments(), agencies=d.t("m_agency_cv.csv").to_dict("records"),
                               qc_n=len(qc), qc_customers=sorted(qc.customer_id.unique()) if len(qc) else [],
                               has_field="internal_judgment" in qc.columns)

    @app.get("/notices")
    def notices():
        w = wf()
        rows = [dict(n) for n in w.con.execute(
            "SELECT n.*, r.code FROM notices n LEFT JOIN requests r ON r.id=n.request_id WHERE to_role=? ORDER BY n.id DESC", (role(),))]
        w.con.execute("UPDATE notices SET read=1 WHERE to_role=?", (role(),))
        w.con.commit()
        return render_template("notices.html", rows=rows)

    @app.get("/audit")
    def audit():
        rows = [dict(a) for a in wf().con.execute(
            "SELECT a.*, r.code FROM audit_log a LEFT JOIN requests r ON r.id=a.request_id ORDER BY a.id DESC")]
        if request.args.get("format") == "csv":
            buf = io.StringIO()
            wr = csv.writer(buf)
            wr.writerow(["event_id", "request", "at", "actor", "action", "before", "after", "payload"])
            for a in reversed(rows):
                wr.writerow([a["id"], a["code"], a["at"], a["actor"], a["action"], a["before"], a["after"], a["payload"]])
            return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                            headers={"Content-Disposition": "attachment; filename=audit_log.csv"})
        return render_template("audit.html", rows=rows)

    @app.get("/data")
    def data_page():
        d = app.config["DATA"]
        inv, extra = d.inventory()
        return render_template("data.html", inv=inv, extra=extra, root=str(d.root), cash=engine.cash(d))

    @app.post("/reset")
    def reset():
        con = g.pop("con", None)
        if con:
            con.close()
        g.pop("wf", None)
        p = app.config["DB_PATH"]
        if p.exists():
            p.unlink()
        flash("시연 데이터(요청·결재·로그)를 초기화했습니다. 원천 데이터는 그대로입니다.", "ok")
        return redirect(url_for("inbox"))

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5080))
    create_app().run(host="127.0.0.1", port=port, debug=False)
