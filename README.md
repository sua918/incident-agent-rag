# 운영 인시던트 대응 에이전트 개선

기존 FastAPI·LangGraph·MCP·Vue 실습 프로젝트에 매뉴얼 의미 검색, 조회 결과 검증, 진단 이력 조회를 추가했다. 시스템의 증상을 입력하면 모니터링 자료와 대응 매뉴얼을 확인하고 대응 절차를 작성한다.

![진단 근거 확인](doc/screenshots/diagnosis.png)

## 주요 변경

| 항목 | 기존 동작 | 개선 동작 |
|---|---|---|
| 매뉴얼 선택 | 알림 키워드로 문서 하나 선택, 매칭 실패 시 DB Down 선택 | 질문과 알림을 임베딩해 문단 3개 검색. 임베딩 장애 시 규칙 검색으로 대체 |
| 답변 검증 | 길이와 번호 형식 확인, 최대 시도 후 강제 통과 | 관측 수치·상태·출처 ID 대조, 3회 미통과 시 확인 필요로 종료 |
| 병렬 조회 | 개별 실패가 전체 조사에 영향 | 성공한 자료 유지, 실패 대상 별도 기록 |
| 진단 기록 | 대화 저장 | 진단별 결과·근거·실패 사유 저장, 검색·상태 필터·상세 조회 |

원본의 라우팅, 계획, 게이트, MCP Tool·Resource·Prompt, 병렬 실행, 재시도, 모델 분리, SQLite 대화 저장 구조를 유지했다. 변경 지점은 [구조와 변경 내용](doc/changes.md)에 정리했다.

## 실행

Windows PowerShell과 Python 3.11 이상이 필요하다. 확인 환경은 Windows와 Python 3.14.4다. Vue는 기존처럼 CDN을 사용하므로 처음 화면을 열 때 인터넷 연결이 필요하다.

```powershell
cd C:\Users\teo\workspace\incident_agent_revision
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
```

Ollama를 실행하고 모델을 확인한다. 아래 모델이 이미 있으면 내려받기를 생략한다.

```powershell
ollama list
ollama pull qwen3:0.6b
ollama pull qwen3:8b
ollama pull qwen3-embedding:0.6b
```

Ollama 앱이 꺼져 있다면 별도 터미널에서 `ollama serve`를 실행한다. `.env.example`을 복사한 `.env`의 기본값은 Ollama다. 분류 모델과 답변 모델, 임베딩 모델을 각각 설정한다.

```powershell
# 최초 인덱스 생성. 문서나 모델 변경 시 바뀐 항목을 다시 생성한다.
.\.venv\Scripts\python.exe -m agent.rag

# 터미널 1
powershell -ExecutionPolicy Bypass -File scripts/start-backend.ps1

# 터미널 2
powershell -ExecutionPolicy Bypass -File scripts/start-frontend.ps1
```

화면: <http://127.0.0.1:8630> / API 문서: <http://127.0.0.1:8130/docs>. 종료는 각 서버 터미널에서 `Ctrl+C`를 누른다. 프론트엔드는 별도 Node 빌드가 필요 없다.

모델 없이 흐름을 재현하려면 백엔드 터미널에서 다음 설정을 먼저 적용한다. `fake`는 고정 응답이며 실제 모델 품질을 보여주지 않는다.

```powershell
$env:LLM_BACKEND='fake'
$env:RUNBOOK_SEARCH='rules'
powershell -ExecutionPolicy Bypass -File scripts/start-backend.ps1
```

실제 모델로 돌아갈 때는 새 터미널을 쓰거나 위 두 환경변수를 제거한다. 임베딩 서버 장애 시 화면에는 규칙 검색 전환을 표시하며, 키워드도 맞지 않으면 관련 근거가 없다고 기록한다.

## 시연

