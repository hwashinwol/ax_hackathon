"""3대 시나리오를 실제 hackathon_data로 끝까지 돌려 보는 통합 테스트.

실행: python -m unittest discover -s service/tests  (ax_hackathon 폴더에서)
실제 LLM은 부르지 않는다. 에이전트 루프는 가짜 클라이언트로 검증한다.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

os.environ["CV_AI"] = "off"     # 테스트 중에는 환경에 키가 있어도 실제 API를 부르지 않는다
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine  # noqa: E402
from app import create_app  # noqa: E402


class FakeClaude:
    """Messages API 흉내: 첫 턴에 도구를 부르고, 도구 결과를 받으면 요청된 스키마에 맞는 JSON으로 끝낸다."""

    OUT = {
        "recommendation": {"recommendation": "conditional", "title": "가짜 권고 제목", "summary": "가짜 요약입니다.",
                           "reasons": ["합산 3,888,097원 (sales_rebate_payment_cv)"], "ask_sales": ["우대 요율 근거는?"]},
        "sufficient": {"sufficient": False, "assessment": "가짜 소명 평가입니다.", "missing": ["재발 방지 계획"]},
        "proposal": {"proposal": "가짜 대안 설명입니다.", "exclusion_rationale": "가짜 제외 근거입니다.", "risks": ["양사 합의"]},
    }

    def __init__(self):
        self.calls = []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        usage = NS(input_tokens=10, output_tokens=5)
        if len(kw["messages"]) == 1:        # 첫 턴: 도구 3개를 한꺼번에 호출 (하나는 일부러 틀린 문서 이름)
            blocks = [NS(type="tool_use", id="t1", name="get_limit_check", input={}),
                      NS(type="tool_use", id="t2", name="get_corevia_finance", input={}),
                      NS(type="tool_use", id="t3", name="read_document", input={"name": "없는문서.md"})]
            return NS(stop_reason="tool_use", content=blocks, usage=usage, model="fake-model")
        key = next(k for k in self.OUT if k in kw["output_config"]["format"]["schema"]["properties"])
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(self.OUT[key], ensure_ascii=False))],
                  usage=usage, model="fake-model")


class Base(unittest.TestCase):
    ai_client = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.app = create_app(db_path=self.db, ai_client=self.ai_client)
        self.c = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def as_(self, role):
        self.c.set_cookie("role", role)

    def post(self, role, path, **form):
        self.as_(role)
        r = self.c.post(path, data=form, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def page(self, role, path):
        self.as_(role)
        return self.c.get(path).get_data(as_text=True)

    def status(self, rid=1):
        con = sqlite3.connect(self.db)
        s = con.execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()[0]
        con.close()
        return s

    def audit(self):
        con = sqlite3.connect(self.db)
        rows = con.execute("SELECT action, payload FROM audit_log ORDER BY id").fetchall()
        con.close()
        return rows

    def start(self):
        """요청 생성 → 매칭 → 합산 한도·리스크 검사까지."""
        self.post("영업팀", "/new", anchor_id="D-011", period="2026-06", extra_ids="", memo="합산 요청")
        self.assertEqual(self.status(), "REQUESTED")
        html = self.post("영업팀", "/requests/1/match")
        self.assertIn("D-024", html)
        self.assertIn("D-037", html)
        self.assertEqual(self.status(), "MATCHING")                 # 둘 다 2개 이상 일치 → 수동 검토 없음
        return self.post("영업팀", "/requests/1/evaluate")


class Flow(Base):
    def test_scenario1_normal(self):
        """명세 시나리오 1: 관련자 합산 → 보류 없이 결재 → 지급."""
        html = self.start()
        self.assertEqual(self.status(), "AWAITING_APPROVAL")        # 연체는 보류가 아니라 경고
        self.assertIn("3,888,097원", html)                          # 합산 장려금
        self.assertIn("2,000,000원", html)                          # 월 한도
        self.assertIn("22,238,349원", html)                         # 1~5월 한도 초과 지급분
        self.assertIn("설계 단계", html)                              # 환수·재승인은 설계 단계
        self.assertIn("미회수 채권 29,202,840원", html)                # 결재 화면 경고
        html = self.post("영업임원", "/requests/1/approve")
        self.assertIn("재무팀 결재 차례", html)                       # 순서 강제
        self.post("재무팀", "/requests/1/approve")
        self.post("내부감시팀", "/requests/1/approve")
        self.assertEqual(self.status(), "AWAITING_APPROVAL")        # 임원 승인 전에는 지급하지 않음
        self.post("영업임원", "/requests/1/approve")
        self.assertEqual(self.status(), "PAID")

    def hold_by_claims(self):
        html = self.post("영업팀", "/quality", agency_id="D-024", demo="1")
        self.assertIn("품질팀만", html)                               # 권한 없음
        html = self.post("품질팀", "/quality", agency_id="D-024", claim_date="2026-06-01", claim_type="")
        self.assertIn("클레임 유형", html)
        html = self.post("품질팀", "/quality", agency_id="D-024", demo="1")
        self.assertIn("QJ-0003", html)
        html = self.start()
        self.assertEqual(self.status(), "PENDING")
        self.assertIn("포장 파손", html)

    def test_scenario2_malicious_claims_hold(self):
        """명세 시나리오 2: '악의적' 판정 3건이면 결재 전에 보류, 영업 소명 뒤 품질팀이 해제해야 재개."""
        self.hold_by_claims()
        self.assertIn("지급 보류", self.page("품질팀", "/notices"))     # 품질팀에 경고
        self.assertIn("지급 보류", self.page("영업팀", "/notices"))     # 영업팀에 경고
        html = self.post("품질팀", "/requests/1/resolve", key="malicious", kind="released")
        self.assertIn("영업팀 소명이 먼저", html)                      # 소명 없이 해제 불가
        self.post("영업팀", "/requests/1/explain", body="클레임 경위 소명")
        self.post("재무팀", "/requests/1/resolve", key="malicious", kind="released")   # 권한 없음
        self.assertEqual(self.status(), "PENDING")
        html = self.post("재무팀", "/requests/1/resolve", key="overdue", kind="collected")
        self.assertIn("해제할 수 있는 보류 사유가 아닙니다", html)      # 연체는 더 이상 보류 사유가 아님
        self.post("품질팀", "/requests/1/resolve", key="malicious", kind="released", memo="재발 방지 확인")
        self.assertEqual(self.status(), "AWAITING_APPROVAL")

    def test_scenario2_dismiss(self):
        self.hold_by_claims()
        self.post("품질팀", "/requests/1/dismiss", memo="기각")
        self.assertEqual(self.status(), "CLOSED")

    def test_scenario3_cash_reject_credit(self):
        """명세 시나리오 3: 재무팀이 Corevia 자체 근거로 현금 지급 거절 → 크레딧 대안 → 양측 수락."""
        html = self.start()
        self.assertIn("AI 권고", html)
        self.assertIn("규칙 기반 (AI 에이전트 미연결)", html)
        self.assertIn("Corevia 자체 현금 근거", html)
        self.assertIn("3개월 연속 감소", html)                        # 218.5억 → 190.8억
        html = self.post("재무팀", "/requests/1/reject", reason="", memo="")
        self.assertIn("사유 코드", html)
        html = self.post("재무팀", "/requests/1/reject", reason="CASH_SHORTAGE", memo="현금 3개월 연속 감소, 그룹 연체 2,920만 원")
        self.assertEqual(self.status(), "ALT_PROPOSED")
        self.assertIn("대리점 정산채널", html)
        self.assertIn("SKU-1001", html)                                # 제외 품목으로 표시
        self.assertIn("제외 판단 근거", html)
        self.assertIn("LOT-B-2602-P", html)
        self.assertIn("3,749,760,000원", html)                         # Masil 발주 14건
        self.assertIn("1,827,840,000원", html)                         # 더마 4건을 뺀 차감 대상
        basis = [json.loads(p) for a, p in self.audit() if a.startswith("AI 대안 제안")][0]["exclusion_basis"]
        self.assertEqual(basis["excluded"][0]["product_id"], "PL-02")  # 판단 근거가 감사 로그에 남음
        self.assertEqual(basis["excluded"][0]["claims"], 45)
        self.post("영업팀", "/requests/1/alt/accept")
        self.post("재무팀", "/requests/1/alt/accept")
        self.assertEqual(self.status(), "CREDIT_SCHEDULED")
        self.assertIn("SPO-0005 · SKU-2001", html := self.page("Masil", "/requests/1"))   # 발주 내역에서 고르는 목록
        self.assertNotIn('value="SPO-0001"', html)                     # 확정 결함 품목 발주서는 목록에 없음
        html = self.post("Masil", "/requests/1/credit/settle", po="PO-없는번호")
        self.assertIn("발주 내역(sc_purchase_orders_ms)에 없는", html)
        html = self.post("Masil", "/requests/1/credit/settle", po="SPO-0001")
        self.assertIn("확정 결함 품목 발주서", html)
        self.assertEqual(self.status(), "CREDIT_SCHEDULED")
        html = self.post("Masil", "/requests/1/credit/settle", po="SPO-0005")
        self.assertEqual(self.status(), "CREDIT_SETTLED")
        self.assertIn("발주 내역 대조 완료", html)
        self.assertIn("197,711,903원", html)                           # 201,600,000 − 3,888,097
        settled = [json.loads(p) for a, p in self.audit() if a.startswith("Masil 발주서 크레딧 차감 확인")][0]
        self.assertTrue(settled["verified"])
        self.assertEqual(settled["payable_after"], 197711903)

    def test_scenario3_split_deferred_cash(self):
        """분할안: 크레딧 차감 확인 → 이연 현금분 지급 대기 → 재무팀 지급으로 정산 완료."""
        self.start()
        self.post("재무팀", "/requests/1/reject", reason="CASH_SHORTAGE")
        self.post("재무팀", "/requests/1/alt/plan", plan="split")
        self.post("영업팀", "/requests/1/alt/accept")
        self.post("재무팀", "/requests/1/alt/accept")
        self.assertEqual(self.status(), "CREDIT_SCHEDULED")
        self.post("Masil", "/requests/1/credit/settle", po="SPO-0005")
        self.assertEqual(self.status(), "CASH_PENDING")
        self.assertIn("이연 현금", self.page("재무팀", "/notices"))
        html = self.post("Masil", "/requests/1/credit/cash", ref="PAY-1")
        self.assertIn("권한이 없습니다", html)
        html = self.post("재무팀", "/requests/1/credit/cash", ref="")
        self.assertIn("전표", html)
        self.assertEqual(self.status(), "CASH_PENDING")
        html = self.post("재무팀", "/requests/1/credit/cash", ref="PAY-2607-0012", memo="연체 회수 확인")
        self.assertEqual(self.status(), "CREDIT_SETTLED")
        self.assertIn("분할 정산 완료", html)
        self.assertIn("PAY-2607-0012", html)
        con = sqlite3.connect(self.db)
        row = con.execute("SELECT amount, cash_amount, cash_status, cash_ref FROM credits").fetchone()
        con.close()
        self.assertEqual(row, (1944049, 1944048, "PAID", "PAY-2607-0012"))

    def test_next_order_basis_is_text(self):
        """대안 화면의 '다음 발주' 근거에 딕셔너리가 그대로 찍히지 않는다."""
        self.start()
        html = self.post("재무팀", "/requests/1/reject", reason="CASH_SHORTAGE")
        self.assertIn("청구일", html)
        self.assertNotIn("{&#39;rule&#39;", html)

    def test_reject_follows_approval_order(self):
        """거절도 재무팀 → 내부감시팀 → 영업임원 순서를 따른다."""
        self.start()
        html = self.post("영업임원", "/requests/1/reject", reason="POLICY")
        self.assertIn("재무팀 결재 차례", html)
        self.assertEqual(self.status(), "AWAITING_APPROVAL")
        self.post("재무팀", "/requests/1/approve")
        html = self.post("재무팀", "/requests/1/reject", reason="POLICY")     # 이미 승인한 결재자는 거절할 수 없음
        self.assertIn("내부감시팀 결재 차례", html)
        self.post("내부감시팀", "/requests/1/reject", reason="MATCH_DOUBT")
        self.assertEqual(self.status(), "CLOSED")

    def test_duplicate_payment_blocked(self):
        """같은 월·같은 관련자 그룹이 이미 지급됐으면 상신 단계에서 막는다."""
        self.start()
        for who in ("재무팀", "내부감시팀", "영업임원"):
            self.post(who, "/requests/1/approve")
        self.assertEqual(self.status(), "PAID")
        self.post("영업팀", "/new", anchor_id="D-024", period="2026-06")      # 기준 거래처만 바꾼 같은 그룹
        self.post("영업팀", "/requests/2/match")
        html = self.post("영업팀", "/requests/2/evaluate")
        self.assertIn("중복 지급 차단", html)
        self.assertEqual(self.status(2), "MATCHING")
        self.post("영업팀", "/new", anchor_id="D-011", period="2026-05")      # 원천 데이터에서 이미 승인(지급)된 달
        self.post("영업팀", "/requests/3/match")
        html = self.post("영업팀", "/requests/3/evaluate")
        self.assertIn("이미 지급(승인)", html)
        self.assertEqual(self.status(3), "MATCHING")

    def test_parallel_requests_only_one_paid(self):
        """같은 그룹·같은 월 요청이 나란히 결재에 올라와도 한 건만 지급된다."""
        self.start()
        self.post("영업팀", "/new", anchor_id="D-037", period="2026-06")
        self.post("영업팀", "/requests/2/match")
        self.post("영업팀", "/requests/2/evaluate")
        for rid in (1, 2):
            for who in ("재무팀", "내부감시팀", "영업임원"):
                html = self.post(who, f"/requests/{rid}/approve")
        self.assertEqual(self.status(1), "PAID")
        self.assertIn("중복 지급 차단", html)
        self.assertEqual(self.status(2), "AWAITING_APPROVAL")

    def test_audit_page_with_judgment(self):
        """요청에 묶이지 않은 감사 기록(품질 판정)이 있어도 감사 로그 화면이 열린다."""
        self.post("품질팀", "/quality", agency_id="D-024", demo="1")
        self.assertEqual(self.c.get("/audit").status_code, 200)

    def test_unrelated_agency_within_limit(self):
        self.post("영업팀", "/new", anchor_id="D-001", period="2026-06")
        self.post("영업팀", "/requests/1/match")
        self.assertEqual(self.status(), "MATCHING")
        self.post("영업팀", "/requests/1/evaluate")
        self.assertEqual(self.status(), "WITHIN_LIMIT")              # 단독 200만 원 이하 → 전결 범위

    def test_same_name_only_needs_manual_review(self):
        """박준호 2곳은 대표자만 같다(1개 일치). 영업이 후보로 넣어도 자동으로 묶지 않는다."""
        self.post("영업팀", "/new", anchor_id="D-034", period="2026-06", extra_ids="D-041")
        self.post("영업팀", "/requests/1/match")
        self.assertEqual(self.status(), "MANUAL_REVIEW")
        self.post("내부감시팀", "/requests/1/review", agency_id="D-041", decision="exclude", memo="주소·계좌 다름")
        self.assertEqual(self.status(), "MATCHING")
        self.post("영업팀", "/requests/1/evaluate")
        self.assertEqual(self.status(), "WITHIN_LIMIT")

    def test_related_party_rule(self):
        d = self.app.config["DATA"]
        m = engine.match(d, "D-024")                                  # 어느 곳을 기준으로 잡아도 같은 그룹
        self.assertEqual({c["agency_id"] for c in m["candidates"] if c["decision"] == "auto"}, {"D-011", "D-037"})
        self.assertEqual(engine.match(d, "D-001")["candidates"], [])
        agg = engine.aggregate(d, ["D-011", "D-024", "D-037"], "2026-06")
        self.assertTrue(agg["over_limit"])
        self.assertEqual(agg["file_over_single"], 0)                  # 한 건으로 한도를 넘는 장려금은 없음
        self.assertEqual(len(agg["prior_over"]), 5)

    def test_cash_positions(self):
        c = engine.cash(self.app.config["DATA"])
        self.assertEqual(c["cv_decline_months"], 3)                   # Corevia 3~6월 연속 감소
        self.assertEqual(c["breach_date"], "2026-07-21")              # Masil: 발주 대금 지급일에 약정 하한 하회

    def test_audit_append_only_and_pages(self):
        self.start()
        con = sqlite3.connect(self.db)
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("DELETE FROM audit_log")
        con.close()
        for p in ["/", "/notices", "/audit", "/audit?format=csv", "/data", "/new", "/quality", "/requests/1"]:
            self.assertEqual(self.c.get(p).status_code, 200, p)
        self.assertTrue(any(a.startswith("AI 에이전트 미실행") for a, _ in self.audit()))   # LLM 없이 돈 것도 기록


class AgentLoop(Base):
    """가짜 Claude로 에이전트 루프(도구 호출 → 결과 회신 → 구조화 출력)와 세 호출 지점을 검증한다."""
    ai_client = FakeClaude()

    def setUp(self):
        type(self).ai_client = FakeClaude()
        super().setUp()

    def test_review_explain_alt(self):
        self.post("품질팀", "/quality", agency_id="D-024", demo="1")
        self.start()                                                  # PENDING
        html = self.post("영업팀", "/requests/1/explain", body="경위 소명")
        self.assertIn("AI 소명 검토: 가짜 소명 평가입니다.", html)        # ② 소명 검토가 스레드에 달림
        self.assertIn("재발 방지 계획", html)
        self.assertIn("보완 소명 필요", html)
        html = self.post("품질팀", "/requests/1/resolve", key="malicious", kind="released")
        self.assertIn("보완 소명을 제출한 뒤에 해제", html)             # AI가 부족하다고 본 소명은 보완 전에 해제할 수 없음
        self.assertEqual(self.status(), "PENDING")
        self.post("영업팀", "/requests/1/explain", body="재발 방지 계획과 회수 예정일 보완")
        html = self.post("품질팀", "/requests/1/resolve", key="malicious", kind="released")
        self.assertEqual(self.status(), "AWAITING_APPROVAL")
        self.assertIn("가짜 권고 제목", html)                           # ① 결재 권고
        self.assertIn("Claude · fake-model", html)
        self.assertIn("우대 요율 근거는?", html)
        html = self.post("재무팀", "/requests/1/reject", reason="CASH_SHORTAGE", memo="x")
        self.assertIn("가짜 대안 설명입니다.", html)                     # ③ 대안 설명
        self.assertIn("가짜 제외 근거입니다.", html)

        runs = [json.loads(p) for a, p in self.audit() if a.startswith("AI 에이전트 실행")]
        self.assertEqual(len(runs), 4)                                # 소명 검토 2회(첫 소명·보완) + 결재 권고 + 대안 설명
        self.assertEqual([t["tool"] for t in runs[0]["tool_calls"]], ["get_limit_check", "get_corevia_finance", "read_document"])
        self.assertFalse(runs[0]["tool_calls"][2]["ok"])              # 틀린 문서 이름은 오류로 기록

        first, second = self.ai_client.calls[0], self.ai_client.calls[1]
        self.assertEqual(first["model"], "claude-opus-5-5")
        self.assertEqual(first["fallbacks"], "default")
        results = second["messages"][-1]["content"]                   # 도구 결과 3개가 한 메시지에 모여 돌아감
        self.assertEqual([r["tool_use_id"] for r in results], ["t1", "t2", "t3"])
        self.assertEqual(json.loads(results[0]["content"])["amount"], 3888097)
        self.assertTrue(results[2]["is_error"])


if __name__ == "__main__":
    unittest.main()
