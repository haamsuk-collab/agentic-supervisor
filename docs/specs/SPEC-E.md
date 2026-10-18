# agentic supervisor — Stage E specification: decision briefings, result delivery, follow-up missions

Stages B, C and D (SPEC.md, SPEC-C.md, SPEC-D.md) are built and installed, and so is the KST
display change. They stay unchanged and still apply. Stage E changes **how results reach the owner**.

The owner is not a developer and reads everything on a phone. Every report must answer, in this
order:

1. What happened.
2. How much of the goal is achieved.
3. What the owner must do now.
4. Which decisions the owner must make.
5. What to send next.
6. What is left.

Details go below, collapsed.

Read the earlier specs and this whole file before writing code. Record every choice this file
leaves open in DECISIONS.md. The rules from earlier stages still hold:
- Python 3.12, standard library only.
- No `shell=True`.
- Tests use no network and no real model; Telegram and system commands are faked.

## 0. The honesty rules for briefings (most important)

1. **Deterministic, never AI-written.** The sections "지금 하실 일", "판단이 필요합니다", "다음
   지시" and "남은 것" come only from the ledger, the mission's public view and the rules in
   section 2. No model is ever asked to write or summarise them.
2. **AI-written text stays labelled and contained.** Planner assumptions and DECISIONS.md lines
   appear only in the "AI가 정한 것 (검증 안 됨)" part, never in a decision or a recommendation.
   They must never be able to create formatting (section 4).
3. **Numbers come only from check results.** "달성" counts only criteria whose latest
   `check_result` passed (exit code 0). Dropped and invalid criteria count in the total, never as
   achieved.

## 1. One briefing model: `agentic/briefing.py`

`build(records, public_view, decisions_lines, request) -> Briefing` is a pure function. The report
renderer and every Telegram message use it.

**`Briefing` fields:**
- `mission_id`
- `state`: `running | complete | partial | stopped_time | plan_failed | planning`
- `headline`, for example `✅ 완료`, `⚠️ 일부 완료`, `⏱️ 시간 초과`, `❌ 계획 실패`, `🧭 계획 중`,
  `🛠️ 진행 중`
- `achieved`, `total` and `percent`, with `percent = round(100 * achieved / total)` and 0 when
  `total` is 0
- `duration` (minutes), `iterations` and `models_used`: the ordered distinct models, short names
  such as `Sonnet 5.5`, `Opus 5.5`, `Fable 5.1`, `Argon`
- `ended_at` and `updated_at`, as KST strings
- `todo: list[str]`
- `decisions: list[Decision]`
- `next_instructions: list[NextInstruction]`
- `remaining: list[Item]`
- `notices: list[str]`: things to know that need no action
- `ai_notes`: assumptions and decision lines, kept separately
- `evidence: list[Item]`: one per criterion, with status, last check time, exit code and attempts

**`Decision`:** `subject`, `facts: list[str]`, `options: list[(key, text)]`, `recommended: key`
and `reason`.

**`NextInstruction`:** `label` and `text`. `text` is exactly what the owner can copy and send to the
bot.

## 2. Rules (exhaustive; implement as a table-driven function and test every row)

Let `failed_gave_up`, `failed_time`, `dropped`, `invalid` and `suspect` be the criteria in each
condition (as in the Stage B–D report). `id` is the mission id.

