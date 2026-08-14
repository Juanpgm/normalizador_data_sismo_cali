"""
Colombian cadastral address normalization to IGAC standard (Circular 300/01)
plus a compact canonical fingerprint (handshake) and a 3D address vector.

Faithful port of EDA.ipynb sections 3.1 and 3.2. Behaviour is identical; the
only change is packaging into an importable module.
"""
from __future__ import annotations

import re

import numpy as np


# ── 3.1 · normalize_address ───────────────────────────────────────────────────
def normalize_address(address) -> str:
    """
    Normalizes Colombian cadastral addresses to IGAC standard.

    Output format: [TIPO_VÍA] [NÚMERO] # [NÚMERO_GENERADORA]-[PLACA], Complement
    Example: "Calle 80 No. 45-23, barrio el peñón"  →  "CL 80 # 45-23, Barrio El Peñón"

    Title case is applied only to the complement (text after the first comma).
    The nomenclature part (address codes + numbers) stays in uppercase.
    Official Carrera code is KR (not CR).
    """
    if address is None:
        return ""

    value = str(address).strip()
    if not value or value in {"-", " "}:
        return value

    s = value.upper()

    # Strip internal dots from abbreviations  →  K.R.A. → KRA, C.L. → CL
    s = re.sub(
        r'\b([A-Z])\.([A-Z])(?:\.([A-Z]))?\.?',
        lambda m: m.group(1) + m.group(2) + (m.group(3) or ""),
        s,
    )

    # Road type → IGAC standard code (order matters: compound before components)
    _ROAD_TYPES = [
        (r'\bAVENIDA CALLE\b|\bAV CALLE\b|\bAV CL\b',                          'AC'),
        (r'\bAVENIDA K?ARRERA\b|\bAV K?ARRERA\b|\bAV K?R\b|\bAV CRA\b',        'AK'),
        (r'\bAUTOPISTA\b|\bAUTOP\b|\bAUT\b',                                   'AU'),
        (r'\bAVENIDA\b|\bAVDA\b|\bAVD\b|\bAVE\b|\bAV\b',                       'AV'),
        (r'\bCARRETERA\b|\bCARRET\b',                                          'CT'),
        (r'\bCARRERA\b|\bKARRERA\b|\bCARR\b|\bCRA\b|\bKRA\b|\bKRR\b|\bKR\b|\bCR\b', 'KR'),
        (r'\bCALLE\b|\bCALL\b|\bCLLE\b|\bCLL\b|\bCL\b',                        'CL'),
        (r'\bCIRCUNVALAR\b|\bCIRCUNV\b|\bCIRCV\b',                            'CV'),
        (r'\bCIRCULAR\b|\bCIRC\b',                                            'CQ'),
        (r'\bDIAGONAL\b|\bDIAG\b|\bDG\b',                                      'DG'),
        (r'\bTRANSVERSAL\b|\bTRANSV\b|\bTRANS\b|\bTRAV\b|\bTV\b|\bTR\b',      'TV'),
        (r'\bTRONCAL\b|\bTRONC\b',                                            'TC'),
        (r'\bBULEVAR\b|\bBOULEVAR\b|\bBLVD\b|\bBL\b',                        'BL'),
        (r'\bPASAJE\b|\bPSJE\b|\bPJE\b|\bPJ\b',                               'PJ'),
        (r'\bPASEO\b|\bPSO\b',                                                'PS'),
        (r'\bPEATONAL\b|\bPEAT\b',                                            'PT'),
        (r'\bVARIANTE\b',                                                      'VT'),
        (r'\bV[IÍ]A\b',                                                        'VI'),
        (r'\bCUENTAS? CORRIDAS?\b',                                           'CC'),
    ]
    for pattern, code in _ROAD_TYPES:
        s = re.sub(pattern, code, s)

    # Strip stray trailing dots left after code substitution  →  "CL." → "CL"
    s = re.sub(
        r'\b(AU|AV|AC|AK|KR|CL|CV|CQ|CT|DG|TV|TC|BL|PJ|PS|PT|VT|VI|CC)\.',
        r'\1',
        s,
    )

    # Número sign (# separator). (?![A-ZÁÉÍÓÚ]) guards against eating longer words
    s = re.sub(
        r'\b(?:N[ÚU]MERO|NUMERO|N[ÚU]M|NUM|NRO|NO|NN)(?![A-ZÁÉÍÓÚ])\.?\s*',
        '# ',
        s,
        flags=re.UNICODE,
    )
    s = re.sub(r'N[°º]\.?\s*', '# ', s)

    # Whitespace normalization: exactly one space on each side of #
    s = re.sub(r'\s*#\s*', ' # ', s)
    s = re.sub(r'\s+', ' ', s).strip()

    # Casing: nomenclature stays uppercase; complement (after comma) → title
    if ',' in s:
        nomenclatura, complement = s.split(',', 1)
        return nomenclatura.strip() + ', ' + complement.strip().title()
    return s


