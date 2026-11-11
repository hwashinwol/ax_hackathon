"""운영 중 AI 에이전트 (Claude).

워크플로우의 세 지점에서 Claude가 도구로 원천 데이터를 직접 조회하고, 근거를 종합해 사람에게 줄 판단 자료를 만든다.

  review   결재 대기 진입 시 — 결재자용 권고안
  explain  영업팀 소명 제출 시 — 소명이 보류 사유를 풀기에 충분한지 검토
  alt      현금 지급 거절 시 — 대체 지급안 설명과 품목 제외 판단 근거

숫자와 상태 전이는 engine.py·workflow.py의 규칙이 정한다. 에이전트는 조회·해석·설명을 맡고 결정은 사람이 한다.
API 자격 증명이 없거나 호출이 실패하면 None을 돌려주고, 호출한 쪽이 규칙 기반 문구로 대신한다.
"""
import json
import os

import engine
from config import AI_EFFORT, AI_ENABLED, AI_MODEL, RULES

SYSTEM = f"""당신은 Corevia Cosmetics의 판매장려금 결재 시스템에서 일하는 검토 에이전트입니다.
영업팀이 올린 장려금 요청 한 건을 맡아, 결재자(재무팀·내부감시팀·영업임원)가 판단할 수 있도록 근거를 모아 정리합니다.

이 회사의 규칙입니다.
- 동일 관련자: 대표자·주소·정산계좌 중 {RULES['related_min_match']}개 이상 일치하면 동일 관련자이고, 관계는 전이적으로 연결합니다.
  정산계좌는 마스터 등록 계좌와 실제 입금 계좌를 함께 봅니다. 등록 정보가 겹치는 것은 소명·감사 대상이지 부정 확정이 아닙니다.
- 지급 한도: 거래처별 월 지급액 {RULES['limit_monthly']:,}원 이하는 영업지원팀 전결, 초과는 영업임원 승인입니다. 동일 관련자는 합산해 한도를 적용합니다.
- 품질: 외부 보도는 내부에서 확정된 결함(defect_confirmed)일 때만 근거로 씁니다. 미확정 보도는 참고만 합니다.

일하는 방식입니다.
- 답하기 전에 도구로 필요한 데이터를 직접 조회하세요. 서로 독립적인 조회는 한 번에 함께 호출하면 됩니다.
- 조회한 값만 근거로 쓰고, 금액과 건수는 조회 결과 그대로 인용하세요. 데이터에 없는 것은 없다고 쓰세요.
- 당신은 승인하거나 거절하지 않습니다. 권고와 근거를 내고, 결정은 결재자가 합니다.
- 읽는 사람은 바쁜 실무자입니다. 한국어로, 짧고 구체적인 문장으로 쓰세요."""

EMPTY = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}

TOOL_DEFS = [
    ("get_request", "이 요청의 기본 정보: 기준 거래처, 정산 기간, 요청 사유, 현재 상태, 확정된 그룹 구성원.", EMPTY),
    ("get_related_parties", "기준 거래처의 동일 관련자 판정 결과. 후보별로 대표자·주소·정산계좌 일치 여부와 근거(입금 내역 id 포함), 이름만 같은 대조군.", EMPTY),
    ("get_limit_check", "그룹 합산 장려금과 월 한도 비교, 거래처별 금액, 요율과 일반 대리점 요율 비교, 과거 한도 초과 지급 내역.", EMPTY),
    ("get_risk_checks", "리스크 검사 결과: 장려금 대상 여부, 채권 연체(경고), 악성 클레임 판정, 확정 결함 lot, 외부 보도, 판촉 중복. 보류 사유 목록 포함.", EMPTY),
    ("get_corevia_finance", "Corevia 자체 현금 근거: 최근 월말 현금 추이, 연속 감소 개월 수, 전체 미회수 채권과 연체분.", EMPTY),
    ("get_masil_position", "유통 자회사 Masil의 현금·약정 하한·재전망, 회수율 급락 채널, Corevia 앞 발주 내역. 대체 지급안을 설계할 때 참고합니다.", EMPTY),
    ("get_credit_alternative", "현금 대신 Masil 대리점 채널 크레딧으로 지급하는 안의 계산값: 금액, 적용 품목과 제외 품목, 제외 판단 근거, 발주 대금 차감 정산.", EMPTY),
    ("get_explanation_thread", "보류 중인 요청의 소명 스레드(시스템 메시지와 영업팀·품질팀 소명).", EMPTY),
    ("read_document", "정책·회의록·통보 문서 원문을 읽습니다.", {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "문서 파일명. list_documents 결과의 이름을 그대로 씁니다."}},
        "required": ["name"], "additionalProperties": False}),
    ("list_documents", "읽을 수 있는 문서 파일 목록.", EMPTY),
]
TOOLS = [{"name": n, "description": d, "input_schema": s, "strict": True} for n, d, s in TOOL_DEFS]

