import pytest
import torch

from nw.semantic.data import TAGS, Collate, TicketDataset, multi_hot, stratified_subset

pytestmark = pytest.mark.session03


def test_multi_hot_uses_fixed_vocabulary():
    y = multi_hot(["Outage", "Nonsense", "SSO"])
    assert y.shape == (len(TAGS),)
    assert y.sum() == 2 and y[TAGS.index("Outage")] == 1


def test_collate_pads_to_longest_in_batch(tiny_tokenizer, s3_rows):
    ds = TicketDataset(s3_rows[:8])
    batch = Collate(tiny_tokenizer, max_length=64)([ds[i] for i in range(8)])
    assert batch["input_ids"].shape[0] == 8
    assert batch["input_ids"].shape[1] <= 64
    assert batch["tags"].shape == (8, len(TAGS)) and batch["priority"].dtype == torch.long


def test_stratified_subset_keeps_p0(s3_rows):
    sub = stratified_subset(s3_rows, 100)
    assert 80 <= len(sub) <= 120
    assert any(r["priority"] == "P0" for r in sub)
