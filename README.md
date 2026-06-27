# VALD Line List Tools

An updated version of VALDLineList: https://github.com/Anthony-Giacinto/VALDLinelist

Automate downloading and combining line lists from the
[Vienna Atomic Line Database (VALD3)](http://vald.astro.uu.se/).

The VALD3 web interface caps how many lines a single "Extract All" request
returns, so building a line list over a wide wavelength range means submitting
many small requests, waiting for the emailed results, and stitching them back
together. This tool automates that loop:

1. **submit** — fill out the VALD Extract-All form for each wavelength window
   (Selenium).
2. **fetch** — pull the emailed results from Gmail, download, and unzip them
   (Gmail API).
3. **combine** — append the per-window files into one line list with corrected
   header metadata.

The output is suitable for downstream tools such as Turbospectrum's
`vald3line-BPz-freeformat`.

---

## Requirements

- Python 3.8+
- Google Chrome or Chromium installed locally
- A **registered VALD account** (the email you log in with)
- A **Gmail account** receiving the VALD results, with Gmail API access set up

Install Python dependencies:

```bash
pip install -r requirements.txt
```

`webdriver-manager` downloads a matching ChromeDriver automatically on first
run, so you do not need to install or path-manage the driver yourself.

---

## One-time setup: Gmail API

The `fetch` step reads your inbox through the Gmail API and needs an OAuth
token saved as `token.json` in the working directory. This repo includes
`gmail_quickstart.py` (adapted from Google's official quickstart) to generate
that token for you.

1. **Enable the Gmail API and download credentials.** In the
   [Google Cloud Console](https://console.cloud.google.com/), create (or reuse)
   a project, enable the Gmail API, configure an OAuth consent screen, and
   create an **OAuth client ID** of type *Desktop app*. Download its JSON and
   save it as `credentials.json` in this folder. Google's
   [Gmail API quickstart guide](https://developers.google.com/gmail/api/quickstart/python)
   walks through these console steps with screenshots.

2. **Run the quickstart once to authorize.** With `credentials.json` in place:

   ```bash
   python gmail_quickstart.py
   ```

   A browser window opens asking you to sign in and grant read-only access to
   your Gmail. On success it prints your Gmail labels and writes `token.json`
   next to the script. That confirms the connection works.

3. **You're done.** `vald_linelist.py fetch` will reuse `token.json` from then
   on. The token uses the read-only scope (`gmail.readonly`), which is all the
   pipeline needs.

### Refreshing an expired token

The OAuth token does not last forever — it expires after a period of inactivity
(and any time you change scopes). When that happens, `fetch` fails with an
authentication / invalid-credentials error. The fix is to delete the old token
and re-run the quickstart to mint a fresh one:

```bash
del token.json          # Windows (PowerShell / cmd)
# rm token.json         # macOS / Linux
python gmail_quickstart.py
```

Re-running opens the browser consent flow again and writes a new `token.json`.
It's worth doing this any time the pipeline suddenly stops downloading emails.

> **Do not commit `credentials.json` or `token.json`.** They are personal
> secrets. A `.gitignore` entry is included for them.

---

## Usage

The module is runnable as a CLI with three subcommands. Run `--help` on any of
them for the full option list.

### 1. Submit requests to VALD

Submits one Extract-All request per wavelength window. By default it only
submits windows you do not already have downloaded (so it is safe to re-run
after a partial batch); pass `--all` to force every window.

```bash
python vald_linelist.py submit \
    --email you@example.com \
    --teff 3700 --logg 4.8 --det 0.00005 \
    --wave-start 4500 --wave-end 13000 --step 25
```

Results are delivered asynchronously by email — give VALD some time before
running `fetch`.

### 2. Fetch emailed results

Scans your Gmail inbox for messages from the chosen VALD mirror, downloads the
`.gz` attachments, unzips them, and saves them to `vald_ll/`. Already-downloaded
files are skipped, so this is safe to re-run as more results trickle in.

```bash
python vald_linelist.py fetch --server uppsala --token token.json
```

### 3. Combine into a single list

Appends every per-window file into one line list, keeping a single header and
footer and rewriting the header's selected/processed line counts to the totals.

```bash
python vald_linelist.py combine \
    --teff 3700 --logg 4.8 --det 0.00005 \
    --wave-start 4500 --wave-end 13000 --step 25 \
    --input-folder vald_ll \
    --output Early_MD_Teff_3700_logg_48_vmic_1.vald
```

> The stellar parameters passed to `combine` **must match** those used for
> `submit`, because they reconstruct the per-window filenames.

---

## Using it as a library

Every command is also a plain function you can import:

```python
import numpy as np
from vald_linelist import vald_form, vald_email, check_files, vald_combine

wav_ranges = np.arange(4500, 13001, 25)

# Find which windows are still missing, then submit only those.
missing = check_files("vald_ll", teff=3700, logg=4.8,
                      detection_threshold=0.00005, wav_ranges=wav_ranges)
vald_form(missing, email="you@example.com", teff=3700, logg=4.8,
          detection_threshold=0.00005, v_mic=1.0)

# Later, after the emails arrive:
vald_email(server="uppsala")
vald_combine("vald_ll", "combined.vald", teff=3700, logg=4.8,
             detection_threshold=0.00005, wav_ranges=wav_ranges)
```

---

## File naming convention

Per-window files follow:

```
Teff_<teff>_logg_<logg*10>_det_0<last digit of threshold>_<start>_<end>.txt
```

For example, `Teff_3700_logg_48_det_05_4500_4525.txt` is Teff = 3700 K,
log g = 4.8, detection threshold 0.00005, covering 4500–4525 Å. This same string
is used as the VALD request comment so the emailed subject line (and therefore
the saved filename) is self-identifying.

---

## API reference

| Function | Purpose |
| --- | --- |
| `get_chrome_driver(headless=True)` | Build a Selenium Chrome driver. |
| `build_filename(...)` | Construct the canonical per-window filename. |
| `vald_form(tup_list, email, ...)` | Submit one Extract-All request per window. |
| `vald_email(server, token_path, ...)` | Download + unzip emailed results via Gmail. |
| `check_files(...)` | List wavelength windows not yet downloaded. |
| `vald_combine(...)` | Combine per-window files, fixing header metadata. |

### Planned (not yet implemented)

These are stubbed and raise `NotImplementedError`:

- `vald_format` — rewrite "4th element" lines for `vald3line-BPz-freeformat`.
- `vald_combine_format` — combine *and* format in one pass.
- `vald_split` — split a large list into <100 MB chunks for Turbospectrum.
- `vald_to_spectrum` — convert to [SPECTRUM](https://www.appstate.edu/~grayro/spectrum/spectrum.html) format.

---

## Notes & caveats

- The `submit` step depends on the exact layout of the VALD3 form (it locates
  fields by XPath). If VALD changes its page, those XPaths in `vald_form` will
  need updating.
- Test with a few files first before submitting large requests.