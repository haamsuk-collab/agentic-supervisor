# Supervisor Korean Report & Telegram Template Rules

**CRITICAL:** Every supervisor template must put the outcome on line 1, use fixed plain-Korean phrases without translationese, attach measurement conditions to every number, and format all human-facing timestamps as `YYYY-MM-DD HH:MM:SS KST`.

These rules govern `agentic/report.py`, `agentic/notify.py`, `agentic/admin.py`, `agentic/telegram.py`, and `agentic/display.py` in `~/work/agentic-supervisor`. Because the owner reads reports and notifications on a phone over Telegram and Termius SSH, every template must be scannable on a narrow screen and strictly separate supervisor-verified facts from unverified AI notes.

---

## 1. Conclusion First (두괄식 구조와 좁은 화면 가독성)

1. **First line = status badge + event/outcome + mission ID.**
   - Telegram notifications (`notify.py`) and inbox replies (`SPEC-D.md` §3) must state what happened and which mission it affects on line 1:
     - `✅ 미션 완료: <id>` / `⚠️ 미션 일부 완료: <id>` / `⏱️ 미션 시간 초과: <id>` / `❌ 계획 실패: <id>`
     - `🚀 미션 시작: <id> — 계획을 세우는 중입니다`
     - `🧭 계획 완료: <id> — 완료 조건 <N>개[, 제외 <M>개]. 작업을 시작합니다.`
     - `⏹️ 멈췄습니다: <id>`
   - On `mission_finished`, line 2 gives the criterion counts (`✅ <done> · ❌ <failed> · ⚠️ <unverifiable>`) and line 3 gives elapsed time (`걸린 시간: <h>시간 <m>분`) before appending `report.md`.
2. **Report header and section order (`report.py`).**
   - `report.md` opens with the mission ID, goal, optional request preview (≤300 chars), a single status line (`상태: <status>      걸린 시간: <h>시간 <m>분      반복: <n>회`), and the model ladder used (`쓴 모델: <model> (<thinking>) → ...`).
   - Sections are ordered by epistemic certainty:
     1. `## ✅ 된 것 (감독자가 직접 검사해서 통과)` — criteria whose check exited 0 in the supervisor's own run.
     2. `## ❌ 안 된 것` — criteria still failing, given up, or stopped by time, plus plan failures.
     3. `## ⚠️ 확인 불가` — red-baseline invalid checks, dropped criteria, failed reviews, and model-outage wait time.
     4. `## 📝 AI가 정한 것 (AI가 쓴 기록, 검증 안 됨)` — planner assumptions and worker `DECISIONS.md` lines.
3. **Line splitting on Telegram (`telegram.py`).**
   - Messages over `4,000` characters must split only on `\n` line boundaries (`split_message`) so no sentence, timestamp, or criterion line is cut mid-line on a phone screen.

---

## 2. Fixed Phrases in Plain Korean (고정 문구와 번역투·공문서투 금지)

1. **Use plain, active Korean verbs instead of Sino-Korean bureaucratic nouns (`수행/진행/실시/발생/전환/불가`).**
   - Templates are deterministic code strings, never free-form LLM prose. When adding or editing a template string, follow the canonical phrase table:

| Event / Context | Required Fixed Phrase | Forbidden AI / Bureaucratic Phrasing |
|---|---|---|
| Status labels (`STATUS_TEXT`) | `완료` · `일부 완료` · `중단(시간 초과)` · `계획 실패` · `진행 중` | `성공적으로 완료됨` · `부분적 완료 상태` · `타임아웃에 의한 중단` |
| Model escalation (`escalated`) | `⬆️ 더 강한 모델로 바꿈: <id> — <from> → <to>` | `상위 모델로의 에스컬레이션을 수행함` |
| Worker outage (`worker_unavailable`) | `⏸️ 모델을 쓸 수 없어 기다리는 중: <id> — <m>분째, 이유: <reason>` | `모델 가용성 이슈로 인해 대기 진행 중` |
| Outage summary in report | `- 모델을 쓸 수 없어 기다린 시간: <h>시간 <m>분` | `- 모델 장애로 인해 소요된 대기 시간` |
| Review veto (`review_veto`) | `🔍 꼼수 리뷰가 문제를 찾음: <id> — 다시 작업합니다` | `리뷰어에 의해 문제가 탐지되어 재작업을 실시합니다` |
| Review failed (`NO_REVIEW_LINE`) | `- 리뷰를 하지 못함` | `- 리뷰 프로세스 수행 실패` |
| Red-baseline invalid check | `작업 전에도 통과하는 검사라서 증거가 되지 못함` | `사전 테스트에서 통과되어 유효성 검증 불가` |
| Dropped criterion | `믿을 만한 검사를 만들지 못해 제외함: <problem>` | `신뢰성 있는 검증 생성이 불가능하여 제외됨` |
| Supervisor unit crash | `💥 감독자가 비정상 종료됨: <id> — 자동으로 다시 시작합니다` (or `다시 시작하지 않습니다`) | `감독자 프로세스의 비정상 종료가 발생함` |
| Missing report / mission / ledger | `아직 보고서가 없습니다` · `없는 미션입니다: <id>` · `원장이 손상됨` | `해당 보고서가 존재하지 않습니다` |

