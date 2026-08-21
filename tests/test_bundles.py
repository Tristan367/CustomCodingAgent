"""Sharing a profile, and the custom tools that make it work.

A profile alone is not shareable: it says which tools its agents may use and
what each subagent tier is told, and a tier told to run `deploy_check` is broken
on a machine without that tool. An import that quietly produces a profile
pointing at tools you do not have fails later, mysteriously.

Which tools "belong" to a profile is not stated anywhere. A profile records the
tools it *disables*, so the only answer the schema supports is: every enabled
custom tool it does not disable -- exactly the set a session on it would have.

The asymmetry on the way back in is the thing to keep: a profile is text, a
custom tool is a shell script that will run on the importing machine. Nothing is
written before a person has seen the scripts, and imported tools arrive
disabled.
"""

import json

import pytest

from agent_server import bundles
from agent_server import database as db


@pytest.fixture
async def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    await db.init_db()
    yield
    await db.close()


async def _tool(name, script="echo hi", enabled=True, ask=True):
    await db.save_custom_tool(
        name=name, description=f"{name} does a thing",
        parameters='{"type":"object","properties":{}}',
        script=script, enabled=enabled, ask_permission=ask,
    )


# ── What travels ─────────────────────────────────────────────────────────────

async def test_a_bundle_carries_the_profile_and_its_tools(store):
    await db.save_prompt("shipper", "You ship things.", "system", disabled_tools="")
    await _tool("deploy_check")
    await _tool("run_migrations")

    bundle = await bundles.build_bundle("shipper")
    assert bundle["format"] == bundles.FORMAT
    assert bundle["profile"]["name"] == "shipper"
    assert bundle["profile"]["body"] == "You ship things."
    assert {t["name"] for t in bundle["tools"]} == {"deploy_check", "run_migrations"}


async def test_a_tool_the_profile_disables_does_not_travel(store):
    # Comma-separated, which is how the save path writes it. NULL would mean
    # "never configured", which switches every custom tool off.
    await db.save_prompt("careful", "Be careful.", "system",
                         disabled_tools="run_migrations")
    await _tool("deploy_check")
    await _tool("run_migrations")

    bundle = await bundles.build_bundle("careful")
    assert {t["name"] for t in bundle["tools"]} == {"deploy_check"}


async def test_a_tool_travels_even_when_it_is_switched_off_here(store):
    """`enabled` is a fact about this machine, not about the profile.

    This used to assert the opposite, on the reading that switched-off means
    "not part of what this profile does". The reading does not survive contact
    with the round trip: importing a bundle deliberately switches every tool it
    carries off, so under the old rule a bundle that had been imported once
    exported without its scripts. Whoever it was passed to next got a profile
    referring to tools that were not there -- the exact failure this module
    exists to prevent, reintroduced by the export side.
    """
    await db.save_prompt("p", "body", "system", disabled_tools="")
    await _tool("live_one")
    await _tool("shelved", enabled=False)

    bundle = await bundles.build_bundle("p")
    assert {t["name"] for t in bundle["tools"]} == {"live_one", "shelved"}


async def test_a_bundle_survives_being_imported_and_exported_again(store):
    """Sharing a profile you were given must pass on what you were given."""
    await db.save_prompt("origin", "body", "system", disabled_tools="")
    await _tool("deploy_check")

    first = await bundles.build_bundle("origin")
    assert {t["name"] for t in first["tools"]} == {"deploy_check"}

    # Arrives disabled, by design -- it is shell that has not been read yet.
    await bundles.apply_bundle(first, rename="passed_on")
    row = next(r for r in await db.list_custom_tools() if r["name"] == "deploy_check")
    assert not row["enabled"], "imported tools must land switched off"

    second = await bundles.build_bundle("passed_on")
    assert {t["name"] for t in second["tools"]} == {"deploy_check"}, (
        "re-exporting an imported profile dropped its tools")
    assert second["tools"][0]["script"] == first["tools"][0]["script"]


async def test_the_summarising_prompt_travels_too(store):
    """It is part of how a profile behaves and is edited on the same page."""
    await db.save_prompt("p", "body", "system")
    await db.save_prompt("p", "Summarise tersely.", "compaction")

    bundle = await bundles.build_bundle("p")
    assert bundle["profile"]["compaction_body"] == "Summarise tersely."


