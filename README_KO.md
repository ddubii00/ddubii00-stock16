# stock16 · 기관 포트폴리오 변화 분석

국민연금, 노르웨이 GPFG/NBIM, GIC, Temasek, 국내 ETF, 미국 13F 및 한국 공식 공시의 공개시점별 변화를 비교하는 한국어 대시보드다. 기준일·공시일·수집일을 구분하며, 공개되지 않은 숫자를 만들지 않는다. 현재 보유나 투자추천을 의미하지 않는다.

## 구현과 실제 수집 범위

| 소스 | 기본 동작 | 추가 설정 / 한계 |
|---|---|---|
| NPS | 공식 홈페이지의 최근 월별 자산배분·총액과 기금공시 수집 | 국내/해외 상세 보유목록은 공식 다운로드 파일과 컬럼 매핑을 등록하여 import. 연간 세부내역, 월간 배분, 분기 대량보유의 공개주기가 다름 |
| NBIM | 공식 API로 최근 두 반기/연말 주식·채권 **issuer aggregate** 및 자산배분, 공식 CSV 원본 수집·diff | 공식 source-local ID는 검토 상태. 개별 bond issue/수량이 아닌 issuer별 금액. 미공개 shares는 NULL. 새로운 공개시점은 발표일 검증 후 설정 필요 |
| KODEX ETF | 공식 상품 카탈로그의 코드·ID 대조 후 full Portfolio Deposit File JSON 수집. 기본 10개 상품, 추가한 KODEX도 코드로 발견 | KRX 로그인/OTP를 우회하지 않고 공식 운용사 자료 사용. TIGER 등 다른 운용사는 다운로드 adapter 설정 필요. API가 발표일을 주지 않아 NULL로 표시 |
| SEC 13F | 공식 submissions → filing cover/index → informationTable XML → 정규화·diff | 운영자 이름·이메일을 담은 `STOCK16_SEC_USER_AGENT` 설정 필요. recent submissions 범위 내 초기 이력. 추가보고 amendment는 자동 병합하지 않고 검토 |
| GIC | 공식 newsroom의 최신 기사 최대 8개를 날짜·원문 발췌와 함께 수집 | 전체 종목 미공개. 자산군·지역 배분은 검증한 연차자료의 dataset mapping으로 입력 |
| Temasek | 독립 기관·연간 배분/공개투자 dataset·공식 RSS adapter | 검증 환경에서 공식 사이트 HTTP 403. 우회하지 않으며 기본 상태 `needs_configuration`. 검증한 공식 파일을 수동 import하거나 허용된 다운로드/RSS 등록 |
| DART | 공식 API의 대량보유·특정증권 이벤트 공시 수집·원문 연결 | API key와 추적 기업 corp_code 필요. 이벤트만으로 전체 포트폴리오를 생성하지 않음. 종목별 수량은 검증된 구조화 자료 import 필요 |

모든 기관의 상세 보유량과 전체 역사 자동수집이 별도 설정 없이 완성된 서비스는 아니다. 위 범위는 코드로 구분되어 있으며 미설정·실패를 정상 수집처럼 숨기지 않는다. 운영 데이터는 Git에 포함하지 않고 빈 DB로 시작한다. 테스트 fixture는 명확한 합성 자료이며 production에 로드되지 않는다.

화면: 오늘의 변화, 기관별 보유·변화·자산배분, ETF 기간 비교, US/Korea Gurus, Consensus, 종목 검색과 기관별 보유, 공시 Timeline, 추적대상 추가/중지, 저장공간·수집 상태. 테이블 정렬·목록 검색·변화 필터·CSV 다운로드·모바일/명암 테마를 제공한다. API에서 날짜·국가·섹터·상태 필터와 metric별 순위를 조회할 수 있다. 숫자마다 snapshot 출처·날짜가 연결된다.

## 구조

