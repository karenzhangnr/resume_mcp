#!/usr/bin/env python3
"""
Resume Update MCP Server

Exposes 5 tools for Claude to orchestrate resume tailoring:
  1. classify_jd   – detect DS/DA/AI/FDE/DE from a job description
  2. get_bullets   – fetch curated bullets for a job type
  3. add_bullet    – add a bullet to the database
  4. update_resume – write a new versioned .tex file (title + bullets + summary)
  5. compile_pdf   – compile .tex → PDF and open it

Workflow:
  1. classify_jd → confirm job type with user
  2. get_bullets → present bullet options, user selects 8 (Stellantis) + 4 (Santander)
  3. Claude drafts a 50-60 word summary → user reviews and approves/edits
  4. update_resume (summary + bullets + job_type) → writes .tex
  5. compile_pdf → opens PDF
"""

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

# ── Paths ─────────────────────────────────────────────────────────────────────

SERVER_DIR = Path(__file__).parent
PROJECT_DIR = SERVER_DIR.parent
RESUME_DIR = PROJECT_DIR / "resume"
RESUME_TEMPLATE = RESUME_DIR / "resume_ds.tex"
DB_PATH = SERVER_DIR / "bullets_db.json"
PDFLATEX = r"C:\Users\karen\AppData\Local\Programs\MiKTeX\miktex\bin\x64\pdflatex.exe"

mcp = FastMCP("resume-updater")

# ── DB helpers ────────────────────────────────────────────────────────────────

def _load_db() -> dict:
    with open(DB_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_db(db: dict) -> None:
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)


# ── LaTeX helpers (ported from customize_resume.py) ───────────────────────────

def _extract_brace_content(text: str, start: int):
    assert text[start] == "{"
    depth, buf = 0, []
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
            if depth > 1:
                buf.append(ch)
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "".join(buf), i + 1
            buf.append(ch)
        else:
            buf.append(ch)
    raise ValueError("Unmatched brace in LaTeX")


def _parse_resume(path: Path) -> dict:
    all_lines = path.read_text(encoding="utf-8").split("\n")
    preamble, exp_intro, footer = [], [], []
    summary = ""
    jobs = []
    job_header, job_items = [], []
    state = "preamble"

    for line in all_lines:
        s = line.strip()
        if state == "preamble":
            preamble.append(line)
            if s == r"\section{Professional Summary}":
                state = "await_summary"
        elif state == "await_summary":
            if s and not s.startswith("%"):
                summary = s
                state = "pre_exp"
            else:
                preamble.append(line)
        elif state == "pre_exp":
            if s.startswith(r"\resumeSubheading"):
                state = "job_header"
                job_header = [line]
            elif s == r"\resumeSubHeadingListEnd":
                footer.append(line)
                state = "footer"
            else:
                exp_intro.append(line)
        elif state == "job_header":
            if s == r"\resumeItemListStart":
                state = "in_items"
                job_items = []
            else:
                job_header.append(line)
        elif state == "in_items":
            if s == r"\resumeItemListEnd":
                jobs.append({"header": "\n".join(job_header), "items": list(job_items)})
                job_header, job_items = [], []
                state = "between_jobs"
            elif r"\resumeItem" in s:
                idx = line.find(r"\resumeItem")
                brace_idx = line.find("{", idx + len(r"\resumeItem"))
                if brace_idx != -1:
                    content, _ = _extract_brace_content(line, brace_idx)
                    job_items.append(content)
        elif state == "between_jobs":
            if s.startswith(r"\resumeSubheading"):
                state = "job_header"
                job_header = [line]
            elif s == r"\resumeSubHeadingListEnd":
                footer.append(line)
                state = "footer"
        elif state == "footer":
            footer.append(line)

    return {
        "preamble": "\n".join(preamble),
        "summary": summary,
        "exp_intro": "\n".join(exp_intro),
        "jobs": jobs,
        "footer": "\n".join(footer),
    }


