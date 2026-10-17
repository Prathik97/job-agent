"""Resume layout in one place. Mirrors data/raw/resume_reference.pdf:

centred NAME, headline line, contact line; then PROFESSIONAL SUMMARY (one paragraph), SKILLS
("Label: items." lines), WORK EXPERIENCE (company bold, title, italic "City | Month YYYY to
Month YYYY", context paragraph, round bullets with metrics in bold), EDUCATION, CERTIFICATIONS.
Single column, real text, no tables or images, so ATS parsers read it in order.
"""
SECTION_ORDER = ["summary", "skills", "experience", "projects", "education", "certifications"]
HEADINGS = {"summary": "PROFESSIONAL SUMMARY", "skills": "SKILLS", "experience": "WORK EXPERIENCE",
            "projects": "PERSONAL PROJECTS", "education": "EDUCATION", "certifications": "CERTIFICATIONS"}
DATE_JOINER = " to "      # "August 2024 to July 2026"
META_SEPARATOR = " | "    # "Hyderabad | August 2024 to July 2026"
FONT_CANDIDATES = [  # first (regular, bold, italic) that exists and has the rupee sign (U+20B9) wins
    ("/System/Library/Fonts/Supplemental/Georgia.ttf", "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
     "/System/Library/Fonts/Supplemental/Georgia Italic.ttf"),
]
FONT_NAME = "Georgia"
FONT_SIZES = [10.5, 10.0, 9.5, 9.0]   # tried in order until the PDF fits in 2 pages
MARGIN_INCH = 0.7
MAX_PAGES = 2
