"""
press2article — 보도자료를 넣으면 기사가 나오는 서비스

흐름 (자세한 로직은 pipeline.py)
  보도자료 → [기사 작성 에이전트: 기사 + 판정] → 승인이면 → [인터랙티브 기사 에이전트]
  기사     →                                               [인터랙티브 기사 에이전트]
  각 단계의 입력·출력은 /flow/<id> 화면에서 그대로 확인할 수 있음.

검증 (evaluate.py)
  /eval 화면 또는 `python evaluate.py` — 정답지(eval/answers.csv)와 비교해 표로 채점.
"""
import os
import uuid

from flask import Flask, abort, redirect, render_template, request, send_file, url_for

import evaluate
import pipeline
from pipeline import PRESS_TYPES, agent_ready

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 검증용 여러 파일 업로드 고려

# 생성 결과를 잠시 보관 (해커톤 데모용 메모리 저장소 — 서버를 끄면 사라짐)
ARTICLES: dict[str, dict] = {}


# ──────────────────────────────────────────────
# 원문 ↔ 기사 비교 (같은 표현 형광펜, 뺀 문장 취소선, 사용률)
# ──────────────────────────────────────────────
NGRAM = 8  # 공백을 뺀 8글자 이상 같으면 '원문과 같은 표현'으로 봄


def _chars(text: str):
    return [(ch, i) for i, ch in enumerate(text) if not ch.isspace()]


def _grams(text: str) -> set[str]:
    cs = "".join(ch for ch, _ in _chars(text))
    return {cs[i:i + NGRAM] for i in range(len(cs) - NGRAM + 1)}


def _marks(text: str, other: set[str]) -> list[bool]:
    """text의 각 글자가 other에도 있는 8글자 덩어리에 속하면 True."""
    cs = _chars(text)
    flags = [False] * len(text)
    seq = "".join(ch for ch, _ in cs)
    for i in range(len(seq) - NGRAM + 1):
        if seq[i:i + NGRAM] in other:
            for k in range(i, i + NGRAM):
                flags[cs[k][1]] = True
    # 형광펜이 단어 사이 공백에서 끊기지 않게 채우기
    for i in range(1, len(text) - 1):
        if text[i].isspace() and flags[i - 1] and flags[i + 1]:
            flags[i] = True
    return flags


def _segments(text: str, flags: list[bool]) -> list[dict]:
    segs, cur, cur_m = [], "", None
    for ch, m in zip(text, flags):
        if cur_m is None or m == cur_m:
            cur += ch
        else:
            segs.append({"t": cur, "m": cur_m})
            cur = ch
        cur_m = m
    if cur:
        segs.append({"t": cur, "m": bool(cur_m)})
    return segs


def compare(source: str, paragraphs: list[str]) -> dict:
    source = source[:20000]
    src_grams, art_grams = _grams(source), _grams("\n".join(paragraphs))

    art = []
    total = matched = 0
    for p in paragraphs:
        flags = _marks(p, src_grams)
        total += sum(1 for ch in p if not ch.isspace())
        matched += sum(1 for ch, f in zip(p, flags) if f and not ch.isspace())
        art.append(_segments(p, flags))

    src = []
    for line in source.splitlines():
        if not line.strip():
            continue
        flags = _marks(line, art_grams)
        n = sum(1 for ch in line if not ch.isspace())
        used = sum(1 for ch, f in zip(line, flags) if f and not ch.isspace())
        removed = n >= 10 and used / n < 0.15  # 기사에 거의 안 쓰인 문장 → 취소선
        src.append({"segs": _segments(line, flags), "removed": removed})

    return {"art": art, "src": src, "ratio": round(matched * 100 / total) if total else 0}


# ──────────────────────────────────────────────
# 라우트 — 기사 작성 / 흐름 보기
# ──────────────────────────────────────────────
def _finish(item: dict, outlet: str = "") -> str:
    """처리된 item에 화면용 비교 결과를 붙여 저장하고 id를 돌려줌."""
    item["outlet"] = outlet
    if item.get("article"):
        a = item["article"]
        item["cmp"] = compare(item["source"], ([a["lead"]] if a["lead"] else []) + a["body"])
    item_id = uuid.uuid4().hex[:10]
    ARTICLES[item_id] = item
    return item_id


def store(item: dict) -> str:  # 검증 화면에서도 결과를 흐름 화면으로 열 수 있게
    return _finish(item)


def _status():
    return {"ready": agent_ready("writer"), "ready2": agent_ready("interactive")}


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", **_status())


