"""Cleaning rules shared by the ingestion script and the edit form."""
import math
import re
import unicodedata

ID_RE = re.compile(r"^A\d{6}$")
_ID_JUNK = re.compile(r"[\s\-_./]+")
_SPACES = re.compile(r"\s+")

MALE = {"M", "MALE", "MAN", "BOY", "ފިރިހެން"}
FEMALE = {"F", "FEMALE", "WOMAN", "GIRL", "އަންހެން"}


def is_blank(value):
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip().lower() in {"", "nan", "none", "null", "n/a", "na", "-"}


def clean_text(value):
    """Trim, collapse inner whitespace, drop zero-width / control characters."""
    if is_blank(value):
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = "".join(ch for ch in text if unicodedata.category(ch) not in {"Cc", "Cf"} or ch in "\t\n")
    return _SPACES.sub(" ", text).strip()


def normalize_id(value):
    """Return (national_id or None, note or None).

    National ID cards look like A123456. Excel often turns them into floats or
    adds spaces/dashes; six bare digits are assumed to have lost the 'A'.
    """
    if is_blank(value):
        return None, "missing ID"
    raw = str(value).strip()
    if re.fullmatch(r"\d+\.0+", raw):
        raw = raw.split(".")[0]
    nid = _ID_JUNK.sub("", raw).upper()
    if ID_RE.match(nid):
        return nid, None
    if re.fullmatch(r"\d{6}", nid):
        return "A" + nid, "six digits without 'A' prefix; assumed A" + nid
    m = re.fullmatch(r"A0*(\d{1,6})", nid)
    if m and len(nid) != 7:
        fixed = "A" + m.group(1).zfill(6)
        if ID_RE.match(fixed):
            return fixed, f"padded/trimmed '{raw}' to {fixed}"
    if re.fullmatch(r"[A-Z]{1,2}\d{4,8}", nid):
        return nid, f"unusual ID format '{nid}' (kept as is)"
    return None, f"unreadable ID '{raw}'"


def normalize_sex(value):
    text = clean_text(value)
    if not text:
        return "", None
    key = text.upper().rstrip(".")
    if key in MALE:
        return "M", None
    if key in FEMALE:
        return "F", None
    return "", f"unrecognised sex value '{text}'"


def normalize_age(value):
    if is_blank(value):
        return None, None
    if isinstance(value, (int, float)):
        age = int(round(float(value)))
    else:
        m = re.search(r"\d{1,3}", str(value))
        if not m:
            return None, f"unreadable age '{value}'"
        age = int(m.group())
    if not 0 <= age <= 130:
        return None, f"age out of range '{value}'"
    return age, None


def normalize_phone(value):
    """Return (phone, note). Maldives numbers are 7 digits; a 960 country code is dropped."""
    if is_blank(value):
        return "", None
    raw = str(value).strip()
    if re.fullmatch(r"\d+\.0+", raw):
        raw = raw.split(".")[0]
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10 and digits.startswith("960"):
        digits = digits[3:]
    if re.fullmatch(r"[3-9]\d{6}", digits):
        return digits, None
    if 7 <= len(digits) <= 15:
        return digits, f"unusual phone number '{raw}' (kept as is)"
    return "", f"unreadable phone number '{raw}'"


_PHONE_SPLIT = re.compile(r"\s*(?:[,;/|\n&]|\bor\b)\s*", re.I)


def split_phones(text):
    """'7771234, 9991234' -> ['7771234', '9991234'] (a stored phone cell may hold several numbers)."""
    return [p for p in (x.strip() for x in _PHONE_SPLIT.split(text or "")) if p]


def normalize_phones(value):
    """Return (phones, note) for a cell that may hold several numbers ('7771234 / 9991234').

    Each number is cleaned like normalize_phone; the result is joined with ', ' (duplicates dropped).
    Two 7-digit numbers written with only a space between them are split too.
    """
    if is_blank(value):
        return "", None
    raw = str(value).strip()
    if re.fullmatch(r"\d+\.0+", raw):
        raw = raw.split(".")[0]
    parts = _PHONE_SPLIT.split(raw)
    if len(parts) == 1 and re.fullmatch(r"(?:\+?960[\s-]?)?[3-9]\d{6}(?:\s+(?:\+?960[\s-]?)?[3-9]\d{6})+", raw):
        parts = re.findall(r"(?:\+?960[\s-]?)?[3-9]\d{6}", raw)
    numbers, notes = [], []
    for part in parts:
        number, note = normalize_phone(part)
        if note:
            notes.append(note)
        if number and number not in numbers:
            numbers.append(number)
    return ", ".join(numbers), "; ".join(notes) or None


def normalize_party(value):
    return clean_text(value).upper()


def compare_key(text):
    """Loose comparison key: case-insensitive, ignores spaces and punctuation."""
    return re.sub(r"[\W_]+", "", clean_text(text).casefold())
