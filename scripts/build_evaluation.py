"""AI Hub 후보(candidates.csv)와 정답 표(labels.csv)를 합쳐 evaluation.csv를 만든다.

문장과 출처는 후보 파일에서 그대로 가져오고, 사람이 정한 정답만 labels.csv에서 붙인다.
직접 작성한 평가 문의(source_type이 AI Hub가 아닌 행)는 evaluation.csv에 있으면 그대로 유지한다.

실행: py -3.14 scripts/build_evaluation.py
"""
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = ROOT / "data" / "aihub" / "candidates.csv"
LABELS = ROOT / "data" / "aihub" / "labels.csv"
OUT = ROOT / "data" / "evaluation.csv"

DATASET = "AI Hub 소상공인 고객 주문 질의-응답 텍스트 (dataSetSn=102, v1.4, Validation)"
FIELDS = ["id", "question", "expected_knowledge_id", "expected_action", "case_type", "source_type",
          "source_dataset", "source_file", "source_idx", "source_qa_no", "source_intent", "note"]


def read(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main():
    candidates = {row["source_idx"]: row for row in read(CANDIDATES)}
    kept = [row for row in read(OUT)
            if row.get("source_type") != "AI Hub" and not row.get("note", "").startswith("예시")] if OUT.exists() else []
    rows = []
    for label in read(LABELS):
        source = candidates[label["source_idx"]]
        rows.append({
            "question": source["question"],
            "expected_knowledge_id": label["expected_knowledge_id"],
            "expected_action": label["expected_action"],
            "case_type": label["case_type"],
            "source_type": "AI Hub",
            "source_dataset": DATASET,
            "source_file": source["source_file"],
            "source_idx": source["source_idx"],
            "source_qa_no": source["source_qa_no"],
            "source_intent": source["source_intent"],
            "note": label["note"],
        })
    rows += kept
    for number, row in enumerate(rows, 1):
        row["id"] = f"E{number:03d}"
    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} rows -> {OUT}")


if __name__ == "__main__":
    main()
