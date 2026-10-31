# stock16 architecture

Python 3.12+ / Flask / Gunicorn / SQLite WAL. Node 빌드 없이 로컬 정적 CSS/JS와 Jinja 화면을 제공한다. Oracle ARM64에서는 Python wheel 또는 순수 Python dependency를 사용한다. SQLite 버전은 window function을 지원해야 한다(3.25+).

## 저장 경계

`collectors → parser/normalizer → model → Repository → SQLite`. HTTP는 Analytics와 Repository를 호출한다. PostgreSQL 이전은 Repository 연결·SQL dialect, schema migration, SQLite online backup 부분을 교체한다. HTTP·정규화 모델과 분리되어 있지만 SQL 자동 이식 도구를 제공하는 것은 아니다.

기관과 펀드를 분리하고, issuer의 CIK와 security의 ISIN/CUSIP/KRX identifier를 별도 정규화한다. ticker 또는 fuzzy company name만으로 병합하지 않는다. NBIM의 issuer aggregate ID는 source-local security로 보존하며 `review` 표시한다. 채권 issuer aggregate를 개별 채권 보유수량으로 표시하지 않는다. NBIM과 KRX/13F의 교집합은 공식 식별 근거를 확보해 identifier를 연결한 종목에 한해 계산된다.

## 불변 보유 상태

1. `holding_values`: security + position discriminator + shares + value + weight + ownership + currency를 canonical Decimal로 바꿔 SHA256 해시한다. 동일 값을 한 번만 저장하며 해시는 32바이트 BLOB이다.
2. `holding_sets`: 정렬된 보유값 해시의 해시로 포트폴리오 상태를 식별한다.
3. `holding_members`: 정수 `set_id`, `value_id`만 참조하는 WITHOUT ROWID membership. 긴 해시가 모든 연결행에 반복되지 않는다.
4. `portfolio_snapshots`: 날짜와 출처 메타데이터 + 상태 참조만 기록한다. 같은 구성이 여러 날 이어지면 상태·membership을 재사용한다. 같은 날 정정자료는 새로운 불변 revision이다.
5. `holding_changes`: 이전 유효 snapshot과 비교해 달라진 position만 저장한다. 수량·비중·평가액이 모두 같으면 별도 변화행이 없다. 완전한 baseline은 상태와 membership에 있으므로 delta chain의 길이에 의존하지 않고 임의 날짜 복원이 가능하다.

매일 가격이 바뀌면 새로운 보유값이 생긴다. 원본 전체행을 무조건 압축 DB에 저장하는 방식이나 모든 날짜를 하나의 delta chain으로 연결하는 방식은 사용하지 않았다. 읽기 안정성과 불변 이력을 유지하면서 같은 값/상태의 중복과 긴 reference 비용을 줄인다. 보유 숫자는 반올림하여 dedup하지 않는다.

`effective_snapshots`는 기준일마다 가장 늦은 공시 revision을 선택한다. 공시일 미제공인 ETF 원본은 NULL로 보존하고 수집 순서로 revision을 선택한다. 재수집 checksum은 idempotent하므로 과거 revision을 중복 수집해도 유효 이력이 뒤집히지 않는다. 과거 자료 추가/정정 시 바로 이전 effective → 현재, 현재 → 바로 다음 effective의 최대 두 구간만 재계산한다. 비유효 과거 revision 추가는 기존 변화량을 건드리지 않는다. 전체 rebuild는 debug/검증용으로 남긴다. 임의 기간 비교와 Consensus는 공유 period selector로 고른 실제 snapshot 두 개를 직접 비교한다. 원본 snapshot은 변경하지 않는다.

## 날짜·해석

기준일, 발표일, 수집일은 분리한다. 발표일이 없는 공식 API는 NULL이며 수집일이나 기준일로 대체하지 않는다. 초기 관측은 baseline이며 NEW가 아니다. 전체 공개 범위 내 제거는 EXITED, 부분 공개목록 이탈은 LEFT_DISCLOSED_LIST다. 수량 미공개는 VALUE_OR_WEIGHT_CHANGED로만 분류한다. 가격 변화로 비중이 올라도 INCREASED가 아니다. 공식 근거가 등록된 corporate_actions의 분할비율을 적용한 변화와 원수량 변화량을 함께 남긴다. 분할/합병을 수량 비율만으로 추측하지 않는다.

## 원본과 운영 데이터

