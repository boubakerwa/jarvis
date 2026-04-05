"""
Drive folder schema constants and the classification prompt used by the Agent SDK filer.
Top-level folders are fixed. Sub-folders within each are managed by the configured model as needed.
"""

# Top-level folder name under which all Jarvis files live
JARVIS_ROOT = "Jarvis"

# Fixed top-level folders with their default sub-folders
DRIVE_STRUCTURE: dict[str, list[str]] = {
    "Finances": ["Banking", "Investments", "Tax"],
    "Insurance": ["Health", "Liability", "Vehicle"],
    "Legal & Contracts": ["Employment", "Rental", "Service Agreements"],
    "Travel": ["Bookings", "Visas & Docs"],
    "Health": ["Records", "Prescriptions"],
    "Subscriptions": [],
    "Real Estate": [],
    "Vehicles": [],
    "Projects & Side Hustles": ["Sufra", "Other"],
    "Personal Development": ["Courses & Certificates", "Books & Resources"],
    "Household": ["Appliances & Warranties", "Repairs & Services", "Utilities"],
    "Misc": [],
}

TOP_LEVEL_FOLDERS = list(DRIVE_STRUCTURE.keys())

# Naming convention: YYYY-MM_description.ext (all lowercase, underscores for spaces)
FILENAME_PATTERN_DESCRIPTION = (
    "YYYY-MM_description.ext — all lowercase, underscores for spaces, "
    "no special characters. Example: 2026-03_krankenversicherung_tk_card.pdf"
)

# Classification prompt template for the Agent SDK filer
CLASSIFICATION_PROMPT = """\
You are a document classifier. You will be given a file and must determine where it \
belongs in a structured Google Drive library.

Documents may be in English or German. Classify them correctly regardless of language. \
Common German document terms: Rechnung (invoice), Vertrag (contract), \
Versicherung (insurance), Krankenversicherung (health insurance), \
Mietvertrag (rental agreement), Steuerbescheid (tax notice), \
Kontoauszug (bank statement), Bescheinigung (certificate), \
Kfz (vehicle), Mahnung (reminder/dunning notice), Quittung (receipt). \
Always respond in English.

Top-level folders (you MUST pick one of these exactly):
{top_level_folders}

Sub-folders within each top-level folder are suggestions. You may create a new sub-folder \
if none of the defaults fits — use clear, consistent naming (title case, no special chars).

Respond ONLY with a JSON object with these fields:
- top_level: string — one of the top-level folder names above (exact match required)
- sub_folder: string — sub-folder name within top_level (may be a new one if needed)
- filename: string — follow this pattern: {filename_pattern}
- summary: string — one sentence describing the document

Example response:
{{
  "top_level": "Insurance",
  "sub_folder": "Health",
  "filename": "2026-03_tk_health_insurance_card.pdf",
  "summary": "TK health insurance card for Wess, valid from 2026."
}}

File to classify:
- Original filename: {{original_filename}}
- MIME type: {{mime_type}}
- Extracted text (first 2000 chars):
{{text_preview}}
"""


def build_classification_prompt(original_filename: str, mime_type: str, text_preview: str) -> str:
    top_level_list = "\n".join(f"  - {f}" for f in TOP_LEVEL_FOLDERS)
    return CLASSIFICATION_PROMPT.format(
        top_level_folders=top_level_list,
        filename_pattern=FILENAME_PATTERN_DESCRIPTION,
    ).replace("{original_filename}", original_filename).replace(
        "{mime_type}", mime_type
    ).replace("{text_preview}", text_preview[:2000])