```text
stock16/
  schema.sql              정규화 schema / 불변 snapshot / source provenance
  db.py                   Repository 경계
  model.py                Decimal / 식별자 / import / corporate action / diff
  analytics.py            기간 비교 / 교집합 / 자산배분 / 종목 분석
  auth.py, web.py          인증 / CSRF / API / UI
  storage.py              gzip dedup / retention / GFS backup / disk alert
  collectors/             NPS / NBIM / KRX-ETF / SEC / GIC / DART / 공통 adapter
  templates/, static/     한국어 responsive UI, 외부 CDN 없음
config/                   기본 목록과 공식 데이터 매핑
deploy/                   Oracle systemd / nginx / logrotate
tests/                    외부 장애와 무관한 fixture 기반 검증
```

상세 구조는 [ARCHITECTURE.md](docs/ARCHITECTURE.md)에 설명한다. collector 공통 단계는 discover/fetch/parse/normalize/validate/persist/diff이며 공식 특화 모듈은 이 단계를 한 `run()` 흐름으로 실행한다. 동일 원본 재수집과 같은 날 같은 dataset은 중복 snapshot이 생기지 않는다.

## 로컬 설치와 실행

Python 3.12+와 Git이 필요하다. Node는 앱 실행에 필요 없으며 JS 문법 검사에만 사용한다.

```bash
git clone https://github.com/ddubii00/ddubii00-stock16.git stock16
cd stock16
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m stock16 setup
.venv/bin/python -m stock16 init
STOCK16_SECURE_COOKIE=0 .venv/bin/python -m stock16 serve
```

브라우저: `http://127.0.0.1:3016/stock16-7/`. setup에서 지정한 초기 비밀번호를 입력한다. 비밀번호 원문은 저장하지 않고 Werkzeug scrypt 해시만 `.env`에 기록한다. 서명키는 무작위 생성하며 `.env`는 0600으로 생성된다. 실제 `.env`, 원본, DB, 로그, 백업은 Git에 들어가지 않는다. 로컬 HTTP에서만 `SECURE_COOKIE=0`을 사용하고 운영 HTTPS에서는 1을 유지한다.

서명키를 바꾸면 기존 쿠키가 무효화된다. 비밀번호 변경 시 `.env`의 해시를 새로 만들고 서비스 재시작 및 `sessions` 테이블의 기존 세션 삭제를 함께 수행한다. setup은 기존 `.env`를 덮어쓰지 않는다.

## 환경변수

`.env.example`은 비밀값 없는 키 목록이다. 값은 서버 환경 또는 서버 전용 `.env`에만 설정한다. shell `source .env` 대신 앱의 literal loader 또는 systemd EnvironmentFile을 사용한다.

| 변수 | 기본/용도 |
|---|---|
| `STOCK16_PASSWORD_HASH` | 필수. scrypt password hash |
| `STOCK16_SECRET_KEY` | 필수. 32자 이상 무작위 signing key |
| `STOCK16_DATA_DIR` | 기본 checkout/data. 운영 `/var/www/stock16-7/data` |
| `STOCK16_BASE_PATH` | `/stock16-7` (끝 slash 없이) |
| `STOCK16_PORT` | 후보 3016. 설치 시 서버 포트 점검 |
| `STOCK16_SECURE_COOKIE` | 1. 로컬 loopback HTTP만 0 |
| `STOCK16_SEC_USER_AGENT` | SEC 정책에 맞는 실제 운영자 식별 이름·이메일 |
| `OPENDART_API_KEY` | OpenDART key. 로그/Git에 남기지 않음 |
| `STOCK16_RAW_MONTHS` | 12. ETF 전체 원본 유지 개월 |
| `STOCK16_BACKUP_DAILY` | 7 |
| `STOCK16_BACKUP_WEEKLY` | 4 |
| `STOCK16_BACKUP_MONTHLY` | 6 |
| `STOCK16_ALERT_WEBHOOK` | 선택. 임계값 상승 시 저장공간 metric만 보내는 운영자 지정 HTTPS URL |