| condition | todo | decision | next instruction | notice |
|---|---|---|---|---|
| state `running` or `planning` | `없음 — 자동으로 진행 중입니다` | — | `/status <id>` (label `진행 상황 보기`) | current step, model and iteration |
| state `complete` | `결과물 받기 → /files <id>` | — | `/files <id>` | — |
| `ai_notes` not empty and state is not running | `AI가 정한 것 N건을 읽고, 틀린 것이 있으면 이어서 맡기기` | — | `<id> 이어서: (바꿀 점을 쓰세요)` | — |
| `failed_gave_up` not empty | `판단 1건` | **subject** `통과하지 못한 조건 N개`; **facts**: each criterion text and `"n번 시도 후 포기"`; **options** `A) 이 조건 없이 결과 받기`, `B) 요구를 바꿔 이어서 맡기기`; **recommended** `A` if `percent >= 80`, else `B`; **reason** `이미 N%를 달성했습니다` / `달성률이 N%라 결과를 그대로 쓰기 어렵습니다` | for B: `<id> 이어서: <criterion text 1>은(는) …로 바꿔 줘` (one line per criterion, with an ellipsis for the owner to fill) | — |
| `failed_time` not empty | `판단 1건` | **subject** `시간이 다 되어 멈춤`; **options** `A) 남은 조건을 이어서 맡기기`, `B) 지금 결과로 끝내기`; **recommended** `A` | `<id> 이어서: 남은 조건을 마저 해 줘` | — |
| `dropped` or `invalid` not empty | `자동 검사를 못 만든 조건 N개를 직접 확인` | **subject** `자동으로 확인할 수 없는 조건 N개`; **facts**: each text and its reason; **options** `A) 결과물을 받아 직접 확인`, `B) 확인 방법이 드러나게 요구를 구체적으로 바꿔 이어서 맡기기`; **recommended** `A` | `/files <id>` | — |
| `suspect` not empty | `판단 1건` | **subject** `리뷰어가 꼼수를 의심한 조건 N개`; **facts**: file and quote; **options** `A) 일반적으로 동작하게 이어서 맡기기`, `B) 그대로 받기`; **recommended** `A` | `<id> 이어서: 특정 입력에만 맞춘 부분을 일반적으로 동작하게 고쳐 줘` | — |
| state `plan_failed` | `요청을 더 구체적으로 다시 보내기` | — | the original request plus the line `(입력·출력 형식과 성공 기준을 구체적으로 적어 주세요)` | the public failure reason |
| a review failed | — | — | — | `꼼수 리뷰를 하지 못했습니다` |
| outage waiting > 0 | — | — | — | `모델을 쓸 수 없어 기다린 시간: …` |
| mission has a `parent` | — | — | — | `이어서 한 미션: <parent>` |

Order the lists in table order. Number the decisions. If there is no todo, write
`없음 — 지금 하실 일은 없습니다`.

`remaining` holds every non-achieved criterion as `❌ <id> <text> (<reason>)` or
`⚠️ <id> <text> (<reason>)`.

## 3. The report file (`report.md`) — replaces the Stage B–D layout

```
# 미션 보고서 · <id>
**결론: <headline> — 조건 <total>개 중 <achieved>개를 감독자가 직접 검사해 확인**

| 달성 | 걸린 시간 | 반복 | 사용 모델 | 종료 시각 |
|---|---|---|---|---|
| <achieved>/<total> (<percent>%) | <h>시간 <m>분 | <n>회 | <models joined by " → "> | <KST> |

요청: <request preview, 300 chars>        (planned missions)
이어서 한 미션: <parent>                   (follow-ups)

## 1. 지금 하실 일
1. …
## 2. 판단이 필요한 것
### 판단 1 · <subject>
- <fact>
- **A)** … **(추천)** — <reason>
- **B)** …
## 3. 다음 지시 (복사해서 텔레그램으로 보내기)
**<label>**
```text
<text>
```
## 4. 결과
### ✅ 감독자가 직접 확인함 (<n>)
- [c1] <text>
### ❌ 안 됨 (<n>)
### ⚠️ 확인할 수 없음 (<n>)
## 5. 알아 두실 것
## 6. AI가 정한 것 (검증 안 됨)
> [계획] …
> …
## 부록 · 조건별 근거
| 조건 | 결과 | 마지막 검사 (KST) | 종료 코드 | 시도 |
```

- Empty sections write `없음`.
- Every piece of AI-written text inside `report.md` stays in `> ` quotes or code blocks. Escape
  Markdown control characters (`` ` * _ [ ] < > # | ``) in every dynamic field outside code
  blocks, so worker text cannot forge a heading, a table row or a ✅ line.
- A code block that holds a next instruction uses a fence longer than any backtick run in its
  text.

## 4. Telegram messages — HTML cards

**Changed Stage D behaviour.** Messages use `parse_mode: "HTML"`.
- The only tags allowed are `<b>`, `<i>`, `<code>`, `<pre>` and `<blockquote expandable>`. Only
  the formatter emits them.
- **Every dynamic string** goes through one escape function (`html.escape(s, quote=False)`). This
  covers mission text, criteria, AI notes, reasons, file names, quotes and request text.
- A test feeds `<b>`, `</pre>`, `&` and `<a href>` through every field and checks that the output
  contains no tag outside the allowed set and no unbalanced tag.

**Finish card** (the `mission_finished` notification, and the reply to `/status` for a finished
mission):
```
<b>⚠️ 일부 완료 │ wordfreq</b>
━━━━━━━━━━━━━━━━━━
🎯 <b>달성 9 / 11 (82%)</b>
▰▰▰▰▰▰▰▰▱▱
⏱ 1시간 12분 · 반복 6회 · Sonnet 5.5 → Opus 5.5
🕒 2026-10-18 14:53 KST

<b>👉 지금 하실 일</b>
 1. …
<b>⚖️ 판단이 필요합니다</b>
 <b>1. <subject></b>
  · <fact>
  A) … ← 추천 (<reason>)
  B) …
