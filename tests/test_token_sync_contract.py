# coding: utf-8
"""Token 与时间数组同步的输入、长度和继承时间契约。"""

import pytest

from core.tools.token_sync import sync_tokens_from_text


def _assert_sync(raw_tokens, raw_timestamps, formatted_text):
    tokens, timestamps = sync_tokens_from_text(
        raw_tokens, raw_timestamps, formatted_text
    )
    assert len(tokens) == len(timestamps)
    assert "".join(tokens) == formatted_text
    assert set(timestamps) <= set(raw_timestamps)
    return tokens, timestamps


def test_sync_keeps_unchanged_tokens_and_timestamps():
    tokens, timestamps = _assert_sync(
        ["hello", " ", "世界"], [0.1, 0.2, 0.3], "hello 世界"
    )

    assert tokens == ["hello", " ", "世界"]
    assert timestamps == [0.1, 0.2, 0.3]


def test_sync_inserts_punctuation_and_space_with_neighbor_time():
    tokens, timestamps = _assert_sync(["hello", "world"], [0.1, 0.3], "hello, world")

    assert tokens[0] == "hello"
    assert tokens[-1] == "world"
    assert set(timestamps[1:-1]) == {0.1}


def test_sync_replaces_across_tokens_for_numbers_and_hotwords():
    tokens, timestamps = _assert_sync(["三", "百", "米"], [0.1, 0.2, 0.4], "300米")

    assert "".join(tokens) == "300米"
    assert timestamps[-1] == 0.4
    assert set(timestamps[:-1]) == {0.1}


def test_sync_deletes_text_without_shifting_survivor_time():
    tokens, timestamps = _assert_sync(
        ["保留", "删除", "尾部"], [0.1, 0.2, 0.4], "保留尾部"
    )

    assert "".join(tokens) == "保留尾部"
    assert timestamps[0] == 0.1
    assert timestamps[-1] == 0.4


def test_sync_preserves_cjk_ascii_and_multichar_token_text():
    tokens, timestamps = _assert_sync(
        ["你好", "世界", "abc"], [0.1, 0.2, 0.3], "你好世界abc"
    )

    assert "".join(tokens) == "你好世界abc"
    assert len(tokens) == len(timestamps)


def test_sync_handles_adjacent_ascii_apostrophe_and_decimal():
    tokens, timestamps = _assert_sync(
        ["I", " ", "dont", " ", "3", ".", "14"],
        [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
        "I don't 3.14",
    )

    assert "".join(tokens) == "I don't 3.14"
    assert timestamps[0] == 0.1
    assert timestamps[-1] == 0.7


def test_sync_removes_model_at_at_marker_while_preserving_time():
    tokens, timestamps = _assert_sync(["hel@@", "lo"], [0.1, 0.2], "hello")

    assert "".join(tokens) == "hello"
    assert set(timestamps) <= {0.1, 0.2}


def test_sync_accepts_empty_body():
    tokens, timestamps = _assert_sync([], [], "")

    assert tokens == []
    assert timestamps == []


@pytest.mark.parametrize(
    ("tokens", "timestamps"),
    [
        (["one", "two"], [0.1]),
        (["one"], [0.1, 0.2]),
    ],
)
def test_sync_rejects_mismatched_raw_arrays(tokens, timestamps):
    with pytest.raises(ValueError, match="tokens.*timestamps"):
        sync_tokens_from_text(tokens, timestamps, "onetwo")
