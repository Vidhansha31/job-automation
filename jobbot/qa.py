"""Answers for application-form questions (pure functions, no browser).

Only facts you gave (config `answers`) or rules you wrote (`apply.qa_rules`) are ever typed
into a form. A required question with no rule is NOT guessed: the job goes to manual apply.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
import difflib
import json
from pathlib import Path
from typing import Optional

_PLACEHOLDER = re.compile(r"^(-+\s*)?(please\s+)?(select|choose|pick)\b.*$|^-+$|^$", re.I)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*✱]", " ", s or "")).strip().lower()


def similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 25 and short in long_:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.88


def load_bank(path) -> tuple[list, set]:
    """profile/my_answers.yaml -> ([(normalised question, answer)], all normalised questions)."""
    import yaml
    p = Path(path) if path else None
    if not p or not p.exists():
        return [], set()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - a typo in your file must not stop applying
        return [], set()
    if not isinstance(data, dict):
        return [], set()
    allq = {norm(str(q)) for q in data}
    return [(norm(str(q)), str(a).strip()) for q, a in data.items() if a is not None and str(a).strip()], allq


@dataclass
class Ctx:
    answers: dict
    years: float
    rules: list = field(default_factory=list)      # [(regex, answer)]; yours are checked first
    bank: list = field(default_factory=list)       # [(question, answer)] from my_answers.yaml
    bank_path: object = None
    known: set = field(default_factory=set)
    unknown: list = field(default_factory=list)    # [(label, options)] seen without an answer

    def a(self, key: str) -> str:
        return str(self.answers.get(key, "") or "").strip()

    def note(self, label: str, options=()) -> None:
        n = norm(label)
        if n and n not in self.known and all(norm(u[0]) != n for u in self.unknown):
            self.unknown.append((label.strip(), [o for o in real_options(options)][:12]))

    def flush_unknown(self) -> int:
        """Append questions that had no answer to my_answers.yaml (blank, for you to fill in)."""
        if not self.unknown or not self.bank_path:
            self.unknown = []
            return 0
        p, n = Path(self.bank_path), len(self.unknown)
        p.parent.mkdir(parents=True, exist_ok=True)
        text = p.read_text(encoding="utf-8") if p.exists() else BANK_HEADER
        if not text.endswith("\n"):
            text += "\n"
        for label, opts in self.unknown:
            text += (f"# options: {' | '.join(opts)}\n" if opts else "") + f"{json.dumps(label, ensure_ascii=False)}: \"\"\n"
            self.known.add(norm(label))
        p.write_text(text, encoding="utf-8")
        self.unknown = []
        return n


BANK_HEADER = """# Your answers to application questions.
# New questions the bot could not answer are added below with an empty answer ("").
# Fill the answer between the quotes and save. For multiple-choice questions write the answer
# so it matches one of the options (e.g. "Yes" / "No"). Questions with an empty answer are ignored.
# Wording does not have to be exact - similar questions on other sites match too.
"""


def real_options(options) -> list:
    return [o for o in options if not _PLACEHOLDER.match(norm(o))]


def pick_option(answer: str, options) -> Optional[str]:
    a = norm(answer)
    opts = [(o, norm(o)) for o in real_options(options)]
    for o, n in opts:
        if n == a:
            return o
    for o, n in opts:
        if len(n) >= 3 and re.search(r"\b" + re.escape(n) + r"\b", a):
            return o
    for o, n in opts:
        if len(a) >= 3 and re.search(r"\b" + re.escape(a) + r"\b", n):
            return o
    return None


def notice_option(options) -> Optional[str]:
    rx = (r"immediate|7 days|one week|1 week|less than (7|15|30)|< ?(15|30)|0 ?[-–to]+ ?(7|15|30)|"
          r"(up ?to|within|upto) (7|15|30)")
    for o in real_options(options):
        if re.search(rx, norm(o)):
            return o
    return None


def choose_option(label: str, answer: Optional[str], options) -> Optional[str]:
    o = pick_option(answer, options) if answer else None
    if o is None and "notice" in norm(label):
        o = notice_option(options)
    return o


def _lpa(raw: str) -> Optional[float]:
    m = re.search(r"\d+(?:\.\d+)?", raw or "")
    return float(m.group()) if m else None


def _ctc(raw: str, label: str, ftype: str) -> Optional[str]:
    if not raw:
        return None
    if ftype != "number":
        return raw
    v = _lpa(raw)
    if v is None:
        return None
    if re.search(r"lpa|lakh|lac", label):
        return f"{v:g}"
    if re.search(r"inr|rupee|₹|annual|yearly|per annum|\bpa\b", label):
        return str(int(v * 100000))
    return None                                   # ambiguous number field: do not guess


def _years(ctx: Ctx, ftype: str) -> str:
    v = math.floor(ctx.years * 2) / 2             # 2.6 -> 2.5 (never rounds up)
    return str(int(v)) if ftype == "number" else f"{v:g}"


def resolve(label: str, ftype: str, ctx: Ctx) -> Optional[str]:
    l = norm(label)
    if not l:
        return None
    for rx, ans in ctx.rules:
        if re.search(rx, l, re.I):
            return ans
    for q, ans in ctx.bank:
        if similar(l, q):
            return ans
    if re.search(r"relocat", l):
        return "Yes"                                   # your standing answer: open to relocate anywhere in India
    if re.search(r"sponsor", l):
        return "No"                                    # your standing answer: no sponsorship needed
    parts = ctx.a("Full Name").split()
    first, last = (parts[0] if parts else ""), " ".join(parts[1:])
    if re.search(r"first name|given name|forename", l):
        return first or None
    if re.search(r"last name|surname|family name", l):
        return last or None
    if re.search(r"full name|your name|legal name|candidate name|^name$", l):
        return ctx.a("Full Name") or None
    if re.search(r"e-?mail", l):
        return ctx.a("Email") or None
    if re.search(r"phone|mobile|contact number|whatsapp|telephone", l):
        return ctx.a("Phone") or None
    if re.search(r"linked ?in", l):
        return ctx.a("LinkedIn") or None
    if "notice" in l:
        return ctx.a("Notice Period") or None
    cur = re.search(r"(current|present|existing|last drawn).{0,25}(ctc|salary|compensation|package|lpa|pay)"
                    r"|ctc.{0,15}current", l)
    exp = re.search(r"(expected|desired|expectation|target).{0,25}(ctc|salary|compensation|package|lpa|pay)"
                    r"|(salary|ctc|compensation).{0,15}expect", l)
    if cur and exp:
        return None if ftype == "number" else f"Current {ctx.a('Current CTC')}, Expected {ctx.a('Expected CTC')}"
    if cur:
        return _ctc(ctx.a("Current CTC"), l, ftype)
    if exp:
        return _ctc(ctx.a("Expected CTC"), l, ftype)
    if "experience" in l and re.search(r"years?|total|overall|professional|^experience", l) and not re.search(
            r"experience (with|in(?! years)|using|on|of (?!work))|(with|using) .*experience", l):
        return _years(ctx, ftype)
    if re.search(r"^(current |present )?(company|employer|organi[sz]ation)( name)?$|"
                 r"current (company|employer|organi[sz]ation)", l):
        return ctx.a("Current Company") or None
    if re.search(r"current (job )?(title|designation|role|position)|^(job )?title$|^designation$", l):
        return ctx.a("Current Title") or None
    if re.search(r"^(current |present )?(location|city)( \(city\))?$|current (location|city)|"
                 r"where are you (currently )?(based|located)|your (current )?location|current residence", l):
        return ctx.a("Current Location") or None
    if re.search(r"how did you (hear|find|learn)|where did you (hear|find)", l):
        return ctx.a("How did you hear about us") or None
    return None
