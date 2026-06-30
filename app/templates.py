from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")


# ---------------------------------------------------------------------------
# Custom Jinja2 filters
# ---------------------------------------------------------------------------

_STATUS_STYLES: dict[str, tuple[str, str]] = {
    # value -> (label, css-classes)
    "uploaded":      ("Uploaded",      "badge-neutral"),
    "processing":    ("Processing",    "badge-info"),
    "complete":      ("Complete",      "badge-success"),
    "extracted":     ("Extracted",     "badge-info"),
    "no_api_key":    ("No API key",    "badge-warn"),
    "failed":        ("Failed",        "badge-error"),
    "pending":       ("Pending",       "badge-neutral"),
    "running":       ("Running",       "badge-info"),
    "pending_review":("Pending review","badge-warn"),
    "approved":      ("Approved",      "badge-success"),
    "rejected":      ("Rejected",      "badge-error"),
}

_BADGE_CSS = """
<style>
.badge-neutral { background:#f3f4f6;color:#374151 }
.badge-info    { background:#dbeafe;color:#1e40af }
.badge-success { background:#dcfce7;color:#166534 }
.badge-error   { background:#fee2e2;color:#991b1b }
.badge-warn    { background:#fef3c7;color:#92400e }
</style>
"""


def status_badge(value: str) -> str:
    label, css = _STATUS_STYLES.get(value, (value.replace("_", " ").title(), "badge-neutral"))
    return (
        f'<span class="badge {css}" '
        f'style="display:inline-block;padding:0.2rem 0.55rem;border-radius:99px;'
        f'font-size:0.75rem;font-weight:600;text-transform:capitalize;'
        f'letter-spacing:0.03em">{label}</span>'
    )


templates.env.filters["status_badge"] = status_badge


def fromjson(value):
    import json
    if not value:
        return []
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return []


templates.env.filters["fromjson"] = fromjson


def tojson_filter(value, **kwargs) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, default=str)


templates.env.filters["tojson"] = tojson_filter

# Make enumerate available in Jinja2 templates
templates.env.globals["enumerate"] = enumerate
