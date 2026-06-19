"""
Academic policy fixture documents for RAG tests.

These represent the types of documents ingested from SSU official policy pages.
Real scraping requires auth; fixtures demonstrate the ingestion pipeline.
"""

from __future__ import annotations

ACADEMIC_FIXTURES: list[dict] = [
    {
        "text": (
            "숭실대학교 졸업학점 기준: 학부생은 최소 130학점을 이수하여야 졸업할 수 있다. "
            "전공필수 과목과 교양필수 과목을 포함하여 졸업 요건을 충족하여야 한다. "
            "이중전공 또는 부전공을 선택한 경우 추가 학점이 요구될 수 있다."
        ),
        "metadata": {
            "source": "학칙 제23조",
            "policy_type": "graduation",
            "effective_date": "2024-03",
            "title": "졸업 이수학점 기준",
        },
    },
    {
        "text": (
            "채플 이수 요건: 숭실대학교 재학생은 매 학기 채플에 참석하여야 하며, "
            "신입생은 4학기 동안 각 학기 6회 이상 출석하여 채플 학점을 이수하여야 졸업이 가능하다. "
            "채플 1회 결석 시 경고 처리되며, 3회 이상 결석 시 해당 학기 채플 불인정된다."
        ),
        "metadata": {
            "source": "학칙 제31조",
            "policy_type": "chapel",
            "effective_date": "2024-03",
            "title": "채플 이수 기준",
        },
    },
    {
        "text": (
            "장학금 유지 조건: 교내 성적 장학금을 유지하기 위해서는 "
            "직전 학기에 최소 12학점 이상을 이수하고, GPA 3.0 이상을 취득하여야 한다. "
            "휴학 학기는 장학금 유지 조건 산정에서 제외된다. "
            "성적 장학금 종류에 따라 요구 GPA가 3.5 이상으로 높을 수 있다."
        ),
        "metadata": {
            "source": "장학금 규정 제7조",
            "policy_type": "scholarship",
            "effective_date": "2024-03",
            "title": "장학금 유지 조건",
        },
    },
    {
        "text": (
            "계절학기 수강 규정: 계절학기는 하계와 동계로 구분되며, "
            "한 계절학기에 최대 6학점까지 수강할 수 있다. "
            "계절학기 수강 신청은 정규 학기 성적 확인 후 지정된 기간에 가능하며, "
            "재수강 및 타 대학 학점 교류를 통해 학점을 취득할 수 있다."
        ),
        "metadata": {
            "source": "학칙 제29조",
            "policy_type": "semester",
            "effective_date": "2024-03",
            "title": "계절학기 수강 제한",
        },
    },
    {
        "text": (
            "전공필수 이수 기준: 각 전공별로 지정된 전공필수 과목은 "
            "반드시 이수하여야 졸업이 가능하다. "
            "전공필수 과목 미이수 시 졸업 신청이 불가하며, 재수강을 통해 이수 가능하다. "
            "전과 또는 복수전공의 경우 해당 전공의 필수 과목을 별도로 이수하여야 한다."
        ),
        "metadata": {
            "source": "학칙 제24조",
            "policy_type": "graduation",
            "effective_date": "2024-03",
            "title": "전공필수 이수 기준",
        },
    },
    {
        "text": (
            "학사경고 기준: 학기 평점평균(GPA)이 1.5 미만인 경우 학사경고가 부여된다. "
            "동일 학기에 학사경고와 계절학기 수강신청은 제한될 수 있다. "
            "3회 이상 학사경고 누적 시 제적 처리될 수 있으며, "
            "재입학을 통해 학업을 재개할 수 있다."
        ),
        "metadata": {
            "source": "학칙 제32조",
            "policy_type": "academic_warning",
            "effective_date": "2024-03",
            "title": "학사경고 기준",
        },
    },
    {
        "text": (
            "수강신청 학점 제한: 정규 학기 수강신청은 학기당 최대 19학점까지 가능하다. "
            "직전 학기 GPA 4.0 이상인 경우 최대 21학점까지 수강 신청할 수 있다. "
            "1학년 학생의 경우 첫 학기는 최대 18학점으로 제한된다."
        ),
        "metadata": {
            "source": "학칙 제27조",
            "policy_type": "enrollment",
            "effective_date": "2024-03",
            "title": "수강신청 학점 제한",
        },
    },
]
