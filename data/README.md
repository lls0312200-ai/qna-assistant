# MORU 시연 데이터

[실행 안내](../README.md)에 따라 Python 환경을 준비한 뒤 `start-shop.bat`을 실행하면 두 CSV를 MORU의 로컬 SQLite DB에 처음 한 번 적재한다.

| 파일 | 건수 | 내용과 출처 |
| --- | ---: | --- |
| `knowledge.csv` | 52 | 조은빛이 가상 의류 쇼핑몰에 맞춰 작성한 정책 FAQ. 공개 쇼핑몰 이용안내를 참고한 행에는 `source_url`과 적용 조건을 기록했다. |
| `demo_questions.csv` | 113 | MORU 정책으로 답할 수 있는 질문과 담당자 판단이 필요한 사례를 시연하기 위해 새로 작성한 가상 문의. |

이전 로컬 시연에는 AI Hub [소상공인 고객 주문 질의-응답 텍스트](https://www.aihub.or.kr/aihubdata/data/view.do?currMenu=115&topMenu=100&aihubDataSe=data&dataSetSn=102) v1.4의 의류 검증용 고객 문의를 활용했다. [AI Hub 이용 안내](https://aihub.or.kr/aihubnews/faq/list.do?currMenu=146&topMenu=104)는 원본 및 단순 가공 데이터의 재배포를 제한한다. 따라서 원문이 담긴 `evaluation.csv`, AI Hub 원본·추출 파일과 수집한 쇼핑몰 페이지 원문은 로컬에 보관한다.

원래 평가 자료를 재현하려면 이용 승인받은 의류 검증 CSV를 `data/aihub/validation/의류_validation.csv`에, 팀의 로컬 라벨 표를 `data/aihub/labels.csv`에 둔다. `python scripts/sample_aihub_candidates.py`와 `python scripts/build_evaluation.py`를 차례로 실행하면 `data/evaluation.csv`가 생성된다. 이 파일과 `knowledge.csv`를 저장소 상위 폴더의 `data/`에 두면 기존 방식으로 새 DB를 초기화할 수 있다. 시연용 가상 문의에는 평가 정답이 포함되어 있지 않다.
