"""
에이전트 성능 검증 — 정답지(eval/answers.csv)와 에이전트 결과를 비교해 표로 채점

사용법
  1) eval/inputs/ 에 보도자료(또는 기사) 파일을 넣는다
  2) eval/answers.csv 에 파일명별 정답을 적는다 (엑셀로 편집 가능)
       파일명,입력,유형,판정,인터랙티브유형
       보도자료1.pdf,보도자료,협약·MOU,승인,지도형
       보도자료2.pdf,보도자료,인사·동정,거부,
     - 입력: 보도자료 / 기사  (비우면 보도자료. eval/inputs/기사/ 폴더에 넣어도 기사로 처리)
     - 정답을 비워둔 칸은 채점하지 않음 (예측값만 표시 → 정답지 만들 때 참고)
  3) python evaluate.py         ← 터미널에서 표 출력 + eval/results/*.csv 저장
     또는 웹 화면 /eval 에서 [검증 실행]

채점 항목을 늘리려면 아래 FIELDS에 한 줄 추가하고 정답지에 같은 이름의 열을 만들면 됨.
"""
import argparse
import csv
import io
import os
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import pipeline
from pipeline import agent_ready, normalize_decision, normalize_itype, normalize_type

BASE = os.path.dirname(os.path.abspath(__file__))
EVAL_DIR = os.path.join(BASE, "eval")
INPUTS = os.path.join(EVAL_DIR, "inputs")
ANSWERS = os.path.join(EVAL_DIR, "answers.csv")
RESULTS = os.path.join(EVAL_DIR, "results")
UPLOADS = os.path.join(EVAL_DIR, "uploads")
WORKERS = int(os.getenv("EVAL_WORKERS", "3"))  # 동시에 돌릴 파일 수


def _ia(key):
    return lambda item: (item.get("interactive") or {}).get(key)


# ──────────────────────────────────────────────
# 채점 항목: 정답지 열 이름 / 어느 에이전트 결과인지 / 예측값 꺼내기 / 표기 통일
# ──────────────────────────────────────────────
FIELDS = [
    {"name": "유형", "agent": "writer", "desc": "보도자료 유형 분류",
     "predict": lambda item: item.get("detected"), "norm": normalize_type},
    {"name": "판정", "agent": "writer", "desc": "인터랙티브 재료 판정 (승인/보류/거부)",
     "predict": lambda item: item.get("decision"), "norm": normalize_decision},
    {"name": "인터랙티브유형", "agent": "interactive",
     "desc": "인터랙티브 유형 (계산기형/대시보드형/스크롤리텔링형/지도형/해당없음)",
     "predict": _ia("itype"), "norm": normalize_itype},
]


# ──────────────────────────────────────────────
# 정답지 · 입력 파일 읽기
# ──────────────────────────────────────────────
def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):  # 엑셀 '쉼표로 구분' 저장은 cp949일 수 있음
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="ignore")


def read_answers(src: str | bytes | None = "") -> dict[str, dict]:
    """{파일명(소문자): 행} — 파일명 열은 '파일명' 또는 'file'. 인자를 안 주면 eval/answers.csv."""
    if src == "":
        src = ANSWERS
    if src is None:
        return {}
    if isinstance(src, str):
        if not os.path.exists(src):
            return {}
        with open(src, "rb") as f:
            src = f.read()
    rows = {}
    for row in csv.DictReader(io.StringIO(_decode(src))):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        name = row.get("파일명") or row.get("file") or row.get("filename")
        if name and not name.startswith("#"):
            rows[name.lower()] = row
    return rows


def _match(answers: dict, relpath: str) -> dict | None:
    base = os.path.basename(relpath).lower()
    stem = base.rsplit(".", 1)[0]
    for key in (relpath.replace("\\", "/").lower(), base):
        if key in answers:
            return answers[key]
    for k, row in answers.items():  # 확장자 없이 적은 경우
        if k.rsplit(".", 1)[0] == stem:
            return row
    return None


def list_inputs(folder: str | None = None) -> list[tuple[str, str]]:
    """[(상대경로, 전체경로)] — 하위 폴더 포함, 지원 형식만."""
    folder = folder or INPUTS
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in sorted(files):
            if fn.startswith((".", "~$")) or fn.rsplit(".", 1)[-1].lower() not in pipeline.ALLOWED_EXT:
                continue
            full = os.path.join(root, fn)
            out.append((os.path.relpath(full, folder).replace("\\", "/"), full))
    return sorted(out)


def _kind(relpath: str, row: dict | None) -> str:
    v = (row or {}).get("입력", "").lower()
    if v in ("기사", "article"):
        return "article"
    if v in ("보도자료", "press"):
        return "press"
    parts = relpath.lower().split("/")[:-1]
    return "article" if any(p in ("기사", "article", "articles") for p in parts) else "press"


