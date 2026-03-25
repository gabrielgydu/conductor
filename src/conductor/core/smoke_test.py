from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def discover_frontend_routes(project_dir: Path) -> list[str]:
    """Parse Vue Router config to extract route paths."""
    candidates = [
        project_dir / "frontend" / "src" / "router" / "index.ts",
        project_dir / "frontend" / "src" / "router" / "index.js",
        project_dir / "frontend" / "src" / "router.ts",
        project_dir / "frontend" / "src" / "router.js",
    ]

    router_file = None
    for candidate in candidates:
        if candidate.exists():
            router_file = candidate
            break

    if router_file is None:
        return []

    content = router_file.read_text(encoding="utf-8")
    raw_paths = re.findall(r"path:\s*['\"]([^'\"]+)['\"]", content)

    seen: set[str] = set()
    routes: list[str] = []
    for path in raw_paths:
        # Replace dynamic segments like :id, :invoiceId with 1
        normalized = re.sub(r":[A-Za-z][A-Za-z0-9_]*", "1", path)
        if normalized not in seen:
            seen.add(normalized)
            routes.append(normalized)

    if not routes:
        logger.warning("discover_frontend_routes: no routes found in %s", router_file)

    return routes


def discover_api_endpoints(project_dir: Path) -> list[tuple[str, str]]:
    """Discover API route registrations from PHP files."""
    # Collect candidate files
    php_files: list[Path] = []

    api_routes_dir = project_dir / "api" / "Routes"
    if api_routes_dir.exists():
        php_files.extend(api_routes_dir.glob("*.php"))

    routes_api = project_dir / "routes" / "api.php"
    if routes_api.exists():
        php_files.append(routes_api)

    api_bootstrap = project_dir / "api" / "bootstrap.php"
    if api_bootstrap.exists():
        php_files.append(api_bootstrap)

    if not php_files:
        return []

    # Regex patterns
    laravel_pattern = re.compile(
        r"Route::(get|post|put|patch|delete)\s*\(\s*['\"]([^'\"]+)['\"]",
        re.IGNORECASE,
    )
    slim_pattern = re.compile(
        r"\$app->(get|post|put|patch|delete)\s*\(\s*['\"]([^'\"]+)['\"]",
        re.IGNORECASE,
    )
    map_pattern = re.compile(
        r"->map\s*\(\s*\[['\"]([A-Z]+)['\"]\]\s*,\s*['\"]([^'\"]+)['\"]",
        re.IGNORECASE,
    )

    def normalize_path(p: str) -> str:
        # Replace {id}, {invoiceId}, etc. with 1
        return re.sub(r"\{[A-Za-z][A-Za-z0-9_]*\}", "1", p)

    endpoints: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for php_file in php_files:
        content = php_file.read_text(encoding="utf-8")

        for match in laravel_pattern.finditer(content):
            method = match.group(1).upper()
            path = normalize_path(match.group(2))
            key = (method, path)
            if key not in seen:
                seen.add(key)
                endpoints.append(key)

        for match in slim_pattern.finditer(content):
            method = match.group(1).upper()
            path = normalize_path(match.group(2))
            key = (method, path)
            if key not in seen:
                seen.add(key)
                endpoints.append(key)

        for match in map_pattern.finditer(content):
            method = match.group(1).upper()
            path = normalize_path(match.group(2))
            key = (method, path)
            if key not in seen:
                seen.add(key)
                endpoints.append(key)

    return endpoints


def generate_smoke_test(project_dir: Path) -> str:
    """Return source code of a Playwright smoke test file."""
    routes = discover_frontend_routes(project_dir)
    endpoints = discover_api_endpoints(project_dir)
    get_endpoints = [(method, path) for method, path in endpoints if method == "GET"]

    lines: list[str] = []

    lines.append("import { test, expect } from '@playwright/test';")
    lines.append("")
    lines.append("const BASE_URL = process.env.APP_URL || 'http://localhost:5173';")
    lines.append("const API_BASE = process.env.API_URL || 'http://localhost:8000';")
    lines.append("")
    lines.append("test.describe('Conductor Smoke Tests', () => {")

    if not routes and not get_endpoints:
        # Minimal fallback: just check the base URL loads
        lines.append("  test('base url loads', async ({ page }) => {")
        lines.append("    await page.goto(BASE_URL);")
        lines.append("    await page.waitForLoadState('networkidle');")
        lines.append(
            "    const text = await page.evaluate(() => document.body.innerText);"
        )
        lines.append("    expect(text.length).toBeGreaterThan(0);")
        lines.append("  });")
    else:
        for route in routes:
            test_name = f"route: {route} loads without errors"
            lines.append(f"  test('{test_name}', async ({{ page }}) => {{")
            lines.append("    const errors: string[] = [];")
            lines.append("    page.on('console', msg => {")
            lines.append("      if (msg.type() === 'error') errors.push(msg.text());")
            lines.append("    });")
            lines.append("")
            lines.append(f"    await page.goto(`${{BASE_URL}}{route}`);")
            lines.append("    await page.waitForLoadState('networkidle');")
            lines.append("")
            lines.append("    // No Vite error overlay")
            lines.append("    const overlay = await page.$('.vite-error-overlay');")
            lines.append("    expect(overlay).toBeNull();")
            lines.append("")
            lines.append("    // Not a blank page")
            lines.append(
                "    const text = await page.evaluate(() => document.body.innerText);"
            )
            lines.append("    expect(text.length).toBeGreaterThan(50);")
            lines.append("")
            lines.append(
                "    // No console errors (filter out known noise like favicon 404)"
            )
            lines.append(
                "    const realErrors = errors.filter(e => !e.includes('favicon'));"
            )
            lines.append("    expect(realErrors).toHaveLength(0);")
            lines.append("  });")
            lines.append("")

        for _method, path in get_endpoints:
            test_name = f"api: GET {path} responds"
            lines.append(f"  test('{test_name}', async ({{ request }}) => {{")
            lines.append(
                f"    const response = await request.get(`${{API_BASE}}{path}`);"
            )
            lines.append("    // Accept 2xx or auth-related 4xx (401, 403)")
            lines.append(
                "    expect([200, 201, 401, 403]).toContain(response.status());"
            )
            lines.append("  });")
            lines.append("")

    lines.append("});")
    lines.append("")

    return "\n".join(lines)