<b>✉️ 다음 지시</b>  (눌러서 복사)
<code><text></code>
<b>📋 남은 것</b>
 ❌ …
<b>ℹ️ 알아 두실 것</b>
 …
<blockquote expandable>📝 AI가 정한 것 (검증 안 됨)
…</blockquote>
```

- The progress bar has 10 cells, `▰` for achieved and `▱` for the rest, filled as
  `round(percent / 10)`.
- Omit a section when it is empty, except "지금 하실 일", which always appears.
- The finish card is followed by the full `report.md`, sent **as a document** named
  `report-<id>.md` instead of as split text.

**Other cards** use the same header, then one or two lines and `👉 지금 하실 일: 없음 — 자동으로
진행 중입니다`:
- `🧭 계획 완료`: the criteria count and the first 5 criterion texts.
- `⬆️ 모델 승격`, `⏸️ 모델 대기`, `🔍 꼼수 발견`, `💥 비정상 종료`, `🚀 접수`.

**`/status` while running:** header `🛠️ 진행 중`; achieved so far from the latest check results;
the progress bar; the current model and iteration; the elapsed time; the last 3 events in KST.

**`/list`:** one block per mission:
```
<b>id</b> · <headline> · <achieved>/<total> · <updated KST>
```
Newest first, at most 20.

**Splitting.** Messages are built as a list of blocks. A message is split only between blocks and
stays at most 4,000 characters after escaping. A single block that is too long is cut into
`<pre>` chunks; never cut inside a tag.

**Documents.** An outbox item may carry `"document": "<file name in the outbox>"`, `"filename"`
and `"caption"`.
- Root writes the document file into the outbox the same way as items: O_EXCL, O_NOFOLLOW,
  fchown, rename.
- The bot sends it with `sendDocument` (multipart/form-data built with the standard library), then
  deletes both files.
- Documents larger than 20 MB are never written. Reply with a message that says so instead.

## 5. `/files <id>` — results as a zip

- New inbox type `files` (requires `id`). The bot command is `/files <id>`.
- Root builds `results-<id>.zip` from the worker folder:
  - regular files only, opened with `O_NOFOLLOW`; skip symlinks, devices, fifos, and the
    SNAPSHOT_SKIP folders;
  - paths are relative and inside the folder;
  - add `report-<id>.md` at the zip root;
  - the zip must be at most 20 MB, or reply `결과물이 20MB를 넘어 보낼 수 없습니다 (<size>)`;
  - nothing from the state folder except report.md (no ledger, no mission.json, no checks).
- The reply is the document plus the caption `📦 <id> 결과물 · 파일 <n>개`.
- An unknown mission, or one with an empty worker folder, gets a Korean message.

## 6. Follow-up missions (`이어서 맡기기`)

**Asking.** A plain-text message matching `^<mission id>\s*이어서\s*[:：]\s*(.+)` (dot-all, 10 to
20,000 bytes after the colon) is a follow-up request. So is `/continue <id> <text>`.
- The bot shows the preview with `시작` / `취소` buttons, exactly as for a new request.
- `시작` writes an inbox item of the new type `continue`, with `id` and `text`.

**Root side.** `admin.continue_mission(parent_id, text)`:
- The parent must exist and its unit must not be active or activating. Otherwise reply
  `아직 진행 중인 미션입니다` or `없는 미션입니다`.
- MAX_ACTIVE_MISSIONS still applies.
- New id: `<parent base>-c<n>`, where the base drops any earlier `-c<k>` suffix and n is the next
  free number, e.g. `wordfreq-c1`, `wordfreq-c2`. The id must stay valid; if it would be too long,
  use the default id scheme.
- `request.md` is composed deterministically:
  ```
  [이어서 하는 미션] 이전 미션: <parent>
  [이전 요청]
  <parent request, or "(직접 작성한 미션)">
  [이전 결과] 확인됨: <ids and texts> / 안 됨: <ids and texts and reasons>
  [작업 폴더] 이전 미션의 결과물이 그대로 들어 있습니다.
  [새 요구]
  <text>
  ```
- The parent's worker folder is copied into the new worker folder **as the worker user**:
  `runuser -u agent -- cp -a <parent work>/. <new work>/`, after `mkdir -p` as agent.
- The new mission's `mission.json` gets an optional public field `parent` (a valid id).
- Then start it like `new_mission`.

**Planner change.** For a follow-up, the design prompt adds:

> The work folder already contains the previous result. Write criteria for the complete final
> state the owner wants: keep what already works, and add or change what the new requirement asks.

The empty-folder veto stays as it is: checks are still proven red on an empty folder.

**Baseline change (changed Stage B behaviour).**
- For a mission with a `parent`, a check that passes on the inherited folder at baseline is
  **already achieved**, not `invalid_check`. Its validity was already proven on an empty folder
  during planning.
- Record it in `baseline` as `inherited_pass` and count it as passing.
- If every criterion passes at baseline, run the cheat review on the inherited files (so
  `review_due` must allow `iteration == 0` when inherited passes exist), then finish.
- Missions without a `parent` keep the Stage B rule.

## 6a. Writing rules for every fixed Korean text (from agentic-guard rule 13)

These rules cover every Korean string the code writes itself: templates, headings, todos, decision
subjects, options, reasons and notices. Dynamic and AI-written text is exempt; it is shown as data.
1. Put the conclusion first: the card header and the report's first line state the outcome.
   Write one idea per sentence.
2. No translationese: `~에 있어서`, `~를 통해`, `~하는 것이 가능하다`, `~되어진다`, `~에 대해`.
3. No AI stock phrases: `물론입니다`, `훌륭한 질문`, `종합적으로`, `다양한`, `효율적으로`,
   `결론적으로`, `도움이 되었기를`.
4. Every number gets its conditions. The achievement line says the count is "감독자가 직접 검사한
   결과", and times say KST.
5. Verdict words (`완료`, `통과`, `확인`) appear only next to check-backed facts.

Put the banned phrases in one list in the code. Add a test that renders every briefing row, card
type and report section with neutral sample data and asserts that none of the banned phrases
appears in the output.

## 7. Tests

All earlier tests keep passing. Add:

1. **Briefing rules.** One test per row of the section 2 table, including the `percent >= 80`
   switch, empty sections, ordering and numbering.
2. **Honesty:**
   - The achieved count equals the number of criteria whose last check passed.
   - Dropped and invalid criteria are never counted as achieved.
   - No decision, todo or next-instruction text contains a planner assumption or a DECISIONS.md
     line.
3. **Report.** The layout of section 3. Forged Markdown in worker or planner text cannot create a
   heading, a table row or a ✅ line. Code fences are adaptive.
4. **HTML.** Escaping holds for every field with hostile strings. Only the allowed tags appear,
   balanced. Splitting happens only between blocks and stays at most 4,000 characters.
5. **Documents.** Outbox document safety (pre-planted symlink, size limit). The bot sends a
   multipart request (fake API) and deletes both files.
6. **`/files`:**
   - The zip contains the worker files and report.md, and nothing from the state folder.
   - Symlinks are not followed.
   - Over 20 MB gives a message instead of a file.
   - Unknown and empty missions give their messages.
7. **Follow-up:**
   - The message pattern and `/continue` go through the confirm buttons.
   - An active or unknown parent is refused.
   - The id becomes `-c1`, then `-c2`.
   - `request.md` has exactly the composed text.
   - The copy runs as the worker user, in order (fake runner).
   - `inherited_pass` counts as passing.
   - All-pass at baseline runs the review and finishes complete.
   - A non-follow-up mission still marks baseline passes invalid.
8. **KST.** Every time shown in a card or the report is KST.

## 8. Done means

- `make test` passes and you show its full output.
- README.md documents the briefing, `/files` and follow-ups.
- DECISIONS.md records every choice.
- Commit with a clear message.
- SPEC.md, SPEC-C.md, SPEC-D.md and SPEC-E.md are not modified.

Out of scope: AI-written summaries of any kind, sending files other than the results zip and the
report, editing a running mission, and installing anything.