def _get_stellantis_title(job_type: str, jd_text: str = "") -> str:
    """Return the full 3rd \\resumeSubheading argument for Stellantis based on job type."""
    jd_lower = jd_text.lower()
    has_ai = any(kw in jd_lower for kw in [
        "llm", "large language", "generative ai", "gen ai", "gpt", "openai",
        "anthropic", "claude", "ai initiative", "artificial intelligence",
    ])

    jt = job_type.upper()
    if jt == "DA":
        # "Business Analytics" replaces both title suffix and department
        return "Sr Analyst, Business Analytics"
    elif jt == "AI":
        return "Sr Risk Analyst | AI Initiative Lead, Risk Management"
    elif jt == "FDE":
        return "Sr Risk Analyst | AI Initiative Lead, Risk Management"
    elif jt == "DS":
        base = "Data Scientist | Sr Risk Analyst"
    elif jt == "DE":
        base = "Sr Risk Analyst | Data Engineering"
    else:
        base = "Sr Risk Analyst"

    if has_ai and "AI Initiative Lead" not in base:
        base += " | AI Initiative Lead"

    return f"{base}, Risk Management"


def _replace_subheading_title(header: str, new_full_title: str) -> str:
    """Replace the 3rd brace argument of \\resumeSubheading (job title + dept)."""
    idx = header.find(r"\resumeSubheading")
    if idx == -1:
        return header
    pos = header.find("{", idx)
    _, pos = _extract_brace_content(header, pos)  # skip arg 1: company
    pos = header.find("{", pos)
    _, pos = _extract_brace_content(header, pos)  # skip arg 2: dates
    pos = header.find("{", pos)                    # arg 3 opens here
    _, end = _extract_brace_content(header, pos)   # end is index after closing }
    return header[:pos + 1] + new_full_title + header[end - 1:]


def _escape_latex(text: str) -> str:
    # Escape bare % and $ that aren't already preceded by a backslash
    text = re.sub(r"(?<!\\)%", r"\\%", text)
    text = re.sub(r"(?<!\\)\$", r"\\$", text)
    return text


def _rebuild_tex(parsed: dict, new_content: dict, title_overrides: dict | None = None) -> str:
    parts = [parsed["preamble"]]
    parts.append(_escape_latex(new_content["summary"]))
    parts.append(parsed["exp_intro"])
    for i, job in enumerate(parsed["jobs"]):
        parts.append("")
        header = job["header"]
        if title_overrides and i in title_overrides:
            header = _replace_subheading_title(header, title_overrides[i])
        parts.append(header)
        parts.append(r"\resumeItemListStart")
        for item_text in new_content["jobs"][i]["items"]:
            parts.append(f"\\resumeItem{{{_escape_latex(item_text)}}}")
        parts.append(r"\resumeItemListEnd")
    parts.append("")
    parts.append(parsed["footer"])
    return "\n".join(parts)


# ── Tool 1: classify_jd ───────────────────────────────────────────────────────

_KEYWORDS: dict[str, list[str]] = {
    "DS": [
        "data scientist", "machine learning", "predictive model", "statistical model",
        "regression", "classification", "feature engineering", "scikit", "xgboost",
        "random forest", "hypothesis test", "experimental design", "a/b test",
    ],
    "DA": [
        "data analyst", "dashboard", "reporting", "kpi", "tableau", "power bi",
        "looker", "visualization", "business intelligence", "bi analyst",
        "data-driven decision", "ad hoc", "stakeholder report",
    ],
    "AI": [
        "llm", "large language model", "generative ai", "gen ai", "gpt", "openai",
        "anthropic", "claude", "nlp", "natural language", "computer vision",
        "neural network", "deep learning", "transformer", "prompt engineering",
        "ai engineer", "ml engineer", "rag", "vector database", "langchain",
    ],
    "FDE": [
        "forward deployment", "solutions engineer", "technical account",
        "implementation engineer", "customer success", "pre-sales", "field engineer",
        "professional services", "onboarding", "integration", "enterprise deployment",
    ],
    "DE": [
        "data engineer", "pipeline", "etl", "elt", "spark", "kafka", "airflow",
        "dbt", "data warehouse", "lakehouse", "databricks", "snowflake", "bigquery",
        "data infrastructure", "orchestration", "datalake", "batch processing",
    ],
}


