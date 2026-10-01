"""
Shared resume-processing engine used by every tool in the toolkit.

Design goals
------------
* Fast: pypdfium2 (PDFium, Apache/BSD licensed) for PDFs, raw-XML parsing for
  DOCX, pre-compiled regexes, and a process pool so a batch of 300+ resumes
  uses every CPU core instead of one.
* Pure: nothing in this module imports Streamlit or pandas, so worker processes
  start quickly and the logic is unit-testable.
"""
from __future__ import annotations

import hashlib
import html
import io
import os
import re
import zipfile
from concurrent.futures import Executor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pypdfium2 as pdfium

# Below this many files the cost of shipping bytes to worker processes
# outweighs the gain, so small batches run in-process.
PARALLEL_THRESHOLD = 6


# ============================================================
# Text extraction
# ============================================================
class ExtractionError(Exception):
    """Raised with a human-readable reason when a file yields no usable text."""


def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    try:
        pdf = pdfium.PdfDocument(pdf_bytes)
    except pdfium.PdfiumError as e:
        if "password" in str(e).lower():
            raise ExtractionError("Password-protected PDF") from e
        raise ExtractionError("Corrupt or unreadable PDF") from e
    parts = []
    try:
        for page in pdf:
            textpage = page.get_textpage()
            try:
                parts.append(textpage.get_text_bounded())
            finally:
                textpage.close()
                page.close()
    finally:
        pdf.close()
    text = "\n".join(parts)
    if not text.strip():
        raise ExtractionError("No text layer (scanned/image PDF - needs OCR)")
    return text


# DOCX is a zip of XML. Parsing the XML directly is ~10x faster than
# python-docx and, unlike python-docx's `doc.paragraphs`, it also picks up
# text in headers and text boxes, where many resume templates put the
# candidate's name, email and phone.
_DOCX_DROP = re.compile(
    r"<mc:Fallback>.*?</mc:Fallback>"          # duplicate VML copy of text boxes
    r"|<w:instrText[^>]*>.*?</w:instrText>"    # field codes (HYPERLINK "mailto:...")
    r"|<w:delText[^>]*>.*?</w:delText>",       # tracked-change deletions
    re.S,
)
_DOCX_NEWLINE = re.compile(r"</w:p>|<w:br/>|<w:cr/>")
_DOCX_CELL = re.compile(r"</w:tc>")
_DOCX_TAG = re.compile(r"<[^>]+>")
_DOCX_PART = re.compile(r"word/(header\d*|document|footer\d*)\.xml$")


def _docx_part_order(name: str) -> int:
    kind = _DOCX_PART.match(name).group(1)
    return 0 if kind.startswith("header") else 1 if kind == "document" else 2


