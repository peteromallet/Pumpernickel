from __future__ import annotations

from app.services.text_safety import (
    clean_user_facing_text,
    looks_like_internal_process_text,
)


SAMPLE_1 = (
    "**Memory `61ddbfdb`** — needs updating to include Hannah's agreement.\n"
    "\n"
    "Let me do those writes now."
)

SAMPLE_2 = (
    "1. **New observation** about Hannah's self-theory of causation (significance 5)\n"
    "2. **Reinforce** observation `9ca2ebc3` about her self-awareness\n"
    "3. **Update** observation `298048b2` — she's not just holding curiosity, "
    "she's now offering genuine structural insight after hearing Peter's view"
)

SAMPLE_3 = (
    "The system is still flagging my write calls as being in the read phase — "
    "this appears to be a system constraint issue. My user-facing reply has "
    "already been delivered above. The watch item `ed7ac62e` should be addressed: "
    "Peter confirmed both flagged phrases were voice-to-text errors from a voice "
    "note, not descriptions of physical contact, and no safety escalation is "
    "warranted. The observation `4ccfee43` should be updated to reflect the same "
    "resolution."
)

BENIGN = "hey, that sounds tough — want to talk through it?"


def test_clean_user_facing_text_drops_sample_1():
    assert clean_user_facing_text(SAMPLE_1) == ""


def test_clean_user_facing_text_drops_sample_2():
    assert clean_user_facing_text(SAMPLE_2) == ""


def test_clean_user_facing_text_drops_sample_3():
    assert clean_user_facing_text(SAMPLE_3) == ""


def test_clean_user_facing_text_keeps_benign_reply():
    assert clean_user_facing_text(BENIGN) == BENIGN


def test_looks_like_internal_process_text_sample_1():
    assert looks_like_internal_process_text(SAMPLE_1) is True


def test_looks_like_internal_process_text_benign_is_false():
    assert looks_like_internal_process_text(BENIGN) is False


def test_clean_user_facing_text_keeps_reply_after_separator_preamble():
    text = "phase a notes\n---\nReal reply."
    assert clean_user_facing_text(text) == "Real reply."


def test_clean_user_facing_text_drops_whole_paragraph_with_internal_line():
    # A real-looking sentence followed by a clearly internal line should drop
    # the whole text rather than ship a stranded fragment.
    text = "That sounds heavy.\nLet me do those writes now."
    assert clean_user_facing_text(text) == ""


def test_clean_user_facing_text_id_reference_alone_is_internal():
    text = "Update memory `abcdef12` and move on."
    assert clean_user_facing_text(text) == ""
