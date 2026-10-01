"""Streamlit page shared by the toolkit's batch tools (upload -> process -> table -> Excel)."""
from __future__ import annotations

import datetime
import os
import sys
import threading
import time
import types
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from typing import List, Optional

import pandas as pd
import streamlit as st

from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from resume_core import BatchResult, Profile, process_batch, warm_up

MAX_FILES = 500

# One pool for the whole server process. Kept as a module global (not in
# session state) so every user session shares it and worker start-up cost is
# paid once, not on every click.
_pool: Optional[ProcessPoolExecutor] = None
_pool_lock = threading.Lock()


@contextmanager
def _without_streamlit_main():
    """
    With the "spawn" start method (Windows, macOS) each new worker re-executes
    the parent's __main__ module, which under Streamlit is the app script:
    every worker would import Streamlit + pandas and re-run module-level UI
    code. Hiding __main__ while workers start makes them import only
    resume_core, which is all they need.
    """
    real_main = sys.modules["__main__"]
    sys.modules["__main__"] = types.ModuleType("__worker_main__")
    try:
        yield
    finally:
        sys.modules["__main__"] = real_main


def get_executor() -> Optional[ProcessPoolExecutor]:
    global _pool
    workers = min(8, (os.cpu_count() or 1) - 1)
    if workers < 2:
        return None                                   # single-core host: run in-process
    with _pool_lock:
        if _pool is None or getattr(_pool, "_broken", False):
            _pool = ProcessPoolExecutor(max_workers=workers)
            # Submitting one task per worker spawns all of them now (and in the
            # background), so they are ready by the time files are uploaded and
            # no later submit ever needs to spawn a process.
            with _without_streamlit_main():
                for _ in range(workers):
                    _pool.submit(warm_up)
        return _pool


def excel_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Remove characters Excel cannot store (they would crash the export) from every text cell."""
    return df.map(lambda v: ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v)


def generate_excel(result: BatchResult, sheet_name: str) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        excel_safe(pd.DataFrame(result.rows)).to_excel(writer, index=False, sheet_name=sheet_name)
        ws = writer.sheets[sheet_name]
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions
        issues = [(f, r) for f, r in result.failures] + [(d, f"Duplicate of {o}") for d, o in result.duplicates]
        if issues:
            excel_safe(pd.DataFrame(issues, columns=["Filename", "Reason"])).to_excel(writer, index=False, sheet_name="Not Processed")
    return output.getvalue()


def render_batch_tool(key: str, header: str, intro_md: str, profile: Profile,
                      checked_label: str, excel_prefix: str) -> None:
    get_executor()                                   # pre-warm workers on first page load
    st.header(header)
    st.markdown(intro_md)
    skill_names: List[str] = [name for name, _ in profile.skills]

    col1, col2 = st.columns([1, 2])
    with col1:
        uploaded_files = st.file_uploader(
            f"Upload resumes (PDF or DOCX) — up to {MAX_FILES} files",
            type=["pdf", "docx"], accept_multiple_files=True, key=f"{key}_uploader",
        )
        if uploaded_files:
            if len(uploaded_files) > MAX_FILES:
                st.warning(f"⚠️ You uploaded {len(uploaded_files)} files. Processing the first {MAX_FILES}.")
                uploaded_files = uploaded_files[:MAX_FILES]
            st.success(f"✅ {len(uploaded_files)} file(s) ready to process")
        process_button = st.button("🚀 Process All Resumes", type="primary", key=f"{key}_process")

    with col2:
        st.subheader(checked_label)
        cols = st.columns(3)
        for idx, skill in enumerate(skill_names):
            cols[idx % 3].write(f"• {skill}")

    upload_signature = tuple((f.name, f.size) for f in uploaded_files or [])
    cache = st.session_state.setdefault(f"{key}_cache", {})

    if process_button:
        if not uploaded_files:
            st.error("⚠️ Please upload at least one resume first.")
        else:
            progress_bar = st.progress(0.0, text="Starting…")

            def on_progress(done: int, total: int, name: str) -> None:
                progress_bar.progress(done / total if total else 1.0,
                                      text=f"Processed {done}/{total}" + (f" · {name}" if name else ""))

            files = [(f.name, f.getvalue()) for f in uploaded_files]
            started = time.perf_counter()
            result = process_batch(files, profile, executor=get_executor(), cache=cache, on_progress=on_progress)
            elapsed = time.perf_counter() - started
            progress_bar.empty()
            st.session_state[f"{key}_result"] = {
                "signature": upload_signature,
                "result": result,
                "elapsed": elapsed,
                "uploaded": len(files),
                "excel": generate_excel(result, "Resume_Data") if result.rows else None,
            }

    # Render from session state so results survive reruns (e.g. clicking Download).
    saved = st.session_state.get(f"{key}_result")
    if not saved or saved["signature"] != upload_signature:
        st.markdown("---")
        st.caption("Have a GOOD DAY!!!")
        return

    result: BatchResult = saved["result"]
    n_ok = len(result.rows)
    if not n_ok:
        st.error("❌ Could not extract data from any of the uploaded files.")
    else:
        st.success(f"✅ Processed {n_ok} of {saved['uploaded']} file(s) in {saved['elapsed']:.1f}s "
                   f"({saved['uploaded'] / max(saved['elapsed'], 0.01):.0f} resumes/sec)")

    st.subheader("📊 Processing Summary")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Files Uploaded", saved["uploaded"])
    c2.metric("Processed", n_ok)
    c3.metric("Failed", len(result.failures))
    c4.metric("Duplicates Skipped", len(result.duplicates))

    if result.failures or result.duplicates:
        with st.expander(f"⚠️ {len(result.failures) + len(result.duplicates)} file(s) not processed — see why"):
            issues = [(f, r) for f, r in result.failures] + [(d, f"Duplicate of {o}") for d, o in result.duplicates]
            st.dataframe(pd.DataFrame(issues, columns=["Filename", "Reason"]), hide_index=True, width="stretch")

    if not n_ok:
        return

    df = pd.DataFrame(result.rows)
    st.subheader(f"📈 {checked_label} — Statistics")
    counts = df[skill_names].sum()
    num_cols = 4
    for i in range(0, len(skill_names), num_cols):
        stat_cols = st.columns(num_cols)
        for j, skill in enumerate(skill_names[i:i + num_cols]):
            count = int(counts[skill])
            stat_cols[j].metric(skill, f"{count}/{n_ok}", f"{count / n_ok * 100:.0f}%", delta_color="off")

    st.subheader("Complete Data Table")
    st.dataframe(df, width="stretch", hide_index=True)

    st.download_button(
        label="📥 Download Excel File with All Data",
        data=saved["excel"],
        file_name=f"{excel_prefix}_{datetime.datetime.now():%Y%m%d_%H%M%S}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        key=f"{key}_download",
    )
    st.markdown("---")
    st.caption("Have a GOOD DAY!!!")
