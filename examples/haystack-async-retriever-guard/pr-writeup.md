Fixes #13380

### Summary

Three async retrievers (`FilterRetriever`, `AutoMergingRetriever`, `SentenceWindowRetriever`) assumed `document_store.filter_documents_async` was always implemented on every `DocumentStore`. Following maintainer review on #13381, a shared `_call_with_async_fallback` helper in `haystack/utils/async_utils.py` dispatches async store methods when available and offloads sync-only `DocumentStore` implementations to a worker thread across retrievers, `CacheChecker`, and `DocumentWriter`.

### Verification

- RED on `main` (`34a1a09`): all three async retrievers fail with `AttributeError` on sync-only `MockedDocumentStore`.
- GREEN on fix (`de63990`): `pytest test/components/retrievers/test_filter_retriever_async.py test/components/retrievers/test_auto_merging_retriever_async.py test/components/retrievers/test_sentence_window_retriever_async.py -q` — **38 passed**, plus `CacheChecker` and `DocumentWriter` sync-fallback suites and `pre-commit` hooks clean.