# ──────────────────────────────────────────────
# 실행
# ──────────────────────────────────────────────
def evaluate_one(relpath: str, data: bytes, row: dict | None, store=None) -> dict:
    kind = _kind(relpath, row)
    answered = {f["name"] for f in FIELDS if (row or {}).get(f["name"])}
    # 판정은 1번 에이전트가 냄. 2번은 기사 입력이거나 인터랙티브유형 정답이 있을 때만 (시간·비용 절약)
    # 보도자료는 그중에서도 판정이 '승인'일 때만 실제로 실행됨 (pipeline.process)
    use2 = agent_ready("interactive") and (
        kind == "article" or any(f["agent"] == "interactive" for f in FIELDS if f["name"] in answered))
    res = {"file": relpath, "kind": pipeline.KIND_LABEL[kind], "status": "완료", "error": "",
           "seconds": 0, "item_id": None, "fields": {}}
    t0 = time.time()
    item = None
    try:
        if kind == "press" and not agent_ready("writer"):
            raise RuntimeError("기사 작성 에이전트 설정이 .env에 없습니다.")
        if kind == "article" and not use2:
            raise RuntimeError("인터랙티브 기사 에이전트(UPSTAGE_AGENT2_ID) 설정이 .env에 없습니다.")
        item = pipeline.process(os.path.basename(relpath), data, kind=kind, with_interactive=use2)
        err2 = (item.get("interactive") or {}).get("error")
        if err2:
            res["error"] = f"인터랙티브 기사 에이전트 실패: {err2}"
        if store:
            res["item_id"] = store(item)
    except Exception as e:
        res.update(status="오류", error=str(e))
    res["seconds"] = round(time.time() - t0, 1)

    for f in FIELDS:
        expected = f["norm"]((row or {}).get(f["name"], "")) if (row or {}).get(f["name"]) else None
        predicted = None
        if item is not None:
            p = f["predict"](item)
            predicted = f["norm"](p) if p else None
        ok = None if expected is None else (predicted == expected)
        res["fields"][f["name"]] = {"expected": expected, "predicted": predicted, "ok": ok}
    return res


def summarize(rows: list[dict]) -> dict:
    out = {"files": len(rows), "errors": sum(r["status"] != "완료" for r in rows), "fields": {}}
    for f in FIELDS:
        scored = [r["fields"][f["name"]] for r in rows
                  if r["fields"].get(f["name"], {}).get("ok") is not None]
        correct = sum(1 for x in scored if x["ok"])
        out["fields"][f["name"]] = {"scored": len(scored), "correct": correct,
                                    "pct": round(correct * 100 / len(scored), 1) if scored else None}
    fully = [r for r in rows if any(v["ok"] is not None for v in r["fields"].values())]
    all_ok = sum(1 for r in fully if all(v["ok"] is not False for v in r["fields"].values()))
    out["all"] = {"scored": len(fully), "correct": all_ok,
                  "pct": round(all_ok * 100 / len(fully), 1) if fully else None}
    return out


def save_csv(run: dict) -> str:
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, f"result_{run['id']}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # 엑셀에서 한글 안 깨지게
        w = csv.writer(f)
        head = ["파일명", "입력", "상태"]
        for fd in FIELDS:
            head += [f"정답_{fd['name']}", f"예측_{fd['name']}", f"일치_{fd['name']}"]
        w.writerow(head + ["소요(초)", "오류"])
        for r in run["rows"]:
            line = [r["file"], r["kind"], r["status"]]
            for fd in FIELDS:
                x = r["fields"][fd["name"]]
                line += [x["expected"] or "", x["predicted"] or "",
                         "" if x["ok"] is None else ("O" if x["ok"] else "X")]
            w.writerow(line + [r["seconds"], r["error"]])
    return path


RUNS: dict[str, dict] = {}  # 웹 화면용 실행 기록 (메모리)


def _new_run(source: str) -> dict:
    run_id = time.strftime("%Y%m%d-%H%M%S")
    while run_id in RUNS:
        run_id += "b"
    run = {"id": run_id, "source": source, "status": "running", "total": 0, "done": 0,
           "rows": [], "summary": None, "csv": None, "started": time.strftime("%H:%M:%S"),
           "missing": []}
    RUNS[run_id] = run
    return run


def execute(run: dict, entries: list[tuple[str, str]], answers: dict, store=None, on_row=None):
    """entries: [(상대경로, 전체경로)]. run을 채워가며 실행 (진행 상황이 실시간으로 보임)."""
    run["total"] = len(entries)
    matched = {id(m) for rel, _ in entries if (m := _match(answers, rel)) is not None}
    run["missing"] = [r.get("파일명") or r.get("file") or "" for r in answers.values()
                      if id(r) not in matched]
    slots: list[dict | None] = [None] * len(entries)

    def job(i, rel, full):
        with open(full, "rb") as fp:
            data = fp.read()
        return i, evaluate_one(rel, data, _match(answers, rel), store=store)

    with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as ex:
        futures = [ex.submit(job, i, rel, full) for i, (rel, full) in enumerate(entries)]
        for fut in as_completed(futures):
            i, res = fut.result()
            slots[i] = res
            run["rows"] = [s for s in slots if s]
            run["done"] += 1
            if on_row:
                on_row(run, res)
    run["rows"] = [s for s in slots if s]
    run["summary"] = summarize(run["rows"])
    run["csv"] = save_csv(run)
    run["status"] = "done"
    return run


