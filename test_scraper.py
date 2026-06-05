import asyncio
from playwright.async_api import async_playwright

async def run_visual_recon():
    print("🚀 Launching visual browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Let's hit a direct product page instead of the search page. 
        # Search pages trigger the bot-defense much faster.
        target_url = "https://shopee.com.my/search?keyword=macbook%20pro"
        print(f"🌐 Navigating to: {target_url}")
        
        await page.goto(target_url)
        print("✅ Page loaded!")
        
        # This will freeze the script and open the Playwright Inspector.
        print("🕵️‍♂️ Script paused! Go to the browser, close the login popup (or log in), and inspect the elements.")
        await page.pause()

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run_visual_recon())