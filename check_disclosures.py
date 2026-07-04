import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
import requests
from rich.console import Console
from rich.table import Table

try:
    from google import genai
    from google.genai import types
    from pydantic import BaseModel
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

console = Console()

class SentimentResponse(BaseModel):
    sentiment: str
    rationale: str


def get_scheduled_time() -> tuple[int, int]:
    """
    Reads the scheduled run time (hour, minute) from environment variables or sidecar.json.
    Defaults to (17, 30) if not found or unparseable.
    """
    env_hour = os.getenv("SCHEDULED_HOUR")
    env_min = os.getenv("SCHEDULED_MIN")
    if env_hour is not None and env_min is not None:
        try:
            return int(env_hour), int(env_min)
        except ValueError:
            pass

    path = os.path.expanduser("~/.gemini/config/sidecars/my-portfolio-disclosures/sidecar.json")
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                data = json.load(f)
                args = data.get("args", [])
                if args:
                    cron_expr = args[0]
                    parts = cron_expr.split()
                    if len(parts) >= 2:
                        minute = int(parts[0])
                        hour = int(parts[1])
                        return hour, minute
    except Exception:
        pass
    return 17, 30  # Default fallback

def get_time_window() -> tuple[datetime, datetime]:
    """
    Calculates the start and end datetimes for filtering disclosures.
    - Monday's run: from Friday's scheduled completion to Monday's start.
    - Other days' runs: from the previous day's scheduled completion to the current start.
    """
    sched_hour, sched_min = get_scheduled_time()
    now = datetime.now()
    today_scheduled = now.replace(hour=sched_hour, minute=sched_min, second=0, microsecond=0)
    
    if now >= today_scheduled:
        last_scheduled = today_scheduled
    else:
        last_scheduled = today_scheduled - timedelta(days=1)
        
    weekday = last_scheduled.weekday()
    if weekday == 0:  # Monday
        # Previous scheduled task was Friday's run (3 days ago)
        start_dt = last_scheduled - timedelta(days=3)
    elif weekday == 6:  # Sunday
        # Previous scheduled task was Friday's run (2 days ago)
        start_dt = last_scheduled - timedelta(days=2)
    else:
        # Previous scheduled task was the day before
        start_dt = last_scheduled - timedelta(days=1)
        
    end_dt = now
    return start_dt, end_dt

def parse_announcement_time(dt_str: str) -> datetime | None:
    """
    Safely parses an announcement timestamp string into a datetime object.
    """
    if not dt_str or dt_str == "N/A":
        return None
    clean_str = dt_str.strip()
    if clean_str.endswith("Z"):
        clean_str = clean_str[:-1]
    try:
        return datetime.fromisoformat(clean_str)
    except Exception:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(clean_str, fmt)
            except Exception:
                continue
    return None


def get_bse_scrip_code(symbol: str) -> str:
    """
    Resolves the stock symbol to its BSE Scrip Code.
    """
    console.print(f"[bold blue][INFO][/bold blue] Resolving BSE Scrip Code for {symbol}...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bseindia.com/"
    }
    
    # Try different search terms starting from full symbol to shortened versions
    search_queries = [symbol]
    
    # Generate fallbacks by stripping common suffixes
    cleaned = symbol
    for suffix in [r"\bPassenger Vehicles\b", r"\bGreen Energy\b", r"\bInfrastructure Investment Trust\b", r"\bLtd\b", r"\bLimited\b", r"\bIndia\b"]:
        cleaned = re.sub(suffix, "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = " ".join(cleaned.split())
        if cleaned and cleaned not in search_queries:
            search_queries.append(cleaned)
            
    # Try each search query
    for query in search_queries:
        url = f"https://api.bseindia.com/Msource/1D/getQouteSearch.aspx?Type=EQ&text={query}&flag=site"
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                if "No Match Found" in response.text:
                    continue
                
                # Try finding a 6-digit number
                match = re.search(r"\b\d{6}\b", response.text)
                if match:
                    scrip = match.group(0)
                    console.print(f"[bold green][SUCCESS][/bold green] Resolved '{symbol}' to Scrip Code: {scrip} (via '{query}')")
                    return scrip
        except Exception as e:
            console.print(f"[bold red][ERROR][/bold red] Exception resolving '{query}': {e}")
            
    console.print(f"[bold red][ERROR][/bold red] Could not resolve symbol '{symbol}'")
    return None

def fetch_announcements(scrip_code: str, start_dt: datetime, end_dt: datetime, errors: list) -> list:
    """
    Fetches the corporate announcements for the given BSE Scrip Code
    between start_dt and end_dt.
    """
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bseindia.com/"
    }
    
    params = {
        "pageno": 1,
        "strCat": "-1",
        "strPrevDate": start_dt.strftime("%Y%m%d"),
        "strToDate": end_dt.strftime("%Y%m%d"),
        "strScrip": scrip_code,
        "strSearch": "P",
        "strType": "C",
        "subcategory": ""
    }
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if response.status_code == 200:
            data = response.json()
            raw_announcements = data.get("Table", [])
            
            # Filter announcements by time
            filtered_announcements = []
            for ann in raw_announcements:
                dt_str = ann.get("News_submission_dt") or ann.get("NEWS_DT")
                if dt_str:
                    ann_time = parse_announcement_time(dt_str)
                    if ann_time and start_dt < ann_time <= end_dt:
                        filtered_announcements.append(ann)
            return filtered_announcements
        else:
            err_msg = f"Failed to fetch announcements for {scrip_code} (HTTP {response.status_code})"
            console.print(f"[bold red][ERROR][/bold red] {err_msg}")
            errors.append(err_msg)
    except Exception as e:
        err_msg = f"Exception fetching announcements for {scrip_code}: {e}"
        console.print(f"[bold red][ERROR][/bold red] {err_msg}")
        errors.append(err_msg)
    return []

