#!/usr/bin/env python3
"""

An updated version of VALDLineList: https://github.com/Anthony-Giacinto/VALDLinelist

Download and manipulate Vienna Atomic Line Database (VALD) line lists.

This module automates the VALD3 "Extract All" workflow end to end: it fills out
the web form for one or more wavelength windows, retrieves the resulting line
lists from Gmail, and stitches the per-window files back into a single line list
ready for downstream tools such as Turbospectrum's ``vald3line-BPz-freeformat``.

The VALD3 web interface caps the number of lines returned per request, so a wide
wavelength range must be split into many small windows. The helpers here manage
that splitting, track which windows are still missing, and recombine them while
preserving the header line-count metadata.

Tested against:
    * VALD3 web interface  (http://vald.astro.uu.se/), interface as of 2021-02-15
    * vald3line-BPz-freeformat (https://www.lupm.in2p3.fr/users/plez/), 2019-03-28

Implemented functions:
    get_chrome_driver  -- Construct a (headless) Selenium Chrome driver.
    vald_form          -- Submit one Extract-All request per wavelength window.
    vald_email         -- Pull the newest VALD emails via the Gmail API and
                          download + unzip the attached line lists.
    check_files        -- Report which wavelength windows are not yet downloaded.
    vald_combine       -- Append per-window line lists into one file, fixing the
                          header metadata (selected / processed line counts).

Planned functions (not yet implemented -- see stubs at the bottom of the file):
    vald_format          -- Rewrite "4th element" lines for vald3line-BPz-freeformat.
    vald_combine_format  -- Combine files *and* apply vald_format in one pass.
    vald_split           -- Split a large list into <100 MB chunks for Turbospectrum.
    vald_to_spectrum     -- Convert a VALD list to SPECTRUM format
                            (https://www.appstate.edu/~grayro/spectrum/spectrum.html).

Requirements:
    selenium, webdriver-manager, google-api-python-client,
    google-auth, requests, numpy, plus a local Google Chrome / Chromium.

Authentication:
    vald_email needs a Gmail OAuth ``token.json`` in the working directory (see
    README). The VALD email address used to log in is supplied at call time and
    is never hard-coded.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import os
import re
import sys

import numpy as np
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# Email address VALD sends results from, keyed by mirror. Used to filter Gmail.
VALD_SENDERS = {
    "uppsala": "vald@physics.uu.se",
    "montpellier": "vald@vald.lupm.univ-montp2.fr",
    "moscow": "vald3@inasan.ru",
}

# Display names of the non-default mirror links on the VALD landing page.
VALD_MIRROR_LINKS = {
    "moscow": "VALD3 Mirror Moscow",
    "montpellier": "VALD3 Mirror Montpellier",
}


def build_filename(teff, logg, detection_threshold, wave_start, wave_end, ext="txt"):
    """Build the canonical per-window filename used throughout the pipeline.

    The same naming scheme is used as the VALD request "comment" (so the
    downloaded email subject matches) and when recombining files, which is why
    it lives in one place. ``logg`` and the detection threshold are encoded
    compactly: ``logg`` is multiplied by 10 (4.8 -> 48) and the detection
    threshold contributes its final digit (0.00005 -> 5).

    :param teff: (int) Effective temperature in Kelvin.
    :param logg: (float) Surface gravity log g (cgs).
    :param detection_threshold: (float) VALD detection threshold.
    :param wave_start: (int|float) Window start wavelength in angstroms.
    :param wave_end: (int|float) Window end wavelength in angstroms.
    :param ext: (str) File extension without the dot (default "txt").
    :return: (str) e.g. "Teff_3700_logg_48_det_05_4500_4525.txt".
    """
    det_digit = str(detection_threshold)[-1]
    return (
        f"Teff_{teff}_logg_{int(logg * 10)}_det_0{det_digit}"
        f"_{wave_start}_{wave_end}.{ext}"
    )


def get_chrome_driver(headless=True):
    """Create a Selenium Chrome driver, downloading a matching driver binary.

    Uses webdriver-manager so the user does not have to install or path-manage a
    chromedriver themselves; it fetches the correct version for the installed
    Chrome on first run and caches it.

    :param headless: (bool) Run Chrome without a visible window (default True).
    :return: (selenium.webdriver.Chrome) A ready-to-use driver.
    """
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


def vald_form(
    tup_list,
    email,
    extraction_format="long",
    data_retrieval="ftp",
    detection_threshold=0.0003,
    v_mic=1,
    teff=3600,
    logg=4.9,
    linelist_config="default",
    server="uppsala",
    show_browser=False,
):
    """Submit one VALD "Extract All" request per wavelength window.

    VALD3 limits how much data a single request returns, so wide ranges are
    passed as a list of ``(wave_start, wave_end)`` windows and submitted one at a
    time. Each request is tagged with a comment matching :func:`build_filename`,
    so the resulting email (and downloaded file) is self-identifying. Results are
    delivered asynchronously by email; retrieve them later with
    :func:`vald_email`.

    Requires Selenium and a local Chrome/Chromium install. The XPaths below are
    tied to the VALD3 form layout and may need updating if VALD changes its page.

    :param tup_list: (list[tuple]) Wavelength windows, e.g. [(4500, 4525), ...].
    :param email: (str) Your registered VALD login email. Required; not stored.
    :param extraction_format: (str) "long" or "short" (default "long").
    :param data_retrieval: (str) "ftp" or "email" (default "ftp").
    :param detection_threshold: (float) Minimum line strength to extract.
    :param v_mic: (float) Microturbulent velocity in km/s.
    :param teff: (int) Effective temperature in Kelvin.
    :param logg: (float) Surface gravity log g (cgs).
    :param linelist_config: (str) "default" or "custom" (default "default").
    :param server: (str) "uppsala", "montpellier", or "moscow" (default "uppsala").
    :param show_browser: (bool) Show the browser window while running (default False).
    :return: None. Results arrive by email.
    """
    server = server.lower()

    for wave_start, wave_end in tup_list:
        print(f"Submitting request for {wave_start}-{wave_end} A")

        # The comment doubles as the file identifier (see build_filename), so the
        # downloaded email subject lines up with what vald_combine expects.
        comment = build_filename(
            teff, logg, detection_threshold, wave_start, wave_end, ext=""
        ).rstrip(".")

        driver = get_chrome_driver(headless=not show_browser)
        try:
            driver.get("http://vald.astro.uu.se/")

            # Switch to a non-default mirror if requested.
            if server in VALD_MIRROR_LINKS:
                driver.find_element(
                    By.LINK_TEXT, VALD_MIRROR_LINKS[server]
                ).click()

            # Log in with the registered email address.
            login = driver.find_element(By.NAME, "user")
            login.send_keys(email)
            login.send_keys(Keys.RETURN)

            # Wait for the Extract-All form to appear, then open it.
            wait = WebDriverWait(driver, 5)
            extract_button = (
                "/html/body/table/tbody/tr[1]/td[2]/form/table/tbody"
                "/tr/td[5]/input"
            )
            wait.until(EC.visibility_of_element_located((By.XPATH, extract_button)))
            driver.find_element(By.XPATH, extract_button).click()

            # Fill the request fields. XPaths index rows of the VALD form table.
            form_base = "/html/body/table/tbody/tr[2]/td[2]/form/table/tbody"
            fields = {
                2: wave_start,            # start wavelength
                3: wave_end,              # end wavelength
                4: detection_threshold,   # detection threshold
                5: v_mic,                 # microturbulence
                6: teff,                  # effective temperature
                7: logg,                  # surface gravity
            }
            for row, value in fields.items():
                driver.find_element(
                    By.XPATH, f"{form_base}/tr[{row}]/td[2]/input"
                ).send_keys(str(value))

            # Toggle the radio/checkbox options.
            if extraction_format.lower() == "long":
                driver.find_element(By.XPATH, f"{form_base}/tr[10]/td[2]/input").click()
            if data_retrieval.lower() == "ftp":
                driver.find_element(By.XPATH, f"{form_base}/tr[12]/td[2]/input").click()
            if linelist_config.lower() == "custom":
                driver.find_element(By.XPATH, f"{form_base}/tr[23]/td[2]/input").click()
            if comment:
                driver.find_element(
                    By.XPATH, f"{form_base}/tr[27]/td[2]/input"
                ).send_keys(comment)

            # Submit the request.
            driver.find_element(By.XPATH, f"{form_base}/tr[29]/td[1]/input").click()
        finally:
            driver.quit()


def vald_email(server="uppsala", token_path="token.json", out_folder="vald_ll",
               gzip_folder="vald_gzip", sender=None, query=None):
    """Download and unzip the line lists VALD has emailed via the Gmail API.

    Scans the Gmail inbox for messages from the chosen VALD mirror, follows the
    ``.gz`` download link in each, saves the compressed file, unzips it, and
    renames it to ``<email subject>.txt`` (the subject is the comment set in
    :func:`vald_form`). Files already present are skipped, so this is safe to
    re-run as more results arrive.

    Requires a Gmail OAuth ``token.json`` with read access. See the Gmail API
    quickstart: https://developers.google.com/gmail/api/quickstart/python

    :param server: (str) VALD mirror whose default sender to filter on
        (default "uppsala"). Ignored if ``sender`` or ``query`` is given.
    :param token_path: (str) Path to the Gmail OAuth token (default "token.json").
    :param out_folder: (str) Where unzipped line lists are saved (default "vald_ll").
    :param gzip_folder: (str) Where downloaded .gz files are cached (default "vald_gzip").
    :param sender: (str|None) Override the sender address to match. Use this if
        VALD emails arrive from an address other than the built-in default.
    :param query: (str|None) A raw Gmail search query, used verbatim if given
        (e.g. 'subject:Teff' or 'has:attachment vald'). Overrides ``sender``.
    :return: None.
    """
    if not os.path.exists(token_path):
        raise FileNotFoundError(
            f"Gmail token '{token_path}' not found. See the README for how to "
            "generate one with the Gmail API quickstart."
        )

    creds = Credentials.from_authorized_user_file(token_path)
    service = build("gmail", "v1", credentials=creds)

    # Build the Gmail search query. The `from:` operator is the correct way to
    # filter by sender; a bare address is treated as a full-text search and can
    # miss messages whose visible "from" differs from the envelope address.
    if query is not None:
        gmail_query = query
    else:
        sender = sender or VALD_SENDERS[server.lower()]
        gmail_query = f"from:{sender}"

    print(f"Searching Gmail with query: {gmail_query!r}")
    result = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], q=gmail_query)
        .execute()
    )
    messages = result.get("messages", [])
    print(f"Found {len(messages)} VALD message(s) to process...")

    if not messages:
        print(
            "No VALD email found.\n"
            "  - Check the actual 'From' address on a VALD email in your inbox; "
            "if it differs from the default, pass it with --sender, or search "
            "by subject with --query (e.g. --query \"subject:Teff_3700\").\n"
            "  - Make sure the emails are in the INBOX (not Spam or another tab/label)."
        )
        return

    os.makedirs(out_folder, exist_ok=True)
    os.makedirs(gzip_folder, exist_ok=True)

    for message in messages:
        txt = (
            service.users()
            .messages()
            .get(userId="me", id=message["id"])
            .execute()
        )
        payload = txt["payload"]

        # The subject header carries the comment we set in vald_form; use it as
        # the saved filename so windows are identifiable on disk.
        email_subject = payload["headers"][-6]["value"].split(":")[1].strip()
        save_name = os.path.join(out_folder, f"{email_subject}.txt")

        # Extract the .gz download link from the (base64) email body.
        data = payload["parts"][0]["body"]["data"]
        decoded = base64.b64decode(data).decode()
        links = re.findall(r"https?://[^\s]+\.gz", decoded)
        if not links:
            print(f"No download link in message {message['id']}, skipping.")
            continue
        link = links[0]

        gz_name = link.split("/")[-1]
        gz_path = os.path.join(gzip_folder, gz_name)
        if os.path.isfile(gz_path):
            print(f"Already downloaded: {gz_name}")
            continue

        print(f"Downloading: {email_subject}")
        response = requests.get(link)
        response.raise_for_status()
        with open(gz_path, "wb") as f:
            f.write(response.content)

        # Unzip the .gz next to it, then rename to the subject-based filename.
        extracted = gz_path[:-3]  # strip ".gz"
        with gzip.open(gz_path, "rb") as f_in, open(extracted, "wb") as f_out:
            f_out.write(f_in.read())

        try:
            os.replace(extracted, save_name)
        except OSError as exc:
            print(f"Could not rename {extracted} -> {save_name}: {exc}")


def check_files(input_folder, teff, logg, detection_threshold, wav_ranges):
    """Return the wavelength windows that have no downloaded file yet.

    Useful for resubmitting only the missing windows after a partial run: feed
    the returned list straight back into :func:`vald_form`.

    :param input_folder: (str) Folder holding the per-window line lists.
    :param teff: (int) Effective temperature used in the filename.
    :param logg: (float) Surface gravity used in the filename.
    :param detection_threshold: (float) Detection threshold used in the filename.
    :param wav_ranges: (np.ndarray) Edges of the wavelength windows; consecutive
        pairs define each window, e.g. np.arange(4500, 13001, 25).
    :return: (list[tuple]) Missing (wave_start, wave_end) windows.
    """
    missing = []
    for i in range(1, len(wav_ranges)):
        fname = build_filename(
            teff, logg, detection_threshold, wav_ranges[i - 1], wav_ranges[i]
        )
        if not os.path.exists(os.path.join(input_folder, fname)):
            print(f"Missing: {fname}")
            missing.append((wav_ranges[i - 1], wav_ranges[i]))
    return missing


def vald_combine(input_folder, output_file, teff, logg, detection_threshold,
                 wav_ranges, silent=True):
    """Append per-window VALD line lists into a single file.

    Each per-window file repeats a 3-line header and a trailing reference block
    (the "castelli..." footnotes). This keeps the header from the first window
    and the line data from every window, drops the duplicated headers/footers in
    between, and rewrites the header's "selected" and "processed" line counts to
    the totals across all windows so the combined file's metadata is correct.

    :param input_folder: (str) Folder holding the per-window line lists.
    :param output_file: (str) Path to write the combined line list to.
    :param teff: (int) Effective temperature used in the filename.
    :param logg: (float) Surface gravity used in the filename.
    :param detection_threshold: (float) Detection threshold used in the filename.
    :param wav_ranges: (np.ndarray) Edges of the wavelength windows; consecutive
        pairs define each window.
    :param silent: (bool) Suppress per-window progress printing (default True).
    :return: None.
    """
    selected_counts = []   # "selected" line count from each window header
    processed_counts = []  # "processed" line count from each window header
    lines = []

    last_index = len(wav_ranges) - 1
    for i in range(1, len(wav_ranges)):
        fname = build_filename(
            teff, logg, detection_threshold, wav_ranges[i - 1], wav_ranges[i]
        )
        c_file = os.path.join(input_folder, fname)
        if not silent:
            print(f"Reading {wav_ranges[i - 1]}-{wav_ranges[i]} A")

        with open(c_file, "r") as f:
            for j, line in enumerate(f):
                # Header line carries per-window line counts at fields 2 and 3.
                if j == 0:
                    meta = line.split(",")
                    selected_counts.append(int(meta[2]))
                    processed_counts.append(int(meta[3]))

                # Stop before the trailing reference block, except in the last
                # window where we keep it as the combined file's footer.
                if i != last_index and "castelli" in line:
                    break

                # Keep the 3-line header only from the first window; skip it
                # everywhere else.
                if j < 3:
                    if i == 1:
                        lines.append(line)
                    continue

                lines.append(line)

    # Rewrite the header: end wavelength and the summed line counts.
    meta = lines[0].split(",")
    meta[1] = wav_ranges[-1]
    meta[2] = sum(selected_counts)
    meta[3] = sum(processed_counts)
    lines[0] = ", ".join(map(str, meta))

    with open(output_file, "w") as f:
        f.writelines(lines)

    print(
        f"Wrote {output_file}: {sum(selected_counts)} selected / "
        f"{sum(processed_counts)} processed lines."
    )


# ---------------------------------------------------------------------------
# Planned helpers -- not yet implemented. Tracked here so the public API and the
# module docstring stay in sync. Contributions welcome.
# ---------------------------------------------------------------------------

def vald_format(*args, **kwargs):
    """TODO: Rewrite "4th element" lines so a single list works with
    Turbospectrum's vald3line-BPz-freeformat. Not yet implemented."""
    raise NotImplementedError("vald_format is not implemented yet.")


