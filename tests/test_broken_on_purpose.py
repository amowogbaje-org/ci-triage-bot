"""
Intentionally broken test used to generate a real CI failure for the
triage bot to diagnose. Two distinct failure modes are included so the
demo history shows the bot handling more than one kind of error.
"""


def divide(a, b):
    return a / b


def test_division_by_zero():
    # Raises ZeroDivisionError -> the bot should identify this clearly.
    assert divide(10, 0) == 0


def test_wrong_assertion():
    # A plain failed assertion, no exception.
    assert 2 + 2 == 4
