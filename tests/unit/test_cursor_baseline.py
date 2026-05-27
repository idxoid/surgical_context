import json

from QA.cursor_baseline import discover_transcripts, summarize_transcript


def test_discover_transcripts_filters_project_slug(tmp_path):
    transcript = (
        tmp_path
        / "home-idxoid-surgical-context"
        / "agent-transcripts"
        / "session"
        / "session.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    other = tmp_path / "other-project" / "agent-transcripts" / "session" / "session.jsonl"
    other.parent.mkdir(parents=True)
    other.write_text("", encoding="utf-8")

    paths = discover_transcripts(tmp_path, "surgical")

    assert paths == [transcript]


def test_summarize_transcript_matches_question_and_counts_visible_files(tmp_path):
    transcript = tmp_path / "home-idxoid-surgical-context" / "agent-transcripts" / "s1" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    rows = [
        {
            "role": "user",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "<user_query>\nquestion_id: click_q01\nHow is command registered?\n</user_query>",
                    }
                ]
            },
        },
        {
            "role": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"path": "/repo/src/click/core.py"},
                    },
                    {
                        "type": "text",
                        "text": "The answer cites /repo/src/click/core.py and command.",
                    },
                ]
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    questions = [
        {
            "id": "click_q01",
            "question": "How is command registered?",
            "expected_files": ["src/click/core.py"],
        }
    ]

    summary = summarize_transcript(transcript, questions=questions)

    assert summary.question_id == "click_q01"
    assert summary.matched_by == "explicit_id"
    assert summary.tool_calls == 1
    assert summary.expected_file_recall == 1.0
    assert summary.missing_expected_files == []
