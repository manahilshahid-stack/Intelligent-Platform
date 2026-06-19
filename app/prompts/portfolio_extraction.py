"""
Portfolio KPI extraction prompt.

Builds the messages list for an OpenRouter chat completion call.
The model is instructed to return a single JSON object matching the schema below.

JSON schema
-----------
{
  "company_name":         string | null,
  "document_title":       string | null,
  "period":               string | null,       # e.g. "Q4 2024", "FY 2023", "October 2024"
  "business_description": string | null,       # 1-2 sentences on what the company does

  "cash_position": {
    "value":       number | null,
    "currency":    string | null,
    "source_text": string | null
  },
  "monthly_burn": {
    "value":       number | null,
    "currency":    string | null,
    "source_text": string | null
  },
  "runway_months": {
    "value":       number | null,
    "source_text": string | null
  },
  "revenue": {
    "value":       number | null,
    "currency":    string | null,
    "period":      string | null,              # period this revenue figure covers
    "source_text": string | null
  },
  "mrr": {
    "value":       number | null,              # Monthly Recurring Revenue
    "currency":    string | null,
    "source_text": string | null
  },
  "arr": {
    "value":       number | null,
    "currency":    string | null,
    "source_text": string | null
  },
  "gross_margin": {
    "value":       number | null,              # percentage, e.g. 72.5 means 72.5%
    "source_text": string | null
  },
  "growth_metrics": {
    "mom_growth_pct":  number | null,          # Month-over-Month revenue growth %
    "yoy_growth_pct":  number | null,          # Year-over-Year revenue growth %
    "description":     string | null,          # free text if growth stated differently
    "source_text":     string | null
  },
  "headcount": {
    "value":       number | null,
    "source_text": string | null
  },
  "customers": {
    "value":       number | null,
    "source_text": string | null
  },

  "key_wins":       [string],                  # complete sentences describing achievements
  "key_challenges": [string],                  # complete sentences describing obstacles
  "risks":          [string],                  # complete sentences describing forward risks
  "asks":           [string],                  # complete sentences describing investor asks
  "next_milestones":[string],                  # upcoming goals / targets mentioned in doc

  "summary":    string,                        # 3-5 sentence plain-text overview
  "confidence": "low" | "medium" | "high",
  "missing_fields": [string],

  "custom_kpis": {
    "<field_key>": {
      "label":       string,
      "value":       string | number | boolean | null,
      "type":        string,
      "source_text": string | null,
      "confidence":  "low" | "medium" | "high"
    }
  }
}
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# Maximum characters of raw_text sent to the model.
_MAX_TEXT_CHARS = 60_000

_SYSTEM_BASE = """\
You are an expert financial analyst assistant for a venture capital portfolio management \
platform. Your task is to extract structured KPI and qualitative intelligence from \
portfolio company documents and return it as a single JSON object.

You are reading documents that may be board decks, monthly or quarterly investor updates, \
IC memos, pitch decks, or financial summaries. Extract every piece of relevant information.

Required top-level fields:
company_name, document_title, period, business_description,
cash_position, monthly_burn, runway_months,
revenue, mrr, arr, gross_margin, growth_metrics,
headcount, customers,
key_wins, key_challenges, risks, asks, next_milestones,
summary, confidence, missing_fields, custom_kpis.

Rules:
1.  Return VALID JSON ONLY. No markdown fences, no prose before or after, no comments.

2.  Do not invent or estimate any metric not explicitly stated in the document. \
    If a value is not present, set it to null.

3.  For every numeric field, include a "source_text" quoting the exact sentence or phrase \
    (≤ 50 words) from which the number was taken. If you cannot find the source, omit it.

4.  Preserve original currencies exactly (e.g. "EUR", "USD", "GBP"). \
    Numeric values must be plain numbers — no commas, symbols, or units. \
    Example: 1500000 not "1.5M" or "€1,500,000".

5.  For "period": extract the reporting period as precisely as possible. \
    Look for month names, quarter indicators (Q1–Q4), fiscal year labels, \
    or date ranges. Examples: "Q3 2024", "October 2024", "FY2024", "H1 2025".

6.  For "business_description": write 1-2 sentences describing what the company \
    builds or sells, who its customers are, and its business model. \
    Derive this from context clues in the document even if not stated explicitly.

