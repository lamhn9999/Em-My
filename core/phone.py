"""
phone.py
--------
Vietnamese phone number normalisation and validation.

Accepted input formats:
  +84912345678   → 0912345678
  84912345678    → 0912345678
  0912345678     → 0912345678  (unchanged)

Rules:
  - Strip spaces, dashes, dots
  - Convert country-code prefix (84 / +84) to leading 0
  - Must be exactly 10 digits after normalisation
  - Must start with 0 after normalisation
  - Valid Vietnamese prefixes (first 3 digits): 032–039, 056, 058,
    070, 076–079, 081–086, 089, 090–099

Returns:
  (normalised, None)        on success
  (None,       error_msg)   on failure
"""
from __future__ import annotations

import re

# Vietnamese mobile prefixes (first 3 digits of the 10-digit number)
_VALID_PREFIXES = {
    "032", "033", "034", "035", "036", "037", "038", "039",  # Viettel
    "056", "058",                                              # Vietnamobile
    "059",                                                     # Gmobile
    "070", "076", "077", "078", "079",                        # Mobifone
    "081", "082", "083", "084", "085", "086",                 # Vinaphone
    "089",                                                     # Mobifone
    "090", "091", "092", "093", "094", "095",                 # legacy 3-op
    "096", "097", "098", "099",                               # Viettel legacy
}


def normalise_phone(raw: str) -> tuple[str | None, str | None]:
    """
    Normalise a Vietnamese phone number.

    Returns:
        (normalised_number, None)   — valid
        (None, error_message)       — invalid
    """
    if not raw:
        return None, "Số điện thoại không được để trống."

    # 1. Strip whitespace, dashes, dots, parentheses
    cleaned = re.sub(r"[\s\-\.\(\)]", "", raw)

    # 2. Remove leading +
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]

    # 3. Convert country code prefix → leading 0
    if cleaned.startswith("84") and len(cleaned) == 11:
        cleaned = "0" + cleaned[2:]

    # 4. Must be exactly 10 digits
    if not re.fullmatch(r"\d{10}", cleaned):
        return None, (
            f"'{raw}' không phải số điện thoại hợp lệ "
            f"(phải có 10 chữ số sau khi chuẩn hoá, nhận được: '{cleaned}')."
        )

    # 5. Must start with 0
    if not cleaned.startswith("0"):
        return None, f"'{raw}' không bắt đầu bằng 0 sau khi chuẩn hoá."

    # 6. Prefix check
    prefix = cleaned[:3]
    if prefix not in _VALID_PREFIXES:
        return None, (
            f"'{raw}' có đầu số '{prefix}' không thuộc danh sách "
            f"mạng di động Việt Nam."
        )

    return cleaned, None