def extract_pdf_text(pdf_link: str, max_pages: int = 10) -> str:
    """Downloads the PDF and extracts text from the first max_pages."""
    if not pdf_link or pdf_link == "N/A":
        return ""
    try:
        response = requests.get(pdf_link, timeout=15)
        if response.status_code == 200:
            from pypdf import PdfReader
            pdf_file = io.BytesIO(response.content)
            reader = PdfReader(pdf_file)
            text_parts = []
            num_pages = min(len(reader.pages), max_pages)
            for i in range(num_pages):
                page_text = reader.pages[i].extract_text()
                if page_text:
                    text_parts.append(page_text)
            return "\n".join(text_parts)
    except Exception as e:
        console.print(f"[bold yellow][WARN][/bold yellow] Could not extract text from PDF {pdf_link}: {e}")
    return ""

def classify_sentiment(category: str, headline: str, pdf_link: str = "N/A") -> tuple[str, str]:
    """
    Classifies the sentiment of the announcement based on the category and headline.
    If Gemini is available, uses the LLM to analyze the PDF contents (or headline).
    Otherwise, falls back to simple heuristic matching.
    """
    cat = category.lower()
    hl = headline.lower()
    
    # Optional LLM logic
    if HAS_GENAI and os.getenv("GEMINI_API_KEY"):
        try:
            client = genai.Client()
            pdf_text = extract_pdf_text(pdf_link)
            prompt = f"You are an expert financial analyst. Determine the sentiment impact on the stock of the following corporate disclosure. You must categorize it strictly as 'Positive', 'Negative', 'Slightly Positive', or 'Neutral'. Provide a single-sentence rationale.\n\nHeadline: {headline}\nCategory: {category}\n\n"
            if pdf_text:
                prompt += f"Document Snippet:\n{pdf_text[:15000]}" # Limiting token usage roughly
                
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=SentimentResponse,
                    temperature=0.1,
                ),
            )
            # Add a small delay to avoid rate limits
            time.sleep(1)
            
            if response.text:
                data = json.loads(response.text)
                return data.get("sentiment", "Neutral"), data.get("rationale", "Analyzed by AI.")
        except Exception as e:
            console.print(f"[bold red][ERROR][/bold red] Gemini API failed for '{headline}': {e}")
            time.sleep(2) # Backoff
            # Fall through to heuristic logic

    # Fallback Heuristic Logic
    sentiment = "Neutral"
    rationale = "Routine administrative or corporate announcement."
    
    if "esop" in hl or "rsu" in hl or "allotment of shares" in hl or "allotment of equity shares" in hl:
        sentiment = "Neutral"
        rationale = "Routine equity allotment under employee stock option scheme; minor dilution but standard compensation practice."
    elif "director" in hl or "directorate" in hl or "appointment" in hl:
        if "resign" in hl or "cessation" in hl or "completion" in hl:
            sentiment = "Neutral"
            rationale = "Resignation or end of director tenure, part of normal governance transitions."
        else:
            sentiment = "Neutral to slightly Positive"
            rationale = "Board appointment or election ensures governance continuity."
    elif "tenure completion" in hl or "retirement" in hl:
        sentiment = "Neutral"
        rationale = "Standard completion of tenure or retirement, representing routine corporate changes."
    elif "investor meet" in hl or "analyst" in hl or "conference call" in hl or "clsa" in hl:
        sentiment = "Neutral"
        rationale = "Routine investor relations update with no immediate price-sensitive information."
    elif "loss of share" in hl or "share certificates" in hl:
        sentiment = "Neutral"
        rationale = "Administrative notice regarding loss/issue of duplicate share certificates."
    elif "acquisition" in hl:
        sentiment = "Positive"
        rationale = "Strategic acquisition indicating business expansion or partnership."
    elif "rating" in hl or "credit rating" in hl:
        if "downgrade" in hl:
            sentiment = "Negative"
            rationale = "Credit rating downgrade indicating decreased creditworthiness."
        else:
            sentiment = "Positive"
            rationale = "Credit rating update indicating creditworthiness."
    elif "penalty" in hl or "fine" in hl or "default" in hl or "warning" in hl or "fraud" in hl:
        sentiment = "Negative"
        rationale = "Negative corporate event, regulatory penalty, or financial default."
        
    return sentiment, rationale

