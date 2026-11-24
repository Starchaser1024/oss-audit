### Background / Description

Word writes a blank line as a `<w:p>` with no visible text. `WordParser` keeps paragraphs that contain no runs at all, but drops paragraphs that still carry a whitespace or break-only run:

- a paragraph holding a single space (space before Enter)
- a paragraph holding only a soft line break (`<w:br/>` from Shift+Enter on an empty line)

`_extract_text_from_paragraph()` strips extracted text to `""`, reaching the blank-line branch in `src/agentscope/rag/_parser/_word.py:422`, where `para._element.findall(".//" + qn("w:r"))` rejects the paragraph and merges surrounding paragraphs with a single newline instead of preserving the paragraph boundary.

### Minimal Reproduction

```python
import io
from docx import Document
from agentscope.rag import WordParser

doc = Document()
doc.add_paragraph("Paragraph one.")
doc.add_paragraph(" ")  # or: doc.add_paragraph().add_run().add_break()
doc.add_paragraph("Paragraph two.")
buffer = io.BytesIO()
doc.save(buffer)

sections = await WordParser(include_image=False).parse(buffer.getvalue(), "demo.docx")
assert sections[0].content.text == "Paragraph one.\n\nParagraph two."
```

Observed on `main` (`35cd00c`): `'Paragraph one.\nParagraph two.'`
Expected: `'Paragraph one.\n\nParagraph two.'`