async def test_the_subagent_hierarchy_travels(store):
    """The tiers are most of what makes a profile worth sharing."""
    await db.save_prompt("deep", "body", "system",
                         subagent_body="You are a subagent.",
                         subagent_parallel_cap=4)
    tiers = json.dumps([{"body": "tier one", "disabled_tools": ""}])
    await db._execute(
        "UPDATE prompts SET subagent_tiers = ?, master_spawn_limit = ? "
        "WHERE kind='system' AND name='deep'", (tiers, 9))

    bundle = await bundles.build_bundle("deep")
    assert bundle["profile"]["subagent_body"] == "You are a subagent."
    assert bundle["profile"]["subagent_parallel_cap"] == 4
    assert bundle["profile"]["master_spawn_limit"] == 9
    assert json.loads(bundle["profile"]["subagent_tiers"])[0]["body"] == "tier one"


async def test_a_never_configured_profile_carries_no_custom_tools(store):
    """NULL in that column does not mean "nothing is disabled". It means the
    profile has never been configured, and such a profile offers no custom tools
    at all -- so exporting one must not sweep up every tool on the machine and
    claim the profile uses them. Reading the column directly did exactly that."""
    await db.save_prompt("fresh", "body", "system")
    assert (await db.get_prompt("fresh", "system"))["disabled_tools"] is None
    await _tool("something")

    bundle = await bundles.build_bundle("fresh")
    assert bundle["tools"] == [], "an unconfigured profile exported tools it does not offer"


async def test_an_unknown_profile_has_no_bundle(store):
    assert await bundles.build_bundle("nope") is None


# ── Reading one back ─────────────────────────────────────────────────────────

def test_a_round_trip_keeps_everything():
    bundle = {
        "format": bundles.FORMAT, "version": 1,
        "profile": {"name": "p", "body": "do the thing"},
        "tools": [{"name": "t", "description": "d", "parameters": "{}",
                   "script": "echo hi", "ask_permission": False}],
    }
    parsed = bundles.read_bundle(bundles.dump_bundle(bundle))
    assert parsed["profile"]["body"] == "do the thing"
    assert parsed["tools"][0]["script"] == "echo hi"
    assert parsed["tools"][0]["ask_permission"] is False


@pytest.mark.parametrize("raw,fragment", [
    ("not json at all", "valid JSON"),
    ('[]', "JSON object"),
    ('{"format": "something-else"}', "not a MyriadCode"),
    (json.dumps({"format": bundles.FORMAT, "version": 99}), "newer version"),
    (json.dumps({"format": bundles.FORMAT, "version": 1}), "no profile"),
    (json.dumps({"format": bundles.FORMAT, "version": 1,
                 "profile": {"name": "p", "body": "  "}}), "empty system prompt"),
    (json.dumps({"format": bundles.FORMAT, "version": 1,
                 "profile": {"name": "p", "body": "b"},
                 "tools": [{"name": "t", "script": ""}]}), "no script"),
    (json.dumps({"format": bundles.FORMAT, "version": 1,
                 "profile": {"name": "p", "body": "b"},
                 "tools": [{"name": "t", "script": "x", "parameters": "{oops"}]}),
     "invalid parameters"),
])
def test_a_bad_bundle_says_what_is_wrong_with_it(raw, fragment):
    """Every one of these is shown to a person, so it names their problem."""
    with pytest.raises(bundles.BundleError) as err:
        bundles.read_bundle(raw)
    assert fragment in str(err.value)


# ── Importing ────────────────────────────────────────────────────────────────

async def test_imported_tools_arrive_switched_off(store):
    """The whole safety property. An imported tool is a shell script from
    somebody else; the gap between "I imported this" and "an agent on my machine
    can run it" has to be a decision, not a side effect."""
    parsed = bundles.read_bundle(json.dumps({
        "format": bundles.FORMAT, "version": 1,
        "profile": {"name": "p", "body": "b"},
        "tools": [{"name": "risky", "description": "", "parameters": "{}",
                   "script": "rm -rf ~/everything", "ask_permission": True}],
    }))
    await bundles.apply_bundle(parsed)

    rows = {t["name"]: t for t in await db.list_custom_tools()}
    assert rows["risky"]["enabled"] == 0, "an imported tool was live immediately"
    assert rows["risky"]["script"] == "rm -rf ~/everything"