인증 쿠키는 HttpOnly, Secure, SameSite=Strict, 경로 제한을 적용한다. 서버 DB 세션은 8시간이며 로그아웃 시 폐기한다. 미인증 화면은 로그인으로 이동하고 API는 401을 반환한다. 로그인 포함 모든 변경 요청에 CSRF token이 필요하다. DB 기반 IP당 15분 8회, 전체 80회 로그인 시도 제한은 worker 간 공유된다. nginx가 전달한 임의 XFF를 신뢰하지 않으므로 IP 제한은 직접 연결 IP 기준이며 reverse proxy에서는 proxy IP로 묶인다. 필요 시 신뢰된 단일 proxy만 명시하는 배포 설정으로 실제 IP를 전달해야 한다.

## 수동 수집

```bash
.venv/bin/python -m stock16 collect
.venv/bin/python -m stock16 collect --only nps nbim
.venv/bin/python -m stock16 collect --only krx-etf
.venv/bin/python -m stock16 collect --only sec-13f
.venv/bin/python -m stock16 collect --only gic temasek dart
```

한 collector 실패 시 다음 collector를 계속 실행한다. data_fetch_runs에 마지막 점검·성공·다음 예정·오류·신규 없음 상태가 남는다. 새로운 NBIM dataset의 발표일은 공식 보고서/발표에서 확인한 뒤 sources.json의 publication_dates에 추가한다. 임의 수집일을 발표일로 바꾸지 않는다. NPS 월별 자산배분의 공시일도 해당 월의 공식 공시 제목·날짜와 일치할 때만 입력한다.

### 검증된 공식 파일 import

`config/dataset.example.json`을 복사해 URL·기준일·공시일·단위·공개범위와 **실제 파일 컬럼**을 맞춘다. 예시 URL과 날짜는 자동수집용 기본값이 아니다. 같은 mapping 객체를 config/sources.json 기관별 datasets 배열에 넣으면 scheduler가 다운로드한다. 특정 날짜 파일을 가리키는 mapping은 새로운 공개파일이 나올 때 갱신한다. ETF 자동 KODEX adapter는 날짜를 직접 탐색한다.

```bash
.venv/bin/python -m stock16 import /path/to/official.csv --collector nbim --config /path/to/reviewed-mapping.json
```

CSV encoding/delimiter/skip_rows, XLSX sheet/skip_rows, structured JSON 배열을 지원한다. canonical 필드는 name, isin/cusip/kr_code 또는 source_security_id, shares, market_value, weight, ownership_pct, country, sector, currency, position_key다. weight는 0~100 퍼센트이며 market_value는 명시된 currency의 실제 통화 단위다. 예를 들어 million 단위 파일은 검증 후 기본 통화단위로 정규화해야 한다. ticker만으로 자동 병합하지 않는다. 식별 불확실성은 source-local ID와 review 상태로 남긴다.

자산배분 dataset은 `kind: "allocation"`, mapping 필드 original_category, standard_category, weight, target_weight, market_value, dimension을 사용한다. dimension은 asset/region/sector/listing 등의 원본 구분을 보존한다. GIC와 Temasek의 분류를 NPS에 억지로 맞추지 않는다. PDF 표는 자동 추정 파싱하지 않으며 공식 CSV/XLSX 또는 검토한 추출 JSON을 사용한다.

외부 fetch는 공식 hostname allowlist, HTTPS, 공개 IP 확인, robots 정책, 연결 5초/읽기 20초, 스트림 60초, 25MB 한도를 적용한다. redirect 대상도 재검증한다. 실제 timeout, 403, 구조 변경은 수집 오류로 남고 우회하지 않는다. 문서 링크로 이동하는 사용자 브라우저에는 별도 공식 도메인이 포함될 수 있다.

## 분석 해석