# ── 3.2 · handshake (canonical fingerprint) ───────────────────────────────────
_HANDSHAKE_RE = re.compile(
    r'^(AU|AV|AC|AK|KR|CL|CV|CQ|CT|DG|TV|TC|BL|PJ|PS|PT|VT|VI|CC)'
    r'\s+(\d+[A-Z]?(?:\s?BIS)?)'
    r'(?:\s+#\s+(\d+[A-Z]?)(?:[-\s](\d+))?)?',
    re.IGNORECASE,
)


def make_handshake(address) -> str:
    """Extracts a compact canonical fingerprint from a normalized address.

    "CL 5 # 43A-15, El Lido, Cali"  →  "CL5#43A-15"
    "KR 72 CON CL 11, La Hacienda"  →  "KR72"
    """
    if not address or str(address).strip() in {"", "-"}:
        return ""
    nomenclatura = str(address).split(",")[0].strip().upper()
    m = _HANDSHAKE_RE.match(nomenclatura)
    if m:
        road_type = m.group(1)
        road_num = m.group(2).replace(" ", "")
        cross_num = m.group(3) or ""
        distance = m.group(4) or ""
        if cross_num and distance:
            return f"{road_type}{road_num}#{cross_num}-{distance}"
        if cross_num:
            return f"{road_type}{road_num}#{cross_num}"
        return f"{road_type}{road_num}"
    return re.sub(r'\s+', '', nomenclatura)


# ── 3.2 · 3D address vector (three-pass parser) ───────────────────────────────
_ROAD_HEAD_RE = re.compile(
    r'^(AU|AV|AC|AK|KR|CL|CV|CQ|CT|DG|TV|TC|BL|PJ|PS|PT|VT|VI|CC)\s+'
    r'(\d+)\s*(BIS\b|[A-Z](?![A-Z]))?',
    re.IGNORECASE,
)
_CROSS_RE = re.compile(
    r'#\s*(?:[A-Z][A-Z0-9]*\.?\s+)*(\d+)\s*([A-Z](?![A-Z]))?(?:[-\s]+(\d+))?',
    re.IGNORECASE,
)
_DIST_QUALIFIER_RE = re.compile(r'#\s*[A-Z][A-Z0-9]*\.?-(\d+)', re.IGNORECASE)
_CROSS_NO_HASH_RE = re.compile(
    r'(?<!\w)(\d{1,3})\s*([A-Z](?![A-Z]))?\d*[-\s]+(\d{1,3})(?!\d)',
    re.IGNORECASE,
)

_ROAD_CODES = {
    'CL': 1, 'KR': 2, 'AV': 3, 'AC': 4, 'AK': 5, 'DG': 6, 'TV': 7, 'AU': 8, 'BL': 9,
    'CT': 10, 'CQ': 11, 'CV': 12, 'CC': 13, 'PJ': 14, 'PS': 15, 'PT': 16,
    'TC': 17, 'VT': 18, 'VI': 19,
}


def _to_decimal(num_str, letter=None, bis=False) -> float:
    """5 → 5.0 · 5A → 5.01 (A=+.01) · 5 BIS → 5.50 (mid-point)."""
    if not num_str:
        return 0.0
    base = float(num_str)
    if bis:
        return base + 0.50
    if letter:
        return base + (ord(letter.upper()) - ord('A') + 1) * 0.01
    return base


def address_to_vector(address) -> np.ndarray:
    """Converts a normalized address to a 3D vector.

    [0] road_full  = road_type_code * 1000 + road_num_decimal
    [1] cross_full = cross_num_decimal
    [2] distance   = placa distance from corner
    """
    null_vec = np.zeros(3, dtype=np.float32)
    if not address or str(address).strip() in {"", "-"}:
        return null_vec
    nomenclatura = str(address).split(",")[0].strip().upper()

    head = _ROAD_HEAD_RE.match(nomenclatura)
    if not head:
        return null_vec

    road_type, road_num, suffix = head.groups()
    bis = bool(suffix) and suffix.upper() == 'BIS'
    road_letter = suffix if (suffix and not bis) else None
    road_code = _ROAD_CODES.get(road_type.upper(), 0)

    rest = nomenclatura[head.end():]
    cross_num = cross_letter = distance = None

    cross_m = _CROSS_RE.search(rest)
    if cross_m:
        cross_num, cross_letter, distance = cross_m.groups()
        if not distance:
            dist_m = _DIST_QUALIFIER_RE.search(rest)
            if dist_m:
                distance = dist_m.group(1)
    else:
        no_hash_m = _CROSS_NO_HASH_RE.search(rest)
        if no_hash_m:
            cross_num, cross_letter, distance = no_hash_m.groups()

    return np.array([
        road_code * 1000 + _to_decimal(road_num, road_letter, bis),
        _to_decimal(cross_num, cross_letter),
        float(distance) if distance else 0.0,
    ], dtype=np.float32)


def add_handshake(df, source_col: str = "direccion_norm"):
    """Inserts integration_handshake (3D vector as tuple) after source_col."""
    loc = df.columns.get_loc(source_col) + 1
    df.insert(loc, "integration_handshake",
              df[source_col].apply(lambda x: tuple(address_to_vector(x))))
    return df
