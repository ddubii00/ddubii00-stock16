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

## 액티브 ETF 및 웹 수집 queue 추가 검증

- 전체 pytest **156 passed** (기존 105개 유지, 신규 51개). 인증/CSRF, 입력 allowlist·추가 필드 거절, HTTP inline 실행 없음, queued/running 중복·동시 요청, 다른 수집기 대기, all 실패 격리, worker 오류 비밀값 제외, 설정 필요, 자동 flock 충돌, UTC/origin, 중단 복구, metadata만 정리, unit 설정을 검증했다.
- 한국 TIME 4개·KoAct 3개, 미국 ARK 4개·JPMorgan 2개의 실제 공식 자료 확인. live smoke test만 탐색 범위를 7일로 제한했으며 코드 기본 30일은 유지했다. 한국 35개(5개 기준일 × 7개) 및 미국 6개, 총 **41개 신규 snapshot**을 저장했다. 미국은 실제 2026-10-30 공개 기준일만 저장했다.
- 최초 JAVA의 공식 음수 현금 비중 -0.02%를 확인하고 fund_reported_quantity의 명시적 CURRENCIES 항목만 허용했다. 주식 음수 수량/비중·범위 초과 거절은 별도 회귀 테스트로 유지했다. 수정 뒤 전체 13개 재수집은 오류 0개, 정정 0개, 이미 저장된 40개는 skip, JAVA 1개만 신규였다. ARK 상위 10개 자료는 의도적으로 partial이며 미식별 비상장 항목을 임의 종목으로 만들지 않았다. 마지막 웹 재실행은 신규 0, 정정 0, skip 41, 오류 0으로 동일 자료의 중복 방지를 확인했다.
- 브라우저 실제 ETF 버튼 클릭 → queued 즉시 반환 → 별도 collector-worker CLI 실행 → running → partial 완료 → 2.5초 polling으로 버튼 재활성화와 data-status/실행 이력 갱신 확인. SEC 버튼도 needs_configuration과 STOCK16_SEC_USER_AGENT 설정 안내까지 검증했다. 비밀값은 브라우저에 표시하지 않았다.
- 1440px 및 모바일 390px 화면 확인. 모바일 document.scrollWidth=390으로 가로 넘침 없고 모든 수집 버튼이 viewport 안에 배치됐다. 상세 시각·메시지는 펼침 영역으로 제공한다.
- 기존 v2 사전 migration gzip backup의 원본 행과 현재 v4 비교: snapshot 20, holding value 18,935, holding set 20, holding member 18,937, security/identifier 각 10,379, source document/observation 각 36, allocation 25, disclosure 12개 원본 행이 모두 그대로 포함됐다. integrity_check/foreign_key_check 정상. .env·DB·raw 삭제/초기화 없음. 이전 패시브 기본 ETF는 추적만 중지하고 이력 보존.
- schema v4 신규 queue·origin migration, install.sh stock16 계정 init, update.sh 전체 service/timer 설치·daemon-reload·worker enable/restart 반영. ETF/기관 timer 파일은 수정하지 않았다. macOS에서는 worker unit INI/필수 directive/ExecStart 계약을 검사했고 실제 Linux systemd-analyze verify는 GitHub Actions에 추가했다. Oracle 서버 배포 자체는 이 로컬 작업에서 실행하지 않았다.

## 주간 시가총액 / MSCI (2026-11-01 KST)

- 전체 pytest 217개 통과. ETF/ETN 제외, 전체시장 67→180 추적, 우선주 분리, 직전 주 미확보, 시장 이전/이름 변경, 부분·단위 오류 거절, 공개 자료 무키 경로, zero-volume 보존, KOSDAQ GLOBAL 포함, 늦은 목요일의 금요일 오표시 방지, 원본/정규화 중복 방지, host-scoped 인증과 migration FK 참조를 검증했다.
- Python compileall, JS 구문, deploy shell 구문, pip check 통과. Linux worker/weekly service/timer의 systemd-analyze verify를 CI에 포함했다. Oracle 서버 배포 및 실제 timer 실행은 이 로컬 작업에서 수행하지 않았다.
- 공개 FinanceData/marcap revision `6b36e3ca01e7b4dc73c84f279e093843889d26dd`의 2020–2026 연도 파일을 Git blob hash로 확인했다. KOSPI/KOSDAQ 각각 356주(2020-01-03–2026-10-23), 전종목 순위 총 919,470행을 실제 로컬 DB에 수집했다. 원본 최신일이 2026-10-29여서 2026-10-30 주는 미확보다. Secondary로 구별하며 NASDAQ은 0/357주·자료 연결 필요다. 현재 종목이나 현재 발행주식 수로 과거 순위를 채우지 않았다.
- 실제 2020-01-03 KOSPI 67위 두산밥캣은 2026-10-23 기준 94위, KOSDAQ 67위 엔지켐생명과학은 797위였다. 모두 356주 이력을 조회했다. 이는 최신 확보일의 관측이며 현재 주 순위 확정값이 아니다.
- MSCI 공식 Standard PDF 27개 분기(2020-02–2026-08) 수집, 한국 요약 건수/두 열/날짜 검증 및 최신 PDF의 시각 검토를 완료했다. 2026-08 발표일 08-12, 장 마감 반영일 08-31, 편입 1/편출 4건. 이후 미발표 분기는 생성하지 않았다.
- 실제 재수집은 주간 2주 및 MSCI 최신 2분기를 재검증해 신규 0건/오류 0건이었다.
- 실제 browser에서 Top 100 100행, 진입/이탈, 67위 추적, NASDAQ 미확보, MSCI 27분기를 확인했다. 390px 모바일과 1440px 화면에서 페이지 가로 넘침이 없었다. 임시 TEST 전용 UI 서버/DB는 종료·삭제했고 실제 DB에 TEST 순위는 넣지 않았다.
- v4 사전 backup과 전체 기존 행을 비교했다. portfolio snapshot 61, holding value 21,091, set 61, member 21,094, securities/identifiers 10,813씩, source document/observation 117씩, 자산배분 25, 공시 12, queue 3, run 22개가 보존됐다. integrity_check=ok, foreign_key_check 빈 결과. 데이터/비밀 설정/원본은 Git에 넣지 않는다.
- 측정 당시 DB 약 58.55MiB, raw 28.05MiB, 연도별 압축 cache 134.23MiB, 데이터 전체 약 233.73MiB. Mac 전체 파일시스템은 약 98.5% 사용으로 90% 경고 구간이다. 이 수치는 Oracle 서버의 사용률을 의미하지 않는다.