@mcp.tool()
def classify_jd(jd_text: str) -> dict:
    """
    Analyze a job description and classify it as DS, DA, AI, FDE, or DE.

    Returns a ranked list of scores so the user can confirm or override the
    classification before proceeding.

    Args:
        jd_text: Full text of the job description.

    Returns:
        {
          "top_type": "AI",
          "scores": {"AI": 7, "DS": 3, "DA": 1, "FDE": 0, "DE": 0},
          "matched_keywords": {"AI": ["llm", "rag", ...], ...}
        }
    """
    text_lower = jd_text.lower()
    scores: dict[str, int] = {}
    matched: dict[str, list[str]] = {}
    for jtype, kws in _KEYWORDS.items():
        hits = [kw for kw in kws if kw in text_lower]
        scores[jtype] = len(hits)
        matched[jtype] = hits

    top_type = max(scores, key=lambda t: scores[t])
    return {
        "top_type": top_type,
        "scores": scores,
        "matched_keywords": matched,
    }


# ── Tool 2: get_bullets ───────────────────────────────────────────────────────

@mcp.tool()
def get_bullets(job_type: str) -> dict:
    """
    Return bullets from the database for a given job type, grouped by company/job.

    Supports two bullet formats:
    - Plain strings: all bullets returned (no tag filtering yet).
    - Structured dicts {id, text, tags, priority}: filtered to job_type and sorted by priority.

    Args:
        job_type: One of DS, DA, AI, FDE, DE.

    Returns:
        {
          "job_type": "AI",
          "jobs": [
            {
              "company": "...",
              "title": "...",
              "required_bullet_count": 8,
              "available_count": N,
              "bullets": ["bullet text", ...]   # plain strings
                      or [{"id": ..., "text": ..., "tags": ..., "priority": ...}, ...]
            },
            ...
          ]
        }
    """
    job_type = job_type.upper()
    db = _load_db()
    result_jobs = []
    for job in db["jobs"]:
        raw = job["bullets"]
        if raw and isinstance(raw[0], str):
            matching = raw  # plain strings — no tag filtering
        else:
            matching = [b for b in raw if job_type in b.get("tags", [])]
            matching = sorted(matching, key=lambda b: b.get("priority", 5))
        result_jobs.append({
            "company": job["company"],
            "title": job["title"],
            "required_bullet_count": job["required_bullet_count"],
            "available_count": len(matching),
            "bullets": matching,
        })
    return {"job_type": job_type, "jobs": result_jobs}


# ── Tool 3: add_bullet ────────────────────────────────────────────────────────

@mcp.tool()
def add_bullet(
    company: str,
    bullet_text: str,
    job_types: list[str],
    priority: int = 5,
) -> str:
    """
    Add a bullet point to the database for a given company.

    Args:
        company:     Substring of the company name (e.g. "Stellantis" or "Santander").
        bullet_text: LaTeX-formatted bullet. Use \\textbf{} for emphasis.
        job_types:   List of applicable job type codes, e.g. ["AI", "DS"].
        priority:    Lower = shown first (1–10). Default 5.

    Returns:
        Confirmation string with the assigned bullet ID.
    """
    db = _load_db()
    company_lower = company.lower()
    target = None
    for job in db["jobs"]:
        if company_lower in job["company"].lower():
            target = job
            break
    if target is None:
        companies = [j["company"] for j in db["jobs"]]
        return f"Company '{company}' not found. Known companies: {companies}"

    # Generate next ID
    prefix = "".join(w[0] for w in target["company"].split()[:2]).lower()
    existing_ids = [b["id"] for b in target["bullets"]]
    nums = [int(re.search(r"\d+$", bid).group()) for bid in existing_ids if re.search(r"\d+$", bid)]
    next_num = (max(nums) + 1) if nums else 1
    bullet_id = f"{prefix}-{next_num:03d}"

    # Match existing format: plain strings or structured dicts
    raw = target["bullets"]
    if not raw or isinstance(raw[0], str):
        target["bullets"].append(bullet_text)
        _save_db(db)
        return f"Added bullet to '{target['company']}' as plain string ({len(target['bullets'])} total)"
    else:
        bullet = {
            "id": bullet_id,
            "text": bullet_text,
            "tags": [t.upper() for t in job_types],
            "priority": priority,
        }
        target["bullets"].append(bullet)
        _save_db(db)
        return f"Added bullet {bullet_id} to '{target['company']}' (tags: {bullet['tags']}, priority: {priority})"


# ── Tool 4: update_resume ─────────────────────────────────────────────────────

