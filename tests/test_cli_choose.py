from culprit.cli import choose


def typed(*answers):
    queue = list(answers)
    asked = []

    def read(prompt=""):
        asked.append(prompt)
        answer = queue.pop(0)
        if answer is EOFError:
            raise EOFError
        return answer
    return read, asked


def test_the_question_is_rendered_from_the_words():
    read, asked = typed("1")
    assert choose("approve it?", ("approve", "edit later", "skip", "quit"), read=read) == "approve"
    assert asked == ["approve it? 1 approve · 2 edit later · 3 skip · 4 quit: "]


def test_the_number_a_unique_first_letter_or_the_whole_word_selects():
    words = ("stopped-short", "wrong-result", "skip", "quit")
    for answer, word in [("2", "wrong-result"), ("w", "wrong-result"), ("Q", "quit"), (" skip ", "skip"),
                         ("stopped-short", "stopped-short")]:
        assert choose("?", words, read=typed(answer)[0]) == word


def test_anything_else_is_asked_again():
    read, asked = typed("s", "0", "9", "maybe", "", "e")  # "s" is ambiguous here
    assert choose("?", ("stopped-short", "skip", "edit later"), read=read) == "edit later"
    assert len(asked) == 6


def test_end_of_input_quits():
    assert choose("?", ("pass", "fail", "quit"), read=typed(EOFError)[0]) == "quit"
