"""Profiles and their custom tools, as one shareable file.

A profile alone is not a shareable thing. It sets which tools its agents may
use, how many subagents each tier may spawn, and what the tiers are told -- and
a tier told to "run the deploy check" is broken on a machine where the
`deploy_check` tool does not exist. Exporting the profile without them produces
an import that fails later, mysteriously, which is worse than not importing.

Which tools "belong" to a profile is not something the data says outright. A
profile records the tools it *disables*, not the ones it uses, so the honest
answer -- and the only one the schema supports -- is every custom tool the
profile does not disable.

Note "every", not "every enabled". A tool's `enabled` flag is a fact about this
machine, and `apply_bundle` deliberately clears it on the way in so nothing can
run before a person has read it. Filtering the export by it therefore made
bundles lossy in exactly one step: import a bundle and export it again, and the
scripts were gone, because importing had switched them off. Whether a tool is
switched on here says nothing about whether the profile needs it there.

The asymmetry that matters on the way back in: a profile is text, and a custom
tool is a shell script that will run on the importing machine. So a bundle is
never applied straight from the file. `read_bundle` parses and validates; the
caller shows the scripts to a person; `apply_bundle` writes only after that, and
lands every imported tool disabled so nothing can run before it has been looked
at.
"""

import json
from typing import Any

from agent_server import database as db

FORMAT = "myriadcode.profile-bundle"
VERSION = 1

# Columns of a `prompts` row that describe the profile rather than identify the
# row or record when it was touched. Listed rather than inferred so a column
# added later is a deliberate decision about whether it should travel.
PROFILE_FIELDS = (
    "body",
    "disabled_tools",
    "subagent_body",
    "subagent_disabled_tools",
    "subagent_parallel_cap",
    "master_spawn_limit",
    "subagent_model",
    "sa_tier_model",
    "sa_tier_effort",
    "max_concurrent_subagents",
    "subagent_tiers",
)

TOOL_FIELDS = ("name", "description", "parameters", "script", "ask_permission")


def _disabled(profile: dict, custom_names: set[str]) -> set[str]:
    """Tool names this profile really switches off.

    The column does not mean what it looks like it means. NULL is not "nothing
    is disabled" -- it is "this profile has never been configured", and such a
    profile offers no custom tools at all. Reading the column directly exported
    every custom tool on the machine as though the profile used them, which is
    the same mistake the Profiles tool grid made before it started asking
    `_effective_disabled`.

    Same rule as that helper, but the set of custom tools comes from the
    database rather than the registry, which is only populated once tools have
    been loaded into a running process.
    """
    from agent_server.routes.prompts import STARTER_DISABLED_TOOLS

    raw = profile.get("disabled_tools")
    if raw is None:
        return set(custom_names) | STARTER_DISABLED_TOOLS.get(profile["name"], set())
    return {n.strip() for n in str(raw).split(",") if n.strip()}


async def build_bundle(name: str) -> dict | None:
    """Everything needed to recreate this profile somewhere else."""
    profile = await db.get_prompt(name, "system")
    if profile is None:
        return None

    rows = await db.list_custom_tools()
    off = _disabled(profile, {row["name"] for row in rows})
    # Not filtered by `row["enabled"]` -- see the module docstring. That made a
    # bundle lossy the moment it had been imported once.
    tools = [
        {field: row[field] for field in TOOL_FIELDS}
        for row in rows
        if row["name"] not in off
    ]

    # The summarising prompt is part of how a profile behaves and is edited on
    # the same page, so it travels with it.
    compaction = await db.get_prompt(name, "compaction")

    return {
        "format": FORMAT,
        "version": VERSION,
        "profile": {
            "name": profile["name"],
            **{f: profile.get(f) for f in PROFILE_FIELDS},
            "compaction_body": (compaction or {}).get("body"),
        },
        "tools": tools,
    }


class BundleError(ValueError):
    """The file is not a bundle this version can apply."""


