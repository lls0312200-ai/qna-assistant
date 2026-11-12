"""AI Hub '소상공인 고객 주문 질의-응답 텍스트'(의류, validation)에서 평가 후보 문의를 뽑는다.

- 고객 질문(QA여부=q)만 사용하고, 원본 점원 답변은 쓰지 않는다.
- 개인정보가 의심되는 행은 가리지 않고 제외한다.
- 출처 추적을 위해 원본 파일명·IDX·QA번호·인텐트를 함께 남긴다.

실행: py -3.14 scripts/sample_aihub_candidates.py
결과: data/aihub/candidates.csv (사람이 검토해 evaluation.csv로 옮긴다)
"""
import csv
import random
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "aihub" / "validation" / "의류_validation.csv"
OUT = ROOT / "data" / "aihub" / "candidates.csv"
PER_INTENT = 15
SEED = 20261103

INTENTS = [
    "배송_비용_질문", "배송_날짜_질문", "배송_지역_질문", "배송_방법_질문", "배송_오류_질문",
    "교환|반품|환불_시간_질문", "교환|반품|환불_방법_질문", "교환|반품|환불_비용_질문",
    "제품_불량_질문", "구매_취소_요청", "구매_변경_요청", "행사_정보_질문",
]

SUSPECT = [
    re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}"),
    re.compile(r"\d{2,4}[- .]?\d{3,4}[- .]?\d{4}"),
    re.compile(r"\d{6}[- ]?[1-4]\d{6}"),
    re.compile(r"(주문|송장|운송장|계좌)\s*번호"),
    re.compile(r"\*{2,}"),
]


def usable(row):
    text = row["발화문"].strip()
    if row["사람"].strip() or any(p.search(text) for p in SUSPECT):
        return False
    return 8 <= len(text) <= 120


def main():
    with open(SOURCE, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["QA여부"] == "q"]
    rng = random.Random(SEED)
    picked = []
    for intent in INTENTS:
        pool = [r for r in rows if r["인텐트"] == intent and usable(r)]
        picked += rng.sample(pool, min(PER_INTENT, len(pool)))
    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["source_file", "source_idx", "source_qa_no", "source_intent", "question"])
        for r in picked:
            writer.writerow([SOURCE.name, r["IDX"], r["QA번호"], r["인텐트"], r["발화문"].strip()])
    print(f"{len(picked)} candidates -> {OUT}")


if __name__ == "__main__":
    main()
