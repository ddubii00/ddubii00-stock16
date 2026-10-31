# 공식 adapter와 검증 범위 · 2026-10-31

| 운용사 | 기본 adapter | 기준일·완전성 확인 |
|---|---|---|
| TIME | 공식 거래소 코드 확인 → 날짜별 `pdf_excel.php` XLSX | 공식 pdfDate 요청별 전체 5개 컬럼, 해외 EQUITY 수량 구분, 휴일 빈 표 skip |
| KoAct | 공식 상품 JSON → 날짜별 `etf-pdf` JSON | stkTicker/fId, gijunYMD, list/totalCnt 대조 |
| JPMorgan | 공식 catalogue ticker/CUSIP → product-data dailyHoldingsAll | 전체 행, 공식 개수 이상, effectiveDate와 모든 navDate 일치, latest-only |
| ARK | 공식 상품 ID → 상위 10개 holdings HTML 임시 파싱 | 실제 As of, 4개 컬럼, 최대 10개, partial·수량 unavailable. HTML 미보관, 추출 JSON gzip만 보존 |
| KODEX | 공식 상품목록 → product-pdf JSON | 거래소 코드/fId 대조, gijunYMD, list 길이/totalCnt 일치 |
| SOL | 공식 상품목록의 etfResultData → `/api/fund/pdfList` | ETF_CD6/FUND_CD 교차 검증, 각 행 WORK_DT 일치, 날짜별 전체 배열 |
| PLUS | 공식 상세페이지 → `/api/v1/product/pdf/list` | 상세페이지 summary__product-code 대조, 모든 페이지/totalElements, 각 행 wkdate 일치 |
| TIGER | 검증한 다운로드 mapping | 현재 환경 공식 사이트 403. 우회하지 않고 설정 필요 |
| RISE | 검증한 다운로드 mapping | 현재 환경 TLS 검증 실패. 인증서 검증을 끄지 않음 |
| ACE | 검증한 다운로드 mapping | 공식 페이지 접근은 확인했으나 안정적인 날짜별 전체 다운로드 계약 미검증 |

SOL은 공식 카탈로그에서 거래소 코드로 상품번호를 찾는다. PLUS는 watchlist source_url에 공식 상세페이지 `https://www.plusetf.co.kr/product/detail?n=상품번호`를 지정한다. 거래소 코드가 일치해야 수집한다. KODEX·SOL·PLUS 모두 원본이 제공하지 않는 발표일은 NULL로 남긴다. PLUS API의 휴일 요청이 직전 영업일을 반환하는 것을 확인했으며 그 날짜를 요청일로 변경하지 않는다. PLUS는 공개하지 않는 평가액을 수량이나 비중에서 역산하지 않는다.

