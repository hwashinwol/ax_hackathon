"""3대 시나리오를 실제 hackathon_data로 끝까지 돌려 보는 통합 테스트.

실행: python -m unittest discover -s service/tests  (ax_hackathon 폴더에서)
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app  # noqa: E402


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.app = create_app(db_path=self.db)
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

    def status(self, rid=1):
        con = sqlite3.connect(self.db)
        s = con.execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()[0]
        con.close()
        return s

    def start_to_pending(self):
        self.post("영업팀", "/new", anchor_id="D-011", period="2026-06", extra_ids="", memo="합산 요청")
        self.assertEqual(self.status(), "REQUESTED")
        html = self.post("영업팀", "/requests/1/match")
        self.assertIn("D-024", html)
        self.assertEqual(self.status(), "MANUAL_REVIEW")           # D-037 76점
        self.post("영업팀", "/requests/1/review", agency_id="D-037", decision="confirm")   # 권한 없음
        self.assertEqual(self.status(), "MANUAL_REVIEW")
        self.post("내부감시팀", "/requests/1/review", agency_id="D-037", decision="confirm", memo="계좌 동일")
        self.assertEqual(self.status(), "MATCHING")
        html = self.post("영업팀", "/requests/1/evaluate")
        self.assertIn("23,324,352원", html)
        self.assertIn("3,888,097원", html)
        self.assertEqual(self.status(), "PENDING")                  # 90일 초과 연체 29,202,840원
        self.assertIn("29,202,840", html)

    def release(self):
        html = self.post("재무팀", "/requests/1/resolve", key="overdue", kind="collected", memo="x")
        self.assertIn("영업팀 소명이 먼저", html)                     # 소명 없이 해제 불가
        self.post("영업팀", "/requests/1/explain", body="7월 회수 완료 예정")
        self.post("재무팀", "/requests/1/resolve", key="overdue", kind="collected", memo="회수 확인")
        self.assertEqual(self.status(), "AWAITING_APPROVAL")

    def test_scenario1_normal(self):
        self.start_to_pending()
        self.release()
        html = self.post("내부감시팀", "/requests/1/approve")
        self.assertIn("재무팀 결재 차례", html)                       # 순서 강제
        self.post("재무팀", "/requests/1/approve")
        self.post("내부감시팀", "/requests/1/approve")
        self.assertEqual(self.status(), "PAID")

    def test_scenario2_hold_and_dismiss(self):
        self.start_to_pending()
        self.post("품질팀", "/requests/1/dismiss", memo="기각")
        self.assertEqual(self.status(), "CLOSED")

    def test_scenario3_cash_reject_credit(self):
        self.start_to_pending()
        self.release()
        html = self.post("재무팀", "/requests/1/reject", reason="", memo="")
        self.assertIn("사유 코드", html)
        html = self.post("재무팀", "/requests/1/reject", reason="CASH_SHORTAGE", memo="Masil 약정")
        self.assertEqual(self.status(), "ALT_PROPOSED")
        self.assertIn("대리점 정산채널", html)
        self.assertIn("SKU-1001", html)                                # 제외 품목으로 표시
        self.post("영업팀", "/requests/1/alt/accept")
        self.post("재무팀", "/requests/1/alt/accept")
        self.assertEqual(self.status(), "CREDIT_SCHEDULED")
        self.post("Masil", "/requests/1/credit/settle", po="PO-MS-2607-001")
        self.assertEqual(self.status(), "CREDIT_SETTLED")

    def test_unrelated_agency_not_eligible(self):
        self.post("영업팀", "/new", anchor_id="D-001", period="2026-06")
        self.post("영업팀", "/requests/1/match")
        self.assertEqual(self.status(), "MATCHING")
        self.post("영업팀", "/requests/1/evaluate")
        self.assertEqual(self.status(), "NOT_ELIGIBLE")

    def test_audit_append_only_and_pages(self):
        self.start_to_pending()
        con = sqlite3.connect(self.db)
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("DELETE FROM audit_log")
        con.close()
        for p in ["/", "/notices", "/audit", "/audit?format=csv", "/data", "/new", "/requests/1"]:
            self.assertEqual(self.c.get(p).status_code, 200, p)


if __name__ == "__main__":
    unittest.main()
