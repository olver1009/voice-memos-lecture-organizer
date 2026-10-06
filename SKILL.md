---
name: lecture-recording-organizer
description: Rename lecture recordings and sort them into course folders inside Apple Voice Memos. Use for manual, scheduled, or catch-up runs. Does not attach recordings to Apple Notes or handle transcription.
---

# 음성 메모 강의 정리

강의 녹음을 `M월 D일 과목명`으로 이름 붙이고, 음성 메모 안의 같은 과목 폴더로 이동한다. 메모앱·공유·파일 내보내기는 사용하지 않는다. 오디오를 재생하거나 녹음·폴더를 삭제하지 않는다.

## 판정

Asia/Seoul 기준 2026-09-01~2026-12-18의 누락분을 매번 보충한다. 시간표와 분할 제목 계산은 `scripts/plan.py`에만 정의되어 있으므로 추론으로 다시 판정하지 않는다. 시작 시각은 DB의 `ZDATE`; 10분 이하와 09:00~18:00 밖의 시작은 제외한다. 제목 형식은 `M월 D일 과목명`, 분할 번호는 `n-k`다. `M/D` 날짜와 `n/k` 분할 표기는 잘못된 형식이므로 플래너가 정규화한다. 이미 표준 형식인 제목은 띄어쓰기·Unicode 차이만으로 바꾸지 않고, 현재 녹음 개수에 맞는 하이픈 분할 번호는 유지한다. 단독으로 남은 과거 분할 녹음의 번호도 유지한다.

대상 폴더는 미주지역지리, 지도학및실습, 도시지리학특강, 응용 지형학, 기후변화와 미래환경이다. 없는 폴더를 만들거나 비슷한 다른 폴더로 보내지 않는다.

## 실행

먼저 읽은 SKILL.md의 디렉터리를 `skillDir`로 확인하고, `python3 /absolute/lecture-recording-organizer/scripts/plan.py --check`를 실행한다. 아래 모든 `/absolute/lecture-recording-organizer`는 이 스킬의 실제 절대 경로로 치환한다. 예약의 현재 작업 폴더에 `scripts/plan.py`가 있거나 `CODEX_HOME` 환경 변수가 설정되어 있다고 가정하지 않는다. `no_changes`이면 앱을 열지 않고 종료한다. 기억한 처리 목록이나 “오늘 새 녹음이 없을 것”이라는 추정으로 작업을 생략하지 않는다. 매 실행에서 DB를 다시 읽으므로 매일 새 녹음·늦게 동기화된 과거 녹음·복원·제목/폴더 변경도 다시 확인한다. 날짜가 바뀌어 기존 미래 녹음이 처리 대상이 된 경우도 감지한다.

캐시는 마지막으로 전체 UI와 대조한 DB 지문만 저장한다. DB의 처리 대상이 UI에서 빠졌으면 `db_candidate_not_visible`로 보류하고 캐시를 저장하지 않는다. 삭제 흔적인지 동기화 누락인지 추측하지 않는다. 재생 위치·속도·음향 설정과 내부 버전/폴더 정렬은 비교에서 제외하지만, 삭제 플래그와 의미를 모르는 필드는 유지한다. 캐시가 없거나 손상되거나 코드·판정·관련 데이터가 바뀌면 `needs_ui`가 반환된다. 이때의 `snapshot_fingerprint`를 UI 수집 전의 기준으로 사용한다.

### 목록 수집과 플래너

녹음이 진행 중이거나 Mac 잠금·권한 문제로 조작할 수 없으면 변경 없이 보류한다. 컴퓨터 도구는 `cua_repl`을 사용한다. 처음에는 곧바로 `var vm = await cua.getApp("com.apple.VoiceMemos");`로 앱을 얻는다. 앱을 알고 있으므로 전체 앱 목록을 먼저 조회하지 않는다.

목록 수집은 `scripts/collect.mjs`를 로드해 실행한다. 수집 코드를 새로 작성하거나 전체 목록 JSON을 모델이 다시 출력하지 않는다. `skillDir`는 이 SKILL.md가 있는 디렉터리의 절대 경로, `inventoryPath`는 현재 작업 폴더의 `work/voice-memos-inventory.json` 절대 경로로 설정한다. `snapshotFingerprint`는 이번 `--check`의 `snapshot_fingerprint` 값으로 설정한다. 다음 코드를 한 `cua_repl` 호출에서 실행한다.

```javascript
var fs = await import("node:fs/promises");
var url = await import("node:url");
var collector = await import(url.pathToFileURL(skillDir + "/scripts/collect.mjs").href + "?v=" + snapshotFingerprint);
var inventory = await collector.collectInventory(vm);
await fs.mkdir(inventoryPath.slice(0, inventoryPath.lastIndexOf("/")), {recursive: true});
await fs.writeFile(inventoryPath, JSON.stringify(inventory), {mode: 0o600});
nodeRepl.write({inventoryPath, total_count: inventory.total_count, folder_count: inventory.folders.length});
```