SCHEMAS = {
    "review": {
        "type": "object",
        "properties": {
            "recommendation": {"type": "string", "enum": ["approve", "conditional", "hold"],
                               "description": "approve=승인 권고, conditional=조건부(결재자 확인 필요 사항 있음), hold=결재 전에 풀어야 할 문제가 있음"},
            "title": {"type": "string", "description": "권고를 한 줄로"},
            "summary": {"type": "string", "description": "결재자가 먼저 읽을 2~3문장"},
            "reasons": {"type": "array", "items": {"type": "string"}, "description": "근거. 항목마다 수치와 출처 파일을 넣습니다"},
            "ask_sales": {"type": "array", "items": {"type": "string"}, "description": "영업팀에 소명을 요청할 질문. 없으면 빈 배열"},
        },
        "required": ["recommendation", "title", "summary", "reasons", "ask_sales"], "additionalProperties": False},
    "explain": {
        "type": "object",
        "properties": {
            "sufficient": {"type": "boolean", "description": "소명이 보류 사유를 풀 만큼 구체적인가"},
            "assessment": {"type": "string", "description": "담당 부서가 읽을 2~3문장 평가"},
            "missing": {"type": "array", "items": {"type": "string"}, "description": "더 받아야 할 증빙이나 답변. 없으면 빈 배열"},
        },
        "required": ["sufficient", "assessment", "missing"], "additionalProperties": False},
    "alt": {
        "type": "object",
        "properties": {
            "proposal": {"type": "string", "description": "영업팀과 재무팀에 보여 줄 대체 지급안 설명 2~4문장"},
            "exclusion_rationale": {"type": "string", "description": "일부 품목을 크레딧 대상에서 뺀 판단 근거. 감사 로그에 남습니다"},
            "risks": {"type": "array", "items": {"type": "string"}, "description": "이 안을 수락하기 전에 확인할 점"},
        },
        "required": ["proposal", "exclusion_rationale", "risks"], "additionalProperties": False},
}

TASKS = {
    "review": "이 요청이 결재 대기로 넘어왔습니다. 관련자 판정, 합산 한도, 리스크, Corevia 자체 현금 근거를 조회해 결재자용 권고안을 쓰세요. "
              "현금으로 지급해도 되는지 판단할 때는 Corevia 자체 사정(현금 추이, 이 그룹에서 받을 돈이 밀려 있는지)을 근거로 삼으세요.",
    "explain": "이 요청은 보류 상태이고 방금 소명이 제출됐습니다. 보류 사유와 소명 스레드를 조회해, 소명이 그 사유를 풀기에 충분한지 평가하세요. "
               "해제 여부는 담당 부서가 정합니다.",
    "alt": "재무팀이 현금 지급을 거절했습니다(사유 CASH_SHORTAGE). 크레딧 대체안의 계산값과 품질·발주 데이터를 조회해, "
           "영업팀과 재무팀이 수락 여부를 판단할 수 있도록 안을 설명하고 품목 제외 판단 근거를 쓰세요.",
}


def available():
    """LLM을 부를 수 있는가. 꺼 두었거나 자격 증명이 없으면 False."""
    if not AI_ENABLED:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _client():
    import anthropic
    # 워크스페이스에 묶이지 않은 키는 요청마다 워크스페이스 ID 헤더가 있어야 한다 (service/.env 의 ANTHROPIC_WORKSPACE_ID)
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    return anthropic.Anthropic(timeout=180.0, max_retries=1,
                               default_headers={"anthropic-workspace-id": ws} if ws else None)


