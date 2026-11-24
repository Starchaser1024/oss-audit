### Describe the bug

`FilterRetriever.run_async`, `AutoMergingRetriever.run_async`, and `SentenceWindowRetriever.run_async` call `document_store.filter_documents_async()` unconditionally. Because async methods are optional on the `DocumentStore` protocol (`haystack/document_stores/types/protocol.py`), passing a sync-only `DocumentStore` raises `AttributeError` instead of falling back or guarding consistently with `DocumentWriter.run_async` and `CacheChecker.run_async`.

### Minimal Reproduction

```python
import asyncio
from haystack.components.retrievers import FilterRetriever
from haystack.testing.factory import document_store_class

store = document_store_class("MockedDocumentStore")()
retriever = FilterRetriever(document_store=store)
asyncio.run(retriever.run_async())
```

Observed on `main` (`34a1a09`): `AttributeError: 'MockedDocumentStore' object has no attribute 'filter_documents_async'`.
