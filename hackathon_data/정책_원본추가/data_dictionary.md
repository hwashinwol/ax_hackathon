# data_dictionary — 공통 데이터 정의·고정 수식

본 문서는 데이터룸 전체에 공통 적용되는 정의입니다. 참가자는 아래 규칙을 **추측 없이** 사용합니다.

## 1. 기준 시점·기간
| 항목 | 값 |
|---|---|
| 데이터 기간 | 2024-01-01 ~ 2026-06-30 (30개월) |
| 현재 시점(As-of) / 데이터 컷오프 | 2026-07-01 |
| 이사회 | As-of + 5일 |
| 의사결정 지평 | 90일 액션플랜 (2026-07~09) |
| 환율 데이터 범위 | ext_fx_daily.csv = 2024-07 ~ 2026-06 USD/KRW 일별 종가 |

## 2. 수치·표기 규칙 (고정 가정)
- **금액**: 모든 금액 컬럼은 원(KRW), 정수. 천원 단위 파일 없음.
- **부가세**: 모든 금액은 공급가액(VAT 제외).
- **수량**: 제품 = EA, 원료 = kg.
- **주차·영업일**: 주차는 월요일 시작. 영업일 월~금, 공휴일 없음 가정.
- **구분자**: 모든 CSV는 파이프(`|`) 구분, UTF-8, 헤더 1행. 값 내부에는 파이프 없음.
- **빈 셀 없음**: 해당 없음은 명시적 코드 `NONE`으로 표기(필수 컬럼은 완전 채움).

## 3. 환율·매출 인식
- 원재료 외화 결제는 **구매일(발주일)** 환율 적용(입고일 아님). `amount_krw`는 환산 완료값이며
  `unit_price_fx × qty × fx_rate_applied = amount_krw`로 재검증 가능.
- Corevia 매출은 전액 원화 거래. 해외(Masil) 마켓플레이스 주문·반품·쿠폰 금액은 **판매 시점 원화 확정값**.
- 재무 매출 인식은 **출하(ship_date)** 기준. 수주(order_date)는 항상 별도 컬럼으로 제공.
- COGS는 `production_material_consumption_cv.applied_unit_cost_krw`(실제 투입 단가) 기준.

## 4. 고정 수식 (요약, 상세는 calculation_definitions)
- **GPM** = 매출총이익 ÷ 매출(출하 기준)
- **GMV** = 쿠폰 반영 후·VAT 제외 총 주문금액
- **Net Revenue** = GMV − 반품환불액
- **실질 공헌이익** = Net Revenue − COGS − 쿠폰 − 광고비 − 채널수수료 − 배송/반품물류비 − 인플루언서비
- **재고 커버(주)** = on_hand_qty ÷ 최근 4주 평균 주간판매량
- **covenant 위반** = 매 영업일 closing_balance < 30억
- **재구매** = 최초주문 후 90일 이내 재주문
- **동일 관련자** = 식별정보(대표자·주소·정산계좌) 2개 이상 일치.
  일치 관계는 **전이적으로 연결**한다(A-B가 2개 일치, A-C가 2개 일치면 A·B·C를 한 그룹으로 본다).
  실제 입금계좌는 마스터와 다를 수 있으므로 `fin_collection_history_cv.bank_account_no`도 함께 대조한다.