def vald_combine_format(*args, **kwargs):
    """TODO: Combine per-window lists *and* apply vald_format in one pass.
    Not yet implemented."""
    raise NotImplementedError("vald_combine_format is not implemented yet.")


def vald_split(*args, **kwargs):
    """TODO: Split a large line list into <100 MB chunks for Turbospectrum's
    input size limit. Not yet implemented."""
    raise NotImplementedError("vald_split is not implemented yet.")


def vald_to_spectrum(*args, **kwargs):
    """TODO: Convert a VALD line list to SPECTRUM format. Not yet implemented."""
    raise NotImplementedError("vald_to_spectrum is not implemented yet.")


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------

def _build_parser():
    parser = argparse.ArgumentParser(
        description="Download and combine VALD line lists.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Shared stellar-parameter arguments used to build filenames / requests.
    def add_common(p):
        p.add_argument("--teff", type=int, default=3700, help="Effective temperature (K).")
        p.add_argument("--logg", type=float, default=4.8, help="Surface gravity log g.")
        p.add_argument("--det", type=float, default=0.00005,
                       dest="detection_threshold", help="Detection threshold.")
        p.add_argument("--wave-start", type=int, default=4500, help="Range start (A).")
        p.add_argument("--wave-end", type=int, default=13000, help="Range end (A).")
        p.add_argument("--step", type=int, default=25, help="Window width (A).")

    # submit -----------------------------------------------------------------
    p_submit = sub.add_parser("submit", help="Submit Extract-All requests to VALD.")
    add_common(p_submit)
    p_submit.add_argument("--email", required=True, help="Registered VALD login email.")
    p_submit.add_argument("--v-mic", type=float, default=1.0, help="Microturbulence (km/s).")
    p_submit.add_argument("--server", default="uppsala",
                          choices=sorted(VALD_SENDERS), help="VALD mirror.")
    p_submit.add_argument("--input-folder", default="vald_ll",
                          help="Folder to check for already-downloaded windows.")
    p_submit.add_argument("--all", action="store_true",
                          help="Submit all windows, not just missing ones.")
    p_submit.add_argument("--show-browser", action="store_true",
                          help="Show the Chrome window while running.")

    # fetch ------------------------------------------------------------------
    p_fetch = sub.add_parser("fetch", help="Download emailed results via Gmail.")
    p_fetch.add_argument("--server", default="uppsala",
                         choices=sorted(VALD_SENDERS), help="VALD mirror.")
    p_fetch.add_argument("--token", default="token.json", help="Gmail OAuth token path.")
    p_fetch.add_argument("--out-folder", default="vald_ll", help="Output folder.")
    p_fetch.add_argument("--sender", default=None,
                         help="Override the sender address to filter on.")
    p_fetch.add_argument("--query", default=None,
                         help="Raw Gmail search query, used verbatim (overrides --sender).")

    # combine ----------------------------------------------------------------
    p_combine = sub.add_parser("combine", help="Combine per-window files into one.")
    add_common(p_combine)
    p_combine.add_argument("--input-folder", default="vald_ll", help="Input folder.")
    p_combine.add_argument("--output", required=True, help="Combined output file.")

    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)

    if args.command == "submit":
        wav_ranges = np.arange(args.wave_start, args.wave_end + 1, args.step)
        if args.all:
            windows = [
                (wav_ranges[i - 1], wav_ranges[i])
                for i in range(1, len(wav_ranges))
            ]
        else:
            windows = check_files(
                args.input_folder, args.teff, args.logg,
                args.detection_threshold, wav_ranges,
            )
        if not windows:
            print("Nothing to submit; all windows already present.")
            return
        vald_form(
            windows, email=args.email, detection_threshold=args.detection_threshold,
            v_mic=args.v_mic, teff=args.teff, logg=args.logg,
            server=args.server, show_browser=args.show_browser,
        )

    elif args.command == "fetch":
        vald_email(server=args.server, token_path=args.token,
                   out_folder=args.out_folder, sender=args.sender,
                   query=args.query)

    elif args.command == "combine":
        wav_ranges = np.arange(args.wave_start, args.wave_end + 1, args.step)
        vald_combine(
            args.input_folder, args.output, args.teff, args.logg,
            args.detection_threshold, wav_ranges, silent=False,
        )


if __name__ == "__main__":
    sys.exit(main())
