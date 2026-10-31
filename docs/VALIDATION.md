# 검증 기록 · 2026-10-31 KST

## 자동 검증

- Python 3.13, macOS ARM64 로컬 환경: pytest **56 passed**.
- JS: `node --check stock16/static/app.js` 성공.
- 배포 shell: `bash -n deploy/install.sh deploy/update.sh` 성공.
- 설치 dependency: `pip check` 충돌 없음.
- 외부 통신 없이 통과하는 synthetic fixtures. SEC end-to-end mock은 두 분기 filing discovery → XML → snapshot → diff → replay까지 검증.
- DB online backup·무결성 검사·gzip 복원과 일/주/월 세대 보존 검증.
- raw 만료 뒤에도 과거 normalized holdings와 delta 조회 유지 확인.

## 공식 소스 실수집

- NPS: 공식 홈페이지의 최근 월말 배분과 기금공시 성공. 기준일과 공개일을 별도로 파싱.
- NBIM: 2025-12-31, 2026-06-30 issuer aggregate와 NAV, 주식·채권 공식 CSV 수집·비교 성공. shares 추정 없음.
- ETF: 현행 공식 카탈로그로 코드/상품ID를 확인한 KODEX 10개 full PDF JSON 수집 성공. cash는 shares에서 제외. unknown 발표일은 NULL.
- GIC: 공식 뉴스룸 8개 기사에서 날짜·제목·원문 발췌 수집 성공. HTML 전체 영구 보관 없음.
- 동일 자료 재수집: NPS/NBIM/GIC 신규 0, ETF 신규 0. TIGER 다운로드 설정 필요를 partial로 표시.
- Temasek/TIGER 공식 사이트: 검증 환경에서 403, 우회하지 않음. SEC 실제 운영자 User-Agent와 DART key 미설정: needs_configuration.
- 실제 raw retention dry run과 DB backup 성공. 로컬 filesystem 90% 초과 경고 확인. 이는 Oracle 서버 측정값이 아니다.

## 브라우저 QA

- 실제 초기 서버 전용 비밀번호로 로그인, 세션 쿠키, 인증 후 대시보드 진입 확인. 비밀번호는 코드/문서에 포함하지 않음.
- Desktop 1440×1000: 대시보드, NPS, ETF, Gurus, 순위, Consensus, Asset Allocation, Timeline, 저장공간, 추적대상 설정 이동 성공.
- Mobile 390×844: 대시보드, ETF, 배분, Timeline, 저장공간에서 document scrollWidth=390. 표 안에서만 필요한 수평 스크롤.
- KODEX 200 상세 구성종목과 100개 단위 페이지 표시 확인.
- 빠른 메뉴 이동의 응답 경쟁을 AbortController로 해결하고 재확인.
- 콘솔 오류/경고 0 (최종 검증 navigation). 인증 API 접근·CSRF·로그아웃 폐기·rate limit은 자동 테스트로 확인.
- 대시보드 실제 수집 화면은 프로젝트 상위 outputs/stock16-preview.png에 저장. Git에는 운영 데이터·화면을 포함하지 않음.

## 검증 한계

Oracle Ubuntu ARM64 실서버 접속, 실제 포트 충돌, systemd 실행, nginx TLS 연결은 수행하지 않았다. 배포 파일은 shell 문법과 코드 경로를 검토했으며 서버 운영 검증은 별도다. GitHub Actions는 push 뒤 실행되며 로컬 통과와 구분한다. 모든 기관의 상세 보유 자동수집이 무설정으로 가능하다는 의미가 아니다. 기관별 수집 범위와 필요한 공식 mapping은 README_KO.md에 명시했다.