@mcp.tool()
def update_resume(
    company_name: str,
    stellantis_bullets: list[str],
    santander_bullets: list[str],
    summary: Optional[str] = None,
    job_type: Optional[str] = None,
    jd_text: str = "",
) -> str:
    """
    Write a new versioned .tex file using the master template.

    Reads resume_ds.tex as a read-only template, substitutes the professional
    summary, Stellantis job title, and bullet lists, then saves as
    resume_ds_MMDD_XX.tex (XX = first 2 letters of company_name).

    Args:
        company_name:       Target company name (sets filename suffix, e.g. "Google" → "Go").
        stellantis_bullets: Exactly 8 bullet strings for Stellantis Financial Services.
        santander_bullets:  Exactly 4 bullet strings for Santander Consumer.
        summary:            50-60 word tailored summary drafted by Claude and approved
                            by the user. If None, the original template summary is kept.
        job_type:           Job type code (DS, DA, AI, FDE, DE) — sets the Stellantis
                            title automatically. If None, title is unchanged.
        jd_text:            Full JD text — used to detect AI/LLM mentions for title suffix.

    Stellantis title logic:
        DS              → "Data Scientist | Sr Risk Analyst, Risk Management"
        DA              → "Sr Analyst, Business Analytics"
        AI / FDE        → "Sr Risk Analyst | AI Initiative Lead, Risk Management"
        DE              → "Sr Risk Analyst | Data Engineering, Risk Management"
        Any type + JD mentions LLM/GenAI → appends "| AI Initiative Lead"

    Returns:
        Path of the newly created .tex file.
    """
    if not RESUME_TEMPLATE.exists():
        return f"Master template not found: {RESUME_TEMPLATE}"

    parsed = _parse_resume(RESUME_TEMPLATE)
    n_jobs = len(parsed["jobs"])

    expected_counts = [job["required_bullet_count"] for job in _load_db()["jobs"][:n_jobs]]
    provided = [stellantis_bullets, santander_bullets]

    errors = []
    for i, (exp, got) in enumerate(zip(expected_counts, provided)):
        if len(got) != exp:
            co = parsed["jobs"][i]["header"].split("\n")[0] if i < n_jobs else f"Job {i}"
            errors.append(f"Job {i} ({co}): need {exp} bullets, got {len(got)}")
    if errors:
        return "Bullet count mismatch — fix before saving:\n" + "\n".join(errors)

    new_content = {
        "summary": summary if summary is not None else parsed["summary"],
        "jobs": [{"items": bullets} for bullets in provided],
    }

    title_overrides = None
    if job_type:
        title_overrides = {0: _get_stellantis_title(job_type, jd_text)}

    tex_content = _rebuild_tex(parsed, new_content, title_overrides)

    suffix = (company_name[:2] if len(company_name) >= 2 else company_name).capitalize()
    stem = f"resume_ds_{datetime.now().strftime('%m%d')}_{suffix}"
    out_path = RESUME_DIR / (stem + ".tex")
    out_path.write_text(tex_content, encoding="utf-8")
    return str(out_path)


# ── Tool 5: compile_pdf ───────────────────────────────────────────────────────

@mcp.tool()
def compile_pdf(tex_filename: str) -> str:
    """
    Compile a .tex file in the resume/ directory to PDF and open it.

    Args:
        tex_filename: Filename only (e.g. "resume_ds_0520_Go.tex") or full path.

    Returns:
        Success message with PDF path, or pdflatex error output on failure.
    """
    tex_path = Path(tex_filename)
    if not tex_path.is_absolute():
        tex_path = RESUME_DIR / tex_filename
    if not tex_path.exists():
        return f"File not found: {tex_path}"

    stem = tex_path.stem
    pdflatex = PDFLATEX if os.path.exists(PDFLATEX) else "pdflatex"

    result = subprocess.run(
        [pdflatex, "-interaction=nonstopmode", f"-output-directory={RESUME_DIR}", str(tex_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    pdf_path = RESUME_DIR / (stem + ".pdf")

    for ext in (".aux", ".log", ".out"):
        aux = RESUME_DIR / (stem + ext)
        if aux.exists():
            aux.unlink()

    if result.returncode != 0:
        log_path = RESUME_DIR / (stem + ".log")
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "pdflatex failed. Last 30 log lines:\n" + "\n".join(lines[-30:])
        return f"pdflatex failed (exit {result.returncode}):\n{result.stdout[-2000:]}"

    if pdf_path.exists():
        os.startfile(str(pdf_path))
        return f"PDF compiled and opened: {pdf_path}"
    return "pdflatex reported success but PDF not found."


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
