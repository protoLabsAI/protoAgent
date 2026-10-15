from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from evals.tool_disclosure import catalog, invoked_this_turn


def test_catalog_is_stable_and_records_are_distinct():
    a, b = catalog(), catalog()
    assert [t.name for t in a] == [t.name for t in b]
    assert [t.description for t in a] == [t.description for t in b]
    assert a[17].invoke({}) == 'catalog-value-17-cobalt'
    assert a[23].invoke({}) == 'catalog-value-23-cobalt'


def test_prior_invocation_does_not_satisfy_a_new_request():
    old = [HumanMessage(content='read'), ToolMessage(content='value', name='fixture_17', tool_call_id='a'),
           AIMessage(content='value'), HumanMessage(content='verify again'), AIMessage(content='value')]
    assert not invoked_this_turn(old, {'fixture_17'})
    assert invoked_this_turn([*old, ToolMessage(content='value', name='fixture_17', tool_call_id='b')], {'fixture_17'})
