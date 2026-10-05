"""Typed questions for System One decision models: links per paragraph, coding per paper.

Ported from POTS-phenotyping (scripts/judge_paragraphs.py), with the questions
read from config instead of hard-coded: the term groups, each predicate's
domain, range, answer option and statement template come from
config/relations.yaml, and the per-paper questions from config/study_coding.yaml.

No generative model is involved. A decision model answers two kinds of
question about one passage (`paragraph`):

    yes/no  ("noul")   one probability
    choice             a probability per option; the options always include
                       "none", and a choice is asked in rounds -- the top answer
                       is kept and removed, and the question asked again until
                       "none" comes out on top -- so one question can yield
                       several answers.

Links, per paragraph, four gated stages:

    1 terms       "Which of these <group> does `paragraph` mention?" for each
                  group a predicate can link. Gate: two or more terms.
    2 relational  yes/no: does it state a relationship between those terms?
    3 relation    which predicate(s), from relations.yaml `option`
    4 statement   for each predicate: which ordered pairs its domain and range
                  allow does the paragraph state, phrased by `statement`

Every round is stored with its probabilities (never the text), so a stricter
threshold is applied offline (cascade(..., gate=0.7)), not by re-running.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Vocabulary

NONE = "none"
GATE = 0.5

# ------------------------------------------------------------------ vocabulary


def terms(vocab: Vocabulary) -> dict[str, dict]:
    """facet id -> {group, label, gloss, option} for every facet a question may name."""
    out = {}
    for f in vocab.facet_list:
        first = re.split(r"(?<=[a-z0-9)])[.:;](?:\s|$)", " ".join(f["definition"].split()))[0]
        gloss = " ".join(first.split()[:18])
        short = " ".join(gloss.split()[:8])
        out[f["id"]] = {"group": f["group"], "label": f["label"], "gloss": gloss,
                        "option": f"{f['label']} ({short})" if short else f["label"]}
    return out


def link_predicates(vocab: Vocabulary) -> dict[str, dict]:
    """Predicates the decision models are asked about: every one with a domain and range."""
    return {p: spec for p, spec in vocab.predicates.items() if spec.get("domain") and spec.get("range")}


def link_groups(vocab: Vocabulary) -> list[str]:
    """Facet groups asked in stage 1: those some predicate can link, in facets.yaml order."""
    used = {g for spec in link_predicates(vocab).values() for g in spec["domain"] + spec["range"]}
    return [g for g in vocab.groups if g in used]


def group_name(vocab: Vocabulary, group: str) -> str:
    return vocab.groups[group]["label"].lower() + "s"


def pairs(predicate: str, present: list[str], vocab: Vocabulary, tv: dict[str, dict]) -> list[tuple[str, str]]:
    """Ordered pairs of present terms that the predicate's domain and range allow."""
    spec = vocab.predicates[predicate]
    out: list[tuple[str, str]] = []
    for a in present:
        for b in present:
            if a == b or tv[a]["group"] not in spec["domain"] or tv[b]["group"] not in spec["range"]:
                continue
            if spec.get("symmetric") and (b, a) in out:
                continue
            out.append((a, b))
    return out


def canon(s: str, p: str, o: str, vocab: Vocabulary) -> tuple[str, str, str]:
    """Symmetric predicates compare as unordered pairs."""
    return (min(s, o), p, max(s, o)) if vocab.predicates.get(p, {}).get("symmetric") else (s, p, o)