원본 비압축 byte SHA256으로 blob을 구분하고 gzip 1개를 저장한다. URL·기관·날짜별 `source_observations`는 같은 blob을 참조한다. HTML은 메모리에서만 파싱하고 필요한 구조화 JSON 추출본만 압축 보존한다. 추출본은 Official-derived다. HTML 원문 전체가 보존되지 않으므로 이후 사이트 수정에 대한 완전한 HTML 재현은 제공하지 않는다.

수집은 systemd oneshot + timer다. flock으로 scheduler 중복 실행을 막고 한 소스 실패가 다음 collector를 막지 않는다. 수집 상태는 success / no_new_data / needs_configuration / error로 기록한다. 오류 문자열과 credential URL은 저장하지 않는다. 구조 변경/추가보고/불명확 식별자는 검토가 필요하다.

SQLite online backup, 무결성 검사, gzip, 일/주/월 세대 순환을 제공한다. 같은 backup은 hard link로 세대 간 공유한다. 보존·수집의 raw 파일 작업은 flock, 임시파일과 atomic rename으로 보호한다. 원본 보존 삭제는 normalized 데이터와 source checksum을 지우지 않는다.

## v2 migration과 수량 의미

기존 v1 schema를 생성 기반으로 유지하고 `migrations.py`의 버전별 SQL로 업그레이드한다. online 사전 백업 뒤 BEGIN IMMEDIATE/COMMIT으로 새 column·index·상태 table을 추가하며 실패는 전체 rollback한다. schema_versions는 완료한 버전만 기록한다. 초기화는 파일 잠금으로 직렬화한다. 운영 snapshot과 holding value/set hash는 유지한다.

quantity_type은 snapshot 단위의 `shares` / `pdf_per_cu_quantity` / `unavailable`이다. 기존 ETF snapshot은 migration에서 PDF 의미로 주석을 붙인다. API quantity 별칭은 저장된 기존 값을 사용하므로 중복 수량 column을 만들지 않는다. PDF 수량과 실제 주식 수 사이의 비교는 NULL delta와 QUANTITY_SEMANTICS_CHANGED로 분리한다. 기관/ETF 수량의 합계나 매수 추정치는 만들지 않는다.

source_status는 기관·펀드·구성요소별 마지막 점검·성공·예정·상태를 기록한다. aggregate collector의 일부 실패가 정상 ETF를 실패로 덮지 않는다. history_review_queue는 발표일 미검증 NBIM 자료나 검토가 필요한 filing을 분석에 투입하지 않고 표시한다. SEC 목록 cache는 원본 역사자료와 독립적인 bounded gzip cache다.

기본 GFS는 3/2/3이며 명시한 환경 설정이 우선한다. 매일 maintenance는 bounded PRAGMA optimize와 WAL checkpoint만 실행한다. 체크포인트가 사용 중이면 busy 결과를 반환한다. VACUUM은 수동 명령이며 공간 검사·backup이 선행한다. 정규화 자료는 보존한다.


## v3/v4 ETF 분류와 수집 queue

v3는 fund FK를 가진 etf_profiles와 과거 기본 패시브 watchlist 중지만 추가한다. v4는 collection_jobs와 queued/running partial unique index, 실행 origin/requested_at/job_id/details_json을 추가한다. 기존 데이터와 hash를 수정하지 않는다. migration 전체는 사전 gzip backup 후 하나의 transaction으로 수행한다.

HTTP POST는 고정 allowlist 검증과 BEGIN IMMEDIATE 아래 중복 검사/INSERT만 수행한다. worker는 별도 프로세스로 collector.lock flock을 획득한 뒤 job을 claim한다. 자동 systemd의 외부 flock은 그대로이고 CLI에 내부 잠금 획득 또는 신뢰된 systemd 전용 --lock-held를 사용한다. 웹은 이 인수를 받지 않는다. worker의 lifetime lock은 중복 worker 실행을 차단한다. 전역 flock을 얻은 뒤 남은 running job은 중단 기록으로 마무리하고 이미 저장한 정규화 자료를 보존한다. 사용자 입력으로 프로세스를 시작하지 않는다.

미국 전체 펀드 보고수량은 fund_reported_quantity이며 한국 PDF의 CU당 구성수량과 분리한다. latest-only 소스는 날짜별 API처럼 반복 호출하지 않는다. 원본 bytes가 매번 달라지는 XLSX/JSON은 정규화 set·메타가 동일하면 불필요한 revision을 만들지 않되 raw checksum 관측은 따로 보존한다. 원본 정책은 source_type 문자열 대신 funds.kind=ETF에 적용한다.
