# ax_hackathon

Aurora 그룹 · Corevia Cosmetics 관련 거래처 판매장려금 결재 시스템 MVP 기획.

- `corevia_incentive_mvp.html` — 처리 구조, 3개 시연 시나리오 와이어프레임(클릭 재생), 상태 머신, 화면 목록(S0~S7), 판단 규칙, 데이터 테이블, 역할·권한, MVP 범위

브라우저로 HTML 파일을 열면 됩니다. 화면의 거래처·금액은 `hackathon_data`의 실제 값이고, 동일 관련자 판정 기준(2개 이상 일치)과 지급 한도(월 200만 원)는 `hackathon_data/정책_원본추가`의 정책 문서를 따릅니다.
- `service/` — 위 기획을 `hackathon_data`로 실제 구동하는 Flask 서비스(MVP). 실행 방법과 명세-데이터 차이는 `service/README.md` 참고
