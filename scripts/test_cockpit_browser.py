"""Optional real-browser smoke for the isolated NEXUS Cockpit.

No frontend build or browser package is required to RUN the application. To run
this diagnostic, install Playwright and a Chromium browser in your dev env::

    pip install playwright
    python -m playwright install chromium
    python -m nexus_ai_agent.cockpit --preview --port 3000
    python scripts/test_cockpit_browser.py --url http://127.0.0.1:3000

--executable accepts a locally installed Chromium when Playwright's CDN is not
available. --axe accepts a LOCAL axe-core/axe.min.js to add WCAG 2/2.1 AA checks;
no remote script is fetched. --screenshots writes optional evidence to an ignored
artifact directory such as ci-artifacts/cockpit-browser, not to source control.

For real protected-mode browser checks, start another cockpit WITHOUT --preview
and pass --protected-url. Supply its test token through NEXUS_COCKPIT_TEST_TOKEN
in the test process environment (never a flag, URL or printed output).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Route


async def run(args: argparse.Namespace) -> None:
    from playwright.async_api import async_playwright, expect

    passed: list[str] = []
    page_errors: list[str] = []
    external_requests: list[str] = []
    allowed = {urlsplit(args.url).netloc, urlsplit(args.protected_url or args.url).netloc}
    screenshots: Path | None = args.screenshots
    if screenshots:
        screenshots.mkdir(parents=True, exist_ok=True)

    def record(name: str) -> None:
        passed.append(name)
        print(f"PASS {len(passed):02d} · {name}", flush=True)

    def watch(page: Page) -> None:
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                external_requests.append(urlsplit(request.url).netloc)
                if urlsplit(request.url).scheme in {"http", "https"}
                and urlsplit(request.url).netloc not in allowed
                else None
            ),
        )

    async def go(page: Page, name: str) -> None:
        await page.goto(f"{args.url.rstrip('/')}/#{name}", wait_until="networkidle")
        await expect(page.locator(f'[data-nav="{name}"]').first).to_have_attribute(
            "aria-current", "page"
        )
        await expect(page.locator(".page-heading")).to_be_visible()

    async def no_overflow(page: Page, name: str) -> None:
        sizes = await page.evaluate(
            "({width:innerWidth, scroll:document.documentElement.scrollWidth})"
        )
        assert sizes["scroll"] <= sizes["width"], f"horizontal overflow on {name}: {sizes}"

    async def capture(page: Page, name: str) -> None:
        if screenshots:
            await page.screenshot(path=str(screenshots / f"{name}.png"), full_page=True)

    async def accessibility(page: Page, label: str) -> None:
        if not args.axe:
            return
        # Test instrumentation only, not an application script or CSP exception.
        await page.evaluate(args.axe.read_text(encoding="utf-8"))
        violations = await page.evaluate("""async () => {
          const result = await axe.run(document, {
            runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa']}
          });
          return result.violations.map(v => ({id:v.id, impact:v.impact,
            nodes:v.nodes.map(n => ({target:n.target, reason:n.failureSummary}))}));
        }""")
        assert not violations, (
            f"accessibility {label}: {json.dumps(violations, ensure_ascii=False)}"
        )

    async def protected_checks(browser: Browser) -> None:
        if not args.protected_url:
            print("NOT RUN · protected-mode browser checks (--protected-url not supplied)")
            return
        token = os.environ.get("NEXUS_COCKPIT_TEST_TOKEN")
        if not token:
            raise ValueError("NEXUS_COCKPIT_TEST_TOKEN is required for --protected-url")
        context = await browser.new_context(viewport={"width": 1280, "height": 960})
        page = await context.new_page()
        watch(page)
        response = await context.request.get(
            f"{args.protected_url.rstrip('/')}/api/cockpit/catalog",
            headers={"Authorization": f"Bearer {token}"},
        )
        pack_count = len((await response.json())["packs"])
        await page.goto(args.protected_url, wait_until="networkidle")
        await expect(page.locator("#access-token")).to_be_visible()
        assert await page.locator(".pack-card").count() == 0
        await page.locator("#access-token").fill("wrong-browser-test-token-never-production")
        await page.locator("form button[type=submit]").click()
        await expect(page.locator("#auth-error")).not_to_be_empty()
        await expect(page.locator("#access-token")).to_have_value("")
        record("protected UI refuses wrong token and clears its input")
        await page.locator("#access-token").fill(token)
        await page.locator("form button[type=submit]").click()
        await expect(page.locator(".pack-card")).to_have_count(pack_count)
        stored = await page.evaluate("JSON.stringify({...localStorage})")
        assert token not in stored
        assert token not in await page.content()
        assert token not in page.url
        await page.reload(wait_until="networkidle")
        await expect(page.locator("#access-token")).to_be_visible()
        record("real Bearer login works; reload loses credentials, no persistent secret")
        await page.locator("#access-token").fill(token)
        await page.locator("form button[type=submit]").click()
        await expect(page.locator(".pack-card")).to_have_count(pack_count)
        await page.locator('.primary-nav [data-nav="lab"]').click()
        await page.locator("#command-input").fill('{"private_marker":"DO_NOT_PERSIST"}')
        await page.locator("#lock-session").click()
        await expect(page.locator("#access-token")).to_be_visible()
        assert "DO_NOT_PERSIST" not in await page.content()
        assert "DO_NOT_PERSIST" not in await page.evaluate("JSON.stringify({...localStorage})")
        assert await page.locator("#command-input").count() == 0
        record("explicit lock clears catalog, credentials, dialogs and input")
        await context.close()

    async def desktop_checks(context: BrowserContext) -> None:
        page = await context.new_page()
        watch(page)
        await go(page, "overview")
        snapshot = await (await context.request.get(f"{args.url}/api/cockpit/catalog")).json()
        await expect(page.locator(".pack-card")).to_have_count(len(snapshot["packs"]))
        assert await page.locator("html").get_attribute("dir") == "rtl"
        assert await page.evaluate(
            "document.fonts.ready.then(() => document.fonts.check('14px Vazirmatn'))"
        )
        assert await page.evaluate(
            "[...document.images].every(i => i.complete && i.naturalWidth > 0)"
        )
        await no_overflow(page, "desktop overview")
        await capture(page, "overview-desktop")
        record("RTL overview uses actual catalog and local font/SVG assets")
        async with page.expect_download() as event:
            await page.get_by_role("button", name="دریافت گزارش", exact=True).click()
        download = await event.value
        report = json.loads(Path(await download.path()).read_text())
        assert report["fingerprint"] == snapshot["fingerprint"]
        assert report["execution_enabled"] is False and report["bot_connected"] is False
        record("downloadable report preserves local-only evidence boundary")
        await page.get_by_role("button", name="نیازمند تأیید:", exact=False).click()
        await expect(page.locator('[data-level="C"]')).to_have_attribute("aria-pressed", "true")
        await expect(page.locator("[data-operation-row]")).to_have_count(
            snapshot["summary"]["confirmation_required"]
        )
        record("overview metric opens a real permission-level filter")
        await go(page, "overview")
        await page.locator(".pack-edit").click()
        await expect(page.locator("#pack-filter")).to_have_value("nexus.edit.timeline")
        await page.locator("#catalog-search").fill("timeline.trim")
        await expect(page.locator("[data-operation-row]")).to_have_count(1)
        await page.locator('[data-favorite-id="timeline.trim"]').click()
        await expect(page.locator('[data-favorite-id="timeline.trim"]')).to_have_attribute(
            "aria-pressed", "true"
        )
        record("pack search/filter and favorites work on actual operations")
        await page.get_by_role("button", name="بررسی timeline.trim", exact=True).click()
        await expect(page.locator("#inspector")).to_be_visible()
        await page.locator("#inspector summary").click()
        assert '"additionalProperties": false' in await page.locator(".raw-schema").inner_text()
        await accessibility(page, "inspector")
        await page.keyboard.press("Escape")
        await expect(page.locator("#inspector")).not_to_be_visible()
        record("inspector exposes real JSON Schema and is keyboard dismissible")
        await page.locator("#catalog-search").fill("no-such-capability-938451")
        await expect(page.locator(".empty-state")).to_be_visible()
        await page.get_by_role("button", name="پاک‌کردن فیلترها", exact=True).click()
        first = await page.locator("[data-operation-row]").first.get_attribute("data-operation-row")
        await page.get_by_role("button", name="صفحهٔ بعد", exact=True).click()
        assert first != await page.locator("[data-operation-row]").first.get_attribute(
            "data-operation-row"
        )
        await page.get_by_role("button", name="صفحهٔ قبل", exact=True).click()
        assert first == await page.locator("[data-operation-row]").first.get_attribute(
            "data-operation-row"
        )
        record("empty states, clear filters and pagination are functional")
        await page.keyboard.press("Control+k")
        await expect(page.locator("#command-palette")).to_be_visible()
        await page.locator("#palette-input").fill("timeline.trim")
        await accessibility(page, "command palette")
        await page.keyboard.press("ArrowDown")
        await page.keyboard.press("Enter")
        await expect(page.locator("#lab-operation")).to_have_value("timeline.trim")
        await expect(page.locator("#export-draft")).to_be_disabled()
        await page.keyboard.press("Control+Enter")
        await expect(page.locator('[data-validation="valid"]')).to_be_visible()
        await capture(page, "lab-desktop")
        record("command palette keyboard selection and real schema validation succeed")
        await page.locator(".skip-link").focus()
        await page.locator(".skip-link").press("Enter")
        await expect(page.locator("#main")).to_be_focused()
        assert page.url.endswith("#lab")
        record("keyboard skip link preserves the active lab route")
        async with page.expect_download() as event:
            await page.locator("#export-draft").click()
        draft_download = await event.value
        draft = json.loads(Path(await draft_download.path()).read_text())
        assert draft["kind"] == "nexus.cockpit.input-draft.v1"
        assert draft["operation"] == "timeline.trim"
        assert draft["execution_authorized"] is False
        assert draft["validation"]["scope"] == "input_schema_only"
        assert "confirmed" not in draft
        record("export is an input draft, never an authorized TypedCommand")
        await page.locator("#command-input").fill(
            json.dumps(
                {
                    "clip_asset_id": "INPUT_MUST_NOT_PERSIST",
                    "in_point_us": 100,
                    "out_point_us": 0,
                }
            )
        )
        await expect(page.locator("#export-draft")).to_be_disabled()
        await page.locator("#validate-input").click()
        await expect(page.locator('[data-validation="invalid"]')).to_be_visible()
        assert "INPUT_MUST_NOT_PERSIST" not in await page.locator("#lab-result").inner_text()
        await page.locator("#command-input").fill('{"clip_asset_id":"one","clip_asset_id":"two"}')
        await page.locator("#validate-input").click()
        await expect(page.locator("#lab-result")).to_contain_text("کلید تکراری")
        await page.locator("#command-input").fill("{broken")
        await page.locator("#validate-input").click()
        await expect(page.locator('[data-validation="invalid"]')).to_be_visible()
        record("invalid constraints, duplicate keys and malformed JSON never export success")
        await page.locator("#load-example").click()
        await page.locator("#format-json").click()
        await expect(page.locator("#export-draft")).to_be_disabled()
        waiting, release = asyncio.Event(), asyncio.Event()

        async def delay_validation(request: Route) -> None:
            waiting.set()
            await release.wait()
            await request.continue_()

        await page.route("**/api/cockpit/validate", delay_validation)
        await page.locator("#validate-input").click()
        await asyncio.wait_for(waiting.wait(), timeout=5)
        await page.locator("#command-input").fill("{}")
        async with page.expect_response("**/api/cockpit/validate"):
            release.set()
        await page.unroute("**/api/cockpit/validate", delay_validation)
        await expect(page.locator("#export-draft")).to_be_disabled()
        assert await page.locator("[data-validation]").count() == 0
        record("late valid response cannot validate newer edited input")
        stored = await page.evaluate("Object.fromEntries(Object.entries(localStorage))")
        assert set(stored) <= {"nexus.cockpit.theme", "nexus.cockpit.favorites"}
        assert "INPUT_MUST_NOT_PERSIST" not in json.dumps(stored)
        assert json.loads(stored["nexus.cockpit.favorites"]) == ["timeline.trim"]
        await page.reload(wait_until="networkidle")
        await expect(page.locator("#command-input")).not_to_have_value("{}")
        await go(page, "favorites")
        await expect(page.locator("[data-operation-row]")).to_have_count(1)
        record("only theme and known favorite IDs survive reload, not draft input")
        await go(page, "overview")

        async def offline(request: Route) -> None:
            await request.abort("failed")

        await page.route("**/api/cockpit/catalog", offline)
        await page.locator("#refresh").click()
        await expect(page.locator("#connection-banner")).to_be_visible()
        await expect(page.locator("#connection-banner")).to_contain_text("نسخهٔ قبلی")
        await expect(page.locator(".pack-card")).to_have_count(len(snapshot["packs"]))
        await page.unroute("**/api/cockpit/catalog", offline)
        await page.locator("#retry-connection").click()
        await expect(page.locator("#connection-banner")).not_to_be_visible()
        record("network failure retains explicitly stale catalog; retry recovers")
        for theme in ("light", "dark"):
            if await page.locator("html").get_attribute("data-theme") != theme:
                await page.locator("#theme-toggle").click()
            for name in ("overview", "capabilities", "lab", "trust", "guide"):
                await go(page, name)
                await accessibility(page, f"{theme}/{name}")
                await no_overflow(page, f"{theme}/{name}")
            if theme == "light":
                await go(page, "overview")
                await capture(page, "overview-light")
        record("all desktop routes and both themes render without overflow")
        if args.axe:
            record("axe WCAG 2/2.1 AA: both themes, five routes, inspector and palette")

        async def malicious_metadata(request: Route) -> None:
            response = await request.fetch()
            data = await response.json()
            target = next(op for op in data["operations"] if op["id"] == "timeline.trim")
            target["description"] = '<img src="https://untrusted.invalid/x" onerror="window.XSS=1">'
            await request.fulfill(response=response, json=data)

        await page.route("**/api/cockpit/catalog", malicious_metadata)
        await go(page, "capabilities")
        await page.locator("#refresh").click()
        await expect(page.locator("#refresh")).to_be_enabled()
        await page.locator("#catalog-search").fill("timeline.trim")
        await page.get_by_role("button", name="بررسی timeline.trim", exact=True).click()
        await expect(page.locator(".inspector-description")).to_contain_text("<img")
        assert await page.locator("#inspector img").count() == 0
        assert await page.evaluate("window.XSS === undefined")
        await page.keyboard.press("Escape")
        await page.unroute("**/api/cockpit/catalog", malicious_metadata)
        record("hostile metadata renders as text, not HTML or external requests")
        await page.close()

    async def mobile_checks(context: BrowserContext) -> None:
        page = await context.new_page()
        watch(page)
        for width in (390, 320):
            await page.set_viewport_size({"width": width, "height": 844})
            for name in ("overview", "capabilities", "lab", "trust", "guide"):
                await go(page, name)
                await no_overflow(page, f"mobile {width}/{name}")
                await accessibility(page, f"mobile {width}/{name}")
            await go(page, "overview")
            await capture(page, f"overview-mobile-{width}")
            await page.locator('.mobile-nav [data-nav="lab"]').click()
            await expect(page.locator("#command-input")).to_be_visible()
            await page.locator("#validate-input").click()
            await expect(page.locator('[data-validation="valid"]')).to_be_visible()
            await page.locator("#open-palette").click()
            await page.locator("#palette-input").fill("timeline.trim")
            await expect(page.locator(".palette-result")).to_have_count(1)
            bounds = await page.locator("#command-palette").bounding_box()
            assert bounds and bounds["x"] >= 0 and bounds["width"] <= width
            await page.keyboard.press("Escape")
            await expect(page.locator("#command-palette")).not_to_be_visible()
            record(f"mobile {width}px: all routes, navigation, real validation and palette")
        await page.close()

    async with async_playwright() as playwright:
        options: dict[str, Any] = {"headless": True}
        if args.executable:
            options["executable_path"] = str(args.executable)
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1050}, reduced_motion="reduce", accept_downloads=True
        )
        try:
            await desktop_checks(context)
            mobile = await browser.new_context(
                viewport={"width": 390, "height": 844},
                reduced_motion="reduce",
                is_mobile=True,
                has_touch=True,
                device_scale_factor=1,
            )
            await mobile_checks(mobile)
            await mobile.close()
            await protected_checks(browser)
            assert not page_errors, f"uncaught browser errors: {page_errors}"
            assert not external_requests, f"unexpected third-party requests: {external_requests}"
            record("no uncaught JavaScript errors or third-party requests")
        finally:
            await context.close()
            await browser.close()
    print(f"\n{len(passed)} browser scenarios passed. Local Chromium evidence only.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default="http://127.0.0.1:3000", help="Running PUBLIC PREVIEW URL")
    parser.add_argument("--protected-url", help="Optional separate protected cockpit URL")
    parser.add_argument("--executable", type=Path, help="Optional local Chromium executable")
    parser.add_argument("--screenshots", type=Path, help="Optional ignored evidence directory")
    parser.add_argument("--axe", type=Path, help="Optional local axe-core/axe.min.js")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
