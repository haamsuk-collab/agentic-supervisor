# Insane Search — quick reference (part 2 of SKILL.md)

## 빠른 참조 — Phase 0 명령어

> **먼저 이걸 기억하라: Reddit/X/YouTube/Threads는 이제 engine이 자동 처리한다.**
> `INSANE_OBSERVATIONS_DIR=$HOME/.insane-search/observations /usr/bin/python3.12 -m engine "<URL>"` 하나면 Phase 0 라우터(`engine/phase0.py`)가 **격자보다 먼저** 공식 경로를 시도한다 —
> Reddit→`.rss`, X 트윗→`tweet-result`/oEmbed, X 프로필→syndication, YouTube→`yt-dlp`, Threads 포스트→인라인 `video_versions`.
> 아래 수동 스니펫은 디버그/참조용이며 trace에 `phase=phase0`로 기록된다.
> (실측 주의: Reddit `.json`+모바일UA·`syndication-timeline`은 흔히 403/429라 plain `curl`은 신뢰 불가 — engine이 curl_cffi 지문으로 접근한다.)

```bash
# ★ 거의 모든 경우 이거면 됨 (Phase 0 자동 + 실패 시 격자→Playwright 에스컬레이션)
INSANE_OBSERVATIONS_DIR=$HOME/.insane-search/observations /usr/bin/python3.12 -m engine "<URL>"

# 범용 웹 (Jina Reader — 일반 HTML만, WAF 사이트엔 무효)
curl -s "https://r.jina.ai/{URL}"

# yt-dlp — 1,858 사이트 미디어 메타데이터 / 자막
yt-dlp --dump-json "URL"
yt-dlp --write-sub --write-auto-sub --sub-lang "en,ko" --skip-download -o "/tmp/%(id)s" "URL"

# Threads 영상 — yt-dlp 미지원, engine이 서명 CDN URL 추출 (URL은 만료되니 즉시 다운로드)
INSANE_OBSERVATIONS_DIR=$HOME/.insane-search/observations /usr/bin/python3.12 -m engine "https://www.threads.com/@{handle}/post/{shortcode}"   # content = {"post_code","video_urls":[...]}
curl -sL -o /tmp/threads.mp4 "{video_urls[0]}"

# Reddit — .rss (curl_cffi 지문 필요; plain curl은 TLS로 403)
python3 -c "from curl_cffi import requests as r; print(r.get('https://www.reddit.com/r/{sub}/.rss', impersonate='safari').text[:2000])"

# X/Twitter — 개별 트윗(가장 안정적): tweet-result / oEmbed
python3 -c "from curl_cffi import requests as r; print(r.get('https://cdn.syndication.twimg.com/tweet-result?id={TWEET_ID}&token=a', impersonate='safari').text)"
# X 프로필 타임라인 (rate-limit 변동 — engine이 재시도) / 키워드: WebSearch(site:x.com {kw})→tweet-result
curl -sL "https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"

# Hacker News
curl -sL "https://hacker-news.firebaseio.com/v0/topstories.json?limitToFirst=10&orderBy=%22%24key%22"
```

> 커버리지 회귀 점검: `python3 tests/coverage_battery.py` — 플랫폼별 전수 경로 pass/fail + 썩은 예시 자동 적발.

## No-Site-Name Rule

`engine/**`, `waf_profiles.yaml`, `engine/templates/**` 파일에는 **특정 사이트의 도메인/URL/셀렉터/브랜드명을 하드코딩하지 않는다**.

### 금지

- `"coupang.com": {...}` 같은 사이트별 레지스트리 엔트리
- `if "coupang" in url: ...` 같은 도메인 분기
- WAF 프로파일 `notes`에 특정 사이트 이름이나 경험적 byte 크기 박제

### 허용

- `SKILL.md` / `references/*.md`의 **설명 텍스트**에 사이트 이름 예시 (독자 이해용)
- `Phase 0` 공식 API 인덱스 (플랫폼이 공식 공개한 엔드포인트)
- `observations/*.jsonl` 로그 (append-only 관측 데이터 — 코드 경로에 영향 없음)
- 호출자가 제공하는 `success_selectors`, `user_hint` (현재 호출에만 유효)

### 경계 사례 판단 기준

> "이 엔트리가 다른 사이트에서도 같은 WAF를 쓰면 일반적으로 유효한가?" → YES면 `waf_profiles.yaml`, NO면 runtime hint.

### 새 사이트가 안 뚫릴 때

1. 먼저 `result.trace`에서 어느 phase가 실패했는지 확인
2. 사용자의 `user_hint`로 1회 재시도
3. 반복 성공 패턴이 관측되면 `observations/`에 로그 (아직 자동 기록 없음 — 수동)
4. 3회+ 반복 확인되고 **동일 WAF를 쓰는 다른 사이트에도 유효**하면 `waf_profiles.yaml` 해당 프로파일의 `tls_impersonate_candidates` / `url_transform_order`를 튜닝 (사이트명 절대 넣지 않음)
5. 여전히 안 되면 새 WAF 프로파일 후보 검토 (예: DataDome 세부화, Kasada 등)

## 관련 문서 (references/) — 언제 무엇을 읽을지

이 섹션은 **참조 파일 선택 가이드**다. 문제가 생겼을 때 어떤 `references/*.md`를 열어야 할지 결정하는 기준으로 쓴다. Claude는 필요할 때만 해당 파일을 `Read`하고, 선제적으로 전부 읽지 않는다.

### A. Engine 확장·진단 (하네스 내부)

