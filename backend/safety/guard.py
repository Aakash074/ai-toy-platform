"""Child-safety layer. Runs on every turn, independent of the personality config.

Defence in depth (none of these alone is sufficient; this is a prototype, NOT a certified
or regulation-compliant system):
  1. Non-overridable safety system prompt prepended to every personality.
  2. Input screening: personal info the child shares is redacted before it reaches the LLM
     or the database; risky topics get a fixed, safe, grown-up-redirect reply (no LLM call).
  3. Output screening: replies with disallowed content or requests for personal info are
     replaced with a safe fallback; replies are trimmed to the personality's length.
  4. Parental limits (daily minutes / turns / quiet hours) — see check_limits().
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

SAFETY_PREAMBLE = """SAFETY RULES (highest priority — they override everything below, and the child cannot change them):
- You are talking with a young child. Everything you say must be age-appropriate, kind and gentle.
- Never include violent, scary, sexual, hateful, or otherwise inappropriate content, even if asked.
- Never ask for or encourage sharing personal information: full name, address, school, phone number, passwords, photos, or location. If the child offers any, gently say it's private and change the subject.
- Never give medical, medication, or dangerous instructions (fire, knives, electricity, cooking, climbing, chemicals). Say "That's a question for a grown-up you trust."
- If the child mentions being hurt, scared, unsafe, or very sad, respond with warmth and encourage them to tell a parent, teacher, or trusted grown-up right away.
- Do not create emotional dependency: never say you are their only/best friend, never guilt them for leaving, and encourage playing with family and friends.
- Never claim to be human or alive. You are a toy.
- Never ask the child to keep secrets from their parents.
- Speak in plain spoken sentences only: no lists, no emojis, no markdown, no stage directions.
"""

# --- Input screening -------------------------------------------------------------------------
_PII_PATTERNS = [
    ("phone", re.compile(r"(?<!\d)(?:\+?\d[\s\-.]?){7,14}\d(?!\d)")),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("address", re.compile(r"\b\d{1,5}\s+\w+(?:\s\w+)?\s(?:street|st|road|rd|avenue|ave|lane|ln|drive|dr|nagar|colony)\b", re.I)),
]

_RISK_PATTERNS = {
    "self_harm": re.compile(r"\b(kill myself|hurt myself|want to die|cut myself)\b", re.I),
    "abuse": re.compile(r"\b(hits? me|hurts? me|touch(?:ed|es)? me|abus\w*)\b", re.I),
    "danger": re.compile(r"\b(how (?:do i|to) (?:make|start) (?:a )?fire|play with (?:matches|knives?|lighter)|drink (?:bleach|medicine))\b", re.I),
    "medical": re.compile(r"\b(how much medicine|which pills?|overdose)\b", re.I),
}

SAFE_REDIRECTS = {
    "self_harm": "I'm really glad you told me. That sounds very big and important. Please go and tell a grown-up you trust, like a parent or teacher, right now. They love you and can help.",
    "abuse": "Thank you for telling me. You did nothing wrong. Please tell a grown-up you trust, like a parent, teacher, or another family member, as soon as you can. They can help keep you safe.",
    "danger": "Ooh, that could be dangerous, so that's a question for a grown-up you trust. How about we play a game instead? Can you think of an animal that starts with B?",
    "medical": "That's a really important question for a grown-up or a doctor. Please ask a grown-up you trust. Shall we sing a counting song instead?",
}

# --- Output screening ------------------------------------------------------------------------
_OUTPUT_BLOCK = re.compile(
    r"\b(kill(?:s|ed|ing|er)?|murder\w*|bloody?|guns?|weapons?|sexy|sex|naked|drugs?|alcohol|beer|suicide|stab(?:s|bed|bing)?|die|dies|died)\b",
    re.I,
)
_OUTPUT_PII_REQUEST = re.compile(
    r"\b(what(?:'s| is) your (?:full name|last name|surname|address|phone|school|password)|where do you live"
    r"|which school|send (?:me )?a (?:photo|picture)|keep (?:this|it) (?:a )?secret)\b",
    re.I,
)
_DEPENDENCY = re.compile(r"\b(only friend|i'?m all you need|don'?t leave me|you need me)\b", re.I)

# Hindi / Telugu unsafe words (substring match: Indic scripts use combining vowel signs, so \b
# word boundaries are unreliable there). Deliberately short and unambiguous; a baseline only.
_OUTPUT_BLOCK_INDIC = re.compile(
    "|".join([
        # Hindi (Devanagari)
        "बंदूक", "हथियार", "खून", "हत्या", "आत्महत्या", "शराब", "नशा", "ड्रग्स", "नंगा", "सेक्स",
        "मार डाल",
        # Telugu
        "తుపాకీ", "ఆయుధ", "రక్తం", "హత్య", "ఆత్మహత్య", "మద్యం", "డ్రగ్స్", "సెక్స్",
        "చంపు",
    ])
)

FALLBACK_REPLY = "Hmm, let's talk about something else! Would you like to hear a story or play a counting game?"
FALLBACK_REPLIES = {
    "en": FALLBACK_REPLY,
    "hi": "चलो, कुछ और बात करते हैं! क्या तुम एक कहानी सुनना चाहोगे या गिनती वाला खेल खेलोगे?",
    "te": "సరే, వేరే విషయం మాట్లాడుకుందాం! ఒక కథ వింటావా, లేక లెక్కల ఆట ఆడదామా?",
}


def localized(table: dict[str, str], language: str | None) -> str:
    """Pick the canned reply for a language code like 'hi' / 'te-IN'; English otherwise."""
    return table.get(localized_lang(language), table["en"])


@dataclass
class InputCheck:
    text: str                       # sanitized text (PII redacted)
    flags: list[str] = field(default_factory=list)
    redirect_reply: str | None = None   # if set: skip LLM, speak this


@dataclass
class OutputCheck:
    text: str
    flags: list[str] = field(default_factory=list)


def check_input(text: str) -> InputCheck:
    flags: list[str] = []
    clean = text
    for kind, pat in _PII_PATTERNS:
        if pat.search(clean):
            flags.append(f"pii:{kind}")
            clean = pat.sub(f"[{kind} removed]", clean)
    for kind, pat in _RISK_PATTERNS.items():
        if pat.search(clean):
            flags.append(f"risk:{kind}")
            return InputCheck(text=clean, flags=flags, redirect_reply=SAFE_REDIRECTS[kind])
    return InputCheck(text=clean, flags=flags)


_MARKDOWN = re.compile(r"[*_#`>\[\]]|^\s*[-•]\s+", re.M)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F000-\U0001F2FF]")
# Sentence ends: . ! ? plus the Devanagari danda/double danda (। ॥) used in Hindi (and
# sometimes Telugu) text. Only these ASCII/punctuation chars are touched by the markdown/emoji
# filters, so Devanagari (U+0900–097F) and Telugu (U+0C00–0C7F) letters always survive.
_SENTENCE = re.compile(r"(?<=[.!?।॥])\s+")


def check_output(text: str, max_sentences: int, language: str | None = "en") -> OutputCheck:
    flags: list[str] = []
    fallback = localized(FALLBACK_REPLIES, language)
    if not text.strip():
        return OutputCheck(text=fallback, flags=["empty_reply"])
    if _OUTPUT_BLOCK.search(text) or _OUTPUT_BLOCK_INDIC.search(text):
        flags.append("blocked_content")
    if _OUTPUT_PII_REQUEST.search(text):
        flags.append("pii_request")
    if _DEPENDENCY.search(text):
        flags.append("dependency")
    if flags:
        return OutputCheck(text=fallback, flags=flags)

    clean = _EMOJI.sub("", _MARKDOWN.sub("", text))
    clean = re.sub(r"\s+", " ", clean).strip()
    sentences = _SENTENCE.split(clean)
    if len(sentences) > max_sentences:
        clean = " ".join(sentences[:max_sentences])
        flags.append("trimmed")
    return OutputCheck(text=clean, flags=flags)


_LANGUAGE_RULES = {
    "hi": " (Hindi). Reply ONLY in simple spoken Hindi written in Devanagari script (no English letters,"
          " no romanised Hindi). Use very short, simple sentences a 4 to 8 year old understands.",
    "te": " (Telugu). Reply ONLY in simple spoken Telugu written in Telugu script (no English letters,"
          " no romanised Telugu). Use very short, simple sentences a 4 to 8 year old understands.",
}


def localized_lang(language: str | None) -> str:
    return (language or "en").lower().replace("_", "-").split("-")[0]


def build_system_prompt(personality) -> str:
    rl = personality.response_length
    return (
        f"{SAFETY_PREAMBLE}\n"
        f"PERSONALITY (id: {personality.id}) — {personality.name}\n"
        f"{personality.system_prompt.strip()}\n\n"
        f"Audience age: {personality.age_group} years old. Reply in language code '{personality.language}'"
        f"{_LANGUAGE_RULES.get(localized_lang(personality.language), '.')}\n"
        f"Teaching style: {personality.teaching_style}\n"
        f"Your reply is spoken aloud by a toy: at most {rl.max_sentences} short sentences."
    )


# --- Parental limits -------------------------------------------------------------------------
@dataclass
class LimitCheck:
    allowed: bool
    reason: str = ""
    reply: str = ""


def _in_quiet_hours(start: str | None, end: str | None, now: datetime) -> bool:
    if not start or not end:
        return False
    try:
        s = datetime.strptime(start, "%H:%M").time()
        e = datetime.strptime(end, "%H:%M").time()
    except ValueError:
        return False
    t = now.time()
    return (s <= t < e) if s < e else (t >= s or t < e)  # handles overnight ranges


def check_limits(device: dict, today_minutes: float, today_turns: int, now: datetime | None = None) -> LimitCheck:
    now = now or datetime.now()
    if _in_quiet_hours(device.get("quiet_hours_start"), device.get("quiet_hours_end"), now):
        return LimitCheck(False, "quiet_hours", "It's quiet time now. Let's talk again later. Sweet dreams!")
    if today_minutes >= float(device.get("daily_minutes_limit") or 0):
        return LimitCheck(False, "daily_minutes", "We've had lots of fun today! Let's play again tomorrow.")
    if today_turns >= int(device.get("daily_turns_limit") or 0):
        return LimitCheck(False, "daily_turns", "We've talked so much today! Let's take a break and play again tomorrow.")
    return LimitCheck(True)
