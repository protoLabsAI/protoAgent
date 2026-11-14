"""``parse_links`` reads an issue's links whether they arrive as the stored JSON string or
as an already-decoded list, so no reader can mistake one shape for "no links"."""

from tasks.store import parse_links


def test_parse_links_reads_the_stored_json_string():
    assert parse_links({"links": '[{"ref": "bd-5a"}, {"kind": "no-ref"}]'}) == [{"ref": "bd-5a"}]


def test_parse_links_accepts_an_already_decoded_list():
    assert parse_links({"links": [{"ref": "bd-5a"}, {"x": 1}, "junk"]}) == [{"ref": "bd-5a"}]


def test_parse_links_tolerates_empty_and_malformed_values():
    assert parse_links({"links": ""}) == []
    assert parse_links({"links": "{bad"}) == []
    assert parse_links({"links": '{"ref": "x"}'}) == []
    assert parse_links({}) == []
