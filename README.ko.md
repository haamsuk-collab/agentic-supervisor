# agentic supervisor

`agentic-supervisor`는 미션의 모든 숨은 인수 검사(acceptance check)가 통과할 때까지 자율 코딩 에이전트(`omp`)를 이전 기억이 없는 새 세션으로 반복 실행합니다. 각 반복은 격리된 Linux 계정과 `systemd` 샌드박스(sandbox)에서 실행되어 검사 스크립트나 감독자 상태, 다른 사용자의 프로세스를 읽을 수 없으며 감독자는 모든 단계를 추가 전용(append-only) 해시 체인 원장(ledger)에 기록합니다. 미션이 끝나면 감독자는 언어 모델을 호출하지 않고 원장만으로 결정론적(deterministic) 보고서와 Telegram 브리핑을 생성합니다.

## 정직성 보장 (Honesty Guarantee)

`agentic-supervisor`에서 완료란 감독자가 직접 돌린 숨은 검사가 종료 코드 0으로 끝났다는 뜻입니다("done means a hidden check, run by the supervisor, exited 0"). 작업자(worker)의 주장이나 커밋 메시지, 메모는 절대 증거로 인정하지 않습니다. 조건 하나가 달성으로 인정받으려면 해당 검사가 작업 전 빈 폴더(baseline)에서 실패하고 작업자 산출물 스냅샷(snapshot)에서 종료 코드 `0`을 반환한 뒤 작업자가 수정한 파일을 0바이트로 비운 카나리아(canary) 복사본에서 다시 실패해야 합니다. 이어서 변이 테스트(mutation testing)를 통과하거나 자동 검사 강화를 거치고 인용된 꼼수 코드가 실제 파일에 그대로 있는지 대조하는 교차 벤더(cross-vendor) 꼼수 리뷰까지 통과해야 합니다.

```mermaid
flowchart LR
    TG["Telegram / CLI Request"] --> PL["Planner (agplan)<br/>brief → design → checks → critique"]
    PL --> WK["Worker (agwork)<br/>fresh omp session per iteration"]
    WK --> HC["Hidden Checks<br/>DynamicUser sandbox on snapshot"]
    HC -->|exit != 0| WK
    HC -->|all exit 0| CN["Canary<br/>truncate changed files to 0 B"]
    CN -->|still passes: canary_failed| WK
    CN -->|fails as required| MU["Mutation Score<br/>up to 6 AST mutants / 10 min"]
    MU -->|"< 50% killed (n >= 3): strengthen checks"| HC
    MU -->|holds| RV["Cheat Review (agplan)<br/>cross-vendor verbatim quote check"]
    RV -->|verified veto (max 2)| WK
    RV -->|clean| RP["Report & Telegram Card<br/>deterministic briefing from ledger"]
```

## 요구 사항 (Requirements)

- 운영체제: `systemd` 기반 Linux (RHEL/Rocky Linux 8–9 또는 Debian/Ubuntu)
- 권한: 설치, 서비스 관리, 감독자 실행에 필요한 `root` 권한
- 런타임: `/usr/bin/python3.12`(외부 서드파티 패키지 없이 Python 3.12 표준 라이브러리만 사용) 및 `git`
- 에이전트 바이너리: `omp` 실행 파일(설치 스크립트가 `/opt/omp/omp`에 고정)
- 모델 접근: `127.0.0.1:4000`의 `omp-auth-gateway`가 제공하는 소유자 본인의 모델 공급자 로그인과 `agwork`·`agplan`용으로 분리된 클라이언트 토큰

## 빠른 설치 (Quick Install)

`root` 권한으로 `omp` 바이너리 경로를 지정해 `install.sh`를 실행한 뒤 설정을 검증합니다.

```bash
sudo ./install.sh --omp /path/to/omp [--timezone Asia/Seoul --language ko --telegram]
agentic config check
```

기본값으로 `install.sh`는 `/etc/agentic/agentic.toml`에 `timezone = "UTC"`와 `language = "en"`을 기록하고 격리된 시스템 계정(`agwork`, `agplan`, `agbot`)을 생성하며 `/opt/agentic`과 `systemd` 유닛을 설치합니다. 또한 `/proc`를 `hidepid=2`(`procview` 그룹)로 마운트하고 대화형 사용자의 가이던스 파일(`/home/agent/.omp/agent`)을 `chattr +i`로 잠급니다. 한국어 출력과 KST 시각을 쓰는 한국 서버나 이전 단계에서 업그레이드하는 서버는 `/etc/agentic/agentic.toml`의 `[display]` 섹션에 `language = "ko"`와 `timezone = "Asia/Seoul"`을 설정합니다(`install.sh --language ko --timezone Asia/Seoul` 또는 `--timezone Asia/Seoul --language ko` 실행). `install.sh` 실행이 끝나면 [`docs/OPERATIONS.md`](docs/OPERATIONS.md) 안내에 따라 `omp-auth-gateway` 클라이언트 토큰을 설정합니다.

## 첫 미션 실행 (Running Your First Mission)