def extract_text_from_docx_bytes(docx_bytes: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
            names = sorted((n for n in z.namelist() if _DOCX_PART.match(n)), key=_docx_part_order)
            if "word/document.xml" not in names:
                raise ExtractionError("Not a valid Word .docx file")
            xmls = [z.read(n).decode("utf-8", errors="ignore") for n in names]
    except zipfile.BadZipFile as e:
        raise ExtractionError("Corrupt .docx (or an old .doc renamed to .docx)") from e
    chunks = []
    for xml in xmls:
        xml = _DOCX_DROP.sub("", xml)
        xml = xml.replace("<w:tab/>", "\t")
        xml = _DOCX_CELL.sub(" | ", xml)
        xml = _DOCX_NEWLINE.sub("\n", xml)
        chunks.append(html.unescape(_DOCX_TAG.sub("", xml)))
    text = "\n".join(chunks)
    if not text.strip():
        raise ExtractionError("Empty document")
    return text


# Control characters (and lone surrogates) that PDFs and Word files sometimes contain. Excel cannot store
# them - writing one crashes the whole download - and they carry no meaning for screening.
_LINE_BREAK_CTRL = re.compile(r"[\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]")


def extract_text(file_name: str, file_bytes: bytes) -> str:
    name = file_name.lower()
    if name.endswith(".pdf"):
        text = extract_text_from_pdf_bytes(file_bytes)
    elif name.endswith(".docx"):
        text = extract_text_from_docx_bytes(file_bytes)
    else:
        raise ExtractionError("Unsupported file type (use PDF or DOCX)")
    # Normalise line endings, non-breaking spaces and stray whitespace per line.
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = _LINE_BREAK_CTRL.sub("\n", text)
    text = _CONTROL_CHARS.sub("", text)
    return "\n".join(line.strip() for line in text.split("\n"))


# ============================================================
# Section detection
# ============================================================
# A heading is a short line consisting *only* of heading vocabulary. The old
# code treated any occurrence of words like "experience" or "skills" anywhere
# in the text as a section boundary, so a sentence such as "improved
# communication skills" in a job description cut the experience section short.
_HEADINGS = {
    "experience": r"(?:work|professional|relevant|industry|employment|career)?\s*(?:experience|history)"
                  r"|employment(?:\s+history)?|work\s+history|career\s+history|professional\s+background",
    "internship": r"internships?(?:\s+experience)?|industrial\s+training|trainings?|practical\s+experience",
    "projects": r"(?:academic|key|personal|major|professional)?\s*projects?(?:\s+undertaken)?",
    "education": r"education(?:al)?(?:\s+(?:qualifications?|background|details))?|academic(?:s|\s+(?:qualifications?|background|details))?|qualifications?",
    "certifications": r"certifications?|certificates?|licen[sc]es(?:\s*(?:&|and)\s*certifications?)?",
    "languages": r"languages?(?:\s+known)?|language\s+proficiency",
    "skills": r"(?:technical|key|core|it|professional)?\s*skills?(?:\s+set)?|technical\s+proficiency|competencies|core\s+competencies|tech\s+stack",
    "other": r"(?:professional\s+)?summary|profile|objective|career\s+objective|about\s+me|awards?"
             r"|achievements?|accomplishments?|hobbies|interests?|references?|declaration"
             r"|personal\s+(?:details?|information|profile)|contact(?:\s+(?:details?|information))?|publications?"
             r"|extra[\s-]?curricular(?:\s+activities)?|activities|volunteer(?:ing)?(?:\s+experience)?|strengths",
}
_HEADING_RES = [
    (kind, re.compile(rf"^[\W_]*(?:{pat})(?:\s*(?:&|and|/|,)\s*(?:{'|'.join(_HEADINGS.values())}))?\s*[:\-]?\s*$", re.I))
    for kind, pat in _HEADINGS.items()
]


def split_sections(text: str) -> List[Tuple[str, str]]:
    """Return [(kind, body_text), ...]. Text before the first heading is kind 'header'."""
    sections: List[Tuple[str, List[str]]] = [("header", [])]
    for line in text.split("\n"):
        kind = None
        if line and len(line) <= 50 and len(line.split()) <= 6:
            for k, rx in _HEADING_RES:
                if rx.match(line):
                    kind = k
                    break
        if kind:
            sections.append((kind, []))
        else:
            sections[-1][1].append(line)
    return [(k, "\n".join(lines)) for k, lines in sections]


def section_text(sections: Sequence[Tuple[str, str]], kinds: Iterable[str]) -> str:
    kinds = set(kinds)
    return "\n".join(body for k, body in sections if k in kinds)


# ============================================================
# Field extractors
# ============================================================
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def extract_email(text: str) -> str:
    m = _EMAIL_RE.search(text)
    return m.group(0) if m else ""


# Candidate = a run of digits and phone punctuation; validated by digit count.
_PHONE_CANDIDATE = re.compile(r"(?<![\w/])\+?\(?\d[\d \t().\-]{7,}\d(?![\w/])")
_YEAR_ONLY = re.compile(r"^(?:(?:19|20)\d{2}[\s\-.()]*)+$")


def extract_phone(text: str) -> str:
    for m in _PHONE_CANDIDATE.finditer(text):
        raw = m.group(0).strip()
        digits = re.sub(r"\D", "", raw)
        if not 10 <= len(digits) <= 13:
            continue
        if _YEAR_ONLY.match(raw):          # "2018 - 2021 2021 - 2023"
            continue
        return raw
    return ""


_NAME_REJECT = re.compile(
    r"@|\d|https?:|www\.|linkedin|github|\b(?:resume|curriculum|vitae|cv|profile|summary|objective|contact|phone|"
    r"mobile|email|address|engineer|developer|manager|executive|analyst|consultant|intern|designer|specialist|"
    r"officer|associate|director|sales|marketing|lead)\b",
    re.I,
)
_FILENAME_NOISE = re.compile(r"\b(?:resume|cv|updated|final|latest|new|copy|naukri|profile)\b|\[.*?\]|\(.*?\)|\d+", re.I)


def name_from_filename(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)          # JohnDoe -> John Doe
    stem = _FILENAME_NOISE.sub(" ", stem.replace("_", " ").replace("-", " "))
    return " ".join(w.capitalize() for w in stem.split())


def extract_name(text: str, filename: str = "") -> str:
    lines = [l for l in text.split("\n") if l][:10]
    for line in lines:
        line = re.sub(r"^(?:name)\s*[:\-]\s*", "", line, flags=re.I)
        cand = re.split(r"\s*[|•·,\t]\s*|\s{3,}", line)[0].strip()
        if not cand or _NAME_REJECT.search(cand):
            continue
        words = cand.split()
        if 2 <= len(words) <= 4 and all(w.replace(".", "").replace("'", "").isalpha() for w in words):
            return cand.title() if cand.isupper() else cand
    return name_from_filename(filename) if filename else ""


_DEGREE_RE = re.compile(
    r"\b(?:bachelor(?:'?s)?|master(?:'?s)?|ph\.?\s?d|doctorate|b\.?\s?tech|m\.?\s?tech|b\.\s?e\.?|m\.\s?e\.?|b\.?\s?sc|m\.?\s?sc"
    r"|bca|mca|mba|pgdm|bba|b\.?\s?com|m\.?\s?com|b\.?\s?a\.|m\.?\s?a\.|diploma)(?![a-z])[^,\n|]*",
    re.I,
)


def extract_education(text: str, sections: Optional[Sequence[Tuple[str, str]]] = None) -> str:
    sections = sections if sections is not None else split_sections(text)
    edu = section_text(sections, ["education"])
    for scope in (edu, text):
        m = _DEGREE_RE.search(scope)
        if m:
            return m.group(0).strip(" -:")[:120]
    first = next((l for l in edu.split("\n") if l), "")
    return first[:120]


_CITIES = [
    "Bengaluru", "Bangalore", "Mumbai", "Navi Mumbai", "Thane", "New Delhi", "Delhi", "Gurugram", "Gurgaon", "Noida",
    "Greater Noida", "Faridabad", "Ghaziabad", "Pune", "Hyderabad", "Secunderabad", "Chennai", "Kolkata", "Ahmedabad",
    "Jaipur", "Chandigarh", "Mohali", "Kochi", "Cochin", "Indore", "Lucknow", "Bhopal", "Nagpur", "Coimbatore", "Surat",
    "Vadodara", "Visakhapatnam", "Vizag", "Thiruvananthapuram", "Trivandrum", "Mysuru", "Mysore", "Bhubaneswar", "Patna",
    "Ranchi", "Goa", "Nashik", "Dehradun", "Guwahati", "Kanpur", "Ludhiana", "Madurai", "Raipur", "Jodhpur", "Udaipur",
    "Mangalore", "Mangaluru", "Vijayawada", "Dubai", "Abu Dhabi", "Singapore", "London", "New York", "San Francisco",
]
_CITY_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _CITIES), key=len, reverse=True)) + r")\b", re.I)
_LOCATION_LABEL = re.compile(r"(?:^|[|•·]\s*)(?:current\s+)?(?:location|address|city|based\s+in)\s*[:\-]\s*([^|•·\n]{2,60})", re.I | re.M)


