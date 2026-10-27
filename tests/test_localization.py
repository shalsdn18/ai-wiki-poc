from engine.localization import category_label, format_datetime_ko


def test_category_labels_are_localized_without_changing_category_codes():
    assert category_label("investment") == "투자"
    assert category_label("ai") == "AI"
    assert category_label("custom") == "custom"


def test_iso_date_is_formatted_in_korean_time_without_mutating_source_value():
    value = "2026-09-02T01:02:03+00:00"
    assert format_datetime_ko(value) == "2026년 9월 2일 10:02"
    assert value == "2026-09-02T01:02:03+00:00"
