"""Resume layout in one place so it is easy to match to a reference resume.

TODO: mirror the section order and style of data/raw/resume_reference.pdf (not found when this
was written, so this is a conventional single-column order built from master_profile.json).
"""
SECTION_ORDER = ["summary", "experience", "projects", "skills", "education", "certifications"]
HEADINGS = {"summary": "Summary", "experience": "Experience", "projects": "Personal Projects", "skills": "Skills",
            "education": "Education", "certifications": "Certifications"}
FONT_CANDIDATES = [  # first pair that exists and has the rupee sign (U+20B9) wins
    ("/System/Library/Fonts/Supplemental/Georgia.ttf", "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"),
]
FONT_NAME = "Georgia"
FONT_SIZES = [10.5, 10.0, 9.5, 9.0]   # tried in order until the PDF fits in 2 pages
MARGIN_INCH = 0.7
MAX_PAGES = 2
