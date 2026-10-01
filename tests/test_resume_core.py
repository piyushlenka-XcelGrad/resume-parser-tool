import io
import zipfile
from datetime import date

import pytest

import resume_core as rc
import xcelgrad_sales
import xcelgrad_tech


def skill(profile, name):
    return dict(rc._compiled_skills(profile.skills))[name]


# ---------------- contact fields ----------------
@pytest.mark.parametrize("text,expected", [
    ("+91 98765 43210", "+91 98765 43210"),          # most common Indian format (old code: "")
    ("Tel: 098765 43210", "098765 43210"),           # old code truncated to "098765 4321"
    ("+91-9876543210", "+91-9876543210"),
    ("(555) 123-4567", "(555) 123-4567"),
    ("Worked 2018 - 2021 at X, 2021 - 2023 at Y", ""),
    ("2015 - 2019 2019 - 2021", ""),
    ("Graduated 2019\n9876543210", "9876543210"),     # must not span lines
])
def test_phone(text, expected):
    assert rc.extract_phone(text) == expected


def test_email_rejects_pipe_tld():
    assert rc.extract_email("x@y.c|m") == ""
    assert rc.extract_email("Mail: priya.iyer@gmail.com | 98") == "priya.iyer@gmail.com"


@pytest.mark.parametrize("text,expected", [
    ("JOHN DOE\nSoftware Engineer", "John Doe"),
    ("john.doe@gmail.com | +91 9876543210\nJohn Doe", "John Doe"),
    ("Resume\nSoftware Engineer\nPriya Iyer", "Priya Iyer"),
    ("Name: A. K. Sharma", "A. K. Sharma"),
])
def test_name(text, expected):
    assert rc.extract_name(text) == expected


def test_name_falls_back_to_filename():
    assert rc.extract_name("12345\n@@@", "Naukri_RohanGupta[5y_2m].pdf") == "Rohan Gupta"


def test_education_word_boundaries():
    txt = "EDUCATION\nMastered negotiation early.\nB.Tech in Computer Science, IIT Delhi"
    assert rc.extract_education(txt) == "B.Tech in Computer Science"
    assert rc.extract_education("EDUCATION\nM.A. in English, DU") == "M.A. in English"


def test_location():
    assert rc.extract_location("Priya\npriya@x.com | Pune, India") == "Pune"
    assert rc.extract_location("Address: 12, MG Road, Bengaluru 560001") == "Bengaluru"


# ---------------- sections & experience ----------------
RESUME = """Rohan Gupta
rohan@x.com | +91 98765 43210 | Mumbai
SUMMARY
Sales leader with strong communication skills and 6+ years of experience.
WORK EXPERIENCE
Sales Manager - HDFC Bank   Jan 2020 - Present
- Built React dashboards; improved communication skills of the team
- Grew B2B revenue in e-commerce and telecommunications accounts
Business Development Executive - Airtel   Jun 2017 - Dec 2019
Sales Intern - Zomato   May 2016 - Aug 2016
PROJECTS
- Python churn model
EDUCATION
MBA, IIM Lucknow, 2015 - 2017
SKILLS
Java, Django
"""


def test_sections_ignore_inline_keywords():
    kinds = [k for k, _ in rc.split_sections(RESUME)]
    assert kinds == ["header", "other", "experience", "projects", "education", "skills"]
    exp = rc.section_text(rc.split_sections(RESUME), ["experience"])
    assert "e-commerce" in exp                       # not cut off at "communication skills"


def test_total_experience_excludes_internship_and_education():
    years = rc.extract_total_experience(RESUME, today=date(2026, 1, 1))
    # Jun 2017 - Jan 2026 is one continuous stretch (back-to-back jobs): 104 months = 8.7 y
    assert years == 8.7