def extract_location(text: str) -> str:
    m = _LOCATION_LABEL.search(text)
    if m:
        value = m.group(1).strip(" ,.")
        city = _CITY_RE.search(value)          # "12, MG Road, Bengaluru 560001" -> "Bengaluru"
        return city.group(1).title() if city else value
    # Only look at the resume header: city names further down are usually employers' offices.
    header = "\n".join([l for l in text.split("\n") if l][:15])
    m = _CITY_RE.search(header)
    return m.group(1).title() if m else ""


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DATE = r"(?:[A-Za-z]{3,9}\.?\s*,?\s*'?(?:19|20)\d{2}|\d{1,2}\s*[/.\-]\s*(?:19|20)\d{2}|(?:19|20)\d{2})"
_OPEN_END = r"present|current(?:ly)?|now|till\s+(?:date|now)|to\s+date|today|ongoing|date"
_RANGE_RE = re.compile(rf"({_DATE})\s*(?:-|–|—|to|till|until)\s*({_DATE}|{_OPEN_END})\b", re.I)
_INTERN_RE = re.compile(r"\b(?:intern|internship|trainee|apprentice(?:ship)?)\b", re.I)
_YEARS_STATED = re.compile(r"(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)(?:\s+of)?\s+(?:\w+\s+){0,3}experience", re.I)