def _background(run, entries, answers, store):
    def go():
        try:
            execute(run, entries, answers, store=store)
        except Exception as e:  # 예상 못 한 오류도 화면에 표시
            run.update(status="error", error=str(e))
    threading.Thread(target=go, daemon=True).start()


def start_run(folder: str | None = None, answers_path: str | None = None, store=None) -> str:
    folder, answers_path = folder or INPUTS, answers_path or ANSWERS
    entries = list_inputs(folder)
    if not entries:
        raise ValueError(f"검증할 파일이 없습니다. {os.path.relpath(folder, BASE)} 폴더에 파일을 넣어주세요.")
    run = _new_run(f"폴더 {os.path.relpath(folder, BASE)}")
    _background(run, entries, read_answers(answers_path), store)
    return run["id"]


def start_upload_run(files: list[tuple[str, bytes]], answers_csv: bytes | None, store=None) -> str:
    run = _new_run("업로드한 파일")
    folder = os.path.join(UPLOADS, run["id"])
    os.makedirs(folder, exist_ok=True)
    for name, data in files:
        with open(os.path.join(folder, os.path.basename(name)), "wb") as f:
            f.write(data)
    if answers_csv:
        with open(os.path.join(folder, "_answers.csv"), "wb") as f:
            f.write(answers_csv)
        run["source"] += " + 업로드한 정답지"
    entries = list_inputs(folder)
    if not entries:
        RUNS.pop(run["id"], None)
        raise ValueError("지원하는 형식의 파일이 없습니다.")
    answers = read_answers(answers_csv) if answers_csv else read_answers()
    _background(run, entries, answers, store)
    return run["id"]


# ──────────────────────────────────────────────
# 터미널 실행
# ──────────────────────────────────────────────
def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _cut(s: str, n: int) -> str:
    out = ""
    for c in s:
        if _w(out + c) > n - 1:
            return out + "…"
        out += c
    return out


def print_table(run: dict):
    head = ["파일", "입력"] + [f["name"] + " 정답/예측" for f in FIELDS] + ["결과"]
    lines = []
    for r in run["rows"]:
        cells = [_cut(r["file"], 34), r["kind"]]
        marks = []
        for f in FIELDS:
            x = r["fields"][f["name"]]
            mark = "" if x["ok"] is None else ("✔ " if x["ok"] else "✘ ")
            marks.append(x["ok"])
            cells.append(_cut(f"{mark}{x['expected'] or '-'} / {x['predicted'] or '-'}", 30))
        verdict = "오류" if r["status"] != "완료" else (
            "채점 안 함" if all(m is None for m in marks) else ("정답" if False not in marks else "오답"))
        cells.append(verdict)
        lines.append(cells)
    widths = [max(_w(c) for c in col) for col in zip(head, *lines)]

    def fmt(cells):
        return " | ".join(c + " " * (w - _w(c)) for c, w in zip(cells, widths))

    print(fmt(head))
    print("-+-".join("-" * w for w in widths))
    for cells in lines:
        print(fmt(cells))

    s = run["summary"]
    print()
    for f in FIELDS:
        x = s["fields"][f["name"]]
        if x["scored"]:
            print(f"  {f['name']:<8} 정답률 {x['pct']:5.1f}%  ({x['correct']}/{x['scored']})")
        else:
            print(f"  {f['name']:<8} 채점할 정답 없음")
    if s["all"]["scored"]:
        print(f"  {'전체':<8} 모두 맞음 {s['all']['pct']:5.1f}%  ({s['all']['correct']}/{s['all']['scored']})")
    if s["errors"]:
        print(f"  오류 {s['errors']}건:")
        for r in run["rows"]:
            if r["status"] != "완료":
                print(f"    - {r['file']}: {r['error']}")
    if run["missing"]:
        print(f"  정답지에는 있는데 파일이 없음: {', '.join(run['missing'])}")
    print(f"\n  결과 CSV: {os.path.relpath(run['csv'], BASE)}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="정답지와 에이전트 결과 비교")
    ap.add_argument("--inputs", default=None, help="검증할 파일 폴더 (기본 eval/inputs)")
    ap.add_argument("--answers", default="", help="정답지 CSV (기본 eval/answers.csv)")
    args = ap.parse_args()

    entries = list_inputs(args.inputs)
    if not entries:
        sys.exit(f"검증할 파일이 없습니다: {args.inputs or INPUTS}")
    answers = read_answers(args.answers)
    print(f"파일 {len(entries)}개 · 정답지 {len(answers)}행 · 동시 실행 {WORKERS}개\n")

    def progress(run, r):
        print(f"  [{run['done']}/{run['total']}] {r['file']} — {r['status']} ({r['seconds']}초)"
              + (f" {r['error']}" if r["error"] else ""), flush=True)

    run = execute(_new_run("터미널"), entries, answers, on_row=progress)
    print()
    print_table(run)


if __name__ == "__main__":
    main()
