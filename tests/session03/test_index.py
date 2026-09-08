import numpy as np
import pytest

from nw.semantic.embed import TicketIndex, build_index

pytestmark = pytest.mark.session03


def test_index_returns_nearest_by_cosine(tmp_path):
    rows = [
        {
            "ticket_id": f"T-{i}",
            "subject": f"s{i}",
            "body": "b",
            "priority": "P2",
            "queue": "q",
            "tags": [],
            "answer": "a",
        }
        for i in range(5)
    ]
    vectors = np.eye(5, dtype=np.float32)
    index = build_index(rows, vectors=vectors)
    hits = index.search(np.asarray([[0, 0, 0.9, 0.1, 0]], dtype=np.float32), k=2)[0]
    assert hits[0][0] == "T-2" and hits[1][0] == "T-3"
    index.save(tmp_path)
    reloaded = TicketIndex.load(tmp_path)
    assert reloaded.search(np.asarray([[1, 0, 0, 0, 0]], dtype=np.float32), k=1)[0][0][0] == "T-0"