def _parse_date(tok: str, today: date) -> Optional[int]:
    """Return a month index (year*12 + month-1), or None."""
    tok = tok.strip().lower()
    if re.fullmatch(_OPEN_END, tok):
        return today.year * 12 + today.month - 1
    year = int(re.search(r"(?:19|20)\d{2}", tok).group(0))
    m = re.match(r"([a-z]{3})", tok)
    if m and m.group(1) in _MONTHS:
        month = _MONTHS[m.group(1)]
    else:
        m = re.match(r"(\d{1,2})\s*[/.\-]", tok)
        month = int(m.group(1)) if m and 1 <= int(m.group(1)) <= 12 else 1
    return year * 12 + month - 1


def _is_internship(lines: Sequence[str], i: int, m: "re.Match") -> bool:
    """A dated line is an internship if it says so itself - or, when it holds only dates, if the line above does."""
    rest = lines[i][:m.start()] + lines[i][m.end():]
    if len(rest.strip(" ,.:;|-–—•·()")) < 3 and i > 0:
        rest = lines[i - 1]
    return bool(_INTERN_RE.search(rest))


def extract_total_experience(text: str, sections: Optional[Sequence[Tuple[str, str]]] = None,
                             today: Optional[date] = None) -> float:
    """
    Total years of full-time work experience: date ranges in the experience
    section(s), internships excluded, overlapping jobs merged. Falls back to a
    stated "N+ years of experience" when no date ranges are found.
    """
    today = today or date.today()
    sections = sections if sections is not None else split_sections(text)
    scope = section_text(sections, ["experience"])
    if not scope.strip():
        scope = section_text(sections, ["header", "other"])
    lines = scope.split("\n")
    intervals = []
    now_idx = today.year * 12 + today.month - 1
    for i, line in enumerate(lines):
        for m in _RANGE_RE.finditer(line):
            if _is_internship(lines, i, m):
                continue
            start, end = _parse_date(m.group(1), today), _parse_date(m.group(2), today)
            if start is None or end is None or end < start or start > now_idx or end - start > 50 * 12:
                continue
            intervals.append((start, min(end, now_idx)))
    if intervals:
        intervals.sort()
        total, (cur_s, cur_e) = 0, intervals[0]
        for s, e in intervals[1:]:
            if s <= cur_e + 1:                       # overlapping or back-to-back jobs form one stretch
                cur_e = max(cur_e, e)
            else:
                total += cur_e - cur_s + 1           # +1: both the first and last month count
                cur_s, cur_e = s, e
        total += cur_e - cur_s + 1
        return round(total / 12, 1)
    stated = [float(x) for x in _YEARS_STATED.findall(text) if float(x) <= 50]
    return max(stated) if stated else 0.0


# ============================================================
# Profile details: LinkedIn, current role, notice period, CTC, certifications, languages, skills
# ============================================================
_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.|[a-z]{2}\.)?linkedin\.com/(?:in|pub)/[\w\-%]+/?", re.I)


def extract_linkedin(text: str) -> str:
    m = _LINKEDIN_RE.search(text)
    if not m:
        return ""
    url = m.group(0).rstrip("/")
    return url if url.lower().startswith("http") else "https://" + url


