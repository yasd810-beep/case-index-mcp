# Case Index MCP

A local [Model Context Protocol](https://modelcontextprotocol.io/) server for incremental indexing of document folders, with offline Cyrillic OCR on CPU. It is independent of any particular MCP host and does not require Kimi, Claude, or another AI desktop application to be installed.

## What it does

- Indexes PDF, DOCX, TXT, Markdown, RTF, ODT, PNG, JPEG, TIFF, and BMP files.
- Extracts existing PDF text where available; otherwise renders pages at 200 DPI and runs OCR.
- Uses bundled RapidOCR/OpenVINO models for the primary OCR pass.
- Optionally runs PaddleOCR as a second pass for uncertain OCR results.
- Reprocesses only new, changed, or previously failed documents; removes stale index entries when source files disappear.
- Writes a manifest, per-page text, combined transcripts, and a Russian `СТАТУС_дела.md` report.
- Supports foreground and background runs, progress inspection, cancellation, and transcript retrieval.
- Never changes supported source documents. It creates `_work/` and `СТАТУС_дела.md` inside the indexed folder.

OCR transcripts are drafts. Always verify names, dates, numbers, and pages marked `needs_review` against the originals. This software indexes documents; it does not provide legal analysis or legal advice.

## Requirements

- Python 3.11 or newer (3.11–3.13 recommended).
- A CPython environment supported by the pinned OpenVINO and pypdfium2 wheels.
- Network access for Python package installation. The bundled primary OCR works offline afterward.

## Install

```bash
git clone https://github.com/yasd810-beep/case-index-mcp.git
cd case-index-mcp
python -m venv .venv
```

Activate the environment and install the primary engine:

```bash
# Windows PowerShell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# macOS / Linux
source .venv/bin/activate
pip install -r requirements.txt
```

Optional PaddleOCR fallback:

```bash
pip install -r requirements-fallback.txt
```

PaddleOCR models are downloaded by PaddleOCR on first use. Set `CASE_INDEX_CACHE_DIR` to override the default cache directory, `~/.case-index-mcp`. Set `use_paddle_fallback` to `false` when indexing if you do not install or want the fallback.

## Connect an MCP host

Add a stdio server using the absolute path to the virtual environment's Python and to `server.py`. A generic configuration looks like this:

```json
{
  "mcpServers": {
    "case-index": {
      "command": "C:\\tools\\case-index-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\tools\\case-index-mcp\\server.py"]
    }
  }
}
```

On macOS or Linux, for example:

```json
{
  "mcpServers": {
    "case-index": {
      "command": "/opt/case-index-mcp/.venv/bin/python",
      "args": ["/opt/case-index-mcp/server.py"]
    }
  }
}
```

Consult your MCP host's documentation for the location and schema of its configuration file. Restart the host after changing the configuration.

## Tools

| Tool | Purpose |
|---|---|
| `index_project` | Index a folder. Arguments: `project_path`, optional `force`, `use_paddle_fallback`, and `background`. |
| `get_index_status` | Return active-run progress, last-run details, documents, errors, and pages needing review. |
| `cancel_index` | Request safe cancellation after the current processing step. |
| `read_transcript` | Read a complete transcript or one page by project-relative `document_path`. |

For a large folder, call `index_project` with `background=true`, then poll `get_index_status`. Do not delete lock files to stop a run; use `cancel_index`. If files are added while a run is active, start another run after it finishes.

## Output layout

```text
indexed-folder/
├── _work/
│   ├── case-index/
│   │   ├── manifest.json
│   │   └── run.json
│   ├── pages/
│   └── transcripts/
└── СТАТУС_дела.md
```

The manifest contains source paths, content hashes, extraction metadata, and OCR diagnostics. Treat index output as potentially sensitive because it contains text from the indexed documents. Do not publish an indexed project or its `_work` directory with this software.

## Verify

The integration suite creates only synthetic documents under `test-run/`:

```bash
python tests/integration_test.py
```

An additional smoke test checks bundled-model hashes and primary OCR on a generated image:

```bash
python tests/ocr_smoke.py
```

## License and models

Code is provided under Apache License 2.0. The bundled PaddleOCR model files and dictionary are also distributed under Apache License 2.0; see `NOTICE`. SHA-256 hashes in `models/SHA256.json` are verified before OCR starts.
