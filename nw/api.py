"""API versioning for every Northwind service.

Routes live under `/v1`. The unversioned paths stay as aliases for one release: they
answer exactly as before, and OpenAPI marks them deprecated so a generated client and a
reader both see which path to call. Probes and `/metrics` never move: the platform's
health checks and the scraper are configured by path, not by API version.

Every response carries two headers: `x-api-version`, the contract the response follows,
and `x-nw-version`, the package that produced it. A client log line can then be tied to
the code that answered without asking the operator what was deployed that day.

    app = FastAPI(...)
    install_version_headers(app)
    ...routes...
    mount_versioned(app)          # last, after every route is registered
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.routing import APIRoute

from nw import __version__

API_VERSION = "1"
PREFIX = f"/v{API_VERSION}"
# Probes and the scraper are addressed by the platform, never by an API client.
UNVERSIONED = frozenset({"/healthz", "/readyz", "/metrics"})
DEPRECATED_TAG = "deprecated"


def version_fields() -> dict[str, str]:
    """What every service's `/version` adds to its own facts."""
    return {"api_version": API_VERSION, "nw_version": __version__}


def install_version_headers(app: FastAPI) -> None:
    """`x-api-version` and `x-nw-version` on every response, refusals included. Register it
    after the key check and the rate limiter so their 401 and 429 carry the headers too
    (Starlette runs the last-added middleware first)."""

    @app.middleware("http")
    async def version_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["x-api-version"] = API_VERSION
        response.headers["x-nw-version"] = __version__
        return response


def mount_versioned(app: FastAPI, *, exclude: frozenset[str] = UNVERSIONED) -> list[str]:
    """Register every API route again under `/v1` and mark the original as a deprecated
    alias. Returns the versioned paths. Idempotent: a route already under the prefix is
    left alone, so calling it twice adds nothing."""
    added: list[str] = []
    routes = app.router.routes
    existing = {r.path for r in routes if isinstance(r, APIRoute)}
    for route in list(routes):
        if not isinstance(route, APIRoute) or route.path in exclude:
            continue
        if route.path.startswith(PREFIX + "/") or route.path == PREFIX:
            continue
        path = PREFIX + route.path
        if path in existing:
            continue
        app.add_api_route(
            path,
            route.endpoint,
            methods=sorted(route.methods),
            response_model=route.response_model,
            status_code=route.status_code,
            tags=[t for t in route.tags if t != DEPRECATED_TAG],
            dependencies=route.dependencies,
            summary=route.summary,
            description=route.description,
            response_description=route.response_description,
            responses=route.responses,
            response_class=route.response_class,
            name=route.name,
            include_in_schema=route.include_in_schema,
        )
        # The versioned route goes where the original was, so the schema lists it first.
        versioned = routes.pop()
        routes.insert(routes.index(route), versioned)
        route.deprecated = True
        if DEPRECATED_TAG not in route.tags:
            route.tags = [*route.tags, DEPRECATED_TAG]
        route.description = (
            f"Deprecated alias of `{path}`, kept for one release. Call `{path}`."
            + (f"\n\n{route.description}" if route.description else "")
        )
        existing.add(path)
        added.append(path)
    app.openapi_schema = None  # regenerate on the next request to /openapi.json
    return added


def versioned_paths(app: FastAPI) -> list[str]:
    return [
        r.path
        for r in app.router.routes
        if isinstance(r, APIRoute) and r.path.startswith(PREFIX + "/")
    ]


def deprecated_paths(app: FastAPI) -> list[str]:
    return [r.path for r in app.router.routes if isinstance(r, APIRoute) and r.deprecated]
