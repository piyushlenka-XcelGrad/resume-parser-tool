"""Tool 1: tech-stack skills, matched ONLY in Experience / Internship / Projects sections."""
from batch_ui import render_batch_tool
from resume_core import Profile

# Column name -> regex variants (matched case-insensitively).
TECH_SKILL_PATTERNS = {
    "React": [r"\breact(?:\.?js)?\b(?!\s+(?:to|quickly|promptly|swiftly)\b)", r"\breact\s+native\b"],
    "Next.js": [r"\bnext\.?js\b", r"\bnext\s+js\b"],
    ".NET": [r"(?<![\w.])\.net\b", r"\bdotnet\b", r"\basp\.net\b", r"\bnet\s+framework\b"],
    "MongoDB": [r"\bmongo\s?db\b", r"\bmongo\b"],
    "Flask": [r"\bflask\b"],
    "Spring Boot": [r"\bspring[\s\-]?boot\b"],
    "Vue.js": [r"\bvue(?:\.?js)?\b"],
    "Java": [r"\bjava\b(?!\s*script)", r"\bcore\s+java\b"],
    "MySQL": [r"\bmy\s?sql\b"],
    "Django": [r"\bdjango\b"],
    # Bare "express" is avoided: "expressed interest" is common in sales resumes.
    "Express.js": [r"\bexpress\.?js\b", r"\bexpress\s+(?:framework|server|api)\b", r"\bnode(?:\.?js)?\s*(?:/|&|\+|and|,)\s*express\b"],
    "Laravel": [r"\blaravel\b"],
    "Node.js": [r"\bnode\.?js\b", r"\bnode\s+js\b"],
    "Python": [r"\bpython(?:\s*[23])?\b"],
    "PostgreSQL": [r"\bpostgre\s?sql\b", r"\bpostgres\b"],
    "FastAPI": [r"\bfast\s?api\b"],
    "NestJS": [r"\bnest\.?js\b", r"\bnest\s+js\b"],
    "Machine Learning": [r"\bmachine[\s\-]learning\b", r"\bml\b"],
}

PROFILE = Profile(
    name="tech_stack_v3",
    skills=tuple((name, tuple(p)) for name, p in TECH_SKILL_PATTERNS.items()),
    skill_scope="experience",
    extra_fields=("Details",),
)


def main():
    render_batch_tool(
        key="tech",
        header="📄 Batch Resume → Tech Skills Extractor (Experience-Focused)",
        intro_md=(
            "Upload **multiple resumes (PDF or Word .docx)** and get a **single Excel file** with:\n"
            "- Basic information (Name, Email, Phone, Education) — from the entire resume\n"
            "- Skill indicators (1/0) — **only from Experience, Internship and Projects sections**, "
            "so a skill merely listed under *Skills* does not count"
        ),
        profile=PROFILE,
        checked_label="Skills Checked",
        excel_prefix="resume_tech_skills",
    )


if __name__ == "__main__":
    main()
