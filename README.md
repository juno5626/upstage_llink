# 📰 보도자료 → 기사 (press2article)

보도자료를 넣으면 지면에 실릴 수준의 **신문 기사 초안**이 나오는 서비스입니다.
Upstage 해커톤 출품작 · Upstage Studio 에이전트 기반

## 왜 만들었나

지역 언론사 기자는 하루에도 수십 건의 보도자료를 받습니다. 보도자료는 홍보 문구가 많고 형식도 제각각(PDF·스캔본·Word)이라, 기사로 옮기는 데 시간이 많이 듭니다.
이 서비스는 보도자료 파일을 넣기만 하면 **역피라미드 구조의 스트레이트 기사 초안**을 만들어, 기자가 취재와 검증에 더 많은 시간을 쓸 수 있게 돕습니다.

## 화면 구성

| 페이지 | 기능 |
|---|---|
| `/` 보도자료 첨부 | PDF·HWP·DOCX·PPTX·이미지 파일 첨부(드래그 앤 드롭) 또는 텍스트 붙여넣기, 기사 유형·매체명 선택 |
| `/article/<id>` 기사 보기 | 제목·부제·리드·본문을 신문 지면 스타일로 표시, 키워드·기자 확인 사항, 원문 보기, 기사 복사·인쇄 |
| `/flow/<id>` 처리 흐름 | 단계마다 에이전트에 **보낸 것**(파일·file_id·지시문)과 **받은 것**(워크플로우 노드별 원본 출력), 서비스가 해석한 결과, 에이전트 2 실행 여부, 웹페이지 미리보기 |
| `/eval` 정답지 검증 | 여러 파일을 한 번에 돌려 정답지(CSV)와 비교, 항목별 정답률과 파일별 표 |

## 동작 방식

```
[보도자료 파일 / 붙여넣은 텍스트]
          │  (텍스트는 DOCX로 자동 변환)
          ▼
  ① Upstage Files API  ─ 파일 업로드 → file_id
          ▼
  ② Upstage Studio Agent (Agents API, v2/responses)
       Parse 노드: 문서 인식(OCR·레이아웃)
       Instruct 노드: 기사 작성
          ▼
  ③ 결과 폴링 → 제목/리드/본문 분리 → 기사 페이지
```

- 에이전트 결과가 JSON(`headline`, `lead`, `body` …)이면 그대로, 마크다운 텍스트면 `# 제목`/본문을 자동으로 나눠 표시합니다.
- 출처 표시(`【†1】`)·마크다운 기호는 화면에서 자동 제거합니다.

## 에이전트 2개

| 에이전트 | 입력 | 출력 |
|---|---|---|
| 기사 작성 (`UPSTAGE_AGENT_ID`) | 보도자료 파일 | 원문(Parse) · 유형(Classify) · 기사 · 인터랙티브 재료 판정(승인/보류/거부) |
| 인터랙티브 기사 (`UPSTAGE_AGENT2_ID`) | 기사(1번 결과를 DOCX로 변환, 또는 기사 파일 직접) | 인터랙티브 유형, 단일 HTML 웹페이지 |

보도자료는 1번의 판정이 **승인**일 때만 2번을 실행합니다 (보류·거부, 판정 노드가 없는 others·예외문서는 건너뜀). 기사 파일을 넣으면 판정 없이 2번만 실행합니다. 해석 로직은 `pipeline.py`의 `detect_decision` / `detect_itype` / `find_html`에 있습니다.

## 정답지 검증

1. `eval/inputs/`에 보도자료를 넣는다 (기사는 `eval/inputs/기사/`에)
2. `eval/answers.csv`(엑셀 편집 가능)에 정답을 적는다

   | 파일명 | 입력 | 유형 | 판정 | 인터랙티브유형 |
   |---|---|---|---|---|
   | 포항시_관광지도.pdf | 보도자료 | 행사·일정 안내 | 승인 | 지도형 |
   | 인사발령.pdf | 보도자료 | 인사·동정 | 거부 | |

   - 유형: 보도자료유형분류 노드의 9개 + `예외문서` (`others`는 `기타`)
   - 판정: 에이전트 1 지시-1 노드의 `승인` / `보류` / `거부` (others·예외문서는 판정 없음)
   - 인터랙티브유형: 분류-1 노드 (`계산기형` / `대시보드형` / `스크롤리텔링형` / `지도형` / `해당없음`). 승인일 때만 나옴

   빈 칸은 채점하지 않고 예측값만 표시합니다. 판정·인터랙티브유형 정답이 있는 행만 2번 에이전트를 돌립니다.
