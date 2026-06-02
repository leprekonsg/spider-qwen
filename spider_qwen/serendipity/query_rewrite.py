"""T-1.2: Step-Back + HyDE + Query2Doc query expansion for vague/obsolete parts.

``expand_query(q)`` returns a deduped list of ``SearchQuery`` variants:

- ``original``        the query as typed
- ``step_back``       a broader device-class abstraction (Step-Back prompting)
- ``hyde``            a hypothetical pseudo-datasheet paragraph (HyDE)
- ``query2doc``       original query concatenated with the pseudo-doc (Query2Doc)
- ``obsolescence``    query + obsolescence vocabulary (EOL/NRND/LTB/...)
- ``mpn_pattern``     an MPN/cross-reference variant (explicit MPN if present,
                      else a device-class family scaffold)
- ``broker_operator`` a long-tail broker/surplus operator variant

The implementation is deterministic and offline so the golden test runs with no
API key. An optional ``llm`` callable (Qwen flash/max) can enrich the HyDE
paragraph; it degrades to the deterministic pseudo-doc on any failure.
"""

from __future__ import annotations

import re
from typing import Callable

from pydantic import BaseModel

# Obsolescence / cross-reference vocabulary (build plan T-1.2).
OBSOLESCENCE_VOCAB: tuple[str, ...] = (
    "obsolete", "eol", "nrnd", "nla", "ltb", "superseded by",
    "cross reference", "equivalent", "nos",
)

# Device-class normalisation: spoken form -> canonical class + representative
# part-family scaffolds (used to synthesise an MPN-pattern variant when the query
# has no explicit part number).
_DEVICE_CLASSES: dict[str, tuple[str, tuple[str, ...]]] = {
    "op-amp": ("operational amplifier", ("LM358", "TL072", "NE5532")),
    "op amp": ("operational amplifier", ("LM358", "TL072", "NE5532")),
    "opamp": ("operational amplifier", ("LM358", "TL072", "NE5532")),
    "operational amplifier": ("operational amplifier", ("LM358", "TL072", "NE5532")),
    "mcu": ("microcontroller", ("ATmega328", "STM32F103", "PIC16F877")),
    "microcontroller": ("microcontroller", ("ATmega328", "STM32F103", "PIC16F877")),
    "connector": ("connector", ("DF13", "PH2.0", "JST-XH")),
    "transistor": ("transistor", ("2N2222", "BC547", "TIP120")),
    "regulator": ("voltage regulator", ("LM7805", "LM317", "AMS1117")),
    "fpga": ("FPGA", ("XC7A35T", "EP4CE6", "ICE40")),
    "adc": ("analog-to-digital converter", ("ADS1115", "MCP3008", "ADC0804")),
    "dac": ("digital-to-analog converter", ("MCP4725", "DAC8552")),
    "diode": ("diode", ("1N4148", "1N4007", "BAT54")),
    "capacitor": ("capacitor", ("EEU-FR", "GRM188", "T491")),
    "relay": ("relay", ("G5V-1", "JZC-32F", "HK19F")),
}

# Manufacturer aliases seen in queries (used to scope MPN/broker variants).
_MANUFACTURERS = (
    "ti", "texas instruments", "adi", "analog devices", "maxim", "nxp", "st",
    "stmicroelectronics", "microchip", "atmel", "infineon", "onsemi", "on semiconductor",
    "hirose", "molex", "te connectivity", "amphenol", "vishay", "rohm", "renesas",
)

# Long-tail / broker sources for the broker-operator variant (plan T-5.3).
_BROKER_OPERATORS = ("rochester", "lansdale", "oemsecrets", "octopart", "avnet")

# An explicit manufacturer part number: letters then digits, optional suffixes.
_EXPLICIT_MPN_RE = re.compile(r"\b[A-Z]{1,5}\d{2,}[A-Z0-9\-/.]*\b", re.IGNORECASE)
_MAX_LEN = 200