2. **No translationese or chatbot wrappers.**
   - Template strings must never contain `물론입니다`, `도움이 되셨기를`, `~에 있어서`, `~하는 것이 가능하다`, `~되어진다`, `~에 의해`, `~에서의`, or commas immediately after connective endings (`-고,`, `-며,`, `-지만,`).
3. **Semantic status emojis only.**
   - Use only the single leading status icon assigned to each event type (`✅`, `❌`, `⚠️`, `⏱️`, `🧭`, `⬆️`, `⏸️`, `🔍`, `💥`, `🚀`, `⏹️`, `🛠️`, `🧪`, `📏`, `🏳️`, `🏁`, `📝`). Never add decorative trailing emojis (`✨`, `🔥`) or bold-label filler.

---

## 3. Numbers with Conditions (조건이 붙은 수치 표기)

Every number in a report or Telegram message must carry its unit, scope, and verification condition so the owner never has to guess what a bare digit means:

1. **Passing criterion (`report.py`):**
   - Format: `- [<cid>] <text> — 마지막 검사 <YYYY-MM-DD HH:MM:SS KST>, 종료 코드 0`
   - Required conditions: criterion ID, when the check ran (`KST`), and the exact exit code (`종료 코드 0`).
2. **Given-up criterion (`report.py`):**
   - Format: `- [<cid>] <text> — gave_up: "<n>번 시도 후 포기"`
   - Required condition: number of stalled attempts (`<n>번 시도 후`).
3. **Worker iteration event (`admin.py`):**
   - Format: `🛠️ 작업 <iteration>회차 — <model>(<thinking>), 종료 코드 <exit_code>[ (시간 초과)]`
   - Required conditions: iteration number (`회차`), model ID, thinking level, exit code, and timeout state.
4. **Durations and wait times (`report.py`, `notify.py`, `admin.py`):**
   - Elapsed mission time and total outage wait time use `<minutes // 60>시간 <minutes % 60>분` (`0시간 42분`, `2시간 15분`).
   - Active outage alert (`notify.py`) and ledger wait event (`admin.py`) use `<minutes>분째` or `<minutes>분 대기`.
   - Planner outage failure reason (`SPEC-D.md` §6): `모델을 6시간 동안 쓸 수 없었음`.
5. **Input byte limits (`telegram.py`):**
   - Pair byte limits with a Korean character estimate when prompting the user: `최소 10바이트(한글은 약 4글자) 이상으로 자세히 적어 주세요`, `20,000바이트 이하로 줄여 주세요`.

---

## 4. KST Times (한국 표준시 표기 규칙)

1. **Storage vs. display separation (`ledger.py` vs. `display.py`).**
   - The ledger (`ledger.jsonl`) stores UTC ISO-8601 timestamps (`ledger.format_ts`).
   - Every human-facing string (`report.md`, Telegram replies and notifications, `agentic mission` CLI output) must format timestamps through `display.display_time(parse_ts(ts))`.
2. **Exact KST format (`display.py`).**
   - `DISPLAY_TIMEZONE = "Asia/Seoul"`
   - `DISPLAY_FORMAT = "%Y-%m-%d %H:%M:%S %Z"` → e.g., `2026-10-18 14:53:21 KST`
   - Always include four-digit year, two-digit month/day, 24-hour `HH:MM:SS` to the second, and the explicit `KST` suffix. Never print naive datetimes, relative times (`방금 전`), or unlabeled UTC times in Korean output.

---

## 5. Isolating Untrusted Text and Redacting Secrets (데이터 격리와 보안)

1. **Quote and flatten AI-written notes (`report.py`).**
   - Worker `DECISIONS.md` lines and planner `assumptions` are untrusted data. Prefix every line with `> ` (`> [계획] <one_line(a)>` and `> <line>`) under `## 📝 AI가 정한 것 (AI가 쓴 기록, 검증 안 됨)` so AI-written text can never masquerade as a report heading or a `## ✅ 된 것` line.
2. **Single-line sanitization and length caps (`worker.one_line`).**
   - Collapse embedded newlines/whitespace with `one_line()` and cap length before interpolating any external or AI string into a template:
     - Request preview: `REQUEST_PREVIEW_CHARS = 300` (append `…` if truncated)
     - Plan failure reason: `REASON_MAX_CHARS = 300`
     - Admin event fields and suspect quotes: `FIELD_MAX_CHARS = 200`
3. **Plain-text Telegram transport (`telegram.py`).**
   - Send all Telegram messages with `parse_mode` unset so characters like `_`, `*`, `[`, or `` ` `` inside file names, error strings, or `DECISIONS.md` never break delivery or inject formatting.
4. **Never leak hidden checks or credentials.**
   - No template may ever interpolate `check.argv`, `check.files`, check stdout/stderr, planner check-step prompts, or the Telegram bot token.

**CRITICAL:** Every supervisor template must put the outcome on line 1, use fixed plain-Korean phrases without translationese, attach measurement conditions to every number, and format all human-facing timestamps as `YYYY-MM-DD HH:MM:SS KST`.
