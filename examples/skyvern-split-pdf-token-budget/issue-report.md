# Split PDF page excerpts can exceed the configured input token budget

**Observed behavior**

At commit `b04c3f7fb4b2f60ab49c4de05e5324ef97bdda2d`, `SplitPdfBlock._build_page_texts` can return page excerpts whose aggregate token count exceeds `MAX_FILE_PARSE_INPUT_TOKENS = 256_000`:

- 300 pages of dense identifier text: **693,300 tokens** after truncation (2,311 tokens per page against a per-page budget of 853, because truncation slices `per_page_token_budget * 4` characters).
- 1,100 short pages containing 250 tokens each: **275,000 tokens**, because `MIN_PER_PAGE_TOKENS = 256` overrides the aggregate cap.
- 2-page control: preserves text and page numbering at **16 tokens**.

**Policy routing (`issue_only`)**

Choosing between lowering per-page excerpts below `MIN_PER_PAGE_TOKENS` versus rejecting huge PDFs requires an upstream product decision (`design_uncertainties` non-empty). Accordingly, `oss-audit` validated the `RED` execution receipt (`2 failed, 1 passed`) and routed the candidate to `issue_only` rather than fabricating an unapproved implementation.