def questions_sha(vocab: Vocabulary, coding: dict | None = None) -> str:
    """Hash of every question a run asks, so runs with different wording never mix."""
    tv = terms(vocab)
    spec = {p: [s["domain"], s["range"], s["option"], s["statement"]] for p, s in link_predicates(vocab).items()}
    blob = json.dumps([{k: v["option"] for k, v in tv.items()}, spec, coding or {}], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --------------------------------------------------------------------- backend

RETRY_CODES = (429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529)


def _secret(env: str, keychain: str) -> str:
    """An API key from the environment, else the macOS Keychain. Never stored in the repo."""
    key = os.environ.get(env, "").strip()
    if not key and sys.platform == "darwin":
        key = subprocess.run(["security", "find-generic-password", "-s", keychain, "-w"],
                             capture_output=True, text=True).stdout.strip()
    if not key:
        raise SystemExit(f"no API key: set {env}" + (f" or add Keychain item `{keychain}`" if sys.platform == "darwin" else ""))
    return key


@dataclass
class Backend:
    """Where /decide requests go. All four servers take the same request body."""

    name: str
    url: str
    headers: dict[str, str] = field(default_factory=lambda: {"Content-Type": "application/json"})
    max_options: int = 60          # winnow:e4b's budget is about 64, "none" included
    hosted: bool = False           # a third party sees the text: only CC BY may be sent
    answered_by: set = field(default_factory=set)
    input_tokens: int = 0
    transport: Callable[[str, bytes, dict], dict] | None = None   # tests replace the network

    def post(self, model: str, text: str, questions: dict) -> tuple[dict, bool]:
        body = json.dumps({"model": model, "state": {"paragraph": text}, "questions": questions}).encode()
        if self.transport is not None:
            res = self.transport(self.url, body, self.headers)
        else:
            res = self._send(body)
        self.answered_by.add(res.get("model"))
        self.input_tokens += (res.get("usage") or {}).get("input_tokens") or 0
        return res["answers"], bool(res.get("state_truncated"))

    def _send(self, body: bytes) -> dict:
        for attempt in range(8):
            req = urllib.request.Request(self.url, body, self.headers)
            try:
                return json.loads(urllib.request.urlopen(req, timeout=600).read())
            except urllib.error.HTTPError as e:
                if e.code not in RETRY_CODES or attempt == 7:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == 7:
                    raise
            time.sleep(min(60, 2 ** attempt))   # rate limits, overload, transient network
        raise RuntimeError("unreachable")


def backend(name: str) -> Backend:
    """ollaya (local), typesafe (hosted Jev), llamacpp (local llama-server), bioinfolder (lab llama-swap)."""
    if name == "ollaya":
        return Backend("ollaya", os.environ.get("OLLAYA_URL", "http://127.0.0.1:11435/api/decide"))
    if name == "llamacpp":
        return Backend("llamacpp", os.environ.get("LLAMACPP_URL", "http://127.0.0.1:8081/v1/systemone"))
    if name == "bioinfolder":
        key = _secret("BIOINFOLDER_KEY", "bioinfolder-llm")
        return Backend("bioinfolder", "https://llm.bioinfolder.com/v1/systemone",
                       {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    if name == "typesafe":
        key = _secret("TYPESAFE_API_KEY", "typesafe-api-key")
        return Backend("typesafe", "https://api.typesafe.ai/v1/systemone",
                       {"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                       max_options=255, hosted=True)
    raise ValueError(f"unknown backend {name!r}")


# ------------------------------------------------------------------- questions


def rounds(be: Backend, model: str, text: str, instruction: str, options: dict[str, str],
           none: str, single: bool = False) -> tuple[list[dict], bool]:
    """Ask one choice question repeatedly, removing each answer, until "none" is on top.

    Options beyond the backend's budget are asked in chunks, each with its own
    "none". single=True asks once per chunk (a one-answer question). Returns
    one record per round: the pick, its probability, P(none) and the top 5.
    """
    out, truncated = [], False
    keys = list(options)
    for c in range(0, len(keys), be.max_options - 1):
        left = {k: options[k] for k in keys[c:c + be.max_options - 1]}
        while left:
            ans, tr = be.post(model, text, {"q": {"type": "choice", "instructions": instruction,
                                                  "criteria": left | {NONE: none}}})
            truncated |= tr
            probs = {k: round(v, 4) for k, v in ans["q"]["probabilities"].items()}
            top = max(probs, key=lambda k: (probs[k], k == NONE))
            out.append({"pick": top, "p": probs[top], "p_none": probs.get(NONE, 0.0),
                        "top5": dict(sorted(probs.items(), key=lambda kv: -kv[1])[:5])})
            if top == NONE or single:
                break
            del left[top]
    return out, truncated


def yes_no(be: Backend, model: str, text: str, question: str) -> float:
    ans, _ = be.post(model, text, {"r": {"type": "noul", "instructions": question}})
    return round(ans["r"]["noul"], 4)


def chosen(rs: list[dict], gate: float | None = None) -> list[str]:
    """Answers picked in a run of choice rounds, optionally only those with p >= gate."""
    return [r["pick"] for r in rs if r["pick"] != NONE and (gate is None or r["p"] >= gate)]


def judge(be: Backend, model: str, para: dict, vocab: Vocabulary, gate: float = GATE,
          one_term_question: bool = False) -> dict:
    """The four link stages for one paragraph. Returns the stored record (no text)."""
    tv = terms(vocab)
    text = para["normalized_text"]
    rec: dict[str, Any] = {"id": para["id"], "sha": para["sha"], "terms": {}, "truncated": False}
    present: list[str] = []
    groups = link_groups(vocab)
    asked = {"all": groups} if one_term_question else {g: [g] for g in groups}
    for name, members in asked.items():
        opts = {t: v["option"] for t, v in tv.items() if v["group"] in members}
        what = "terms" if one_term_question else group_name(vocab, name)
        rs, tr = rounds(be, model, text, f"Which of these {what} does `paragraph` mention?", opts,
                        f"none of these {what}, or only another one")
        rec["terms"][name] = rs
        rec["truncated"] |= tr
        present += chosen(rs)
    if len(present) < 2:
        return rec
    listed = "; ".join(tv[t]["label"] for t in present)
    rec["relational"] = yes_no(be, model, text, f"Does `paragraph` state a relationship between any two of these: {listed}?")
    if rec["relational"] < gate:
        return rec
    preds = link_predicates(vocab)
    rs, _ = rounds(be, model, text, f"Which relation does `paragraph` state between any of these: {listed}?",
                   {p: s["option"] for p, s in preds.items()}, "no relation between these terms, or another relation")
    rec["relations"] = rs
    rec["links"] = {}
    for p in chosen(rs):
        opts = {f"{a}|{p}|{b}": preds[p]["statement"].format(a=tv[a]["label"], b=tv[b]["label"])
                for a, b in pairs(p, present, vocab, tv)}
        if opts:
            rec["links"][p], _ = rounds(be, model, text, "Which of these does `paragraph` state?", opts,
                                        "none of these statements")
    return rec


def cascade(rec: dict, vocab: Vocabulary, gate: float | None = None) -> set[tuple[str, str, str]]:
    """Links one stored paragraph record yields; gate keeps only picks with p >= gate."""
    present = {t for rs in rec["terms"].values() for t in chosen(rs, gate)}
    if len(present) < 2 or rec.get("relational", 0) < GATE:
        return set()
    preds = set(chosen(rec.get("relations") or [], gate))
    out = set()
    for p, rs in (rec.get("links") or {}).items():
        if p not in preds:
            continue
        for key in chosen(rs, gate):
            s, _, o = key.split("|")
            if s in present and o in present:
                out.add(canon(s, p, o, vocab))
    return out


# --------------------------------------------------------------- study coding


def passages(paper: dict, read: list[str], limit: int) -> list[dict]:
    """The paragraphs a coding field reads, in reading order, at most `limit` per kind."""
    out: list[dict] = []
    for kind in read:
        out += [p for p in paper["paragraphs"] if p["kind"] == kind][:limit]
    return out


def field_options(spec: dict, tv: dict[str, dict]) -> dict[str, str]:
    if "options_from_group" in spec:
        return {t: v["option"] for t, v in tv.items() if v["group"] == spec["options_from_group"]}
    return dict(spec["options"])


def code_paper(be: Backend, model: str, paper: dict, coding: dict, vocab: Vocabulary, limit: int = 12) -> dict:
    """Ask every study_coding.yaml field of the passages it reads. Stored per passage, no text."""
    tv = terms(vocab)
    rec: dict[str, Any] = {"paper": paper["paper"], "source": paper.get("source"), "fields": {}}
    for name, spec in coding["fields"].items():
        answers = []
        for para in passages(paper, spec["read"], limit):
            text = para["normalized_text"]
            if spec["type"] == "yes_no":
                answers.append({"id": para["id"], "p": yes_no(be, model, text, spec["question"])})
                continue
            rs, _ = rounds(be, model, text, spec["question"], field_options(spec, tv), spec["none"],
                           single=spec["type"] == "one_of")
            answers.append({"id": para["id"], "rounds": rs})
        rec["fields"][name] = answers
    return rec


def coding_answer(spec: dict, answers: list[dict], gate: float = GATE) -> Any:
    """A paper's answer to one field, from its stored per-passage answers."""
    if spec["type"] == "yes_no":
        return None if not answers else max(a["p"] for a in answers) >= gate
    if spec["type"] == "one_of":
        best = max((r for a in answers for r in a["rounds"] if r["pick"] != NONE),
                   key=lambda r: r["p"], default=None)
        return best["pick"] if best else NONE
    picked = {t for a in answers for t in chosen(a["rounds"], None)}
    return sorted(picked)


def evidence_for(spec: dict, answers: list[dict], answer: Any) -> str | None:
    """The paragraph id that carried the answer, kept as the paper's verbatim evidence."""
    if spec["type"] == "yes_no":
        return max(answers, key=lambda a: a["p"])["id"] if answers else None
    if spec["type"] == "one_of" and answer != NONE:
        return max(((a["id"], r["p"]) for a in answers for r in a["rounds"] if r["pick"] == answer),
                   key=lambda x: x[1])[0]
    return None
