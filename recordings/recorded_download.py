import asyncio
import re
from playwright.async_api import Playwright, async_playwright, expect


async def run(playwright: Playwright) -> None:
    browser = await playwright.chromium.launch(headless=False)
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto("https://www.echotik.live/login")
    await page.get_by_role("textbox", name="Email").click()
    await page.get_by_role("textbox", name="Email").fill("yr9m6eyds7@xghff.com")
    await page.get_by_role("textbox", name="Password").click()
    await page.get_by_role("textbox", name="Password").fill("aa998877")
    await page.get_by_role("button", name="Login", exact=True).click()
    await page.goto("https://www.echotik.live/board")
    await page.get_by_role("button", name="Start Now").click()
    await page.locator("div:nth-child(3) > .arco-menu-inline-header > .arco-menu-icon-suffix > .arco-icon").click()
    await page.locator("#arco-menu-0-submenu-inline-1").get_by_role("link", name="Hot Promoted").click()
    await page.goto("https://www.echotik.live/products/leaderboard/hot-sell?time_type=daily&time_range=20260305&page=1")
    await page.get_by_role("button").nth(2).click()
    await page.get_by_text("200 Records").click()
    async with page.expect_download() as download_info:
        async with page.expect_popup() as page1_info:
            await page.get_by_role("button", name="Export").click()
        page1 = await page1_info.value
    download = await download_info.value
    await page1.close()
    await page.locator("div:nth-child(4) > .arco-menu-inline-header > .arco-menu-icon-suffix > .arco-icon").click()
    await page.locator("#arco-menu-0-submenu-inline-2").get_by_role("link", name="Best Cross-border Seller").click()
    await page.get_by_role("button").nth(2).click()
    await page.get_by_text("200 Records").click()
    async with page.expect_download() as download1_info:
        async with page.expect_popup() as page2_info:
            await page.get_by_role("button", name="Export").click()
        page2 = await page2_info.value
    download1 = await download1_info.value
    await page2.close()
    await page.close()

    # ---------------------
    await context.close()
    await browser.close()


async def main() -> None:
    async with async_playwright() as playwright:
        await run(playwright)


asyncio.run(main())
