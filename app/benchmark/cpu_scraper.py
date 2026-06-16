import sys
import os
import logging

logger = logging.getLogger(__name__)
from playwright.sync_api import sync_playwright
from sqlmodel import Session
from sqlalchemy.dialects.postgresql import insert

from app.database import engine
from app.benchmark.model import CPUBenchmark

def run_cpu_list_scraper():
    """
    Scrapes the PassMark CPU list and pushes records directly into the database.
    Can be imported and invoked by a FastAPI router endpoint.
    """
    with sync_playwright() as p:
        logger.info("Launching browser...")
        browser = p.chromium.launch(headless=True)
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        logger.info("Navigating to PassMark...")
        try:
            page.goto("https://www.cpubenchmark.net/cpu-list/all", wait_until="domcontentloaded", timeout=60000)
            
            logger.info("Waiting for data table to render...")
            page.wait_for_selector("#cputable tbody tr", timeout=60000)
            
            logger.info("Extracting CPU Name and Mark...")
            cpu_data = page.evaluate("""
                () => {
                    const rows = document.querySelectorAll("#cputable tbody tr");
                    const data = [];
                    rows.forEach(row => {
                        const cols = row.querySelectorAll("td");
                        if (cols.length >= 2) {
                            const rawScore = cols[1].innerText.replace(/,/g, '').trim();
                            if (rawScore && !isNaN(rawScore)) {
                                data.push({
                                    cpu_name: cols[0].innerText.trim(),
                                    cpu_mark: parseInt(rawScore, 10)
                                });
                            }
                        }
                    });
                    return data;
                }
            """)
            
            logger.info(f"Scraped {len(cpu_data)} items. Committing directly to PostgreSQL Database...")
            
            with Session(engine) as session:
                for item in cpu_data:
                    # PostgreSQL native Upsert (on conflict update score)
                    stmt = insert(CPUBenchmark).values(
                        cpu_name=item["cpu_name"],
                        cpu_mark=item["cpu_mark"]
                    ).on_conflict_do_update(
                        index_elements=['cpu_name'],
                        set_=dict(cpu_mark=item["cpu_mark"])
                    )
                    session.exec(stmt)
                
                session.commit()
                
            logger.info("Success! Sync completed for 'cpu_benchmarks' table.")
            return len(cpu_data)
                
        except Exception as e:
            logger.error(f"Extraction or DB persistence failed: {e}")
            raise e
            
        finally:
            browser.close()