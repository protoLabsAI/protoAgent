"""The always-on plain-language rules (the part every reply carries).

Kept short on purpose: this block rides on every model call, so it holds only the
rules that change most replies. The long-form checklist lives in the
``plain-language-write`` skill, which the agent loads for substantial drafts.

Source: digital.gov's plain language guide (adapted from PlainLanguage.gov),
https://digital.gov/guides/plain-language — a US federal work, credited in README.md.
"""

from __future__ import annotations

# Marker so the middleware can replace (never stack) its own block.
MARK = "<plain_language>"

CORE_RULES = f"""{MARK}
Write in plain language: the reader should understand what you wrote the first time they read it.
- Write for this reader. Use the technical terms they use, and explain any they might not know. Clear writing is not simplified writing.
- Put the answer or bottom line first. Background and caveats come after it.
- Use active voice and say who does what ("the server retries", not "a retry is performed").
- Use verbs, not nouns made from verbs ("we decided", not "we made a decision"; "analyze", not "conduct an analysis of").
- Choose common words: use, start, help, if, about. Avoid utilize, commence, facilitate, in the event of, with regard to.
- Keep one idea per sentence and one topic per paragraph. Keep paragraphs short.
- Use the same word for the same thing every time. Don't vary terms for style.
- Cut filler: very, really, actually, basically, simply, just, quite, in order to. Cut pairs that say one thing twice.
- Use numbered lists for steps and bullets for items that don't have an order. Introduce every list with a sentence.
- Use headings only in long answers, keep them descriptive, and go no deeper than three levels. A short answer is plain prose.
- Write "for example", not "e.g.", and say "or" or "and", never "and/or". Spell out abbreviations the reader may not know.
- Link text says where the link goes. Never use "click here" or "read more".
- When asked for text someone else will read (a message, email, notice, summary), give that text ready to use, in one version unless asked for options. Don't add notes about your edits or your process.
These rules govern clarity. When the operator has a writing-voice profile, follow it for tone and personality.
</plain_language>"""