수집기는 「모든 녹음 항목」을 선택하고 검색을 비운 뒤 맨 위부터 실제 제목·길이·사이드바 폴더/개수를 수집한다. 최신 AX의 보조 스크롤 동작을 우선 사용하고, 없으면 일반 스크롤을 사용한다. 겹치는 행은 중복 집계하지 않는다. 전체 개수 불일치, 식별 모호성, 수집 중 목록/검색/폴더 변화는 오류로 중단한다. 최근 삭제 항목은 열지 않는다. 수집기 오류에서 목록을 추측하거나 DB로 UI 목록을 대체하지 않는다.

수집한 파일을 직접 플래너에 전달한다. 아래 경로와 지문을 실제 값으로 넣는다.

```bash
python3 /absolute/lecture-recording-organizer/scripts/plan.py --inventory-file /absolute/work/voice-memos-inventory.json --plan-file /absolute/work/voice-memos-plan.json --expected-fingerprint SNAPSHOT_FINGERPRINT
```

플래너는 수집 전후와 대조 중 DB 지문이 같아야 작업을 반환한다. 작업 전체는 권한 0600의 계획 파일에 쓰고, 화면에는 개수와 보류만 반환한다. `status=fatal`이면 변경 없이 종료한다. `summary.rename=0`, `summary.move=0`, `reports=[]`이면 캐시도 저장되므로 **목록 재수집·별도 remember·추가 check 없이 종료**한다. `cache_saved=false`는 캐시 저장 실패만 보고한다. `reports`는 보류로 보고하고 캐시를 저장하지 않으며, 식별이 확인된 다른 작업은 처리할 수 있다.

### 녹음 변경이 있을 때만

계획 파일은 모델이 다시 작성하지 않는다. 변경 직전에 DB 지문과 식별자를 검사한다. 실패하면 UI 변경 없이 중단한다.

```bash
python3 /absolute/lecture-recording-organizer/scripts/plan.py --validate-plan-file /absolute/work/voice-memos-plan.json
```

고정 실행기 `scripts/apply.mjs`를 로드하고, 전체 작업을 한 `cua_repl` 호출에서 처리한다. `planPath`는 위 계획 파일의 절대 경로다. 기존 `fs`, `url`, `vm`, `skillDir`, `snapshotFingerprint`를 재사용한다.

```javascript
var executor = await import(url.pathToFileURL(skillDir + "/scripts/apply.mjs").href + "?v=" + snapshotFingerprint);
var uiResult = await executor.applyActions(vm, JSON.parse(await fs.readFile(planPath, "utf8")));
nodeRepl.write(uiResult);
```

실행기는 검색으로 정확한 제목·길이의 한 행을 선택한 뒤 검색을 비우고 선택을 확인한다. 제목 텍스트 필드 자체에 포커스를 주고 `setValue`·Return으로 확정한다. 저장 직후 제목 필드와 선택·길이를 확인한다. 목록의 AX 제목이 잠시 이전 값이어도 이를 완료로 간주하지 않고, 이동 후 실제 과목 폴더에서 새 제목·길이를 다시 확인한다. 이름 변경만 있는 경우도 해당 폴더에서 확인한다. 「이동」 보조 동작과 「폴더 선택」 시트의 정확한 과목 버튼만 사용하고, 매 동작 후 최신 AX로 인덱스를 다시 얻는다. 마지막에 전체 녹음으로 돌아와 검색을 비운다.

`uiResult.status=failed`이면 추가 변경과 자동 재시도를 중단하고 부분 실행 결과를 보고한다. `ui_completed`는 UI 확인 결과이며 최종 성공은 아래 DB 검증까지 통과해야 한다. 저장 확인 없이 성공으로 세거나 같은 이동을 반복하지 않는다. 스크린샷은 AX 조작이 실패할 때만 사용하고, 실패한 UI를 추측해 계속 진행하지 않는다. DB는 읽기 전용이며 직접 쓰지 않는다.

```bash
python3 /absolute/lecture-recording-organizer/scripts/plan.py --verify-plan-file /absolute/work/voice-memos-plan.json
```

같은 PK·고유 ID의 제목·폴더·길이 저장을 검증한다. 실패하면 중단하고 보고한다. 성공하면 `--check`부터 목록 수집/플래너 경로를 **한 번만** 다시 실행해 변경 후 전체 상태를 검증하고 캐시를 저장한다. 저장 실패는 빠른 경로만 비활성화하며 실제 변경을 되돌리지 않는다. 목록·계획 파일은 실행 후 삭제한다. 이번 실행의 전달 파일이며 다음 실행의 처리 근거로 쓰지 않는다.

이름 변경·폴더 이동 개수와 실제 보류·오류만 간단히 보고한다. 제외한 녹음은 보고하지 않는다. 이 스킬은 기존 예약의 실행 작업이며 예약 주기·모델·추론 강도를 변경하지 않는다.