- 첫 관측은 비교 baseline. NEW는 이전에 비교 가능한 공개목록이 있을 때만 발생한다.
- shares_delta / shares_delta_pct / market_value_delta / portfolio_weight_delta_pp는 분리된다.
- shares 미공개이면 수량 증가/감소를 생성하지 않는다. 금액·비중만 달라지면 VALUE_OR_WEIGHT_CHANGED다.
- 부분 공개범위의 이탈은 전량 매도로 단정하지 않는다. full snapshot의 EXIT도 **해당 공개범위 내 제외**다.
- ETF applyQ는 CU당 수량일 수 있으며 전체 펀드 매수량이 아니다. 현금·비주식 항목은 shares로 표시하지 않는다.
- SEC 13F는 분기말 공개 롱 포지션이며 제출 시차가 있다. 공매도·비공개 자산·일부 해외자산은 알 수 없다. 옵션 신고수량은 현물 shares로 저장하지 않는다. 2023-01-03 이전 filing value는 천 달러, 이후는 달러 단위로 처리한다.
- NPS 상세 종목은 공개시차가 크다. 월별 기금규모를 월별 상세 보유량으로 해석하지 않는다.
- NBIM은 반기·연말 공개이며 issuer-level aggregates를 개별 security issue로 추측하지 않는다.
- 공식 corporate_actions에 security_id, effective_date, ratio, source_url을 등록하면 분할 조정 후 수량 delta와 원수량 delta를 같이 보존한다. 분할·합병·ticker 변경을 fuzzy 추론으로 자동 합치지 않는다.
- 5b/20b는 저장된 영업일 관측 수다. 1/3/6/12개월 비교는 월말을 유지하는 달력 비교다. 누락된 일자는 채워 넣지 않고 실제 선택된 두 기준일을 반환한다.
- Consensus는 각 fund 최근 공개시점과 출처를 함께 반환한다. 기관 수와 ETF 수를 구분하고 투자추천 점수는 만들지 않는다. 서로 다른 통화의 평가액 delta를 합산하지 않는다.

## API

모든 경로 앞에 `/stock16-7`을 붙이고 로그인 쿠키가 필요하다.

```text
GET /api/dashboard
GET /api/institutions
GET /api/institutions/:id
GET /api/institutions/:id/holdings?as_of=YYYY-MM-DD&q=...&country=...&sector=...
GET /api/institutions/:id/changes?period=previous|1d|1w|1m|3m|quarter|half|1y|5b|20b
GET /api/institutions/:id/allocation
GET /api/securities?q=...
GET /api/securities/:id
GET /api/securities/:id/institution-holdings
GET /api/etfs
GET /api/etfs/:id/changes
GET /api/gurus
GET /api/consensus?period=1m&minimum=3&mode=held|increased|weight|new&institution=nps&institution=nbim
GET /api/disclosures?since=YYYY-MM-DD&institution=nps
GET /api/rankings?metric=shares_delta|portfolio_weight_delta_pp|market_value_delta&direction=asc|desc&currency=KRW
GET /api/data-status
POST, DELETE /api/watchlist/etf
POST, DELETE /api/watchlist/guru
```

holdings/changes의 fund query로 기관 내 펀드를 선택한다. 정렬 가능한 숫자 필드는 sort 및 order로 지정한다. `format=csv`로 평면 목록을 다운로드하며 수식 injection을 방어한다. 변경 요청은 `X-CSRF-Token` header를 사용한다. 수량과 금액은 정밀도를 지키기 위해 JSON에서 Decimal 문자열이다.

## 저장공간 정책

일별 ETF **정규화 snapshot과 변화량은 영구 보존**한다. 같은 날짜 자료의 재시도, 같은 값, 같은 보유 상태는 재사용한다. 압축 전 byte checksum이 같은 원본은 gzip blob 1개만 남긴다. URL·날짜별 provenance는 별도 기록한다.

