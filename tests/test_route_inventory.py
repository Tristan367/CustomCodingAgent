"""The app's URL surface, pinned.

main.py is being broken up into route modules. Moving a handler between files
is only safe if the thing it serves does not move with it, and the failure mode
is quiet: a dropped `include_router`, a prefix applied twice, or a renamed
function silently removes a URL or breaks a template's `url_for`. Nothing else
in the suite would notice -- the handler's own tests keep passing because the
function is still importable and still correct. Only the wiring is gone.

So the full surface is written down here. If a route is added or removed on
purpose, update ROUTES in the same commit and the diff will show exactly which
URLs changed.
"""

import json
from pathlib import Path

import pytest
from starlette.routing import Mount

INVENTORY = Path(__file__).parent / "route_inventory.json"


def _flatten(routes, prefix: str = ""):
    """Yield leaf routes, descending into included routers.

    FastAPI 0.121 keeps an included router as a single `_IncludedRouter` entry
    that delegates at request time, rather than copying its routes onto the
    app. Walking `app.routes` alone therefore reports three entries where the
    app actually serves several dozen URLs.
    """
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            nested = getattr(route, "include_context", None)
            yield from _flatten(inner.routes, prefix + getattr(nested, "prefix", ""))
        else:
            yield prefix, route


def _surface(app) -> list[dict]:
    """(path, methods, endpoint name) for every route, order-independent."""
    out = []
    for prefix, route in _flatten(app.routes):
        if isinstance(route, Mount):
            out.append({"path": prefix + route.path, "methods": ["MOUNT"], "name": route.name})
            continue
        methods = sorted(getattr(route, "methods", None) or ["WEBSOCKET"])
        # HEAD rides along with GET and is not something a handler declares.
        methods = [m for m in methods if m != "HEAD"]
        out.append({"path": prefix + route.path, "methods": methods, "name": route.name})
    return sorted(out, key=lambda r: (r["path"], r["methods"]))


@pytest.fixture(scope="module")
def surface():
    from agent_server.main import app

    return _surface(app)


def test_surface_matches_inventory(surface):
    expected = json.loads(INVENTORY.read_text())
    got = surface

    missing = [r for r in expected if r not in got]
    added = [r for r in got if r not in expected]

    detail = []
    if missing:
        detail.append("REMOVED (a URL the app used to serve):")
        detail += [f"  {r['methods']} {r['path']}  -> {r['name']}" for r in missing]
    if added:
        detail.append("ADDED (new URL, update route_inventory.json if intended):")
        detail += [f"  {r['methods']} {r['path']}  -> {r['name']}" for r in added]
    assert not detail, "\n".join(detail)


def test_endpoint_names_are_unique(surface):
    """`url_for` resolves by name, so a collision silently retargets a link."""
    seen: dict[str, str] = {}
    clashes = []
    for route in surface:
        name = route["name"]
        if name in seen and seen[name] != route["path"]:
            clashes.append(f"{name}: {seen[name]} and {route['path']}")
        seen[name] = route["path"]
    assert not clashes, "duplicate endpoint names:\n" + "\n".join(clashes)


def test_every_handler_is_reachable():
    """A router that is defined but never included serves nothing.

    This is the specific way the split can fail while looking finished: the
    module exists, the handlers are written, the tests import them directly --
    and main.py never calls include_router, so the URLs 404.
    """
    import importlib
    import pkgutil

    from agent_server import routes
    from agent_server.main import app

    mounted = {
        (prefix + r.path, frozenset(getattr(r, "methods", None) or []))
        for prefix, r in _flatten(app.routes)
    }

    orphans = []
    for info in pkgutil.iter_modules(routes.__path__):
        module = importlib.import_module(f"agent_server.routes.{info.name}")
        router = getattr(module, "router", None)
        if router is None:
            continue
            # A router's own routes already carry its prefix in .path.
            for route in router.routes:
                methods = frozenset(getattr(route, "methods", None) or [])
                if (route.path, methods) not in mounted:
                    orphans.append(
                        f"{sorted(methods)} {route.path} (agent_server/routes/{info.name}.py)"
                    )
    assert not orphans, "defined but not included in the app:\n" + "\n".join(orphans)


def test_every_page_template_closes_its_tags():
    """A page rendering inside the topbar passes every other test.

    Adding a nav link once replaced `</header>` instead of preceding it, so the
    whole document nested inside the header: 287 tests green, ruff clean, and
    every page unusable. Nothing else in the suite looks at the markup.
    """
    import re
    from pathlib import Path

    templates = Path(__file__).parent.parent / "web_ui" / "templates"
    problems = []
    for path in sorted(templates.rglob("*.html")):
        text = path.read_text()
        for tag in ("header", "nav", "form", "table", "details", "section", "dialog"):
            opened = len(re.findall(rf"<{tag}[\s>]", text))
            closed = len(re.findall(rf"</{tag}>", text))
            if opened != closed:
                problems.append(f"{path.name}: <{tag}> opened {opened}, closed {closed}")
    assert not problems, "unbalanced tags:\n" + "\n".join(problems)


def test_every_page_container_is_laid_out():
    """A new page div that no layout rule matches renders with no width.

    `#scripts-page` was added to the template and not to the rule listing the
    page containers, so it had no padding, no max-width and no scrolling.
    """
    import re
    from pathlib import Path

    root = Path(__file__).parent.parent / "web_ui"
    css = (root / "static" / "css" / "style.css").read_text()
    laid_out = set(re.findall(r"#([a-z-]+-page)", css))

    declared = set()
    for path in (root / "templates").rglob("*.html"):
        declared |= set(re.findall(r'id="([a-z-]+-page)"', path.read_text()))

    missing = sorted(declared - laid_out)
    assert not missing, f"page containers with no CSS rule: {missing}"