1. `auth-service 로그인 응답 지연 장애를 진단해줘`를 입력한다.
2. 조사 진행과 검증 완료 후 답변을 확인한다. 모니터링 정보와 매뉴얼 문단을 펼친다.
3. `진단 이력`을 열어 `auth-service`를 검색하고 상세 결과를 확인한다.
4. 새로고침 후에도 이력이 남는지 확인한다.
5. `fake` 모드에서 `missing-service 장애 진단해줘`를 입력한다. 미등록 대상은 실패로 기록하고, 성공한 web-01 자료만 유지하며 확인 필요 상태로 남긴다.

질문 예시와 실패 흐름은 [시연 안내](doc/demo.md)에 정리했다.

## 저장 위치와 실제 동작 범위

프로젝트 루트의 `session.db`가 SQLite 파일이다. `SESSION_DATABASE`로 경로를 바꿀 수 있다. 대화는 `messages`, 진단 결과는 `diagnoses`, 매뉴얼 벡터는 `runbook_chunks`에 저장한다. 서버 시작 시 테이블을 만들고 기존 진단 테이블에 상세 정보 열이 없으면 추가한다. DB와 `.env`는 Git에 포함하지 않는다.

FastAPI 요청, LangGraph 분기, MCP 하위 프로세스 통신, Ollama 추론·임베딩, SQLite 저장은 실제 실행된다. **모니터링 수치는 `data/monitoring.json`에 작은 변동을 더한 실습 데이터**다. 실제 서버 지표 수집, 서버 재시작, 티켓 등록 등 운영 조치는 실행하지 않는다.

## 확인 결과

```powershell
powershell -ExecutionPolicy Bypass -File scripts/test.ps1
.\.venv\Scripts\python.exe scripts/evaluate.py
.\.venv\Scripts\python.exe scripts/check_model.py
```

- 자동 테스트 27개 통과. fake 모델과 임시 DB를 사용하며 MCP 하위 프로세스는 실제로 연결한다.
- 고정 12건의 정답 매뉴얼 포함: 기존 규칙 7/12, RAG 상위 1개 10/12, 상위 3개 문단 중 정답 문서 포함 12/12.
- 실제 Ollama 1건: RAG 검색, 답변 검증, 근거·이력 저장 확인. 약 25.25초.
- OpenAI 실제 호출은 키 미설정으로 미검증이다.

측정 범위와 한계는 [검증 결과](doc/results.md), 원시 측정값은 [검색 비교](doc/results/comparison.json)와 [실제 모델 확인](doc/results/model-check.json)을 참고한다. 결과는 한 번의 소규모 로컬 측정이며 일반적인 정확도나 처리 시간으로 해석하지 않는다.

## 구성

```text
server.py                  API, 의존성 생성, SSE 중계
web_demo_server.py         Vue 화면 제공, API 프록시
web_chat.html              채팅, 근거 표시, 진단 이력
agent/graph.py             조사·재작성 흐름
agent/rag.py               문단 분리, 임베딩 인덱스, 코사인 검색
agent/validation.py        관측값과 출처 검사
agent/session_store.py     대화·진단 저장
mcp_server.py              모니터링 Tool, 매뉴얼 Resource, 절차 Prompt
data/                      실습 지표와 원본 매뉴얼 6개
tests/                     실패 격리, 검증, 검색, 저장 테스트
eval/cases.json            고정 검색 비교 자료
```

기존 v1 비교 경로와 OpenAI 어댑터는 남겼다. 추가한 기능은 `SUPERVISOR_VERSION=v2`를 기준으로 확인했다. LangSmith는 설정 시 사용할 수 있으나 기본 실행에서는 끈 상태다.

## 제한

정규식 검사는 지정된 수치·상태 표현만 확인한다. 자연어 전체의 의미나 모든 잘못된 권고를 판별하지 않는다. 검색 점수는 처리 성공 확률이 아니며, 상위 문단에 다른 유형의 문서가 섞일 수 있다. 인증 없는 단일 사용자 로컬 시연을 기준으로 구성했다.