7.  For "gross_margin": extract as a percentage number (e.g. 72.5 for 72.5%). \
    Look for "gross margin", "GM", "contribution margin" mentions.

8.  For "growth_metrics": extract MoM or YoY revenue growth rates if mentioned. \
    If growth is stated in a non-standard way, capture it verbatim in "description".

9.  For "mrr": extract Monthly Recurring Revenue if stated separately from ARR. \
    If only ARR is given, set mrr to null (do not divide ARR by 12).

10. For list fields (key_wins, key_challenges, risks, asks, next_milestones): \
    write COMPLETE, INFORMATIVE SENTENCES — not keywords or fragments. \
    Each item should stand alone and give full context. \
    Aim for 3-7 items per list where evidence exists. \
    Examples of bad: "Revenue growth", "Hiring". \
    Examples of good: \
      "Closed a €2M enterprise contract with Deutsche Bank in October.", \
      "Headcount grew from 18 to 24 FTE following the seed close.", \
      "Customer churn remains elevated at ~5% MoM due to pricing sensitivity."

11. For "summary": write 3-5 sentences of dense, factual prose covering the company's \
    current status, the most important metrics, key developments, and forward outlook. \
    This will be used as the primary context for investor queries — make it information-rich.

12. For "confidence": \
    "high"   = most standard KPIs present and clearly stated, \
    "medium" = partial data, some fields missing or ambiguous, \
    "low"    = very little financial data, document is not a portfolio update.

13. List every top-level schema field that could not be populated in "missing_fields". \
    Only list fields, not sub-fields. Example: ["mrr", "gross_margin", "growth_metrics"].

14. For "custom_kpis": include all company-specific fields defined below. \
    Always include every key — set value to null if not found.\
"""

_SYSTEM_NO_CUSTOM = _SYSTEM_BASE + """
15. There are no custom KPI fields for this company. Return "custom_kpis": {}.\
"""

_CUSTOM_FIELD_INTRO = """
15. Custom KPI fields — extract these company-specific metrics IN ADDITION to the \
    standard fields above. Include each one as a key inside "custom_kpis". \
    The "type" field must match the type shown in the definition.\
"""

_USER_TEMPLATE = """\
Extract all portfolio KPIs and qualitative intelligence from the document below \
and return a single JSON object matching the schema exactly.

Company: {company_name}
Document title: {document_title}

--- BEGIN DOCUMENT ---
{raw_text}
--- END DOCUMENT ---\
"""


@dataclass
class CustomKpiFieldDef:
    field_key: str
    field_label: str
    field_type: str          # text | number | currency | percentage | date | boolean
    description: str | None
    extraction_hint: str | None
    is_required: bool


def _build_custom_fields_block(fields: Sequence[CustomKpiFieldDef]) -> str:
    lines: list[str] = [_CUSTOM_FIELD_INTRO]
    for f in fields:
        req = " [REQUIRED]" if f.is_required else " [optional]"
        line = f"    - {f.field_key} | Label: {f.field_label} | Type: {f.field_type}{req}"
        if f.description:
            line += f" | Description: {f.description}"
        if f.extraction_hint:
            line += f" | Extraction hint: {f.extraction_hint}"
        lines.append(line)

    lines.append(
        "\n    For required fields, if the value cannot be found, still include the key "
        "with value null and confidence \"low\". Never omit a required field."
    )
    return "\n".join(lines)


def build_portfolio_extraction_messages(
    company_name: str,
    document_title: str,
    raw_text: str,
    custom_kpi_fields: Sequence[CustomKpiFieldDef] | None = None,
) -> list[dict]:
    """
    Return a messages list ready for an OpenRouter chat completion call.
    """
    truncated = raw_text[:_MAX_TEXT_CHARS]
    if len(raw_text) > _MAX_TEXT_CHARS:
        truncated += f"\n\n[... document truncated at {_MAX_TEXT_CHARS} characters ...]"

    if custom_kpi_fields:
        system = _SYSTEM_BASE + _build_custom_fields_block(custom_kpi_fields)
    else:
        system = _SYSTEM_NO_CUSTOM

    user_content = _USER_TEMPLATE.format(
        company_name=company_name or "Unknown",
        document_title=document_title or "Untitled",
        raw_text=truncated,
    )

    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": user_content},
    ]