def test_total_experience_merges_overlaps_and_uses_stated_fallback():
    txt = "EXPERIENCE\nA  Jan 2020 - Dec 2021\nB  Jun 2021 - Jun 2022"
    assert rc.extract_total_experience(txt, today=date(2026, 1, 1)) == 2.5
    assert rc.extract_total_experience("SUMMARY\n5+ years of sales experience") == 5.0


# ---------------- skill / industry matching ----------------
def test_tech_skills_only_from_experience_sections():
    row = rc.analyze_resume("r.docx", _docx(RESUME), xcelgrad_tech.PROFILE)["row"]
    assert row["React"] == 1 and row["Python"] == 1
    assert row["Java"] == 0 and row["Django"] == 0   # only listed under SKILLS
    assert row["Skill Source"] == "Experience/Projects"


def test_tech_pattern_fixes():
    p = xcelgrad_tech.PROFILE
    assert skill(p, ".NET").search("5 years in .NET and C#")       # old: \b\.net never matched
    assert not skill(p, "Express.js").search("expressed interest in the role")
    assert not skill(p, "React").search("react to customer escalations")
    assert not skill(p, "Java").search("JavaScript developer")


@pytest.mark.parametrize("name,text", [
    ("E-commerce", "worked at an e-commerce startup"),
    ("B2B", "business-to-business sales"),
    ("Telecom", "telecommunications firm"),
    ("Fintech", "financial technology"),
])
def test_industry_variants_now_match(name, text):
    assert skill(xcelgrad_sales.PROFILE, name).search(text)


def test_it_does_not_match_the_word_it():
    assert not skill(xcelgrad_sales.PROFILE, "IT").search("I made it happen and owned it")
    assert skill(xcelgrad_sales.PROFILE, "IT").search("10 years in the IT services industry")