3. 실행 — 터미널 `python evaluate.py` 또는 웹 `/eval` → 표 + `eval/results/result_<시각>.csv`

채점 항목을 늘리려면 `evaluate.py`의 `FIELDS`에 한 줄 추가하고 정답지에 같은 이름의 열을 만듭니다.
동시 실행 수는 `EVAL_WORKERS`(기본 3)로 조절합니다.

## 실행 방법

```bash
pip3 install -r requirements.txt
cp .env.example .env      # .env를 열어 아래 값 입력
PORT=5001 python3 app.py
```

브라우저에서 http://127.0.0.1:5001 접속
(macOS는 5000번 포트를 AirPlay가 쓰고 있어 5001번 사용을 권장합니다.)

같은 와이파이의 다른 기기에서는 `http://<이 컴퓨터 IP>:5001`로 접속합니다 (서버 실행 시 터미널에 주소가 표시됨).
이 컴퓨터에서만 열려면 `HOST=127.0.0.1`, 코드 수정 시 자동 재시작이 필요하면 `DEBUG=1`을 줍니다.

### .env 설정

Upstage Studio 워크플로우 화면 → **⋯ → 코드** 패널에서 값을 확인할 수 있습니다.

| 변수 | 설명 |
|---|---|
| `UPSTAGE_API_KEY` | 에이전트가 속한 Studio 조직의 API 키 (`up_…`) |
| `UPSTAGE_AGENT_ID` | 에이전트(워크플로우) ID (`agt_…`) |
| `UPSTAGE_CONFIG_ID` | 설정 버전 번호 (비우면 최신 버전) |
| `UPSTAGE_PROJECT_ID` | 에이전트가 속한 프로젝트 ID (`proj_…`), `x-project-id` 헤더로 전송 |
| `UPSTAGE_AGENT2_ID` | 인터랙티브 기사 에이전트 ID (선택, 없으면 1번만 실행) |
| `UPSTAGE_AGENT2_CONFIG_ID` | 2번 에이전트 설정 버전 (`cfg_…`, 선택) |
| `UPSTAGE_AGENT2_INPUT` | 보도자료 입력 시 2번에 줄 것: `article`(1번이 쓴 기사, 기본) / `source`(원본 보도자료) |
| `UPSTAGE_AGENT2_API_KEY` / `UPSTAGE_AGENT2_PROJECT_ID` | 2번 에이전트가 다른 조직·프로젝트일 때만 (비우면 1번 값 사용) |

> `.env`는 `.gitignore`에 포함되어 깃허브에 올라가지 않습니다. API 키를 코드에 직접 쓰지 마세요.

## 지원 형식

PDF, HWP, HWPX, DOCX, PPTX, XLSX, PNG, JPG, BMP, TIFF, HEIC, TXT(자동 변환)

## 프로젝트 구조

```
press2article/
├── app.py               # Flask 서버, 화면 라우트, 원문↔기사 비교
├── pipeline.py          # 에이전트 호출·결과 해석·단계별 기록(trace)
├── evaluate.py          # 정답지 검증 (터미널/웹 공용)
├── eval/
│   ├── inputs/          # 검증할 보도자료 (기사/ 하위 폴더는 기사)
│   └── answers.csv      # 정답지
├── templates/
│   ├── base.html        # 공통 레이아웃(마스트헤드)
│   ├── index.html       # 보도자료 첨부 페이지
│   ├── article.html     # 기사 보기 페이지
│   ├── flow.html        # 처리 흐름 페이지
│   ├── eval.html        # 검증 시작 페이지
│   └── eval_run.html    # 검증 결과 표
├── static/style.css     # 신문 지면 스타일
├── requirements.txt
└── .env.example
```

## 기술 스택

Python · Flask · Upstage Studio Agent (Agents API) · Upstage Files API · OpenAI Python SDK