def read_bundle(raw: str | bytes | dict) -> dict:
    """Parse and check a bundle, without writing anything.

    Every failure here is a message shown to a person, so each says what is
    wrong with *their file* rather than naming a key.
    """
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
            raise BundleError(f"That is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise BundleError("A bundle is a JSON object; this file is not.")
    if data.get("format") != FORMAT:
        raise BundleError(
            "That file is not a MyriadCode profile bundle "
            f"(it says format {data.get('format')!r})."
        )
    if int(data.get("version") or 0) > VERSION:
        raise BundleError(
            f"That bundle was written by a newer version (v{data.get('version')}); "
            f"this one understands up to v{VERSION}."
        )

    profile = data.get("profile")
    if not isinstance(profile, dict) or not str(profile.get("name") or "").strip():
        raise BundleError("The bundle has no profile in it.")
    if not str(profile.get("body") or "").strip():
        raise BundleError("The profile in the bundle has an empty system prompt.")

    tools: list[dict] = []
    for entry in data.get("tools") or []:
        if not isinstance(entry, dict):
            raise BundleError("One of the tools in the bundle is not an object.")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise BundleError("One of the tools in the bundle has no name.")
        if not str(entry.get("script") or "").strip():
            raise BundleError(f"The tool {name!r} has no script.")
        params = entry.get("parameters")
        if isinstance(params, dict):
            params = json.dumps(params)
        try:
            json.loads(params or "{}")
        except (json.JSONDecodeError, TypeError):
            raise BundleError(f"The tool {name!r} has invalid parameters JSON.") from None
        tools.append({
            "name": name,
            "description": str(entry.get("description") or ""),
            "parameters": params or "{}",
            "script": str(entry["script"]),
            "ask_permission": bool(entry.get("ask_permission", True)),
        })

    return {"profile": profile, "tools": tools}


async def describe_bundle(bundle: dict) -> dict:
    """What the import would do, for the confirmation the user is shown.

    The point is that nothing is a surprise: which names already exist and would
    be overwritten, and the full text of every script that is about to be put on
    this machine.
    """
    existing_profiles = {p["name"] for p in await db.list_prompts("system")}
    existing_tools = {t["name"] for t in await db.list_custom_tools()}
    profile = bundle["profile"]
    return {
        "profile": {
            "name": profile["name"],
            "exists": profile["name"] in existing_profiles,
            "body_chars": len(profile.get("body") or ""),
            "tiers": len(json.loads(profile.get("subagent_tiers") or "[]")),
        },
        "tools": [
            {**tool, "exists": tool["name"] in existing_tools}
            for tool in bundle["tools"]
        ],
    }


async def apply_bundle(bundle: dict, rename: str = "") -> dict:
    """Write the bundle. Only ever called after a person has said yes.

    Tools land disabled. An imported tool is a shell script from someone else,
    and the gap between "I imported this" and "this is now callable by an agent
    on my machine" should be a decision, not a side effect.
    """
    profile = bundle["profile"]
    name = (rename or profile["name"]).strip()
    if not name:
        raise BundleError("The profile needs a name.")

    for tool in bundle["tools"]:
        await db.save_custom_tool(
            name=tool["name"],
            description=tool["description"],
            parameters=tool["parameters"],
            script=tool["script"],
            enabled=False,
            ask_permission=tool["ask_permission"],
        )

    await db.save_prompt(
        name,
        profile.get("body") or "",
        "system",
        disabled_tools=profile.get("disabled_tools"),
        subagent_body=profile.get("subagent_body"),
        subagent_disabled_tools=profile.get("subagent_disabled_tools"),
    )
    for field in ("subagent_parallel_cap", "master_spawn_limit", "subagent_model",
                  "sa_tier_model", "sa_tier_effort", "max_concurrent_subagents",
                  "subagent_tiers"):
        value = profile.get(field)
        if value is not None:
            await db._execute(
                f"UPDATE prompts SET {field} = ? WHERE kind = 'system' AND name = ?",
                (value, name),
            )
    if profile.get("compaction_body"):
        await db.save_prompt(name, profile["compaction_body"], "compaction")

    return {
        "name": name,
        "tools": [t["name"] for t in bundle["tools"]],
    }


def bundle_filename(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-")
    return f"{safe or 'profile'}.myriadcode.json"


def dump_bundle(bundle: dict[str, Any]) -> str:
    return json.dumps(bundle, indent=2, ensure_ascii=False) + "\n"