# ---------------- file formats & batch ----------------
def _docx(body: str, header: str = "") -> bytes:
    def paras(t):
        return "".join(f"<w:p><w:r><w:t>{l}</w:t></w:r></w:p>" for l in t.split("\n"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document><w:body>{paras(body)}</w:body></w:document>")
        if header:
            z.writestr("word/header1.xml", f"<w:hdr>{paras(header)}</w:hdr>")
    return buf.getvalue()


def test_docx_header_text_is_read_first():
    text = rc.extract_text("a.docx", _docx("WORK EXPERIENCE\nX", header="Meera Nair\nmeera@x.com"))
    assert text.startswith("Meera Nair")
    assert rc.extract_email(text) == "meera@x.com"


def test_batch_reports_failures_duplicates_and_keeps_order():
    good = _docx(RESUME)
    files = [("b.docx", good), ("bad.pdf", b"not a pdf"), ("copy.docx", good), ("old.doc.docx", b"PK-junk")]
    res = rc.process_batch(files, xcelgrad_sales.PROFILE)
    assert [r["Filename"] for r in res.rows] == ["b.docx"]
    assert dict(res.failures)["bad.pdf"] == "Corrupt or unreadable PDF"
    assert res.duplicates == [("copy.docx", "b.docx")]
    assert "old.doc.docx" in dict(res.failures)


# ---------------- profile details (LinkedIn, current role, notice, CTC, certs, languages, skills) ----------------
DETAILS = """Rohan Gupta
rohan@x.com | linkedin.com/in/rohan-gupta-12 | Pune
CCTC: 12.5 LPA  ECTC: 18 LPA  Notice Period: 30 days
WORK EXPERIENCE
Senior Sales Manager - Hindustan Unilever   Jan 2021 - Present
- Built React dashboards with nodejs
Sales Officer at Dabur India   Jun 2016 - Dec 2020
CERTIFICATIONS
- AWS Certified Cloud Practitioner, 2022
- PMP (PMI), 2020
LANGUAGES
English (fluent), Hindi, Marathi
SKILLS
Frontend: React, Vue.js
Negotiation, Key Account Handling
"""


def _details(text):
    return rc.extract_details(text, rc.split_sections(text))


def test_details_basic():
    d = _details(DETAILS)
    assert d["LinkedIn"] == "https://linkedin.com/in/rohan-gupta-12"
    assert (d["Current Company"], d["Current Designation"]) == ("Hindustan Unilever", "Senior Sales Manager")
    assert (d["Notice Period"], d["Current CTC"], d["Expected CTC"]) == ("30 days", "12.5 LPA", "18 LPA")
    assert d["Certifications"] == "AWS Certified Cloud Practitioner, 2022; PMP (PMI), 2020"
    assert d["Languages"] == "English, Hindi, Marathi"
    assert d["All Skills (Auto-Detected)"].split(", ")[:4] == ["React", "Vue.js", "Negotiation", "Key Account Handling"]
    assert "Node.js" in d["All Skills (Auto-Detected)"]          # found in experience text, not just the Skills list


def test_current_role_when_dates_are_on_their_own_line_and_company_first():
    t = "EXPERIENCE\nHDFC Bank | Relationship Manager\nMar 2022 - Present\nICICI Bank | Executive\nJan 2018 - Feb 2022"
    d = _details(t)
    assert (d["Current Company"], d["Current Designation"]) == ("HDFC Bank", "Relationship Manager")


def test_current_role_ignores_internships_and_uses_labels():
    t = "EXPERIENCE\nSales Intern - Zomato   May 2025 - Aug 2025\nBD Executive - Airtel   Jan 2020 - Dec 2024"
    assert _details(t)["Current Company"] == "Airtel"
    assert _details("Current Company: Infosys\nDesignation: Tech Lead")["Current Designation"] == "Tech Lead"


@pytest.mark.parametrize("text,notice,cur,exp", [
    ("Notice Period: Immediate", "Immediate", "", ""),
    ("Immediate joiner", "Immediate", "", ""),
    ("Currently serving notice period, last day 30 June", "Serving notice period, last day 30 june", "", ""),
    ("Current CTC - Rs. 8,50,000 per annum | Expected CTC - 12 Lacs", "", "Rs. 8,50,000 per annum", "12 Lacs"),
    ("Current Salary: 6 LPA, Expected Salary: 9-10 LPA", "", "6 LPA", "9-10 LPA"),
    ("Achieved 120% of target and grew revenue", "", "", ""),
])
def test_notice_and_ctc(text, notice, cur, exp):
    d = _details(text)
    assert (d["Notice Period"], d["Current CTC"], d["Expected CTC"]) == (notice, cur, exp)


def test_inline_languages_and_certifications_without_headings():
    d = _details("Languages Known: English, Hindi\nCertifications: PMP; Scrum Master")
    assert d["Languages"] == "English, Hindi" and d["Certifications"] == "PMP; Scrum Master"
    assert _details("Languages: Python, Java")["Languages"] == ""     # programming languages are not spoken languages


def test_skill_vocab_avoids_false_positives():
    s = _details("EXPERIENCE\nWe react to escalations, expressed interest, can excel and sap energy; go far")["All Skills (Auto-Detected)"]
    assert s == ""
    assert "SAP" in _details("Implemented SAP HANA")["All Skills (Auto-Detected)"]


def test_new_columns_appear_in_both_tools():
    body = _docx(RESUME_FOR_DETAILS)
    for prof in (xcelgrad_tech.PROFILE, xcelgrad_sales.PROFILE):
        row = rc.analyze_resume("r.docx", body, prof)["row"]
        assert {"LinkedIn", "Current Company", "Notice Period", "All Skills (Auto-Detected)"} <= row.keys()
        assert row["Current Company"] == "Hindustan Unilever"


RESUME_FOR_DETAILS = DETAILS


def test_job_after_internship_line_still_counts():
    txt = "EXPERIENCE\nSales Intern - Zomato   May 2016 - Aug 2016\nBD Executive - Airtel   Jan 2017 - Dec 2018"
    assert rc.extract_total_experience(txt, today=date(2026, 1, 1)) == 2.0
