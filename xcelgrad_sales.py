"""Tool 2: industry / vertical exposure for sales & BD hiring, matched across the whole resume."""
from batch_ui import render_batch_tool
from resume_core import Profile

# Column name -> regex variants (matched case-insensitively).
INDUSTRY_PATTERNS = {
    "Pharma": [r"\bpharma\b", r"\bpharmaceuticals?\b"],
    "Hospitality": [r"\bhospitalit(?:y|ies)\b", r"\bhotels?\b", r"\bfood\s+(?:and|&)\s+beverage\b", r"\bf\s?&\s?b\b", r"\bfnb\b"],
    "Enterprise Software": [r"\benterprise[\s\-]?software\b", r"\benterprise\s+apps?\b", r"\benterprise\s+solutions?\b"],
    "Real Estate": [r"\breal[\s\-]?estate\b", r"\bproperty\s+(?:development|management)\b"],
    "Agritech": [r"\bagri[\s\-]?tech\b", r"\bagriculture\b", r"\bfarming\b"],
    "Sales": [r"\bsales\b"],
    "Business Development": [r"\bbusiness\s+development\b", r"\bbd\s+(?:manager|executive)\b", r"\bbusiness\s+dev\b", r"\bbd\b"],
    "HoReCa": [r"\bhoreca\b", r"\bhotels?,?\s+restaurants?,?\s+(?:and\s+)?caf[eé]s?\b"],
    "Banking": [r"\bbanking\b", r"\bbank\b", r"\bfinancial\s+services\b"],
    "FMCG": [r"\bfmcg\b", r"\bfast[\s\-]moving\s+consumer\s+goods\b"],
    "Telecom": [r"\btelecoms?\b", r"\btelecommunications?\b"],
    "Insurance": [r"\binsurance\b"],
    "Fintech": [r"\bfin[\s\-]?tech\b", r"\bfinancial\s+technology\b"],
    # Never match the bare word "it" — it appears in every resume.
    "IT": [r"\bIT\s+(?:sector|services|industry|company|firm|solutions)\b", r"\binformation\s+technology\b", r"\btechnology\s+company\b"],
    "SaaS": [r"\bsaas\b", r"\bsoftware[\s\-]as[\s\-]a[\s\-]service\b"],
    "B2B": [r"\bb2b\b", r"\bbusiness[\s\-]to[\s\-]business\b"],
    "EdTech": [r"\bed[\s\-]?tech\b", r"\beducation(?:al)?\s+technology\b"],
    "BFSI": [r"\bbfsi\b", r"\bbanking,?\s+financial\s+services,?\s+(?:and|&)\s+insurance\b"],
    "Logistics": [r"\blogistics?\b", r"\bsupply\s+chain\b"],
    "E-commerce": [r"\be[\s\-]?commerce\b", r"\bonline\s+retail\b"],
}

PROFILE = Profile(
    name="industry_v3",
    skills=tuple((name, tuple(p)) for name, p in INDUSTRY_PATTERNS.items()),
    skill_scope="full",
    extra_fields=("Location", "Total Years of Experience", "Details"),
)


def main():
    render_batch_tool(
        key="industry",
        header="📄 Batch Resume → Industry / Vertical Extractor",
        intro_md=(
            "Upload **multiple resumes (PDF or Word .docx)** and get a **single Excel file** with:\n"
            "- Basic information (Name, Email, Phone, Education, Location)\n"
            "- **Total years of work experience** (internships excluded, overlapping jobs counted once)\n"
            "- Industry / vertical presence indicators (1/0) — searched across the **entire resume**"
        ),
        profile=PROFILE,
        checked_label="Industries / Verticals Checked",
        excel_prefix="resume_industries",
    )


if __name__ == "__main__":
    main()
