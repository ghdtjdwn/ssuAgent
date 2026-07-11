"""Tests for the streaming ``<function=...>`` tool-call-leak stripper.

Some free/weaker LLM providers emit a tool call as plain text
(``<function=name>{...}</function>``) instead of a structured tool call, which
would otherwise stream straight to the user as visible garbage. ``main._stream_graph``
runs every text chunk through :class:`_FunctionTagStripper` to drop those blocks.
"""

import pytest

from ssu_agent.main import _FunctionTagStripper


def _run(chunks: list[str]) -> str:
    stripper = _FunctionTagStripper()
    return "".join(stripper.feed(c) for c in chunks) + stripper.flush()


@pytest.mark.parametrize(
    "chunks,expected",
    [
        # whole tag in one chunk
        (['hello <function=get_library_seat_status>{"floor": 3}</function> world'], "hello  world"),
        # delimiter split across token chunks (the streaming case)
        (["hel", "lo <func", "tion=get>{", '"a":1}</func', "tion> bye"], "hello  bye"),
        # plain text is passed through untouched
        (["just normal text, no tags here."], "just normal text, no tags here."),
        # tag at the very end
        (["answer done. ", "<function=x>{}</function>"], "answer done. "),
        # unterminated tag is dropped at end-of-stream
        (["text <function=x>{never closes"], "text "),
        # bare '<' (math / comparisons) must NOT be treated as a tag
        (["a < b < c (math, not tags)"], "a < b < c (math, not tags)"),
        # korean text around a tag split across chunks
        (
            ["seats: 3층 10석. ", "<function=x>", '{"floor": 3}', "</function>"],
            "seats: 3층 10석. ",
        ),
        # multiple tags interleaved with text
        (
            ["two ", "<function=a>{}</function>", " and ", "<function=b>{}</function>", " end"],
            "two  and  end",
        ),
    ],
)
def test_strips_leaked_function_tags(chunks: list[str], expected: str) -> None:
    assert _run(chunks) == expected
