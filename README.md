# resume-parser-tool

Batch resume parser (PDF / DOCX → Excel) with two tools.

## Tools

Both tools also add these columns to every row: LinkedIn, Current Company, Current Designation, Notice Period,
Current CTC, Expected CTC, Certifications, Languages, and **All Skills (Auto-Detected)**. Skills come from the
candidate's Skills section plus a vocabulary scan of the whole resume, so nothing depends on a fixed list.
Fields the resume does not state are left blank.

| Tool | Extracts | Skill/industry scope |
|---|---|---|
| **Skills from Experience (Tech Stack)** — `xcelgrad_tech.py` | Name, Email, Phone, Education, 18 tech skills (1/0) | Experience, Internship and Projects sections only |
| **Industry / Vertical Mapping** — `xcelgrad_sales.py` | Name, Email, Phone, Education, Location, Total Years of Experience, 20 industries (1/0) | Whole resume |

## Run

```bash
python -m venv venv
venv\Scripts\activate          # Windows  (source venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Architecture

- `resume_core.py` — the engine. No Streamlit/pandas imports, so worker processes start fast.
  - PDF text via **pypdfium2** (PDFium; Apache-2.0/BSD licence — safe to ship commercially).
  - DOCX text by reading the XML directly (~10x faster than python-docx, and also reads headers
    and text boxes, where many templates put the candidate's contact details).
  - `process_batch()` runs files across a process pool, skips duplicate files, caches results by
    file hash, and returns a clear reason for every file it could not read.
- `batch_ui.py` — the shared Streamlit page (upload → progress → stats → table → Excel).
- `xcelgrad_tech.py` / `xcelgrad_sales.py` — each tool is just a `Profile` (columns + regex patterns).
  To add a skill or industry, add one line to its pattern dict.

## Performance

300 resumes, 12-core Windows laptop (synthetic 2-page resumes):

| | Time |
|---|---|
| Before (PyPDF2, one file at a time) | 4.9 s |
| After, single process | 2.1 s |
| After, process pool (default in the app) | **0.3–0.7 s** |

The upload limit is 500 files per batch. Scanned (image-only) PDFs are reported as
"No text layer — needs OCR" instead of being silently skipped.

## Tests

```bash
pip install pytest
python -m pytest tests
```
