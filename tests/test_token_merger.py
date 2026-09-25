from core.server.merger.token_merger import merge_tokens_by_sequence_matcher


def test_merge_keeps_space_when_cut_splits_multi_character_token():
    prev_tokens = [
        "hairpiece", "?", " ", "Wait", ",", " ", "does", " ",
        "he", " ", "eat", " ", "chalk", "?",
    ]
    new_tokens = [
        "Wait", ",", " ", "does", " ", "he", " ", "eat", " ",
        "chalk", "? ", "Just", " ", "'", "cause", " ", "I", " ",
        "don", "'", "t",
    ]
    prev_timestamps = [float(i) for i in range(len(prev_tokens))]
    new_timestamps = [i * 0.1 for i in range(len(new_tokens))]

    tokens, _ = merge_tokens_by_sequence_matcher(
        prev_tokens, prev_timestamps, new_tokens, new_timestamps,
        offset=60.0, overlap=4.0,
    )

    merged_text = "".join(tokens)
    assert "chalk? Just" in merged_text
    assert tokens.count("chalk") == 1