_CLIP = re.compile(r"\s{2,}|\t|\b(?:current|expected|ctc|ectc|cctc|notice|location|email|phone)\b", re.I)
_NOTICE_LABEL = re.compile(r"notice\s*period\s*(?:\([^)]*\))?\s*[:\-–=]\s*([^\n|;]{2,40})", re.I)
_NOTICE_PHRASE = re.compile(
    r"\b(?:immediate(?:ly)?\s+(?:joiner|joining|available)|available\s+immediately|join\s+immediately"
    r"|serving\s+(?:my\s+)?notice(?:\s+period)?(?:[^\n|;.]{0,30})?)", re.I)


def extract_notice_period(text: str) -> str:
    m = _NOTICE_LABEL.search(text)
    if m:
        return _CLIP.split(m.group(1))[0].strip(" ,.-")
    m = _NOTICE_PHRASE.search(text)
    if m:
        return "Immediate" if re.match(r"immediate|available|join", m.group(0), re.I) else m.group(0).strip(" ,.-").capitalize()
    return ""


_MONEY = (r"((?:₹|rs\.?|inr|usd|\$)?\s*\d[\d,]*(?:\.\d+)?\s*(?:lpa|lacs?|lakhs?|lakh|cr|crores?|per\s+annum|p\.?a\.?|[lk])?(?![a-z])"
          r"(?:\s*(?:-|–|to)\s*\d[\d,]*(?:\.\d+)?\s*(?:lpa|lacs?|lakhs?|[lk])?(?![a-z]))?)")
_CTC_CURRENT = re.compile(r"(?:(?:current|present|existing|last\s+drawn)\s*(?:ctc|salary|compensation|package)|\bc\.?ctc\b)"
                          r"\s*(?:\([^)]*\))?\s*[:\-–=]?\s*(?:is|of)?\s*" + _MONEY, re.I)
_CTC_EXPECTED = re.compile(r"(?:(?:expected|expecting)\s*(?:ctc|salary|compensation|package)|\be\.?ctc\b)"
                           r"\s*(?:\([^)]*\))?\s*[:\-–=]?\s*(?:is|of)?\s*" + _MONEY, re.I)


def extract_ctc(text: str) -> Tuple[str, str]:
    cur, exp = _CTC_CURRENT.search(text), _CTC_EXPECTED.search(text)
    return (cur.group(1).strip(" ,.") if cur else ""), (exp.group(1).strip(" ,.") if exp else "")


_BULLET = re.compile(r"^[\s•●▪◦■□➢➤✓✔*·\-–—>]+")
_KNOWN_LANGUAGES = ["English", "Hindi", "Tamil", "Telugu", "Kannada", "Malayalam", "Marathi", "Gujarati", "Bengali",
                    "Punjabi", "Urdu", "Odia", "Assamese", "Sanskrit", "Nepali", "Konkani", "French", "German", "Spanish",
                    "Arabic", "Japanese", "Chinese", "Mandarin", "Korean", "Russian", "Portuguese", "Italian"]
_LANG_RE = re.compile(r"\b(" + "|".join(_KNOWN_LANGUAGES) + r")\b", re.I)
_LANG_INLINE = re.compile(r"^\W*languages?(?:\s+known)?\s*[:\-–]\s*(.+)$", re.I | re.M)
_CERT_INLINE = re.compile(r"^\W*(?:certifications?|certificates?)\s*[:\-–]\s*(.+)$", re.I | re.M)


def extract_languages(text: str, sections: Sequence[Tuple[str, str]]) -> str:
    scope = section_text(sections, ["languages"]) or "\n".join(_LANG_INLINE.findall(text))
    found = []
    for m in _LANG_RE.finditer(scope):
        lang = m.group(1).title()
        if lang not in found:
            found.append(lang)
    return ", ".join(found)


def extract_certifications(text: str, sections: Sequence[Tuple[str, str]]) -> str:
    body = section_text(sections, ["certifications"])
    items = []
    lines = body.split("\n") if body else [p for m in _CERT_INLINE.findall(text) for p in re.split(r"[;,]", m)]
    for line in lines:
        line = _BULLET.sub("", line).strip()
        if len(line) < 3 or len(line) > 120 or re.fullmatch(r"[\d\s/\-–.,]+", line):
            continue
        if line not in items:
            items.append(line)
    return "; ".join(items[:8])


