"""
The Bridge's confirmation screen is tap-to-copy too.

WHAT HAPPENED (live, 29 September 2026)

    Peter, its doing the copy and paste problem again

Third time. The first fix was /send's instruction on 10 September; the
second was the deposit notification on 23 September. Both were "the message
he acts on". This is the third such message and it had never been done: the
confirmation preview, shown before he presses Send to client, carrying the
USDT figure and every account number he is about to issue.

    render_send_instruction(...)          # html defaults to False
    *[render_payment_slot(s) for s in slot_objs]
    ...
    await message.answer(preview, reply_markup=kb)   # no parse_mode

confirm_final, forty lines below, passes html=True on both and sends with
parse_mode. So the CLIENT received tap-to-copy figures and the Bridge's own
screen was plain text.

THE COMMENT THAT WAS ALREADY THERE

    Built from the same two renderers the real messages use, so the preview
    cannot drift away from what actually gets sent.

It had drifted anyway. Sharing a renderer guarantees nothing when the two
call sites pass different arguments to it — which is why these tests compare
the preview's arguments against the send's rather than trusting that they
use the same function.

WHY EACH FIX HAS BEEN A SEPARATE MESSAGE

Nothing enforces "a figure the Bridge copies is copyable"; it has been
applied one message at a time, each after a complaint. The last test here
sweeps the module for any other send that renders a slot or an instruction
without html, so the fourth one is found before he finds it.
"""

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import bridge_trade  # noqa: E402


def _preview_src() -> str:
    """The block that builds and sends the confirmation screen."""
    src = inspect.getsource(bridge_trade)
    start = src.index("allocated = sum(")
    end = src.index("@router.callback_query(Confirm.final", start)
    return src[start:end]


class TestThePreviewCarriesTheMarkup:
    def test_the_usdt_figure_is_rendered_for_copying(self):
        block = _preview_src()
        instruction = block[block.index("render_send_instruction("):]
        instruction = instruction[:instruction.index(")")]
        assert "html=True" in instruction

    def test_every_account_number_is_rendered_for_copying(self):
        """
        The slots carry the account numbers he is about to send, and a
        mistyped digit there is the most expensive typo in the system.
        """
        block = _preview_src()
        assert "render_payment_slot(s, html=True)" in block

    def test_the_message_is_sent_as_html(self):
        """
        The other half. Markup without parse_mode prints the tags on screen;
        parse_mode without markup looks perfect and simply will not copy.
        """
        block = _preview_src()
        sends = [ln for ln in block.splitlines()
                 if "message.answer(preview" in ln or "message.edit_text(preview" in ln]
        assert len(sends) == 2, sends
        for ln in sends:
            assert 'parse_mode="HTML"' in ln, ln

    def test_the_edit_path_and_the_send_path_agree(self):
        """
        The preview is re-rendered on edit when he changes a slot. One path
        with markup and one without would make it copyable only the first
        time, which is worse than never.
        """
        block = _preview_src()
        edit = next(ln for ln in block.splitlines() if "edit_text(preview" in ln)
        answer = next(ln for ln in block.splitlines() if "answer(preview" in ln)
        assert ("parse_mode" in edit) == ("parse_mode" in answer)


class TestTurningOnHtmlCannotBreakIt:
    def test_the_client_label_between_the_renderers_is_escaped(self):
        """
        The line joining the two renderers is written here, not by them, and
        interpolates the client's name — which comes from a Telegram group
        title. An unescaped & would make Telegram reject the confirmation
        screen outright, mid-issue.
        """
        block = _preview_src()
        joiner = next(ln for ln in block.splitlines()
                      if "separate " in ln or "as {len(slot_objs)}" in ln)
        assert "esc(" in joiner, joiner

    def test_esc_is_actually_imported(self):
        """
        It was not. The first version of this fix called esc() in a module
        that had never imported it, which would have raised NameError on the
        confirmation screen — turning a copy-paste annoyance into an
        unusable /issue.
        """
        src = inspect.getsource(bridge_trade)
        assert "from html import escape as esc" in src
        assert callable(bridge_trade.esc)


class TestNoOtherMessageIsLeftPlain:
    def test_every_slot_or_instruction_render_passes_html(self):
        """
        The sweep. Three separate complaints, three separate messages, each
        fixed only after someone noticed. This fails on the fourth before he
        does.

        A render that is NOT sent — building a string for a file, a log or a
        test — would be a false positive, and there are none today. If one
        appears, exempt it by name rather than deleting the check.
        """
        src = inspect.getsource(bridge_trade)
        offenders = []
        for m in re.finditer(r"render_(?:payment_slot|send_instruction)\(", src):
            # Walk to the matching close paren. A regex cannot do this: these
            # calls contain nested calls and subscripts, and [^)]* stops at
            # the first inner ")" — which reported a correct call site as an
            # offender the first time this was written.
            depth, i = 0, m.end() - 1
            while i < len(src):
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            call = src[m.start():i + 1]
            if "html=True" not in call:
                line_no = src[:m.start()].count("\n") + 1
                offenders.append(f"line {line_no}: {call.split(chr(10))[0]}")
        assert not offenders, (
            "rendered without html=True, so not tap-to-copy:\n  "
            + "\n  ".join(offenders)
        )
