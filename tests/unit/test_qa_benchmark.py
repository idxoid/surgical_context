from pathlib import Path

from QA.qa_benchmark import load_question_pack, load_questions


def test_load_question_pack_reads_real_repo_metadata():
    pack_path = Path(__file__).parent.parent / "fixtures" / "real_repo_question_pack.yaml"

    pack = load_question_pack(str(pack_path))

    assert pack["kind"] == "real_repo"
    assert len(pack["repositories"]) == 3
    assert len(pack["questions"]) == 24


def test_load_questions_filters_real_repo_core12_subset():
    pack_path = Path(__file__).parent.parent / "fixtures" / "real_repo_question_pack.yaml"

    questions = load_questions(str(pack_path), repo="fastapi", core12_only=True)

    assert len(questions) == 4
    assert all(question["repo"] == "fastapi" for question in questions)
    assert all(question["core12"] is True for question in questions)