ETF 원본은 기준일로 최근 12개월을 전부 보존하고 그 이전은 fund별·월별 **가장 늦은 공개 기준일**의 원본을 대표본으로 보존한다. 월말이 휴일이면 마지막 이용 가능한 영업일이다. 정정본이 같은 대표 기준일에 여러 개면 모두 남긴다. 하나의 blob이 다른 날짜/기관 보존대상에 공유되면 삭제하지 않는다. 삭제해도 source_documents checksum·관측 기록·정규화 보유/변화는 유지된다. NPS/NBIM 등 다른 기관 원본은 기본 장기보존이다.

```bash
.venv/bin/python -m stock16 retention            # dry run
.venv/bin/python -m stock16 retention --apply    # 원본 정책 적용
.venv/bin/python -m stock16 status
.venv/bin/python -m stock16 check-storage
```

`/api/data-status`와 화면은 DB+WAL, raw-data, backup, 데이터 폴더 전체, filesystem 사용률을 표시한다. 디스크 70/80/90%를 넘으면 화면 경고와 app log를 남긴다. webhook 설정 시 임계 등급이 올라갈 때 한 번 전송하며 회복 후 재상승하면 다시 알린다. 전송 실패는 다음 점검에서 재시도한다. 폴더 전체는 data root만 측정하며 checkout/venv/OS 사용량은 filesystem 사용률에 반영된다. macOS APFS는 공간 공유·purgeable 영역으로 도구별 수치가 다를 수 있다.

app log는 WatchedFileHandler + logrotate, Gunicorn은 USR1 reopen을 사용한다. 일간 7개, 10MB maxsize, gzip. debug는 기본 비활성화하고 access log는 비활성화하여 query credential 유출을 줄인다. 만료 세션·로그인 제한 기록은 maintenance에서 청소하고 수집 실행 이력은 최근 365일을 보존한다. 정규화 역사자료는 삭제하지 않는다.

## DB 백업·복원

```bash
.venv/bin/python -m stock16 backup
.venv/bin/python -m stock16 restore /path/to/daily-YYYY-MM-DD.sqlite3.gz --target /path/to/new-restored.sqlite3
```

SQLite online backup → `PRAGMA integrity_check` → gzip → atomic publish. 일간 7개, ISO 주간 4개, 월간 6개가 기본이며 env로 바꿀 수 있다. 같은 파일을 일/주/월 generation이 hard link로 공유한다. 각 주/월에는 마지막 성공본을 남긴다. 복원은 기존 파일을 덮어쓰지 않고 새 복사본만 만든다. 운영 복원은 서비스를 정지하고 검증된 복원본으로 DB를 교체한 뒤 소유권·서비스를 확인한다. WAL/SHM 파일과 실행 중인 DB를 단순 복사하지 않는다.

DB 백업은 raw blob을 포함하지 않는다. 압축 raw 디렉터리는 별도 off-host 백업/동기화가 필요하다. 원본 디렉터리가 없어도 normalized 이력은 DB 복원본에서 조회된다. `.env`는 별도 비밀보관소에서 백업한다. 같은 디스크의 백업은 서버/디스크 장애 복구용 off-host 백업을 대체하지 않는다.

## Oracle Ubuntu ARM64 배포

현재 환경에서는 Oracle 서버에 접속하거나 운영 포트를 확인하지 않았다. 서버 작업용 파일만 제공한다. 기존 앱·nginx site·DB를 삭제하는 명령은 없다.

