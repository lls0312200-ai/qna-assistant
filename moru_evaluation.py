"""Parse and score administrator-labeled cases without storing inquiry text."""
import csv
import io
import re

from rag import normalize


SHOP_AUTO_CATEGORIES = ("배송", "사이즈", "상품", "기타")


def read_cases(content, records, categories):
    if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > 256_000:
        raise ValueError("256KB 이내 UTF-8 평가 CSV를 선택해 주세요.")
    by_id = {str(record["id"]): record for record in records}
    for record in records:
        source = record.get("source_key") or ""
        if source.startswith("moru-faq:"):
            by_id[source.removeprefix("moru-faq:")] = record
    reader = csv.DictReader(io.StringIO(content.removeprefix("\ufeff")), strict=True)
    required = {"id", "question", "expected_action", "expected_knowledge_id"}
    cases, seen_ids, seen_questions = [], set(), set()
    try:
        if not required.issubset(reader.fieldnames or ()) or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("평가 CSV에 id, question, expected_action, expected_knowledge_id 열이 필요합니다.")
        for index, row in enumerate(reader, 1):
            if index > 250:
                raise ValueError("평가 CSV에는 최대 250개 사례를 넣을 수 있습니다.")
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"평가 CSV의 {index}번째 사례 열 수를 확인해 주세요.")
            ident, question = row["id"].strip(), row["question"].strip()
            action = row["expected_action"].strip()
            expected = by_id.get(row["expected_knowledge_id"].strip())
            category = row.get("category", "").strip() or (expected["category"] if expected else "기타")
            if (not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident) or ident in seen_ids
                    or not normalize(question) or len(question) > 3000 or action not in {"auto", "hold", "human"}):
                raise ValueError(f"평가 CSV의 {index}번째 사례 ID·문의·판정값을 확인해 주세요.")
            seen_ids.add(ident)
            if category not in categories:
                continue
            key = (category, normalize(question))
            if key in seen_questions:
                raise ValueError("같은 범주의 평가 CSV에 중복된 문의가 있습니다.")
            seen_questions.add(key)
            if action == "auto" and (not expected or not expected["auto_allowed"] or expected["category"] != category):
                raise ValueError(f"{ident} 사례의 정답은 해당 범주에서 자동 사용을 승인한 활성 지식이어야 합니다.")
            cases.append((ident, {"title": question, "body": question, "category": category}, action, expected))
    except csv.Error:
        raise ValueError("평가 CSV의 따옴표 또는 줄 형식이 올바르지 않습니다.") from None
    if not cases:
        raise ValueError("선택한 자동 답변 범주에 해당하는 평가 사례가 없습니다.")
    return cases


def score_cases(cases, categories, decide):
    totals = {category: {"positive": 0, "negative": 0, "correct": 0, "paraphrases": 0, "wrong": 0}
              for category in categories}
    results = []
    for ident, question, action, expected in cases:
        counts = totals[question["category"]]
        counts["positive" if action == "auto" else "negative"] += 1
        allowed, reason, source = decide(question)
        correct = bool(allowed and action == "auto" and source["id"] == expected["id"])
        if allowed:
            counts["correct" if correct else "wrong"] += 1
        if correct and normalize(question["body"]) != normalize(expected["question"]):
            counts["paraphrases"] += 1
        results.append({"case": ident, "category": question["category"], "expected": action,
                        "automatic": allowed, "correct": correct if allowed else action != "auto", "reason": reason})
    correct = sum(counts["correct"] for counts in totals.values())
    wrong = sum(counts["wrong"] for counts in totals.values())
    positive = sum(counts["positive"] for counts in totals.values())
    return {
        "passed": all(counts["negative"] and counts["paraphrases"] and not counts["wrong"]
                      for counts in totals.values()),
        "total": len(results), "correct_auto": correct, "wrong_auto": wrong,
        "precision": correct / (correct + wrong) if correct + wrong else 0,
        "recall": correct / positive if positive else 0,
        "by_category": totals, "cases": results,
    }