def main():
    # Load portfolio stocks
    try:
        with open("portfolio_stocks.json", "r") as f:
            portfolio = json.load(f)
            stocks = portfolio.get("stocks", [])
    except Exception as e:
        console.print(f"[bold red][ERROR][/bold red] Failed to load portfolio_stocks.json: {e}")
        sys.exit(1)
        
    if not stocks:
        console.print("[bold yellow][WARN][/bold yellow] No stocks found in portfolio_stocks.json.")
        sys.exit(0)
        
    all_results = {}
    errors = []
    
    # Determine the time window
    start_dt, end_dt = get_time_window()
    console.print(f"[bold blue][INFO][/bold blue] Fetching disclosures from [yellow]{start_dt.strftime('%Y-%m-%d %H:%M:%S')}[/yellow] to [yellow]{end_dt.strftime('%Y-%m-%d %H:%M:%S')}[/yellow] (Local Time)")
    
    for symbol in stocks:
        scrip = get_bse_scrip_code(symbol)
        if scrip:
            announcements = fetch_announcements(scrip, start_dt, end_dt, errors)
            all_results[symbol] = announcements
        else:
            all_results[symbol] = []
            errors.append(f"Could not resolve BSE Scrip Code for symbol '{symbol}'")
        # Polite delay to avoid hitting rate limits
        time.sleep(2)
        
    # Calculate overall sentiment for the heatmap
    stock_sentiment = {}
    for symbol, announcements in all_results.items():
        if not announcements:
            stock_sentiment[symbol] = "None"
        else:
            sentiments = []
            for ann in announcements:
                category = ann.get("CATEGORYNAME") or "N/A"
                headline = ann.get("HEADLINE") or ann.get("NEWSSUB") or "N/A"
                
                pdf_file = ann.get("ATTACHMENTNAME")
                pdf_link = "N/A"
                if pdf_file:
                    pdf_link = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{pdf_file}"
                
                sentiment, _ = classify_sentiment(category, headline, pdf_link)
                sentiments.append(sentiment)
            
            if any("Negative" in s for s in sentiments):
                stock_sentiment[symbol] = "Negative"
            elif any("Positive" in s and "Neutral to slightly Positive" not in s for s in sentiments):
                stock_sentiment[symbol] = "Positive"
            elif any("Neutral to slightly Positive" in s for s in sentiments):
                stock_sentiment[symbol] = "Slightly Positive"
            else:
                stock_sentiment[symbol] = "Neutral"

    # Print results to console and generate Markdown report
    status_str = "Error" if errors else "Success"
    report_lines = [
        "# Latest Corporate Disclosures & Regulatory Filings",
        f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {status_str}",
        f"Query Period (Local Time): {start_dt.strftime('%Y-%m-%d %H:%M:%S')} to {end_dt.strftime('%Y-%m-%d %H:%M:%S')}",
        ""
    ]
    
    # Generate Heatmap
    report_lines.append("## Portfolio Heatmap")
    report_lines.append("> 🔴 Negative | 🟢 Positive | 🔵 Slightly Positive | 🟡 Neutral")
    report_lines.append("")
    
    # Sort symbols by sentiment precedence
    sentiment_order = {"Negative": 1, "Positive": 2, "Slightly Positive": 3, "Neutral": 4, "None": 5}
    sorted_stocks = sorted(stock_sentiment.items(), key=lambda item: (sentiment_order[item[1]], item[0]))
    
    heatmap_parts = []
    for symbol, overall_sent in sorted_stocks:
        if overall_sent == "Negative":
            icon = "🔴"
        elif overall_sent == "Positive":
            icon = "🟢"
        elif overall_sent == "Slightly Positive":
            icon = "🔵"
        elif overall_sent == "Neutral":
            icon = "🟡"
        else:
            continue  # Skip "None" entirely

        anchor = symbol.lower().replace(" ", "-")
        heatmap_parts.append(f"{icon} [{symbol}](#{anchor})")
        
    # Group heatmap parts to form a grid, or just space them out
    report_lines.append(" | ".join(heatmap_parts))
    report_lines.append("")
    report_lines.append("---")
    report_lines.append("")
    
    main_table = Table(title="Latest Filings for Portfolio")
    main_table.add_column("Company", style="cyan", no_wrap=True)
    main_table.add_column("Date", style="cyan", no_wrap=True)
    main_table.add_column("Time", style="cyan", no_wrap=True)
    main_table.add_column("Category", style="green")
    main_table.add_column("Headline", style="magenta")
    main_table.add_column("Attachment (PDF)", style="blue")
    main_table.add_column("Sentiment", style="yellow")
    main_table.add_column("Rationale", style="white")

    report_lines.append("## All Disclosures")
    report_lines.append("| Company | Date | Time | Category | Headline | PDF Link | Sentiment | Rationale |")
    report_lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")

    has_any_announcements = False
    
    for symbol, announcements in all_results.items():
        if not announcements:
            continue
            
        has_any_announcements = True
        anchor = symbol.lower().replace(" ", "-")
        
        for i, ann in enumerate(announcements[:10]):  # Limit to top 10 for readability
            dt = ann.get("News_submission_dt") or ann.get("NEWS_DT") or "N/A"
            
            # Split Date and Time
            normalized_dt = dt.replace('T', ' ').strip()
            dt_parts = normalized_dt.split(' ')
            if len(dt_parts) == 2:
                date_part, time_part = dt_parts[0], dt_parts[1]
                if '.' in time_part:
                    time_part = time_part.split('.')[0]
            else:
                date_part = dt
                time_part = "N/A"

            category = ann.get("CATEGORYNAME") or "N/A"
            headline = ann.get("HEADLINE") or ann.get("NEWSSUB") or "N/A"
            pdf_file = ann.get("ATTACHMENTNAME")
            
            pdf_link = "N/A"
            if pdf_file:
                pdf_link = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{pdf_file}"
                pdf_display = f"[link={pdf_link}]Download PDF[/link]"
                markdown_pdf_link = f"[Download PDF]({pdf_link})"
            else:
                pdf_display = "N/A"
                markdown_pdf_link = "N/A"
            
            sentiment, rationale = classify_sentiment(category, headline, pdf_link)
            
            main_table.add_row(symbol if i == 0 else "", date_part, time_part, category, headline, pdf_display, sentiment, rationale)
            
            display_sym = f"<a name=\"{anchor}\"></a>**{symbol}**" if i == 0 else f"**{symbol}**"
            report_lines.append(f"| {display_sym} | {date_part} | {time_part} | {category} | {headline} | {markdown_pdf_link} | {sentiment} | {rationale} |")

    if not has_any_announcements:
        main_table.add_row("No announcements found in the query period.", "", "", "", "", "", "", "")
        report_lines.append("| No announcements found in the query period. | | | | | | | |")
        
    report_lines.append("")
    console.print(main_table)
    console.print()
        
    # Append Errors & Warnings section if any occurred
    if errors:
        report_lines.append("## Errors & Warnings")
        for err in errors:
            report_lines.append(f"- [WARN] {err}")
        report_lines.append("")
        
    # Write to both latest_disclosures.md and a dynamically named date log file
    now_dt = datetime.now()
    year_str = now_dt.strftime("%Y")
    month_str = now_dt.strftime("%m")
    date_str = now_dt.strftime("%d-%m-%Y")
    
    out_dir = os.path.join(year_str, month_str)
    os.makedirs(out_dir, exist_ok=True)
    log_filename = os.path.join(out_dir, f"Portfolio_Disclosure_{date_str}.md")
    
    for filename in ["latest_disclosures.md", log_filename]:
        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write("\n".join(report_lines))
            console.print(f"[bold green][SUCCESS][/bold green] Generated report: {filename}")
        except Exception as e:
            console.print(f"[bold red][ERROR][/bold red] Failed to write report file {filename}: {e}")

if __name__ == "__main__":
    main()