## 5. 판단에 필요한 주요 컬럼 (자주 놓치는 것)
| 컬럼 | 파일 | 의미 |
|---|---|---|
| `cost_linked_clause` | `m_customer_cv` | 원가연동 조항 보유 여부. **True = 원가상승분 판가 전가 가능**. 고객별로 값이 다르다 |
| `revenue_share_pct` | `m_customer_cv` | 고객별 매출 비중. **전 기간 동일 기준의 고정값**이라 어느 기간으로 집계해도 같은 값이다. 거래 데이터(`sales_orders_cv`)를 기간 집계한 비중과는 다를 수 있다. 12개사 합계는 **95.5%**(미귀속 4.5%는 어느 고객에도 속하지 않는다) |
| `payment_terms_days` | `m_customer_cv` | 고객별 회수조건(일). 이탈·현금영향 산정에 사용 |
| `currency` / `sourcing_region` | `m_material_cv` | 원료 조달 통화·지역. **환율 노출 여부**를 가른다(KRW 원료는 환율 무관) |
| `qc_result` | `prod_batch_log_cv` | 배치 품질판정(합격/보류). `보류`는 조사 대상 |
| `input_lot_ids` | `prod_batch_log_cv` | 배치에 투입된 원료 로트. **로트 기준 추적**의 출발점 |
| `defect_confirmed` | `qc_claims_cv` | 규격이탈 **확정** 여부. `보류`·False와 구분되는 별도 상태다 |
| `related_party` 식별정보 | `m_agency_cv` | 대표자·주소·정산계좌. 동일 관련자 판정용 |
| `outstanding_krw` | `fin_ar_aging_*` | 미회수 잔액 |
| `payment_due_date` | `fin_ap_aging_ms`, `sc_purchase_orders_ms` | 지급 예정일 |
| `recurring_flag` | `strat_celtonic_adjustment_analysis` | 조정 항목의 반복 여부 |
| `sga_cert_reg_krw` · `sga_claim_settle_krw` · `sga_retention_bonus_krw` · `sga_equip_relocation_krw` | `celtonic_quarterly_pl` | Celtonic이 **"일회성"이라 주장**하는 4개 조정 항목의 분기별 실제 발생액. 컬럼명은 항목명일 뿐 일회성 여부를 뜻하지 않는다 |
| `revenue_share_pct` | `celtonic_customer_composition`, `celtonic_revenue_by_customer` | 고객별 매출 비중. **전 기간 동일 기준**이라 어느 기간으로 집계해도 같은 값이 나온다 |
| `contract_expiry_date` | `celtonic_customer_composition`, `celtonic_revenue_by_customer` | 고객 계약 만료일. 매출 비중과 **함께** 봐야 이탈 시 손실 규모가 나온다 |
| `added_capacity_qty` | `strat_capex_plan_cv` | 해당 안 집행 시 **확보되는 연간 생산능력(EA)**. CAPEX 금액만이 아니라 이 값으로 C-201 요구 물량 소화 가능 여부를 판단한다 |
| `utilization_pct` | `strat_capacity_cv` | **라인(line_id) 단위** 가동률. 공장(plant_id) 단위 집계가 필요하면 `capacity_qty` 가중평균으로 산출한다(단순평균 아님) |
| `product_id` | `m_sku_ms` | Masil SKU가 대응하는 Corevia 제품군(PL-0n). 더마 SKU → PL-02 → 원료 M-B → 공급처 V-205(독점)로 이어지는 단일 소싱 사슬을 조인할 때 사용 |
| `primary_country` | `m_influencer_ms` | 인플루언서 활동 국가. 국가별 성과 집계 시 사용(`m_campaign_ms.country`·`mkt_orders_ms.country`와 동일 값) |
| `stacked_flag` | `mkt_coupons_ms` | 프로모션 **중복 적용 여부를 나타내는 주문 속성**. 쿠폰 행은 주문당 1건으로 집약되므로 "쿠폰 2장짜리 주문"을 행 단위로 찾을 수는 없다 |

### 주요 마스터·참조 테이블
| 파일 | 내용 |
|---|---|
| `m_influencer_ms` | 인플루언서 10명: `influencer_id`·`influencer_name`·`tier`·`primary_country`·`platform`·`fee_type` |
| `m_campaign_ms` | 캠페인 25건: 인플루언서·국가·SNS 채널/포맷·판매채널·제품라인·기간·귀속창(7일) |
| `m_channel_ms` | 판매채널 8개: `channel_type`(자사몰·마켓플레이스·대형파트너·대리점·라이브)·파트너코드(LC-nnnn)·결제조건 |
| `mkt_cost_master_ms` | SKU×채널 고정 원가: 제품원가·채널수수료율·배송비·반품물류비 |
| `mkt_repurchase_ms` | 고객별 최초구매·90일내 재구매 여부 |
| `ext_industry_benchmark` | 업계 기준선(반품률 8%·재구매율 22%·DSO 55일) |
| `ext_channel_benchmark` | **동종업계 채널유형별 외부 기준선**(Masil 실적이 아님). 반품률·재구매율·공헌이익률을 `m_channel_ms.channel_type` 단위로 비교할 때 사용 |

## 6. 데이터 스케일 (반드시 인지할 것)
- **재고·자금·손익 계열**(`sku_weekly_sales_ms`, `sku_inventory_ms`, `sc_purchase_orders_ms`,
  `fin_cash_daily_ms`, `fin_*_aging_*`, `fin_income_statement_cv`)은 **회사 실제 규모(풀스케일)**다.
  covenant 30억·발주액·매출액을 직접 비교·판단해도 된다.
- **커머스 트랜잭션 계열**(`mkt_orders_ms`, `mkt_returns_ms`, `mkt_coupons_ms`, `mkt_ad_spend_ms`,
  `mkt_traffic_inflow_ms`, `mkt_budget_plan_ms`)은 **분석 가능한 대표 표본**이다.
  캠페인 간 **상대 비교(ROAS·반품률·재구매율·CAC)**에 사용하며, 회사 전체 매출과 절대금액으로 대사하지 않는다.
- 두 계열을 절대금액으로 합산·대조하지 말 것. 각 트랙의 판단은 해당 계열 안에서 완결된다.
- **표본 계열로 무엇을 하는가**: 비중·비율·순위·추세는 표본에서 **유효한 산출물**이다.
  이 계열에서 나오는 이슈의 크기는 금액이 아니라 **배분 원칙의 방향**과 **경영진이 보고받는 지표의
  신뢰성**으로 잰다. **금액을 산출할 수 없다는 것이 중요도가 낮다는 뜻은 아니다** —
  절대금액으로 환산해 규모를 주장하는 것과, 규모를 잴 수 없다는 이유로 판단을 보류하는 것은
  **둘 다 오답이다.**
