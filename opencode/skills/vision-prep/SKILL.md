---
name: vision-prep
description: Preprocess large images and PDF pages before sending them to a vision model. Use when the task mentions reading a large image, a screenshot with small text, a PDF page, or "大图看不清", "PDF 里的图片", "识别 PDF", "图片文字太小". DeepSeek vision downscales each image to ~800x800 and rejects PDF input, so large images must be tiled and PDFs rasterized first.
---

# Vision Preprocessing (large images / PDF pages)

Prepare images and PDF pages so the DeepSeek vision model can actually read
them. Two hard constraints drive this:

1. **DeepSeek rejects PDF input** — only JPEG/PNG/GIF/WebP are accepted. A
   PDF must be rasterized to PNG first.
2. **DeepSeek downscales each image to ~800x800 pixel budget** before
   tokenizing. Small text in a large image becomes unreadable. Slicing into
   overlapping tiles gives each tile its own ~800x800 budget.

## When to use

- The user attaches a large image / screenshot and small text is unreadable.
- The user asks to read content inside a PDF (text or embedded images).
- The vision agent reports it cannot make out details in an image.

## Prerequisites

Python 3 with `pymupdf` and `pillow` (already installed). Scripts live in
`scripts/` next to this file.

## Workflow

### 1. PDF -> PNG pages

```powershell
python <skill_dir>/scripts/pdf2png.py <input.pdf> <output_dir> [--pages 1-3,5] [--zoom 2.0]
```

Renders each page (or `--pages` subset) to `page-<n>.png`. `--zoom` controls
resolution (2.0 ≈ 144dpi, good default for text). Rasterizing preserves page
layout, which matters when the goal is "what is in this image within the
page context".

### 2. Large image / rendered page -> tiles

```powershell
python <skill_dir>/scripts/tile.py <input.png> <output_dir> [--grid 2x2] [--overlap 0.10]
```

Slices into an NxN grid of overlapping tiles (`tile-<row>-<col>.png`). Use
when the image's long edge exceeds ~1600px or it contains dense small text.
The 10% overlap prevents text straddling a cut line from being truncated.

### 3. Read tiles with the vision model

Send each tile to the model separately (e.g. via the `read` tool / vision
agent), so each tile gets its own ~800x800 budget. Extract content per tile,
then merge the results in the text layer. For very dense content (e.g.
300-DPI legal documents), use a finer grid (3x3) or request tiles one at a
time.

## Decision guide

| Input | Action |
|---|---|
| Small image (< ~1600px long edge) | Send directly, no prep |
| Large image / dense small text | `tile.py` into 2x2 (or finer) grid |
| Grid size (when tiling) | `--grid NxN` with N = ceil(long_edge / 800), so each tile stays <= ~800px |
| PDF (any) | `pdf2png.py` to PNG, then tile if page is large |
| Byte/size limit exceeded | Already handled by this repo's `attachment.image` resize (1600px / 2 MiB); no action needed |

## Notes

- This repo caps attachments at 1600x1600 / 2 MiB (`attachment.image` in
  `opencode.jsonc`), below opencode's own default — so the upload limit is not
  the problem; the model's ~800x800 downscale is. Tiling is the fix for
  readability, not resizing.
- Always use absolute paths.
- After tiling, describe each tile's content and combine; do not send the
  original full-size image expecting the model to read fine print.
