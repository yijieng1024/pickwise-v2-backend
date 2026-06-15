import json
from playwright.sync_api import sync_playwright

def scrape_gpu_list():
    with sync_playwright() as p:
        print("Launching browser...")
        browser = p.chromium.launch(headless=True)
        
        # Spoofing the User-Agent to keep Cloudflare happy
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        print("Navigating to PassMark GPU list...")
        try:
            # Target the Video Card URL
            page.goto("https://www.videocardbenchmark.net/gpu_list.php", wait_until="domcontentloaded", timeout=60000)
            
            print("Waiting for data table to render...")
            page.wait_for_selector("#cputable tbody tr", timeout=60000)
            
            print("Extracting GPU Name and G3D Mark...")
            # Run JavaScript inside the browser to grab columns 0 and 1
            gpu_data = page.evaluate("""
                () => {
                    const rows = document.querySelectorAll("#cputable tbody tr");
                    const data = [];
                    rows.forEach(row => {
                        const cols = row.querySelectorAll("td");
                        if (cols.length >= 2) {
                            data.push({
                                gpu_name: cols[0].innerText.trim(),
                                // Strip commas from the score so you can store it as an INT in your database
                                gpu_mark: cols[1].innerText.replace(/,/g, '').trim() 
                            });
                        }
                    });
                    return data;
                }
            """)
            
            # Save to JSON
            with open("gpu_benchmarks_filtered.json", "w", encoding="utf-8") as f:
                json.dump(gpu_data, f, indent=4, ensure_ascii=False)
                
            print(f"\nSuccess! Saved {len(gpu_data)} GPUs to gpu_benchmarks_filtered.json")
                
        except Exception as e:
            print(f"Extraction failed: {e}")
            
        finally:
            browser.close()

if __name__ == "__main__":
    scrape_gpu_list()