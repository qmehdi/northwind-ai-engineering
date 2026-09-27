"""`--no-judge` means no Judge call: the retrieval experiments must be free."""

import pytest

from nw.llm.providers.fake import FakeProvider
from nw.policy.chunking import chunk_corpus
from nw.policy.evaluate import EvalCase, evaluate
from nw.policy.retrieval import HashEmbeddings, OverlapReranker, PolicyIndex
from tests.session04.test_answer import draft

pytestmark = pytest.mark.session04


@pytest.fixture
def small_index(corpus_dir):
    return PolicyIndex(chunk_corpus(corpus_dir), HashEmbeddings(), reranker=OverlapReranker())


async def test_use_judge_false_makes_no_judge_call(small_index, make_client):
    good = small_index.retrieve("Enterprise uptime", k=3)[0].chunk.id
    provider = FakeProvider([draft("Enterprise gets 99.95 percent.", [good])])
    client = make_client(provider, max_concurrency=1)
    cases = [EvalCase(id="1", question="Enterprise uptime?", gold_chunk_ids=[good])]
    results, agg = await evaluate(client, small_index, cases, k=3, concurrency=1, use_judge=False)
    assert len(provider.calls) == 1, "only the answer was generated"
    assert agg["faithfulness"] is None and agg["faithfulness_n"] == 0