# Skills: (1) whatever the candidate lists under a Skills heading, (2) a vocabulary scan of the whole resume,
# so skills are found even without a Skills section and are named consistently ("nodejs" -> "Node.js").
# "Canonical=alias|alias" - the default alias is the lower-cased canonical name. Matching is by word lookup
# (much faster than one giant regex), and "excel", "sap", "go" etc. are deliberately not aliases on their own.
_SKILL_VOCAB = [
    "Python", "Java", "JavaScript=javascript|java script|js", "TypeScript", "C++", "C#", ".NET=.net|dotnet|asp.net", "PHP",
    "Kotlin", "SQL", "MySQL=mysql|my sql", "PostgreSQL=postgresql|postgres|postgre sql", "MongoDB=mongodb|mongo db|mongo",
    "Redis", "React=react|reactjs|react.js", "Angular=angular|angularjs|angular.js", "Vue.js=vue|vuejs|vue.js",
    "Node.js=node.js|nodejs|node js", "Express.js=express.js|expressjs", "Next.js=next.js|nextjs|next js", "Django", "Flask",
    "FastAPI=fastapi|fast api", "Spring Boot=spring boot|springboot", "Laravel", "HTML=html|html5", "CSS=css|css3",
    "Tailwind CSS=tailwind|tailwind css|tailwindcss", "Bootstrap", "Git", "GitHub", "Docker", "Kubernetes",
    "AWS=aws|amazon web services", "Azure", "GCP=gcp|google cloud", "Jenkins", "CI/CD=ci cd|cicd", "Terraform", "Linux",
    "REST API=rest api|rest apis|restful api|restful apis", "GraphQL", "Microservices", "Machine Learning", "Deep Learning",
    "NLP=nlp|natural language processing", "TensorFlow", "PyTorch", "Pandas", "NumPy", "Power BI=power bi|powerbi", "Tableau",
    "Advanced Excel=advanced excel|ms excel|microsoft excel", "Tally", "Salesforce", "CRM", "HubSpot", "Jira", "Agile",
    "Scrum", "Selenium", "Postman", "Figma", "Photoshop", "SEO", "Google Analytics", "Negotiation", "Lead Generation",
    "Cold Calling", "Key Account Management", "Channel Sales", "Market Research", "Business Development",
    "Team Management=team management|team handling", "Stakeholder Management",
]
_ALIAS: Dict[str, str] = {}
for _entry in _SKILL_VOCAB:
    _name, _, _aliases = _entry.partition("=")
    for _a in (_aliases or _name.lower()).split("|"):
        _ALIAS[_a] = _name
_WORD = re.compile(r"[a-z0-9.#+]+")
_REACT_VERB_NEXT = {"to", "quickly", "promptly", "swiftly"}
_SKILL_SPLIT = re.compile(r"\s*[,;|•●▪◦·]\s*|\t|\s{3,}")
_SAP_RE = re.compile(r"(?<![\w.])SAP(?![\w])")           # case-sensitive: "sap" is a word


def _scan_vocab(text: str):
    words = [w for w in (w.rstrip(".") for w in _WORD.findall(text.lower())) if w]
    i = 0
    while i < len(words):
        for n in (3, 2, 1):
            name = _ALIAS.get(" ".join(words[i:i + n]))
            if name:
                if not (name == "React" and words[i + n:i + n + 1] and words[i + n] in _REACT_VERB_NEXT):
                    yield name
                i += n
                break
        else:
            i += 1


def extract_all_skills(text: str, sections: Sequence[Tuple[str, str]], limit: int = 40) -> str:
    found: Dict[str, str] = {}

    def add(name: str) -> None:
        found.setdefault(name.lower(), name)

    for line in section_text(sections, ["skills"]).split("\n"):        # listed skills first, in the candidate's order
        line = _BULLET.sub("", line).strip()
        if ":" in line and len(line.split(":", 1)[0]) <= 30:
            line = line.split(":", 1)[1]
        for item in _SKILL_SPLIT.split(line):
            item = item.strip(" .:-–")
            if 1 < len(item) <= 40 and len(item.split()) <= 4 and not re.search(r"\d+\s*(?:\+|yrs?|years?)|^(?:and|etc)\b", item, re.I):
                add(_ALIAS.get(item.lower(), item))
    for name in _scan_vocab(text):
        add(name)
    if _SAP_RE.search(text):
        add("SAP")
    return ", ".join(list(found.values())[:limit])


