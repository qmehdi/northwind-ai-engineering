# 0003. An exact-match response cache and provider prompt caching, not a semantic cache

Date: 2026-09-18. Status: accepted.

## Context

The policy service answers the same questions many times: a cohort asks "what uptime does Enterprise get" from forty laptops. A model call costs money and seconds. A semantic cache (serve the nearest cached answer when a new question embeds close to an old one) is the popular lever and cuts the most calls. A policy answer that is nearly right is wrong: "can I get a refund after 30 days" and "can I get a refund after 60 days" embed close and have different answers, and the second one quoted to a customer is a liability.

The system prompt and the retrieved chunks are the bulk of every prompt, and the provider caches a prompt prefix at a fraction of the input price.

## Decision

Two caches, neither semantic. First, the provider's prompt cache: the system prompt is marked as a cache breakpoint in `nw/llm/providers/anthropic_base.py`, so every call pays the full price for the question and the chunks only. Second, an exact-match response cache in the service (`nw/policy/cache.py`): the key is the normalised question (case, whitespace, trailing punctuation), the audience, `k`, the index manifest hash, the prompt version and the model id. A rebuilt index, a changed prompt or a changed model empties it without anyone remembering to. TTL and size come from `NW_POLICY_CACHE_TTL_S` and `NW_POLICY_CACHE_SIZE`; the default is off. `cached: true` is on the response, `nw_policy_cache_total{result}` counts hits and misses.

## Consequences

- Repeat questions cost nothing and answer in milliseconds; paraphrases cost a full call. That trade is deliberate and the reference tab explains it.
- The cache can never return an answer produced by another index, prompt or model, so an evaluation run and a served answer with the same key are the same answer.
- Feedback on a cached answer still gets its own `answer_id`, so a wrong cached answer is reported like any other.
- A cohort that wants a semantic cache adds it as a separate layer in front of `/ask` with its own evaluation on the golden set; the exact-match cache is not the place to loosen matching.