1. 서버에서 `python3`, `python3-venv`, `git`, `nginx`, `logrotate`를 준비한다.
2. `/var/www/stock16-7`에 main을 clone한다. 기존 폴더가 있으면 내용을 먼저 확인하고 재사용한다.
3. checkout 소유 계정으로 `bash deploy/install.sh`를 실행한다(sudo 권한 필요). setup에서 지정한 초기 비밀번호를 입력한다. env·DB가 있으면 그대로 보존한다.
4. script가 `ss`로 3016~3099 후보를 확인한다. 선택 포트를 `.env`와 nginx `proxy_pass`에 일치시킨다. 기존 env를 재사용할 때는 해당 포트 충돌을 직접 확인한다.
5. `.env`의 DATA_DIR을 `/var/www/stock16-7/data`, SECURE_COOKIE를 1로 확인하고 API 식별 설정을 채운다.
6. [nginx-stock16.conf](deploy/nginx-stock16.conf)의 location을 **기존 HTTPS server block 안에** 포함한다. `proxy_pass` 끝에 slash를 붙이지 않아 `/stock16-7/` 경로를 보존한다. `sudo nginx -t` 성공 후 reload한다.
7. `systemctl status webapp-stock16-7.service`, `systemctl list-timers 'stock16-*'`로 확인한다.

운영 서비스명은 `webapp-stock16-7.service`. loopback에 Gunicorn을 바인딩하며 2 workers × 2 threads, SQLite WAL과 busy timeout을 사용한다. systemd는 전용 stock16 계정·NoNewPrivileges·ProtectSystem·data 디렉터리 쓰기 제한·메모리 제한을 적용한다. HTTPS는 기존 nginx/TLS 설정을 사용한다.

일정은 Asia/Seoul 기준 ETF 평일 17:30/19:30, 기관·공시 08:30/18:30, backup/retention 03:10, 저장공간 15분마다다. 거래소 공휴일을 완전히 모델링한 calendar는 포함하지 않으며 공식 응답 기준일과 checksum으로 중복을 방지한다. timer는 Persistent이며 임의 지연이 추가된다. UI의 다음 예정은 이 기본 timer에 대한 예상값이므로 운영자가 timer를 수정하면 코드 설정도 맞춘다.

업데이트: `bash deploy/update.sh`. backup 후 fast-forward merge만 사용하고 `.env`, DB, raw를 보존한다. tracked 로컬 변경이 있으면 종료한다. `git clean`, hard reset, down으로 데이터를 지우지 않는다. 코드 schema가 변경되는 향후 릴리스에는 사전 backup과 명시적 migration을 준비한다. 이 초기 릴리스는 schema v1이다.

## 테스트

```bash
.venv/bin/python -m pytest -q
node --check stock16/static/app.js
bash -n deploy/install.sh deploy/update.sh
.venv/bin/pip check
```

snapshot import/중복/동일 상태 재사용, NEW/EXIT, 수량과 가격 분리, 분할 근거, 부분 공개, 과거 backfill·정정, ETF 영업일 비교, NPS 월간 배분·단위, NBIM 미공개 수량, 13F 분기·단위·XML, 날짜 구분, auth/401/CSRF/rate limit/logout, malformed CSV/XLSX/JSON, entity 공격, timeout/소스 장애 격리, 빈 자료, raw 보존·checksum·공유 blob, backup 무결성·GFS·복원, disk 알림을 fixture로 검증한다. 모바일 CSS 검증과 별도로 브라우저 QA는 [VALIDATION.md](docs/VALIDATION.md)에 기록한다.

## 공식 근거

- [국민연금 기금운용본부](https://fund.nps.or.kr/main.do)
- [NBIM 전체 투자와 공식 다운로드](https://www.nbim.no/en/investments/all-investments/)
- [NBIM 2026 반기 발표](https://www.nbim.no/en/news-and-insights/reports/2026/half-year-report-2026/)
- [GIC portfolio](https://www.gic.com.sg/our-portfolio/) / [newsroom](https://www.gic.com.sg/newsroom/)
- [Temasek Review](https://www.temasekreview.com.sg/)
- [KODEX 공식 상품 목록](https://www.samsungfund.com/etf/product/list.do)
- [TIGER 공식 홈페이지](https://www.tigeretf.com/)
- [SEC EDGAR API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) / [Form 13F FAQ](https://www.sec.gov/rules-regulations/staff-guidance/frequently-asked-questions-about-form-13f)
- [OpenDART](https://opendart.fss.or.kr/)