실제 확인한 예시는 [SOL 조선TOP3플러스](https://www.soletf.co.kr/ko/fund/etf/211028?tabIndex=3) 466920 / 211028과 [PLUS 중형주저변동50](https://www.plusetf.co.kr/product/detail?n=006304) 266550 / 006304다. 관심목록에 각 1개를 추가했다. 확인일 이후 공식 구조가 바뀌면 실패 상태를 남기며 이전 자료는 유지한다.

## ETF reviewed mapping

`config/sources.json` → `krx-etf.providers.운용사.거래소코드`에 아래 계약을 설정한다. 예시 endpoint를 실재하는 것처럼 제공하지 않는다. URL·필드·수량 의미·단위·전체 공개 여부를 실제 공식 파일에서 검증한 뒤 등록해야 한다.

| 키 | 의미 |
|---|---|
| reviewed | 검증 완료 시 true |
| url_template | 공식 hostname의 다운로드 URL. `{code}`, `{date:%Y%m%d}`, `{iso_date}` 치환 지원 |
| format / mapping / constants | csv·xlsx·json, 원본 column → canonical field, 검증한 고정값 |
| as_of_path / date_format | JSON 응답 기준일 경로와 날짜 형식. 배열 인덱스는 `content.0.wkdate`처럼 지정 |
| rows_path | wrapper JSON 안의 행 배열 경로. 빈 배열은 정상 skip |
| date_parameter_verified | 응답 자체 기준일이 없을 때 날짜 매개변수의 의미를 공식 자료로 검증한 경우에만 true |
| disclosure_scope | full 또는 partial. 생략 시 partial |
| quantity_type | 기본 pdf_per_cu_quantity. 실제 펀드 전체 shares라는 증거 없이 shares로 바꾸지 않음 |
| multipliers | 검증한 통화·수량 단위 배수. 원본을 바꾸지 않고 normalized 숫자에만 적용 |

SOL/PLUS도 명시적인 reviewed mapping을 등록하면 기본 adapter보다 우선한다. 임의 URL·봇 방어 우회·추측 parser는 사용하지 않는다.

## NPS 상세 파일 / Temasek Review

NPS 월간 자산배분은 기존 공식 홈페이지 parser를 유지한다. 상세 국내주식·해외주식·대량보유는 `datasets`의 검증한 파일 매핑 또는 `discovery_rules`를 사용한다. 기본 설정에는 검증되지 않은 자동 상세 다운로드를 넣지 않았다. 따라서 월간 배분이 정상이어도 상세 파일 구성요소는 `공식 파일 수동 등록 필요`로 표시한다.

각 discovery rule은 reviewed=true, index_url, link_pattern, 선택적 label_pattern, format, mapping, metadata를 포함한다. index HTML은 일시적으로 읽고 링크만 탐색한다. link_pattern의 named group `as_of_date`, `published_at`은 ISO 날짜 metadata에 반영한다. 발표일이 확인되지 않거나 검증한 형식의 파일이 없으면 설정 필요 상태로 남긴다. heuristic PDF/OCR은 사용하지 않는다.

상세 파일은 `nps-domestic-equity`, `nps-foreign-equity`, `nps-large-holdings`에 분리한다. [국민연금 국내주식 공개 데이터 설명](https://www.data.go.kr/data/3070507/fileData.do)은 평가액 단위를 억원, 비중을 국내주식 내 비중으로 설명한다. 해당 설명만으로 다운로드 파일의 구조가 검증됐다는 뜻은 아니다. 실제 파일 column·기준일·발표일을 확인하고, 억원 자료라면 multipliers.market_value=100000000을 지정한다. 공시된 수량이 없으면 shares는 NULL이다. 일부 대량보유 공시는 partial로 두고 공개목록 이탈을 전량매도로 표시하지 않는다.

Temasek은 별도 기관이며 datasets/discovery_rules/공식 feed 계약을 공유한다. 검증한 자료가 없으면 `공식 데이터 자동수집 불가 / 수동 공식자료 import 필요`를 표시한다. GIC는 공식 newsroom과 검토한 연차 배분만 제공하며 전체 포트폴리오를 만들어내지 않는다.


액티브 ETF 기본 목록은 TIME 4개·KoAct 3개·ARK 4개·JPMorgan 2개다. 공식 분류 근거/확인일/운용사/상장국은 config/watchlists.json에 기록하고 etf_profiles로 정규화한다. 이전 패시브 기본 목록은 추적 중지 상태로 보존한다.

공식 확인: [TIME 코스피액티브](https://www.timeetf.co.kr/m11_view.php?idx=11), [KoAct 코리아밸류업액티브](https://www.samsungactive.co.kr/etf/product/view.do?id=2ETFP3), [ARKK](https://www.ark-funds.com/funds/arkk), [JGRO](https://am.jpmorgan.com/us/en/asset-management/adv/products/jpmorgan-active-growth-etf-etf-shares-46654q609), [JAVA](https://am.jpmorgan.com/us/en/asset-management/adv/products/jpmorgan-active-value-etf-etf-shares-46641q167). ARK 전체 CSV 서버의 robots 제한은 우회하지 않는다. latest-only 미국 소스에 과거 날짜를 붙여 backfill하지 않는다.
