"""The tasks API returns ``links`` as a list (v0.241.0 shipped the stored JSON string,
and the console's task detail crashed on ``links.map``)."""

from operator_api.routes import _issue_out


def test_issue_out_decodes_the_stored_json_string():
    issue = {"id": "task-1", "links": '[{"ref": "https://x/1", "kind": "pr"}, {"kind": "no-ref"}]'}
    assert _issue_out(issue)["links"] == [{"ref": "https://x/1", "kind": "pr"}]


def test_issue_out_tolerates_empty_malformed_and_list_values():
    assert _issue_out({"id": "t", "links": ""})["links"] == []
    assert _issue_out({"id": "t", "links": "{bad"})["links"] == []
    assert _issue_out({"id": "t", "links": '{"ref": "x"}'})["links"] == []
    assert _issue_out({"id": "t"})["links"] == []
    assert _issue_out({"id": "t", "links": [{"ref": "a"}, "junk"]})["links"] == [{"ref": "a"}]


def test_issue_out_leaves_other_fields_and_non_dicts_alone():
    assert _issue_out({"id": "t", "title": "x", "links": ""})["title"] == "x"
    assert _issue_out(None) is None