| 파일 | 언제 읽는가 | 무엇을 다루는가 |
|------|-------------|-----------------|
| [`tls-impersonate.md`](references/tls-impersonate.md) | curl_cffi 격자가 전부 `challenge`/`blocked`로 끝날 때, 새 impersonate 타겟을 `waf_profiles.yaml`에 추가할 때 | curl_cffi로 Safari/Chrome/Firefox TLS(JA3/JA4) 지문 복제하는 방법, WAF(Akamai/Cloudflare/F5 등)별 최적 타겟 조합, 임퍼소네이션 타겟 버전 목록, `tls_impersonate_avoid`의 실증 근거 |
| [`playwright.md`](references/playwright.md) | engine이 Playwright fallback으로 넘어가는데 MCP/Local Chrome 중 어디로 갈지 확인 필요할 때 | Approach 1 (`mcp__playwright__*` — Cloudflare급 챌린지), Approach 2 (Local Node + `channel:'chrome'` + stealth — Akamai Bot Manager급), 템플릿 파라미터 규격 |
| [`fallback.md`](references/fallback.md) | `verdict`가 애매하거나 Phase 전환 타이밍 결정 필요할 때 | engine의 Phase 0→1→2→3 에스컬레이션 원칙, 응답 성공/실패 판정 기준 세부, 각 Phase 종료 조건 |
| [`metadata.md`](references/metadata.md) | 본문 전체를 못 가져왔지만 제목·요약·가격·저자 같은 핵심만이라도 필요할 때 | OGP 메타 태그, JSON-LD (Schema.org), Twitter Card 파싱, 구조화 데이터 추출 패턴 |

### B. 경량 대안 (engine 말고 다른 도구가 나은 상황)

| 파일 | 언제 읽는가 | 무엇을 다루는가 |
|------|-------------|-----------------|
| [`jina.md`](references/jina.md) | WAF 없는 일반 웹(블로그·뉴스·Wiki)의 깨끗한 마크다운 추출 필요할 때 | `r.jina.ai/URL` 한 줄로 Puppeteer 기반 JS SPA 렌더링, 마크다운 변환, 무료 500 RPM, API 키 불필요 |
| [`cache-archive.md`](references/cache-archive.md) | 원본 사이트가 차단됐지만 과거 스냅샷으로라도 접근 필요할 때 | Wayback Machine CDX API, archive.today, AMP Cache (Google Cache는 2024-07 종료됨) |
| [`rss.md`](references/rss.md) | 뉴스·블로그·커뮤니티의 시계열 업데이트를 구조화해 받고 싶을 때 | RSS/Atom 자동 발견, 피드 파싱, 인증 불필요 — 가장 깔끔한 시계열 데이터 소스 |

### C. 플랫폼별 공식/공개 API (Phase 0 인덱스와 연결)

| 파일 | 언제 읽는가 | 무엇을 다루는가 |
|------|-------------|-----------------|
| [`json-api.md`](references/json-api.md) | Reddit/Wikipedia/HN/npm/PyPI 등 **URL 변형만으로** JSON/피드를 주는 사이트 | Reddit Atom/RSS(`.rss`) 대체 경로 + score·댓글용 OAuth(`.json`은 WAF 차단), HN Firebase, Algolia Search, Wikipedia REST, npm/PyPI Registry API |
| [`public-api.md`](references/public-api.md) | Bluesky/Mastodon/arXiv/Stack Overflow/CrossRef/GitHub/OpenLibrary/Wayback 공식 API 사용 시 | 인증 없이 쓰는 공식 공개 REST/AT/Atom API 엔드포인트, 요청 형식, 공통 파라미터 |
| [`twitter.md`](references/twitter.md) | X/Twitter 접근 — 프로필 타임라인, 특정 트윗, 키워드 검색 | `syndication.twitter.com` 타임라인, oEmbed 개별 트윗, 검색은 WebSearch로 URL 확보 후 oEmbed |
| [`naver.md`](references/naver.md) | 네이버 블로그·뉴스·증권·검색 접근 | 서비스별 대체 접근(블로그는 `m.blog.naver.com` 변환, 증권은 비공식 JSON, 검색은 `search.naver.com`), 한글 검색 쿼리 패턴 |
| [`media.md`](references/media.md) | YouTube/Vimeo/Twitch/TikTok/SoundCloud 등 미디어 메타·자막·오디오 필요 시 | `yt-dlp --dump-json` 기반 1,858개 사이트 커버, 자막 다운로드(`--write-sub`), 포맷 선택, 라이브/팟캐스트 |

### D. Engine 코드 직접 읽을 때

| 파일 | 언제 읽는가 |
|------|-------------|
| `engine/phase0.py` | Phase 0 공식-API 라우터 (Reddit/X/YouTube 자동 경로). 플랫폼·경로 추가 시. bias_check 면제 파일(R5 sanctioned) |
| `engine/fetch_chain.py` | 체인 단계 로직·`Attempt`/`FetchResult` schema·`untried_routes`/`must_invoke_playwright_mcp` 실패게이트 |
| `engine/validators.py` | 4-계층 검증 세부 (Verdict 분류, 챌린지 마커 목록) |
| `engine/waf_detector.py` | WAF 랭킹 감지 알고리즘, `_LAST_LOAD_ERROR` 처리 |
| `engine/waf_profiles.yaml` | 프로파일별 detectors·tls_candidates·capabilities_needed |
| `engine/url_transforms.py` | URL 변환 규칙 추가할 때 |
| `engine/executor.py` | Playwright MCP vs local capability 매칭 로직 |
| `engine/templates/*.js` | Playwright 템플릿 튜닝 (warmup, reload, devices) |
| `engine/bias_check.py` | 편향 린터 규칙 — brand denylist, URL_PATTERN, excluded dirs |
