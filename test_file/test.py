from playwright.sync_api import sync_playwright
import json

# Target the actual hidden radio inputs directly, bypassing the UI labels
SECTIONS = [
    {"name": "size",      "selector": "input[name='chassis-dimensionScreensize']"},
    {"name": "color",     "selector": "input[name='chassis-dimensionColor']"},
    {"name": "display",   "selector": "input[name='display-dimensionFinish']"},
    {"name": "chip",      "selector": "input[name='processor-dimensionChip']"},
    {"name": "memory",    "selector": "input[name='memory-dimensionMemory']", "expand": "button[data-autom*='edit-memory']"},
    {"name": "storage",   "selector": "input[name='storage-dimensionCapacity']", "expand": "button[data-autom*='edit-storage']"},
    {"name": "power",     "selector": "input[name='power_adapter-wattage']", "expand": "button[data-autom*='edit-power']"},
    {"name": "trade_in",  "selector": "input[value='noTradeIn']"}, # Strictly force 'No Trade In'
    {"name": "applecare", "selector": "input[name='applecare-options']"} # Iterate through both AC+ options
]

def parse_and_print_payload(json_data):
    """Background listener that parses the payload exactly when it updates."""
    try:
        dimensions = json_data["body"]["selectedKits"]["dimensions"]
        price = json_data["body"]["selectedKits"]["priceData"]["currentPrice"]["amount"]
        size = dimensions.get("chassis-dimensionScreensize", {}).get("dimensionValue", "N/A")
        color = dimensions.get("chassis-dimensionColor", {}).get("dimensionValue", "N/A")
        storage = dimensions.get("storage-dimensionCapacity", {}).get("dimensionValue", "N/A")
        display = dimensions.get("display-dimensionFinish", {}).get("dimensionValue", "N/A")
        memory = dimensions.get("memory-dimensionMemory", {}).get("dimensionValue", "N/A")
        
        chip = "N/A"
        if "processor-dimensionChip-cpuCoreCount-gpuCoreCount" in dimensions:
            chip = dimensions["processor-dimensionChip-cpuCoreCount-gpuCoreCount"]["dimensionValue"]
        elif "processor-dimensionChip" in dimensions:
            chip = dimensions["processor-dimensionChip"]["dimensionValue"]
            
        applecare_status = "No AppleCare+"
        if "applecare" in json_data.get("body", {}).get("selectedKits", {}).get("productConfiguration", {}):
            applecare_status = "AppleCare+ Included"
        
        print(f"✅ --- CAPTURED CONFIGURATION ---")
        print(f"Price:    {price}")
        print(f"Size:     {size.upper()}")
        print(f"Chip:     {chip.upper()}")
        print(f"Memory:   {memory.upper()}")
        print(f"Storage:  {storage.upper()}")
        print(f"Color:    {color.capitalize()}")
        print(f"Display:  {display.capitalize()}")
        print(f"AppleCare: {applecare_status}")
        print("-------------------------------\n")
    except KeyError:
        pass 

def background_network_listener(response):
    if "/api/cto/update-config" in response.url and response.status == 200:
        try:
            json_data = response.json()
            if "priceData" in json_data.get("body", {}).get("selectedKits", {}):
                parse_and_print_payload(json_data)
        except Exception:
            pass

def crawl_tree(page, current_depth=0, current_selection=None):
    if current_selection is None:
        current_selection = {}

    if current_depth == len(SECTIONS):
        return

    section = SECTIONS[current_depth]

    # 🌟 FIX 1: THE REACT REMOUNT WAIT
    # When changing a major component (like the Chip), Apple destroys and rebuilds 
    # the entire Memory/Storage HTML. We MUST wait for the new elements to attach.
    try:
        page.wait_for_selector(section['selector'], state="attached", timeout=3000)
    except Exception:
        pass

    locators = page.locator(section['selector'])
    option_count = locators.count()

    # If it's still 0 after waiting, the options genuinely don't exist
    if option_count == 0:
        crawl_tree(page, current_depth + 1, current_selection)
        return

    snapshot = []
    for i in range(option_count):
        el = locators.nth(i)
        try:
            el.wait_for(state="attached", timeout=2000)
            tag_name = el.evaluate("el => el.tagName.toLowerCase()")
        except Exception:
            continue
            
        if tag_name == "input":
            snapshot.append({
                "type": "input", 
                "target_attr": el.get_attribute("id"), 
                "val": el.get_attribute("value") or f"Option {i}"
            })
        elif tag_name == "button":
            autom = el.get_attribute("data-autom")
            snapshot.append({
                "type": "button", 
                "target_attr": autom, 
                "val": autom.split("_")[-1] if autom and "_" in autom else f"Upgrade {i}"
            })

    for item in snapshot:
        if "expand" in section:
            expand_btn = page.locator(section["expand"]).first
            if expand_btn.is_visible() and expand_btn.get_attribute("aria-expanded") == "false":
                expand_btn.click(force=True)
                page.wait_for_timeout(800) 

        if item["type"] == "input":
            click_target = page.locator(f"label[for='{item['target_attr']}']")
        else:
            click_target = page.locator(f"button[data-autom='{item['target_attr']}']")

        # We gracefully skip without printing error spam if an element is hidden
        if not click_target.is_visible():
            continue

        print(f"[{section['name'].upper()}] Checking option: {item['val']}...")
        click_target.scroll_into_view_if_needed()

        if section['name'] in ["trade_in", "applecare"]:
            click_target.click(force=True) 
            page.wait_for_timeout(400) 
        else:
            try:
                with page.expect_response(lambda res: "/api/cto/update-config" in res.url and res.status == 200, timeout=2500):
                    click_target.click() 
            except Exception:
                pass 

        # 🌟 FIX 2: THE MODAL CATCHER (Restored button.modal-confirm)
        # If we miss the modal, its background dims the screen and blocks all future clicks!
        upgrade_modal = page.locator("button.modal-confirm, button:has-text('Change configuration'), button:has-text('Update')")
        if upgrade_modal.is_visible(timeout=1500):
            print(f"🚫 Option '{item['val']}' triggers an upgrade! Canceling modal and skipping branch...")
            
            # We look for the explicit cancel button, or fall back to the Escape key
            cancel_button = page.locator("button.modal-cancel, button:has-text('Cancel'), button[aria-label='Close']").first
            if cancel_button.is_visible():
                cancel_button.click(force=True)
            else:
                page.keyboard.press("Escape")
            
            page.wait_for_timeout(800)
            continue 

        current_selection[section['name']] = item['val']
        crawl_tree(page, current_depth + 1, current_selection)

def run_matrix_scraper(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={'width': 1300, 'height': 750})
        page = context.new_page()
        
        page.on("response", background_network_listener)
        
        print("🚀 Booting up Ultimate Matrix Scraper...")
        page.goto(url)
        
        try:
            page.wait_for_selector("input[name='chassis-dimensionColor']", timeout=15000)
            print("✅ Configurator found! Starting crawl...\n")
        except Exception:
            print("❌ ERROR: Configurator never appeared.")
            browser.close()
            return
            
        crawl_tree(page)
        
        print("🏁 Tree traversal complete!")
        browser.close()

if __name__ == "__main__":
    target_url = "https://www.apple.com/my/shop/buy-mac/macbook-pro/"
    run_matrix_scraper(target_url)