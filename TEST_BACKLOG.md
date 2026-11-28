# Deferred acceptance tests

Keep this list with the release. When a test runs, record the date, revision, outcome and a reproducible check. Keep evidence from the private room in the private evaluation notes.

## Compaction notices

- [ ] **At the next natural compaction with the room open,** check the temporary label above the message box and the single timestamped Service entry afterward. Ask Lantern to open that entry by its room reference. The browser polls every 1.5 seconds, so a short compaction can finish between polls. Automated coverage exercises the start, completion, interruption and restart cases.

## Semantic history search

- [ ] **After semantic search has been live long enough for Lantern to mark useful passages,** test independent discovery with fresh paraphrased questions that omit the historical keywords. Include a question whose answer is absent. Give exact search and exact-plus-semantic search the same clean starting context, eligible history, cutoff and evidence budget. Keep the answer key and earlier test discussions out of both inputs.
- [ ] Score two things separately: whether retrieval returned the original evidence, and whether Lantern used it accurately. Check citations, authorship, timing, scope and acknowledgement of missing data. Compare the calls, latency and cost. The three existing exact-search trials are recorded privately. The third recovered sources after compaction with topic words still present in the checkpoint.
- [ ] After marks have accumulated, compare curated passages with a broader sample of eligible passages at the same budget. Record what curation missed, index size and query latency. Use those results to decide whether to test reranking.

## Rolling context

- [ ] **Before the trial,** verify that the deployed compaction version matches the baseline tag. Reconcile per-request instrumentation against billing receipts, including retries, failures and missing receipt coverage. Collect the compaction baseline before enabling rolling mode.
- [ ] On the development branch, compare maximum-fit and batched scrolling on matched tasks with the same route and output reserve. Check the earliest event, complete tool-call/result pairs, room for large outputs and recovery after restart. Measure cache-read tokens as a share of input tokens, latency, retrieval accuracy and settled cost per completed turn.
- [ ] Rehearse returning to the tagged compaction version with a copy of the trial history. Check existing references and a new turn. Record the result and cost comparison before deciding whether to merge.
