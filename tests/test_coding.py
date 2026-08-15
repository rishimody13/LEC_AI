from common.coding import (
    batch_id_from_label,
    check_digit,
    check_digit_ok,
    is_well_formed,
    label_code,
)


def test_round_trip():
    for batch in ["B-2288", "B-2290", "B-2291", "B-2293", "B-2296"]:
        code = label_code(batch)
        assert check_digit_ok(code)
        assert batch_id_from_label(code) == batch


def test_known_values():
    assert label_code("B-2288") == "B-2288-0"
    assert label_code("B-2290") == "B-2290-5"
    assert label_code("B-2291") == "B-2291-4"


def test_single_digit_change_always_breaks_the_check_digit():
    """This is what lets a reader tell a misread apart from a genuinely different code."""
    for body in ["2288", "2290", "2291", "2293", "2296", "1042", "9999"]:
        good = check_digit(body)
        for pos in range(4):
            for d in "0123456789":
                if d == body[pos]:
                    continue
                altered = body[:pos] + d + body[pos + 1 :]
                assert check_digit(altered) != good, f"{body} -> {altered} kept its check digit"


def test_malformed_codes_rejected():
    for bad in ["B-2288", "2288-0", "B-228-0", "B-22880", "", "B-ABCD-0"]:
        assert not is_well_formed(bad)
        assert not check_digit_ok(bad)
        assert batch_id_from_label(bad) is None


def test_wrong_check_digit_is_well_formed_but_fails():
    assert is_well_formed("B-2288-7")
    assert not check_digit_ok("B-2288-7")
