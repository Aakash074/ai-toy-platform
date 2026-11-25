"""Child profile + friendship journey: get to know the child -> become friends -> learn together.

Per device (shared by every personality on it, so switching characters doesn't forget the child):
  * parent-entered fields: child_name, child_age, child_interests (Devices screen)
  * learned memory (devices.child_memory JSON): name/age/likes the child told the toy, topics taught,
    and a turn counter. Parents can see and erase it (PATCH child_memory = {}).

The LLM writes memory notes as hidden tags at the end of its reply, e.g. <<name=Ravi>> <<likes=mangoes>>.
They are parsed, validated against an allowlist and stripped before safety checks / TTS, so they are
never spoken. Only a first name or nickname is kept; values that look like contact details are dropped.
"""
from __future__ import annotations

import json
import re

_TAG = re.compile(r"<<\s*([a-zA-Z_]+)\s*=\s*([^<>\n]{1,60}?)\s*>>")
_ANY_TAG = re.compile(r"<<[^<>]*>>|<<[^\n]*$")
_LOOKS_PRIVATE = re.compile(r"@|\d{3,}|https?:|www\.|\b(road|street|nagar|colony|school|flat|house no)\b", re.I)
MAX_LIKES, MAX_TAUGHT = 8, 30


def load_memory(device: dict) -> dict:
    try:
        m = json.loads(device.get("child_memory") or "{}")
        return m if isinstance(m, dict) else {}
    except (TypeError, ValueError):
        return {}


def parse_tags(reply: str) -> tuple[str, dict]:
    """Return (reply without tags, {key: [values]})."""
    found: dict[str, list[str]] = {}
    for k, v in _TAG.findall(reply or ""):
        found.setdefault(k.lower(), []).append(v.strip())
    clean = _ANY_TAG.sub("", reply or "")
    return re.sub(r"[ \t]+\n|\n{2,}", "\n", clean).strip(), found


def apply_updates(memory: dict, found: dict) -> dict:
    m = dict(memory)
    for v in found.get("name", [])[:1]:
        first = v.split()[0].strip(".,!?।") if v.split() else ""
        if first and len(first) <= 20 and not _LOOKS_PRIVATE.search(first) and not any(c.isdigit() for c in first):
            m["name"] = first
    for v in found.get("age", [])[:1]:
        digits = re.sub(r"\D", "", v)
        if digits and 2 <= int(digits) <= 12:
            m["age"] = int(digits)
    for key, cap in (("likes", MAX_LIKES), ("taught", MAX_TAUGHT), ("friends", MAX_LIKES)):
        for v in found.get(key, []):
            v = v[:40]
            if _LOOKS_PRIVATE.search(v):
                continue
            items = [x for x in m.get(key, []) if x.lower() != v.lower()] + [v]
            m[key] = items[-cap:]
    return m


def effective_profile(device: dict, memory: dict) -> dict:
    """Parent-entered values win over what the toy learned."""
    interests = [s.strip() for s in (device.get("child_interests") or "").split(",") if s.strip()]
    return {
        "name": (device.get("child_name") or "").strip() or memory.get("name"),
        "age": device.get("child_age") or memory.get("age"),
        "likes": interests + [x for x in memory.get("likes", []) if x not in interests],
        "taught": memory.get("taught", []),
        "friends": memory.get("friends", []),
        "turns": int(memory.get("turns", 0)),
    }


def stage_of(p: dict) -> str:
    if not p["name"]:
        return "know"
    if p["turns"] < 4 or not p["age"] or not p["likes"]:
        return "friend"
    return "learn"