async def test_importing_creates_the_profile_and_its_tiers(store):
    parsed = bundles.read_bundle(json.dumps({
        "format": bundles.FORMAT, "version": 1,
        "profile": {"name": "imported", "body": "the body",
                    "subagent_body": "sub", "master_spawn_limit": 2,
                    "compaction_body": "summarise"},
        "tools": [],
    }))
    result = await bundles.apply_bundle(parsed)
    assert result["name"] == "imported"

    row = await db.get_prompt("imported", "system")
    assert row["body"] == "the body"
    assert row["subagent_body"] == "sub"
    assert row["master_spawn_limit"] == 2
    assert (await db.get_prompt("imported", "compaction"))["body"] == "summarise"


async def test_importing_under_a_different_name(store):
    """So a bundle can land next to a profile of the same name rather than
    over it."""
    await db.save_prompt("shipper", "mine", "system")
    parsed = bundles.read_bundle(json.dumps({
        "format": bundles.FORMAT, "version": 1,
        "profile": {"name": "shipper", "body": "theirs"}, "tools": [],
    }))
    await bundles.apply_bundle(parsed, rename="shipper (theirs)")

    assert (await db.get_prompt("shipper", "system"))["body"] == "mine"
    assert (await db.get_prompt("shipper (theirs)", "system"))["body"] == "theirs"


async def test_the_review_says_what_would_be_overwritten(store):
    """Nothing should be a surprise: which names already exist, and every
    script that is about to land here."""
    await db.save_prompt("shipper", "mine", "system")
    await _tool("deploy_check", script="echo mine")
    parsed = bundles.read_bundle(json.dumps({
        "format": bundles.FORMAT, "version": 1,
        "profile": {"name": "shipper", "body": "theirs"},
        "tools": [{"name": "deploy_check", "description": "", "parameters": "{}",
                   "script": "echo theirs", "ask_permission": True},
                  {"name": "brand_new", "description": "", "parameters": "{}",
                   "script": "echo new", "ask_permission": True}],
    }))
    summary = await bundles.describe_bundle(parsed)

    assert summary["profile"]["exists"] is True
    by_name = {t["name"]: t for t in summary["tools"]}
    assert by_name["deploy_check"]["exists"] is True
    assert by_name["brand_new"]["exists"] is False
    assert by_name["deploy_check"]["script"] == "echo theirs", "the script must be shown"


async def test_a_full_round_trip_through_the_database(store):
    """Export a real profile, import it under a new name, and get the same thing."""
    await db.save_prompt("original", "the system prompt", "system",
                         disabled_tools="noisy")
    await _tool("useful", script="echo useful")
    await _tool("noisy", script="echo noisy")

    exported = await bundles.build_bundle("original")
    parsed = bundles.read_bundle(bundles.dump_bundle(exported))
    await bundles.apply_bundle(parsed, rename="copy")

    row = await db.get_prompt("copy", "system")
    assert row["body"] == "the system prompt"
    assert row["disabled_tools"] == "noisy"
    tools = {t["name"] for t in parsed["tools"]}
    assert tools == {"useful"}, f"the disabled tool travelled: {tools}"


# ── The seam between reviewing and importing ─────────────────────────────────

async def test_what_inspect_hands_back_is_something_import_accepts(store):
    """The two endpoints have to agree on a shape.

    `inspect` used to return the parsed halves with no envelope, and `import`
    re-validated expecting a whole bundle -- so the review rendered correctly
    and the button after it failed with "not a MyriadCode profile bundle". A
    unit test that validated a complete bundle could never see it.
    """
    from agent_server.routes.prompts import inspect_bundle

    await db.save_prompt("p", "body", "system", disabled_tools="")
    await _tool("t")
    original = bundles.dump_bundle(await bundles.build_bundle("p"))

    class _Upload:
        async def read(self):
            return original

    class _Req:
        async def form(self):
            return {"bundle": _Upload()}

    response = await inspect_bundle(_Req())
    handed_back = json.loads(response.body)["bundle"]

    # The exact thing the page posts back to /import.
    reparsed = bundles.read_bundle(handed_back)
    assert reparsed["profile"]["name"] == "p"
    assert [t["name"] for t in reparsed["tools"]] == ["t"]
