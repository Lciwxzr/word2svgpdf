# word2svgpdf

**Restore vector figures in a Word-exported PDF.**

> **Translation notice.** This English README is a translation of the Chinese
> original, produced with **DeepSeek-V4.1-Flash**. The Chinese version is the
> authoritative one; if the two disagree, trust the Chinese text.
> → [中文说明 / Chinese original](i18n/README.zh-CN.md)

Word rasterises some of the figures you insert when exporting to PDF. This tool
puts the original vector artwork back, then verifies page by page that nothing
else changed.

## How it works

**The original SVG of a figure inserted into Word can be recovered by unzipping
the .docx.** Converting that SVG to PDF with Inkscape gives a vector version
that can be matched against the rasterised bitmap in the exported PDF and
swapped in.

A `.docx` is a zip archive. When you insert an SVG, Word stores two files:

```
word/media/image1.png      <- the bitmap Word rendered (fallback)
word/media/image2.svg      <- the original SVG you inserted
```

They are paired in `document.xml`: `<a:blip>` points at the PNG and
`<asvg:svgBlip>` at the SVG. **The vector data is never lost** — Word simply
does not use it when exporting to PDF.

If the SVG contains bitmap elements, the exported PDF holds that figure as a
fixed-resolution bitmap. The tool does this:

1. unzip the docx and pull out each figure's original SVG
2. convert that SVG to a vector PDF with Inkscape
3. locate the rasterised bitmap in the exported PDF (document order, then
   display size, then image content)
4. overlay the vector PDF in the same rectangle and make the old bitmap fully
   transparent

The result is a PDF whose figures are true vector artwork — text selectable,
sharp at any zoom — while the body text, captions and page numbers outside the
figure areas stay exactly as they were.

## Why it is needed

Word's SVG support has always been patchy: **if the SVG embeds `<image>`
elements, Word downgrades the whole figure to a bitmap.** The rule is
unambiguous — measured across 14 figures in this project's test documents:

| `<image>` elements in the SVG | What Word exports |
|---|---|
| 0 | stays vector |
| ≥ 1 | whole figure rasterised (usually 266 dpi) |

So in practice only the few figures that contain snapshots or diagrams need
fixing, not all of them.

## Quick start

```
setup.bat     :: first run: build the environment (~2 min, needs network)
start.bat     :: every run after that
```

`start.bat` prints the server address and copies it to your clipboard — press
Ctrl+V in your browser.

The port is chosen automatically: it starts at 8770 and skips any port that is
taken or reserved (Windows reserves whole ranges for Hyper-V / WSL / Docker).

**The browser is not opened automatically by default.** On Windows, if a
browser is already running elevated, a normal-privilege process cannot hand it
a URL — you get *"Edge isn't responding because an existing instance is running
with elevated privileges"*. Set `VECTORFIG_OPEN_BROWSER=1` to opt in.

If the environment is missing, `start.bat` runs `setup.bat` automatically.