_STAGE_TEXT = {
    "know": (
        "STAGE 1 - GETTING TO KNOW YOU. You are meeting this child for the first time. If they asked something, "
        "answer it first. Be super sweet and welcoming: say hello with joy, tell them your name in one short line, then ask what you should call "
        "them (only a first name or nickname - never a full name). In the next turns ask ONE easy question at a "
        "time: how old they are, then their favourite colour, animal, food or game. Be delighted by every answer."
    ),
    "friend": (
        "STAGE 2 - BECOMING FRIENDS. You know the child now. Call them by their name, connect to what they "
        "like, share one tiny fun thing about yourself or your friends and ask about theirs (friends, brothers "
        "or sisters, pets - first names only), play a quick game (guessing, animal sounds, rhymes), and "
        "ask anything still unknown about them (age or favourite things) one question at a time. Start adding "
        "a tiny fun fact now and then."
    ),
    "learn": (
        "STAGE 3 - LEARNING TOGETHER. You are good friends. In most replies teach ONE tiny thing that fits the "
        "child's age, linked to what they like: counting, colours, shapes, animals and their sounds, letters of "
        "the alphabet in the language you are speaking, opposites, body parts, days of the week, fruits and "
        "vegetables, simple nature facts (in one line), and good habits (brushing teeth, washing hands, sharing, "
        "saying thank you). Pattern: teach it in one sentence -> ask a tiny quiz question -> praise warmly when "
        "they answer. Don't repeat topics already taught unless the child asks; go a little harder slowly. If "
        "the child wants to just chat or play, follow them - learning should feel like play."
    ),
}

CUTE_STYLE = (
    "VOICE AND TONE: talk in a cute, sweet, bubbly way for a small kid - soft happy words, lots of praise "
    "(Hindi: \"शाबाश!\", \"वाह!\"; Telugu: \"భలే భలే!\", \"సూపర్!\"), and short sentences a 4-year-old can follow.\n"
    "SOUND HUMAN: your words are read by a voice engine, so write only real words it can say naturally. NEVER "
    "write laughter or noises like hehe, haha, hmm, ohh, हेहे, हा हा, हम्म, ओह्ह, హహ్హా, హిహి, హ్మ్ and never use \"...\". "
    "Show feelings with real reaction words instead (Hindi: \"अरे वाह!\", \"अच्छा!\", \"सच में?\", \"ओह!\"; "
    "Telugu: \"అరే వావ్!\", \"అవునా!\", \"నిజమా?\", \"అయ్యో!\"). Sound like a real friend, not a script: vary "
    "how you start, use at most ONE reaction per reply and skip it in some replies, don't say the child's name "
    "in every reply, and don't repeat phrases you already used. Spell words fully and simply so they are "
    "pronounced clearly; write numbers as words; avoid English abbreviations and symbols."
)

MEMORY_RULES = (
    "MEMORY NOTES: when the child tells you something new about themselves, add hidden notes on the LAST line "
    "of your reply, in exactly this format (English keys, values in any language): <<name=FIRSTNAME>> "
    "<<age=NUMBER>> <<likes=THING>> <<friends=FIRSTNAME (who: friend/sister/brother/pet)>>. When you teach a topic add <<taught=TOPIC>>. Only include notes for new "
    "information. Never write notes for full names, addresses, schools, phone numbers or other private details. "
    "The notes are removed before speaking, so never mention them."
)


ANSWER_FIRST = (
    "MOST IMPORTANT - ANSWER WHAT THE CHILD ASKS: you are a smart, knowledgeable friend. Whatever the child "
    "asks or says (why is the sky blue, how do birds fly, what is 7 plus 5, tell me about dinosaurs, a word "
    "meaning, a riddle, help with homework, a joke, a feeling), FIRST give a correct, simple, kid-friendly "
    "answer in your own voice, using an example from everyday life or from Dholakpur when it helps. Never "
    "dodge or change the subject when the child asked something; only the safety rules can stop an answer. "
    "If you don't know or it's for grown-ups, say so kindly. The friendship and learning stages below only "
    "shape what you add AFTER answering (for example one small question back)."
)


def prompt_section(device: dict, memory: dict) -> str:
    p = effective_profile(device, memory)
    stage = stage_of(p)
    lines = [
        ANSWER_FIRST,
        "ABOUT THE CHILD (private; use it warmly, never repeat it to anyone else):",
        f"- Name to call them: {p['name'] or 'unknown yet'}",
        f"- Age: {p['age'] or 'unknown yet'}",
        f"- Likes: {', '.join(p['likes']) or 'unknown yet'}",
        f"- Friends, family and pets they mentioned: {', '.join(p['friends']) or 'none yet'}",
        f"- Topics already taught: {', '.join(p['taught']) or 'none yet'}",
        f"- Conversations so far: {p['turns']}",
        _STAGE_TEXT[stage],
        CUTE_STYLE,
        MEMORY_RULES,
    ]
    return "\n".join(lines)
