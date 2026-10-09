"""Redaction shared by webhook ingestion and analysis output."""
import re
from review_ai.masking import mask_spec
from review_ai.secrets_pattern import MASK, redact

def safe_text(text):
    # redact() already handles known credential formats; free-form errors also use key=value/header forms.
    text = re.sub(r"(?i)\bbearer\s+[^\s,;\"']+", MASK, text)
    text = re.sub(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
                  lambda m: m.group(1) + "=" + MASK, text)
    return redact(text)


def safe_data(value):
    if isinstance(value, dict):
        return {k: safe_data(v) for k, v in mask_spec(value).items()}
    if isinstance(value, list):
        return [safe_data(v) for v in value]
    return safe_text(value) if isinstance(value, str) else value