@app.route("/generate", methods=["POST"])
def generate():
    kind = "article" if request.form.get("kind") == "article" else "press"
    need = "interactive" if kind == "article" else "writer"
    if not agent_ready(need):
        return render_template("index.html", **_status(),
                               error=f"{pipeline.AGENTS[need]['label']} 설정이 .env에 없습니다.")

    pasted = request.form.get("press_text", "").strip()
    upload = request.files.get("press_file")
    try:
        if upload and upload.filename:
            item = pipeline.process(upload.filename, upload.read(), kind=kind,
                                    with_interactive=agent_ready("interactive"))
        elif pasted:
            item = pipeline.process("pasted.txt", pasted.encode("utf-8"), kind=kind,
                                    with_interactive=agent_ready("interactive"),
                                    display_name="붙여넣은 텍스트")
        else:
            raise ValueError("파일을 첨부하거나 내용을 붙여넣어 주세요.")
    except Exception as e:  # 데모용: 오류를 화면에 그대로 보여줌
        return render_template("index.html", **_status(), error=str(e))

    item_id = _finish(item, request.form.get("outlet", "").strip())
    if item.get("article"):
        return redirect(url_for("article", article_id=item_id))
    return redirect(url_for("flow", article_id=item_id))


@app.route("/article/<article_id>")
def article(article_id):
    item = ARTICLES.get(article_id)
    if not item:
        return redirect(url_for("index"))
    if not item.get("article"):
        return redirect(url_for("flow", article_id=article_id))
    return render_template("article.html", id=article_id, types=PRESS_TYPES,
                           error=request.args.get("error"), **item)


@app.route("/flow/<article_id>")
def flow(article_id):
    item = ARTICLES.get(article_id)
    if not item:
        return redirect(url_for("index"))
    return render_template("flow.html", id=article_id, item=item, kind_label=pipeline.KIND_LABEL)


@app.route("/article/<article_id>/regenerate", methods=["POST"])
def regenerate(article_id):
    item = ARTICLES.get(article_id)
    if not item:
        return redirect(url_for("index"))
    new_type = request.form.get("press_type", "")
    if new_type not in PRESS_TYPES:
        return redirect(url_for("article", article_id=article_id))
    try:
        new = pipeline.process(*item["orig"], kind=item["kind"], press_type=new_type,
                               with_interactive=agent_ready("interactive"),
                               display_name=item["filename"])
    except Exception as e:
        return redirect(url_for("article", article_id=article_id,
                                error=f"다시 만들기에 실패했어요: {e}"))
    outlet = item.get("outlet", "")
    ARTICLES[article_id] = new
    new["outlet"] = outlet
    a = new["article"]
    new["cmp"] = compare(new["source"], ([a["lead"]] if a["lead"] else []) + a["body"])
    return redirect(url_for("article", article_id=article_id))


# ──────────────────────────────────────────────
# 라우트 — 검증(정답지 채점)
# ──────────────────────────────────────────────
@app.route("/eval", methods=["GET"])
def eval_home():
    answers = evaluate.read_answers()
    inputs = []
    for rel, _ in evaluate.list_inputs():
        row = evaluate._match(answers, rel)
        inputs.append({"file": rel, "row": row,
                       "kind": pipeline.KIND_LABEL[evaluate._kind(rel, row)]})
    rel = lambda p: os.path.relpath(p, evaluate.BASE).replace("\\", "/")
    return render_template("eval.html", runs=evaluate.RUNS, inputs=inputs, types=PRESS_TYPES,
                           answers_rel=rel(evaluate.ANSWERS), inputs_rel=rel(evaluate.INPUTS) + "/",
                           fields=evaluate.FIELDS, error=request.args.get("error"), **_status())


@app.route("/eval/run", methods=["POST"])
def eval_run():
    files = [f for f in request.files.getlist("files") if f.filename]
    answers = request.files.get("answers")
    try:
        if files:  # 업로드한 파일로 실행 (업로드본은 eval/uploads/<id>/ 에 보관)
            run_id = evaluate.start_upload_run(
                [(f.filename, f.read()) for f in files],
                answers.read() if answers and answers.filename else None, store=store)
        else:      # eval/inputs 폴더로 실행
            run_id = evaluate.start_run(store=store)
    except Exception as e:
        return redirect(url_for("eval_home", error=str(e)))
    return redirect(url_for("eval_view", run_id=run_id))


@app.route("/eval/<run_id>")
def eval_view(run_id):
    run = evaluate.RUNS.get(run_id)
    if not run:
        return redirect(url_for("eval_home"))
    return render_template("eval_run.html", run=run, fields=evaluate.FIELDS)


@app.route("/eval/<run_id>/csv")
def eval_csv(run_id):
    run = evaluate.RUNS.get(run_id)
    if not run or not run.get("csv"):
        abort(404)
    return send_file(run["csv"], as_attachment=True, download_name=os.path.basename(run["csv"]))


if __name__ == "__main__":
    # 0.0.0.0 → 같은 와이파이의 다른 기기에서도 접속 가능 (HOST=127.0.0.1 이면 이 컴퓨터에서만)
    # 디버그 모드는 네트워크에 열면 위험해서 기본은 끔. 코드 수정 시 자동 재시작이 필요하면 DEBUG=1
    app.run(host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", 5001)),
            debug=os.getenv("DEBUG") == "1")
