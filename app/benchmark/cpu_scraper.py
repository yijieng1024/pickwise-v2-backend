import json
from playwright.sync_api import sync_playwright

def scrape_cpu_list():
    with sync_playwright() as p:
        print("Launching browser...")
        browser = p.chromium.launch(headless=True)
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        print("Navigating to PassMark...")
        try:
            page.goto("https://www.cpubenchmark.net/cpu-list/all", wait_until="domcontentloaded", timeout=60000)
            
            print("Waiting for data table to render...")
            page.wait_for_selector("#cputable tbody tr", timeout=60000)
            
            print("Extracting CPU Name and Mark...")
            # Run JavaScript inside the browser to map the table rows into a clean dictionary
            cpu_data = page.evaluate("""
                () => {
                    const rows = document.querySelectorAll("#cputable tbody tr");
                    const data = [];
                    rows.forEach(row => {
                        const cols = row.querySelectorAll("td");
                        // Ensure the row actually has data columns
                        if (cols.length >= 2) {
                            data.push({
                                cpu_name: cols[0].innerText.trim(),
                                // Strip commas out of the score (e.g., "4,500" -> "4500")
                                cpu_mark: cols[1].innerText.replace(/,/g, '').trim() 
                            });
                        }
                    });
                    return data;
                }
            """)
            
            # Save the extracted dictionary directly to a JSON file
            with open("cpu_benchmarks_filtered.json", "w", encoding="utf-8") as f:
                json.dump(cpu_data, f, indent=4, ensure_ascii=False)
                
            print(f"\nSuccess! Saved {len(cpu_data)} CPUs to cpu_benchmarks_filtered.json")
                
        except Exception as e:
            print(f"Extraction failed: {e}")
            
        finally:
            browser.close()

if __name__ == "__main__":
    scrape_cpu_list()