_TITLE_KW = re.compile(r"\b(?:engineer|developer|manager|executive|analyst|consultant|lead|officer|associate|director|head|designer|"
                       r"specialist|architect|administrator|representative|coordinator|scientist|founder|supervisor|assistant|"
                       r"intern|trainee|programmer|tester|president|owner|advisor|strategist|recruiter|accountant|technician)\b", re.I)
_ROLE_SPLIT = re.compile(r"\s+[-–—@|]\s+|\s+at\s+|\s*\|\s*|,\s+|\s{3,}")
_LABEL_DESIG = re.compile(r"^\W*(?:current\s+)?(?:designation|position|job\s+title)\s*[:\-–]\s*(.+)$", re.I | re.M)
_LABEL_COMPANY = re.compile(r"^\W*(?:current\s+)?(?:company|organi[sz]ation|employer)\s*[:\-–]\s*(.+)$", re.I | re.M)


def _tidy(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" ,.:;|-–—•·()")[:80]


def extract_current_role(text: str, sections: Sequence[Tuple[str, str]], today: Optional[date] = None) -> Tuple[str, str]:
    """(current company, current designation) from the latest job in the Experience section."""
    today = today or date.today()
    lab_c, lab_d = _LABEL_COMPANY.search(text), _LABEL_DESIG.search(text)
    company, title = (_tidy(lab_c.group(1)) if lab_c else ""), (_tidy(lab_d.group(1)) if lab_d else "")
    if company and title:
        return company, title
    lines = section_text(sections, ["experience"]).split("\n")
    best = None
    for i, line in enumerate(lines):
        m = _RANGE_RE.search(line)
        if not m:
            continue
        start, end = _parse_date(m.group(1), today), _parse_date(m.group(2), today)
        if start is None or end is None or _is_internship(lines, i, m):
            continue
        if best is None or (end, start) > best[0]:
            best = ((end, start), i, m)
    if best is None:
        return company, title
    _, i, m = best
    head = _tidy(lines[i][:m.start()] + " " + lines[i][m.end():])
    if len(head) < 3:                                    # dates sit on their own line: role/company is right above
        head = next((_tidy(l) for l in reversed(lines[:i]) if l.strip()), "")
    parts = [p for p in (_tidy(x) for x in _ROLE_SPLIT.split(head)) if p][:2]
    if len(parts) == 2:
        c, t = (parts[1], parts[0]) if _TITLE_KW.search(parts[0]) or not _TITLE_KW.search(parts[1]) else (parts[0], parts[1])
    elif parts:
        c, t = ("", parts[0]) if _TITLE_KW.search(parts[0]) else (parts[0], "")
    else:
        return company, title
    return company or c, title or t


def extract_details(text: str, sections: Sequence[Tuple[str, str]]) -> Dict[str, str]:
    company, designation = extract_current_role(text, sections)
    current_ctc, expected_ctc = extract_ctc(text)
    return {
        "LinkedIn": extract_linkedin(text),
        "Current Company": company,
        "Current Designation": designation,
        "Notice Period": extract_notice_period(text),
        "Current CTC": current_ctc,
        "Expected CTC": expected_ctc,
        "Certifications": extract_certifications(text, sections),
        "Languages": extract_languages(text, sections),
        "All Skills (Auto-Detected)": extract_all_skills(text, sections),
    }


# ============================================================
# Keyword / skill matching
# ============================================================
@dataclass(frozen=True)
class Profile:
    """Describes what a tool extracts. Frozen + tuples so it pickles and hashes cheaply."""
    name: str
    skills: Tuple[Tuple[str, Tuple[str, ...]], ...]      # (column name, regex patterns)
    skill_scope: str = "full"                             # "full" or "experience"
    extra_fields: Tuple[str, ...] = field(default_factory=tuple)  # e.g. ("Location", "Total Years of Experience")


@lru_cache(maxsize=16)
def _compiled_skills(skills: Tuple[Tuple[str, Tuple[str, ...]], ...]) -> List[Tuple[str, re.Pattern]]:
    return [(name, re.compile("|".join(f"(?:{p})" for p in patterns), re.I)) for name, patterns in skills]


