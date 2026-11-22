from weavemem.app.recall_service import estimate_tokens


def test_token_estimate_counts_cjk_and_punctuation_conservatively():
    assert estimate_tokens("记忆") == 2
    assert estimate_tokens("hello") == 2
    assert estimate_tokens("hello!") == 3
    assert estimate_tokens("记忆 hello!") == 5
