# 검증 기록 · 2026-10-31 KST

## 자동 검증

- Python 3.13 / macOS ARM64: pytest **105 passed**. 기존 56개 test와 신규 regression 포함. 외부 API 없이 fixture/mock으로 실행.
- `python -m compileall -q stock16`, `node --check stock16/static/app.js`, 각 `deploy/*.sh`의 `bash -n`, `pip check`, `git diff --check` 성공.
- Consensus 실제 1m/3m 비교, previous와 결과가 다른 경우, 월말 유지, 비교 이전 자료 없음, 5b 저장 관측 비교 검증.
- ETF 3일 중단 복구, 중간 누락일 재시도, 최초 수집 상한·주말, 휴일 빈 응답, checksum dedup, 정정 revision, quantity semantics 검증.
- 순차 입력·과거 backfill·유효/비유효 revision·NEW/EXIT·split·partial에서 incremental diff와 full rebuild 일치, 정상 import 비교 호출 최대 두 번 확인.
- NPS reviewed discovery·단위 변환·동일 파일의 여러 범위 추출·full/partial EXIT 차이·상세 파일 실패 시 월간 배분 보존 검증.
- SOL 공식 응답 형태의 날짜/상품 교차 검증 및 현금 수량 제외, PLUS 페이지 완전성·휴일 stale 응답·상품 식별 검증.
- NBIM explicit history와 미검증 발표일 제외, SEC filings.files·연도 필터·cache·중복 및 backoff/403 비우회 검증.
- GFS 기본 3/2/3, 명시값 7/4/6 보존, hardlink, backup 무결성, migration 실패 rollback/idempotence, data 폴더와 서버 전체 디스크 구분 검증.
- 기관/펀드별 상태 격리, 13F 안내, 오래된 공개 기준일, KST 자정 경계의 발표/수집일 분리, 기관 shares/ETF PDF 순위 분리 검증.
- 기존 인증·CSRF·rate limit·session 폐기·SSRF/XXE·용량/timeout·원본 보존 테스트 유지.

## 기존 데이터 migration

기존 v1 DB의 SQLite online 복사본으로 검증한 뒤 로컬 실수집 DB에도 v2를 적용했다. 원본 12개 snapshot, 18,677개 holding value, 28개 source document를 포함한 기존 22개 table을 비교했다. snapshot·값·원본·식별/provenance table 내용 hash가 유지됐으며 설정된 NPS sub-fund만 추가됐다. 사전 gzip backup을 복원해 v1 원본과 일치함을 확인했다. `integrity_check`/`foreign_key_check` 및 재초기화의 추가 backup 미생성을 확인했다. `.env`를 수정하거나 DB·raw를 초기화하지 않았다.

## 공식 소스 실수집

- NPS 월간 배분/기금공시 재수집 신규 0. 상세 파일 mapping은 미설정으로 명확히 partial/수동 등록 필요 표시.
- NBIM 최근 두 공개시점 재수집 신규 0. issuer shares 추정 없음. 과거 전체 live backfill은 수행하지 않았으며 fixture로 명시적 명령을 검증.
- KODEX 10개 공식 카탈로그/기준일 검증 유지. SOL 466920과 PLUS 266550에서 각각 4개 기준일, 총 **8개 신규 snapshot** 수집. 이번 live smoke test의 탐색 범위만 환경변수로 5 calendar days로 제한했고 코드 기본값은 30일이다.
- 동일 ETF 재수집 신규 **0건**. 기본 full PDF 구성종목과 gzip 원본 재사용 확인.
- SOL 공식 응답 WORK_DT·ETF_CD6·FUND_CD, PLUS 상세페이지 거래소 코드와 API pagination·wkdate 확인. PLUS 휴일 요청은 직전 영업일 응답이므로 요청일로 relabel하지 않음.
- TIGER 403, RISE TLS 검증 실패, ACE 날짜별 전체 다운로드 계약 미검증. 우회·추정하지 않고 reviewed mapping 필요 상태 유지. Temasek은 수동 공식자료 import 필요.
- SEC 운영자 User-Agent와 DART key가 없어 실제 역사 filing 수집은 실행하지 않음. 테스트 통과와 실제 운영 수집을 구분함.
- GIC 기존 공식 newsroom 데이터 보존. 신규 parser는 공식 og:title을 우선하여 navigation h1이 기사 제목에 섞이지 않도록 검증.

## 브라우저 QA

- 실제 local data + 인증 세션으로 확인. 오늘 정보/최근 포트폴리오 변화를 별도 영역으로 표시. 오늘 상세 목록은 펼치기로 제공.
- Desktop 1440×1000: 대시보드, NPS, ETF, Gurus, Consensus, 저장공간 이동 성공. SOL/PLUS 상세에서 실제 비교 행과 `CU당 구성수량`·quantity_type 확인.
- Mobile 390×844: 대시보드, NPS, ETF, Gurus, 순위, Consensus, 배분, Timeline, 저장공간, 추적대상 화면 모두 document scrollWidth=390. 필요한 표만 내부 수평 스크롤.
- 최종 navigation 콘솔 오류/경고 0. 실제 대시보드 화면은 프로젝트 상위 outputs/stock16-preview.png에 저장하며 Git에는 포함하지 않음.

## 검증 한계

Oracle 실서버 접속, Ubuntu ARM64 실행, 운영 systemd/nginx/TLS 배포는 수행하지 않았다. 배포 스크립트와 service 설정을 갱신했고 shell 문법을 검사했다. GitHub Actions 결과는 해당 commit의 원격 실행 결과로 별도 확인한다. NPS 상세 파일·일부 운용사·Temasek은 검증한 공식 mapping이 필요하며 모든 기관의 상세 포트폴리오 자동수집이 무설정으로 완성됐다는 의미가 아니다.