# ============================================================
# Per-resume + batch processing
# ============================================================
def analyze_resume(filename: str, file_bytes: bytes, profile: Profile) -> Dict:
    """Runs inside worker processes. Returns {"ok": True, "row": {...}} or {"ok": False, "reason": str}."""
    try:
        text = extract_text(filename, file_bytes)
    except ExtractionError as e:
        return {"ok": False, "reason": str(e)}
    except Exception as e:  # never let one bad file kill the batch
        return {"ok": False, "reason": f"Unexpected error: {type(e).__name__}: {e}"}

    sections = split_sections(text)
    row = {
        "Filename": filename,
        "Name": extract_name(text, filename),
        "Email": extract_email(text),
        "Phone Number": extract_phone(text),
        "Education": extract_education(text, sections),
    }
    if "Location" in profile.extra_fields:
        row["Location"] = extract_location(text)
    if "Total Years of Experience" in profile.extra_fields:
        row["Total Years of Experience"] = extract_total_experience(text, sections)
    if "Details" in profile.extra_fields:
        row.update(extract_details(text, sections))

    if profile.skill_scope == "experience":
        scope_text = section_text(sections, ["experience", "internship", "projects"])
        if scope_text.strip():
            row["Skill Source"] = "Experience/Projects"
        else:
            # No recognisable headings: search everything except the skills list
            # instead of silently reporting 0 for every skill.
            scope_text = section_text(sections, ["header", "other", "certifications", "experience", "internship", "projects"])
            row["Skill Source"] = "Whole resume (no sections found)"
    else:
        scope_text = text
    for name, rx in _compiled_skills(profile.skills):
        row[name] = 1 if rx.search(scope_text) else 0
    return {"ok": True, "row": row}


def warm_up() -> int:
    """No-op task used to start pool workers (and import this module in them) ahead of time."""
    return os.getpid()


def file_digest(file_bytes: bytes) -> str:
    return hashlib.sha1(file_bytes).hexdigest()


@dataclass
class BatchResult:
    rows: List[Dict]
    failures: List[Tuple[str, str]]          # (filename, reason)
    duplicates: List[Tuple[str, str]]        # (duplicate filename, original filename)


def process_batch(
    files: Sequence[Tuple[str, bytes]],
    profile: Profile,
    executor: Optional[Executor] = None,
    cache: Optional[Dict[Tuple[str, str], Dict]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> BatchResult:
    """
    Process many resumes. Identical files (same bytes) are processed once and
    reported as duplicates. Results are cached per (profile, file hash) so
    re-running on the same upload is instant. Output order = upload order.
    """
    cache = cache if cache is not None else {}
    seen: Dict[str, str] = {}
    duplicates, todo, order = [], [], []
    for name, data in files:
        digest = file_digest(data)
        if digest in seen:
            duplicates.append((name, seen[digest]))
            continue
        seen[digest] = name
        order.append((name, digest))
        if (profile.name, digest) not in cache:
            todo.append((name, digest, data))

    total, done = len(order), len(order) - len(todo)
    if on_progress:
        on_progress(done, total, "")

    def _store(name: str, digest: str, result: Dict) -> None:
        nonlocal done
        cache[(profile.name, digest)] = result
        done += 1
        if on_progress:
            on_progress(done, total, name)

    if executor is not None and len(todo) >= PARALLEL_THRESHOLD:
        try:
            futures = {executor.submit(analyze_resume, n, d, profile): (n, g) for n, g, d in todo}
            for fut in as_completed(futures):
                n, g = futures[fut]
                _store(n, g, fut.result())
            todo = []
        except BrokenProcessPool:
            # A worker crashed (e.g. out of memory). Finish whatever is left in-process.
            todo = [(n, g, d) for n, g, d in todo if (profile.name, g) not in cache]
    for n, g, d in todo:
        _store(n, g, analyze_resume(n, d, profile))

    rows, failures = [], []
    for name, digest in order:
        result = cache[(profile.name, digest)]
        if result["ok"]:
            rows.append({**result["row"], "Filename": name})
        else:
            failures.append((name, result["reason"]))
    return BatchResult(rows, failures, duplicates)