class Toolbox:
    """에이전트 도구의 구현. 요청 한 건에 묶여 있어 인자 없이 호출한다."""

    def __init__(self, wf, rid):
        self.wf, self.rid, self.d = wf, rid, wf.d

    def _r(self):
        return self.wf.get(self.rid)

    def get_request(self):
        r = self._r()
        return {"code": r["code"], "anchor_id": r["anchor_id"], "period": r["period"], "memo": r["memo"],
                "status": r["status"], "members": self.wf.members(r) if r["match"] else [r["anchor_id"]]}

    def get_related_parties(self):
        r = self._r()
        m = r["match"] or engine.match(self.d, r["anchor_id"], r["extra_ids"] or [])
        return {"rule": f"대표자·주소·정산계좌 중 {m['need']}개 이상 일치, 전이 연결", "anchor": m["anchor"],
                "candidates": [{k: c[k] for k in ("agency_id", "agency_name", "score", "max", "decision", "via", "proposed_by_sales")}
                               | {"signals": [{"item": s[0], "evidence": s[1], "match": bool(s[3])} for s in c["signals"]]}
                               for c in m["candidates"]],
                "same_name_controls": m["controls"]}

    def get_limit_check(self):
        r = self._r()
        a = r["agg"] or engine.aggregate(self.d, self.wf.members(r), r["period"])
        keys = ("period", "amount", "limit", "limit_total", "over_limit", "monthly", "max_single", "file_rows", "file_over_single",
                "total_sales", "rate", "std_mean", "std_min", "std_max", "std_n", "std_amount", "diff",
                "prior_paid", "prior_approver", "prior_over", "prior_over_amount", "prior_excess")
        return {k: a[k] for k in keys} | {"members": [{k: m[k] for k in ("agency_id", "agency_name", "sales", "rebate", "rebate_ids")}
                                                      for m in a["members"]],
                                         "source": "sales_rebate_payment_cv, approval_authority_policy"}

    def get_risk_checks(self):
        r = self._r()
        if r["risk"]:
            return r["risk"]
        agg = r["agg"] or engine.aggregate(self.d, self.wf.members(r), r["period"])
        return engine.risk(self.d, self.wf.members(r), r["period"], agg, r["resolutions"], self.wf.judgments())

    def get_corevia_finance(self):
        c = engine.cash(self.d)
        return {k: c.get(k) for k in ("cv_close", "cv_month_net", "cv_series", "cv_decline_months", "cv_decline_amount",
                                      "cv_ar_total", "cv_ar_over", "cv_ar_over_n", "cv_ar_over_agency")} \
            | {"as_of": self.d.as_of, "overdue_days": RULES["overdue_days"], "source": "fin_cash_daily_cv, fin_ar_aging_cv"}

    def get_masil_position(self):
        c = engine.cash(self.d)
        return {k: c.get(k) for k in ("ms_close", "covenant", "headroom", "ms_series", "ms_avg_change", "reforecast",
                                      "collection_drops", "covenant_note", "lc_note")} | {"as_of": self.d.as_of}

    def get_credit_alternative(self):
        r = self._r()
        if r["alt"]:
            return r["alt"]
        agg = r["agg"] or engine.aggregate(self.d, self.wf.members(r), r["period"])
        return engine.alternative(self.d, agg["amount"], self.wf.members(r), engine.cash(self.d))

    def get_explanation_thread(self):
        rows = self.wf.con.execute("SELECT role, kind, body, at FROM threads WHERE request_id=? ORDER BY id", (self.rid,))
        return [dict(x) for x in rows]

    def list_documents(self):
        return sorted(self.d.docs)

    def read_document(self, name):
        if name not in self.d.docs:
            raise KeyError(f"문서 '{name}'가 없습니다. 읽을 수 있는 문서: {', '.join(sorted(self.d.docs))}")
        return self.d.docs[name]


def run(wf, rid, task, client=None, max_turns=8):
    """에이전트를 한 번 돌린다. 반환: {"output": dict, "trace": [...], "model": str, "usage": {...}} 또는 None(LLM 미사용)."""
    if client is None:
        if not available():
            return None
        client = _client()
    box = Toolbox(wf, rid)
    messages = [{"role": "user", "content": TASKS[task]}]
    trace, usage = [], {"input_tokens": 0, "output_tokens": 0}
    for _ in range(max_turns):
        # 서버 측 거절 대체(fallbacks)를 켜 둔다: 안전 분류기가 요청을 거절하면 같은 호출 안에서 대체 모델이 이어 받는다.
        resp = client.beta.messages.create(
            model=AI_MODEL, max_tokens=16000, system=SYSTEM, tools=TOOLS, messages=messages,
            output_config={"effort": AI_EFFORT, "format": {"type": "json_schema", "schema": SCHEMAS[task]}},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        usage["input_tokens"] += resp.usage.input_tokens
        usage["output_tokens"] += resp.usage.output_tokens
        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for b in resp.content:
                if b.type != "tool_use":
                    continue
                args = b.input if isinstance(b.input, dict) else {}
                try:
                    out = getattr(box, b.name)(**args)
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(out, ensure_ascii=False, default=str)})
                    trace.append({"tool": b.name, "args": args, "ok": True})
                except Exception as e:  # 도구 오류는 모델에게 돌려줘 스스로 고치게 한다
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"오류: {e}", "is_error": True})
                    trace.append({"tool": b.name, "args": args, "ok": False, "error": str(e)})
            messages.append({"role": "user", "content": results})   # 결과는 한 메시지에 모아 돌려준다
            continue
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        if resp.stop_reason != "end_turn":      # refusal, max_tokens 등: 결과를 믿지 않는다
            raise RuntimeError(f"에이전트가 정상 종료하지 않음: stop_reason={resp.stop_reason}")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return {"output": json.loads(text), "trace": trace, "model": resp.model, "usage": usage}
    raise RuntimeError(f"에이전트가 {max_turns}턴 안에 끝나지 않음")


def safe_run(wf, rid, task, client=None):
    """run()을 감싸 실패해도 워크플로우가 멈추지 않게 한다. 반환: (결과 또는 None, 오류 문자열 또는 None)"""
    try:
        return run(wf, rid, task, client), None
    except Exception as e:
        try:
            import anthropic
        except ImportError:
            return None, "anthropic 패키지가 설치되지 않음 (pip install -r requirements.txt)"
        if isinstance(e, anthropic.AuthenticationError):
            return None, "API 인증 실패: 키를 확인하세요"
        if isinstance(e, anthropic.RateLimitError):
            return None, "API 호출 한도 초과"
        if isinstance(e, anthropic.APIStatusError):
            return None, f"API 오류 {e.status_code}: {e.message}"
        if isinstance(e, anthropic.APIConnectionError):
            return None, "API 연결 실패"
        return None, f"{type(e).__name__}: {e}"