class SearchQuery(BaseModel):
    text: str
    kind: str  # original|step_back|hyde|query2doc|obsolescence|mpn_pattern|broker_operator
    rationale: str = ""


def _truncate(text: str) -> str:
    text = " ".join(text.split())
    return text[:_MAX_LEN].rstrip()


def _detect_device_class(q: str) -> tuple[str, tuple[str, ...]] | None:
    low = q.lower()
    # Prefer the longest matching key so "operational amplifier" beats "op amp".
    for key in sorted(_DEVICE_CLASSES, key=len, reverse=True):
        if key in low:
            return _DEVICE_CLASSES[key]
    return None


def _detect_manufacturer(q: str) -> str | None:
    low = q.lower()
    for mfr in sorted(_MANUFACTURERS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(mfr)}\b", low):
            return mfr.upper() if len(mfr) <= 3 else mfr.title()
    return None


def _explicit_mpns(q: str) -> list[str]:
    out: list[str] = []
    for m in _EXPLICIT_MPN_RE.findall(q):
        token = m.strip()
        # Skip pure-year noise like "90s" / "1990".
        if token.lower() in {"90s", "80s", "70s"} or re.fullmatch(r"\d{2,4}s?", token):
            continue
        out.append(token)
    return out


def _hyde_doc(query: str, device_class: str | None, llm: Callable[[str], str] | None) -> str:
    if llm is not None:
        try:
            doc = llm(query)
            if doc:
                return _truncate(doc)
        except Exception:
            pass  # degrade to deterministic pseudo-doc
    cls = device_class or "component"
    return _truncate(
        f"Datasheet: {cls}. {query}. Specifications include package, pinout, "
        f"operating temperature, supply voltage, and cross-reference / equivalent "
        f"replacement parts for obsolete or EOL devices."
    )


def expand_query(
    query: str,
    *,
    mode: str | None = None,
    llm: Callable[[str], str] | None = None,
) -> list[SearchQuery]:
    """Expand a (possibly vague/obsolete) query into >=4 distinct search variants."""
    base = " ".join((query or "").split())
    out: list[SearchQuery] = []
    seen: set[str] = set()

    def add(text: str, kind: str, rationale: str = "") -> None:
        t = _truncate(text)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(SearchQuery(text=t, kind=kind, rationale=rationale))

    if not base:
        return out

    device = _detect_device_class(base)
    device_class = device[0] if device else None
    families = device[1] if device else ()
    mfr = _detect_manufacturer(base)

    add(base, "original", "query as typed")

    # Step-Back: broaden to the device class.
    if device_class:
        add(f"{device_class} equivalent replacement cross reference",
            "step_back", "device-class abstraction")
    else:
        add(f"{base} alternative supplier", "step_back", "generic broadening")

    # HyDE pseudo-doc + Query2Doc concatenation.
    hyde = _hyde_doc(base, device_class, llm)
    add(hyde, "hyde", "hypothetical document embedding")
    add(f"{base} {hyde}", "query2doc", "query + pseudo-doc")

    # Obsolescence vocabulary expansion.
    add(f"{base} obsolete EOL NRND NLA LTB superseded by NOS",
        "obsolescence", "lifecycle vocabulary")

    # MPN-pattern / cross-reference variant.
    explicit = _explicit_mpns(base)
    if explicit:
        mpn_text = f"{explicit[0]} cross reference equivalent datasheet"
    else:
        family = families[0] if families else "series"
        prefix = f"{mfr} " if mfr else ""
        cls = device_class or "part"
        mpn_text = f"{prefix}{family} {cls} cross reference equivalent"
    add(mpn_text, "mpn_pattern", "manufacturer part-number cross-reference")

    # Broker / long-tail operator variant.
    operators = " OR ".join(_BROKER_OPERATORS)
    add(f"{base} obsolete stock {operators}", "broker_operator", "long-tail broker sources")

    return out
