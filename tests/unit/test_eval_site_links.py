"""The eval's site_links_grounded check (eval/run_eval.py): every link into PickWise in
Pico's answer must appear word for word in a tool output of the conversation. Pico is
told to link a configuration with the path get_configuration_options returned and never
to build a URL, and this is the deterministic half of holding it to that."""
import json

from ._adapters import run_eval

ID = "6cff98c5-3a91-496e-8630-5f7226ab95d7"
PATH = f"/laptops/{ID}?chip=10-10&memory=32gb&storage=2tb"
# A tool output as the harness keeps it: the ToolMessage's JSON text.
TOOL = [json.dumps({"laptop_id": ID, "configured": True, "total_rm": 9999.0, "path": PATH})]


def _ungrounded(response, tools=TOOL):
    return run_eval._ungrounded_links(response, tools)


def test_a_link_with_exactly_the_tools_path_is_grounded():
    assert _ungrounded(f"It comes to RM 9,999. [View this configuration]({PATH})") == []
    assert _ungrounded(f"See {PATH}.") == [], "a bare path, with the sentence's full stop"


def test_a_changed_param_is_not_grounded():
    changed = PATH.replace("memory=32gb", "memory=64gb")
    assert _ungrounded(f"[View this configuration]({changed})") == [changed]
    reordered = f"/laptops/{ID}?memory=32gb&chip=10-10&storage=2tb"
    assert _ungrounded(f"[View]({reordered})") == [reordered]
    assert _ungrounded(f"[View]({PATH}&display=nano_texture)") == [f"{PATH}&display=nano_texture"]


def test_an_invented_path_is_not_grounded():
    assert _ungrounded("[View](/laptops/00000000-0000-0000-0000-000000000000)") == [
        "/laptops/00000000-0000-0000-0000-000000000000"]
    assert _ungrounded("[Compare them](/compare?ids=a,b)") == ["/compare?ids=a,b"]
    assert _ungrounded(f"[View]({PATH})", tools=[]) == [PATH], "no tool returned it in this conversation"


def test_a_host_put_in_front_of_a_real_path_is_a_built_url():
    built = f"https://pickwise.ngyijie.com{PATH}"
    assert _ungrounded(f"[View]({built})") == [built]


def test_an_external_http_link_is_not_this_checks_business():
    assert _ungrounded("[Search on Shopee](https://shopee.com.my/search?keyword=macbook%20air)") == []
    assert _ungrounded("[Apple](https://www.apple.com/my/shop/buy-mac/macbook-air)") == []


def test_no_link_is_fine():
    assert _ungrounded("The 13-inch Air with 32GB and 2TB comes to RM 9,999.") == []
    assert _ungrounded("", tools=[]) == []


def test_the_rule_check_runs_with_no_tool_output_and_names_what_it_found():
    result = {"text": "[View](/laptops/abc)", "tool_calls": [], "tool_outputs": []}
    [check] = [c for c in run_eval.rule_checks({}, "vague_intent", result, [], []) if c["name"] == "site_links_grounded"]
    assert check["passed"] is False and "/laptops/abc" in check["detail"]
    result["text"] = "Which size do you want?"
    [check] = [c for c in run_eval.rule_checks({}, "vague_intent", result, [], []) if c["name"] == "site_links_grounded"]
    assert check["passed"] is True


def test_a_configuration_case_needs_a_path_the_tool_returned_in_the_answer():
    def given(text):
        result = {"text": text, "tool_calls": ["get_configuration_options"], "tool_outputs": TOOL}
        checks = run_eval.rule_checks({"configuration_link": True}, "tool_routing", result, TOOL, [])
        return next(c["passed"] for c in checks if c["name"] == "configuration_link_given")

    assert given(f"RM 9,999. [View this configuration]({PATH})") is True
    assert given("RM 9,999.") is False
    assert given(f"[View](/laptops/{ID})") is False, "the plain page is not the configuration"
    result = {"text": "RM 9,999.", "tool_calls": [], "tool_outputs": []}
    names = [c["name"] for c in run_eval.rule_checks({}, "tool_routing", result, TOOL, [])]
    assert "configuration_link_given" not in names, "only cases that ask for it"