Offline machines: run `make_wheels.bat` once on a networked machine, copy the
whole folder (including `wheels\`), and `setup.bat` will need no network.

### Two-stage workflow

The UI is deliberately split into two steps, so you can inspect the matching
before paying for the Inkscape conversions:

```
Top pane · Word document  ┐
                          ├─→  ① Match  →  review/edit  →  ② Convert  →  *_vec.pdf
Bottom pane · exported PDF ┘
```

1. **① Match** — upload the `.docx` and the `.pdf` it produced. Matching takes
   milliseconds to seconds, then a table appears with one row per figure that
   needs handling.
2. **Review and edit** — expanding a row gives two columns:
   - **left is the reference image**: the bitmap Word put in the PDF, which is
     what tells you which figure belongs there
   - **right is the candidates**: every possible bitmap placement in the PDF,
     with thumbnail, page number and similarity
   - click any candidate to pin the figure to that placement
   - click **Skip** to leave this figure as Word exported it
   - drop an `.svg` on the dashed tile at the end of the candidate row to
     replace this figure's source artwork
   - click **Undo** to go back to the automatic result
3. **② Convert** — only now does Inkscape run (about 13 s per figure). You get
   the output PDF plus a verification report.
4. **Re-convert** — found a wrong mapping after converting? Fix it and press
   **② Convert** again. Already-converted figures are reused, so only the
   overlay is redone and it takes seconds.

### After conversion

- **Save as** — opens the system "save as" dialog (Chrome/Edge; other browsers
  fall back to a normal download)
- **Page compare** — whole pages side by side, with wipe / overlay / split modes
- **Figure compare** — just the replaced figure areas, enlarged
- **Verification report** — the tool compares every page and tells you whether
  anything outside the figure areas changed

### Embedded photo resolution

The output panel has a selector, defaulting to **"keep original (no
recompression)"**:

| Option | Effect |
|---|---|
| Keep original (default) | bitmaps keep their embedded resolution; only the figure is swapped |
| 600 dpi | balance of size and detail |
| 300 dpi | smallest, still fine for most journals |
| 900 / 1200 dpi | higher detail |

## Figure matching

Matching scores three independent signals and assigns them globally, one to
one, with the Hungarian algorithm:

| Signal | Strength | Notes |
|---|---|---|
| **Image content** | strongest | Every drawing has a PNG preview in the docx — the bitmap Word itself rendered. Composited onto white and correlated against the PDF raster, the correct pair scores **+1.000** and a wrong one **−0.06** |
| Display size | medium | docx EMU size vs the placed rect, 4 pt tolerance |
| Document order | weak (tie-breaker only) | All drawings are inline, so Word emits rasters in document order, and the PDF lists them in reading order |

The reasoning is written into the report's note column, e.g.
`page 10 (image similarity 1.00, size matches)`.

Why not size alone: two figures can legitimately share the same display size.
An earlier version reported "ambiguous" in that case; now the image content
decides — measured with both figures forced to identical sizes, and with the
document order scrambled, the correct pairing is still found (similarity 1.00).

One-to-one assignment also guarantees two figures cannot claim the same raster.

### Manual override

The automatic result is not final. Between **① Match** and **② Convert** every
figure can be corrected:

| Action | Effect |
|---|---|
| click a candidate thumbnail | pin the figure to that bitmap placement |
| Skip | leave this figure as Word exported it |
| Undo | return to the automatic result |
| drop an SVG | replace this figure's source artwork with an external `.svg` (useful when the docx has no usable SVG, or you want a different figure) |
| Restore docx source | undo a custom SVG |

Manually pinned rows are highlighted and their note reads "user specified".
Press **② Convert** to apply.

### Scaling rule

The vector figure is scaled to fit **inside** the original rectangle, keeping
its aspect ratio, so it can never bleed over body text, captions or a
neighbouring figure:

```
scale = min(box_width / fig_width, box_height / fig_height)
```

The result is centred, and at least one dimension exactly meets the box edge.

Example: box 700×900, figure 800×1200 → scale 0.75 → placed 600×900
(height exactly fits, width 600 < 700, fully inside).

A non-uniform scale would distort the artwork; any scale larger than the box
would cover surrounding content.

## Requirements

- Python 3.10+
- [Inkscape](https://inkscape.org/) — **required**, used for SVG → vector PDF

`start.bat` finds both automatically, creates a virtual environment and
installs the pinned dependencies from `requirements.txt`.

If Inkscape is not in a standard location, set `INKSCAPE` to the full path of
`inkscape.exe`.

### Dependencies

| Package | Licence | Used for |
|---|---|---|
| pypdfium2 | Apache-2.0 / BSD-3-Clause | rendering, text extraction, image location |
| pikepdf | MPL-2.0 | object-level PDF editing, keeps object numbers |
| starlette | MIT | HTTP framework |
| uvicorn | BSD-3-Clause | ASGI server |
| python-multipart | Apache-2.0 | form uploads |
| Pillow | HPND | bitmaps, image signatures |
| lxml | BSD-3-Clause | docx and SVG parsing |
| scipy | BSD-3-Clause | Hungarian assignment (optional; a greedy fallback exists) |

External program:

| Program | Licence | Used for |
|---|---|---|
| Inkscape 1.x | GPL-2.0-or-later | SVG → vector PDF |

Inkscape is invoked as a separate executable over the command line; its code is
not linked, so it does not make this project a derivative work. If you
redistribute Inkscape together with this tool, GPL-2.0 requires you to make its
source available too.

### What `setup.bat` actually does

It is a script that configures the environment on your machine, so here is its
complete behaviour. **It needs no administrator rights and does not modify the
system.**

**It only reads:**

| Location | Why |
|---|---|
| `%VECTORFIG_PYTHON%`, then `python.exe` on `PATH`, then `%USERPROFILE%\miniconda3`, `%USERPROFILE%\anaconda3`, `%LOCALAPPDATA%\Programs\Python\Python31{0,1,2,3}`, `C:\ProgramData\{mini,}conda3` | to find a Python 3.10+ interpreter |
| `C:\Program Files\Inkscape`, `C:\Program Files (x86)\Inkscape`, `%LOCALAPPDATA%\Programs\Inkscape`, `env\inkscape`, `%INKSCAPE%` | to find `inkscape.exe` |
| `requirements.txt`, `wheels\` | the dependency list and optional offline cache |

It runs only these commands, all through the interpreter it found:

| Command | Purpose |
|---|---|
| `python -c "import sys; sys.exit(...)"` | check the version is 3.10+ |
| `python --version`, `inkscape.exe --version` | report what it found |
| `python -m venv env\venv` | create the isolated environment |
| `env\venv\Scripts\python.exe -m pip install -r requirements.txt` | install the pinned packages |

There is no `powershell`, `curl`, `reg`, `setx`, `schtasks` or any other
system-modifying command in the script.

**It only writes:**

| Location | What |
|---|---|
| `env\venv\` (in the project folder) | a Python virtual environment |
| `%LOCALAPPDATA%\vectorfig\venv\` | the same, **only if** the first location fails (e.g. OneDrive locks the folder) |
| that venv's `Lib\site-packages\` | the packages listed in `requirements.txt` |

**It never:** edits the registry, changes `PATH` or any environment variable,
installs a service or scheduled task, requests elevation, writes outside the two
locations above, or downloads anything except the packages pinned in
`requirements.txt` (and only from PyPI, or from the local `wheels\` folder when
present).

To undo everything, delete `env\` (and `%LOCALAPPDATA%\vectorfig\` if it was
used). Nothing else is left on the machine.

If you would rather not run a batch file, the manual equivalent is:

```bat
python -m venv env\venv
env\venv\Scripts\python.exe -m pip install -r requirements.txt
start.bat
```

`start.bat` itself only starts the local server, prints its address and copies
it to the clipboard; it opens a browser only if `VECTORFIG_OPEN_BROWSER=1`.

## Licence

**AGPL-3.0**. See [LICENSE](LICENSE).

The PDF layer uses pypdfium2 (Apache-2.0/BSD) and pikepdf (MPL-2.0), both
permissive — AGPL was chosen deliberately, so that this tool and its
derivatives stay open for the people whose manuscripts it processes.

If you need to use it in a closed-source project you may relicense this code
under MIT, provided no GPL component is pulled in; Inkscape is called over the
command line and is not a derivative work.

## Verification

For every page:

1. mask out the replaced figure rectangles
2. compare the **text** in the remaining area (with positions)
3. compare the **rendered pixels** in the remaining area

A page passes when both are identical. Any real difference is reported with the
specific words that differ.

## Repository layout

```
setup.bat  start.bat  make_wheels.bat     entry points
requirements.txt  LICENSE  README.md  .gitignore
i18n/README.zh-CN.md

w2sp/                     the package
  __main__.py             python -m w2sp starts the server
  app.py                  HTTP API (two-stage workflow, overrides, custom SVG)
  core.py                 pipeline: analyse / convert / compose, verification
  smart_match.py          figure matching (order → size → content, Hungarian)
  pdfio.py                PDF reading  — pypdfium2 (render, text, image location)
  pdfedit.py              PDF writing  — pikepdf (overlay, blank bitmap, keep objects)
  downsample.py           optional image downsampling, alpha-safe
  run.py                  launcher (free port, clipboard)
  static/index.html       single-page UI

tools/                    developer utilities, not needed at runtime
  collect_wheels.py       rebuild wheels from installed packages, no network
```

Command line use (no UI):

```python
from core import Progress, run_pipeline
rep = run_pipeline("in.docx", "in.pdf", "out.pdf", Progress(print))
print(rep["ok"], rep["size_after"])
```

For staged control, use `Session`:

```python
from core import Progress, Session

sess = Session("in.docx", "in.pdf", "workdir")
sess.analyse(Progress(print))     # match only, milliseconds
# sess.figures[i].override can be set here for a manual pin
sess.convert(Progress(print))     # convert only, ~13 s per figure
rep = sess.compose("out.pdf", Progress(print))
```

## Known limitations

- Matching prefers document order + size. Content correlation only runs when
  the order cannot be trusted (floating figures, mismatched counts). If it is
  still not decisive the figure is marked **pending** and can be pinned by hand
  — it is never silently skipped.
- Output files grow, because photos inside the figures are restored from the
  266 dpi Word used back to their native resolution. The default "keep
  original" does no recompression; pick 300/600 dpi for a smaller file.
- The "save as" dialog needs a browser with File System Access API support
  (Chrome/Edge) and must be reached over `http://127.0.0.1` (localhost is a
  secure context). Otherwise it falls back to a normal download.
- The browser is not opened automatically by default; the address is copied to
  the clipboard instead.
- Writing uses pikepdf's object-level editing, so all original object numbers
  survive, but the replaced page keeps its old bitmap in transparent form (not
  rendered, small size cost). That is the price of preserving numbering.
- Inkscape cannot be installed automatically: its download page refuses
  automated requests and is JS-rendered. Users install it once, by hand.

## Packaging

A minimal source release is the repository itself. For offline installation,
`collect_wheels.py` rebuilds `wheels/` from the packages already installed,
without a network; `make_wheels.bat` downloads them from PyPI instead.