`root` 권한으로 자연어 요청 파일(UTF-8, 10~20,480바이트)을 지정해 미션을 시작하거나 개인 Telegram 봇에 일반 텍스트 메시지를 보낸 뒤 확인 버튼을 누릅니다.

```bash
sudo agentic mission new request.md
sudo agentic mission status <id>
sudo agentic report <id>
```

`agentic mission status <id>`는 유닛 상태와 최근 원장 이벤트 5건, 렌더링된 보고서(`<state>/missions/<id>/report.md`)를 출력합니다. `agentic report <id>`는 해시 체인 원장에서 직접 `report.md`를 다시 생성합니다. `agentic verify <id>`는 원장의 SHA-256 해시 체인, 각 통과를 마지막 검사 스냅샷에 묶는 증거(proof), `chattr +a` 추가 전용 inode 플래그를 검증하고 작업 폴더가 그 스냅샷 이후 그대로인지, 바뀌었는지(경로 최대 10개), 없어졌는지 알려 줍니다.

## 테스트 및 저장소 위생 검사 (Testing and Hygiene Scan)

Python 3.12로 단위 테스트 스위트와 저장소 위생 검사를 실행합니다.

```bash
make test
/usr/bin/python3.12 tests/repo_scan.py
```

`make test`는 로컬 검사 모드와 스텁(stub) 작업자·계획자 프로세스(`tests/stub_worker.py`, `tests/stub_planner.py`), `127.0.0.1`에서 동작하는 프로세스 내 가짜 Telegram 서버를 사용해 모든 단위 테스트를 오프라인으로 실행합니다(테스트당 타임아웃 60초). `python3.12 tests/repo_scan.py`는 추적 중인 파일에서 호스트 이름, 루프백(loopback)이 아닌 IPv4 주소(`NOTICE`에 적은 서드파티 폴더 안은 제외), Telegram 토큰, 숫자형 채팅 ID, API 키, `/home/agent` 외의 개인 홈 경로를 스캔하고 커밋 작성자 이름과 이메일에서 호스트 이름과 금지 문자열을 찾습니다.

## 보장하지 않는 것 (What It Does Not Guarantee)

- 검사는 테스트한 범위만 증명합니다: 검사를 통과했다는 것은 해당 명령이 스냅샷에서 종료 코드 `0`으로 끝났고 파일을 비운 카나리아 복사본에서 실패했으며 표집된 AST 변이와 꼼수 리뷰를 통과했다는 뜻일 뿐 테스트 범위 밖의 버그가 없음을 증명하지는 않습니다.
- 모델 공급자는 통제 범위 밖에 있습니다: 외부 모델 공급자의 가용성, 호출 한도(rate limit), 출력 품질, 데이터 보관 정책은 `omp-auth-gateway` 뒤에 연결된 각 공급자에 달려 있습니다.
- `root`와 호스트 커널은 신뢰 대상입니다: 상태 디렉터리(`/var/lib/agentic`), 숨은 검사, 원장은 모두 `root` 소유이므로 악의적인 `root` 사용자(또는 `--agent-sudo`로 `NOPASSWD: ALL` sudo 권한을 받은 대화형 계정)는 검사를 읽거나 `chattr +a`를 해제할 수 있습니다.
- AI 메모는 검증되지 않습니다: 계획자의 가정이나 작업자가 `DECISIONS.md`에 적은 내용은 보고서의 미검증 `AI notes`(`AI가 정한 것`) 섹션에 격리되며 감독자가 검증하지 않습니다.

## 문서 및 프로젝트 파일 (Documentation and Project Files)

상세 문서는 영어로 작성되어 있으며 영어판 README는 [`README.md`](README.md)에서 확인할 수 있습니다.

- [`README.md`](README.md): 영어판 개요 및 빠른 시작 안내 (English version)
- [`README.ko.md`](README.ko.md): 한국어판 개요 및 빠른 시작 안내
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): 패키지 모듈, 계정 구조, 계획 및 반복 데이터 흐름, 원장 레코드, 스풀(spool) 프로토콜, `systemd` 유닛, `i18n`
- [`docs/HONESTY.md`](docs/HONESTY.md): 모든 부정행위 방지 메커니즘, 차단하는 공격 유형, 확인 방법과 한계
- [`docs/SECURITY.md`](docs/SECURITY.md): 위협 모델, 계정 경계, 범위 외 위협, 취약점 제보 방법
- [`docs/CONFIG.md`](docs/CONFIG.md): `/etc/agentic/agentic.toml` 섹션, 키, 타입, 기본값, 환경 변수 재정의
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md): `omp-auth-gateway` 설정, Telegram 봇 설정, 일일 카나리아 및 상태 점검 작업, 백업, 업그레이드, 작업 공간 이전
- [`docs/specs/`](docs/specs/): 설계 이력으로 보존된 단계별 명세(`SPEC.md`~`SPEC-G.md`)
- [`DECISIONS.md`](DECISIONS.md): Stage B–G 동안 기록한 구현 결정 사항
- [`CHANGELOG.md`](CHANGELOG.md): 버전 `1.0.0`(Stage B–G) 릴리스 이력
- [`LICENSE`](LICENSE) 및 [`NOTICE`](NOTICE): MIT 라이선스 및 서드파티 저작권 고지
