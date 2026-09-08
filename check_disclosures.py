import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime, time as dtime, timedelta
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

def parse_datetime_input(val: str | None, is_end: bool = False, reference_dt: datetime | None = None) -> datetime | None:
    """
    Parses a user-supplied datetime, date, time-of-day, or relative duration string.
    Supported formats:
      - Relative: '24h', '48h', '2d', '1w'
      - Time-of-day: '09:15', '17:30', '17:30:00' (combined with reference date)
      - Date-only: '2026-09-01', '01-09-2026' (start=00:00:00, end=23:59:59)
      - ISO / standard datetime: '2026-09-01 09:15:00', '2026-09-01T09:15:00'
    """
    if not val or not str(val).strip() or str(val).strip().lower() in ("none", "null", ""):
        return None

    clean_val = str(val).strip()
    ref = reference_dt or datetime.now()

    # Relative duration like "24h", "2d", "1w"
    rel_match = re.match(r"^(\d+)\s*(h|hr|hours?|d|days?|w|weeks?)$", clean_val, re.IGNORECASE)
    if rel_match:
        num = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        if unit.startswith("h"):
            delta = timedelta(hours=num)
        elif unit.startswith("d"):
            delta = timedelta(days=num)
        elif unit.startswith("w"):
            delta = timedelta(weeks=num)
        else:
            delta = timedelta(hours=num)
        return ref - delta

    # Time only: HH:MM or HH:MM:SS
    time_match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", clean_val)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        second = int(time_match.group(3) or 0)
        t = dtime(hour, minute, second)
        return datetime.combine(ref.date(), t)

    # Date only: YYYY-MM-DD or YYYY/MM/DD
    date_match = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", clean_val)
    if date_match:
        y, m, d = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
        dt = datetime(y, m, d)
        return dt.replace(hour=23, minute=59, second=59) if is_end else dt.replace(hour=0, minute=0, second=0)

    # Date only: DD-MM-YYYY or DD/MM/YYYY
    date_match_rev = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$", clean_val)
    if date_match_rev:
        d, m, y = int(date_match_rev.group(1)), int(date_match_rev.group(2)), int(date_match_rev.group(3))
        dt = datetime(y, m, d)
        return dt.replace(hour=23, minute=59, second=59) if is_end else dt.replace(hour=0, minute=0, second=0)

    # Standard ISO / string formats
    iso_clean = clean_val.rstrip("Z")
    try:
        return datetime.fromisoformat(iso_clean)
    except Exception:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ):
        try:
            return datetime.strptime(clean_val, fmt)
        except Exception:
            continue

    return None

def get_time_window(
    cli_start: str | None = None,
    cli_end: str | None = None,
    config: dict | None = None
) -> tuple[datetime, datetime]:
    """
    Calculates the start and end datetimes for filtering disclosures.
    Hierarchy:
      1. CLI arguments (--start, --end)
      2. Environment variables (START_TIME, END_TIME, START_DATETIME, END_DATETIME, START_DATE, END_DATE)
      3. Configuration file (portfolio_stocks.json -> "time_window" object or top-level "start_time"/"end_time")
      4. Default auto-calculation based on scheduled run time and previous day/weekend lookback.
    """
    now = datetime.now()

    # 1. Resolve raw input strings
    raw_start = cli_start
    raw_end = cli_end

    if not raw_start:
        raw_start = os.getenv("START_TIME") or os.getenv("START_DATETIME") or os.getenv("START_DATE")
    if not raw_end:
        raw_end = os.getenv("END_TIME") or os.getenv("END_DATETIME") or os.getenv("END_DATE")

    if config:
        tw = config.get("time_window", {})
        if isinstance(tw, dict):
            if not raw_start:
                raw_start = tw.get("start_time") or tw.get("start_datetime") or tw.get("start_date") or tw.get("start")
            if not raw_end:
                raw_end = tw.get("end_time") or tw.get("end_datetime") or tw.get("end_date") or tw.get("end")
        if not raw_start:
            raw_start = config.get("start_time") or config.get("start_datetime") or config.get("start_date")
        if not raw_end:
            raw_end = config.get("end_time") or config.get("end_datetime") or config.get("end_date")

    # 2. Parse strings into datetime objects
    parsed_end = parse_datetime_input(raw_end, is_end=True, reference_dt=now) if raw_end else None
    ref_for_start = parsed_end or now
    parsed_start = parse_datetime_input(raw_start, is_end=False, reference_dt=ref_for_start) if raw_start else None

    # Handle case where both start and end are time-of-day (e.g., start="17:30", end="17:30" or start="18:00", end="09:00")
    if parsed_start and parsed_end and parsed_start >= parsed_end:
        if re.match(r"^\d{1,2}:\d{2}(?::\d{2})?$", str(raw_start).strip()):
            if parsed_end.weekday() == 0:  # Monday
                parsed_start -= timedelta(days=3)
            else:
                parsed_start -= timedelta(days=1)

    # 3. Apply defaults if either boundary is not specified
    end_dt = parsed_end if parsed_end else now

    if parsed_start:
        start_dt = parsed_start
    else:
        sched_hour, sched_min = get_scheduled_time()
        today_scheduled = end_dt.replace(hour=sched_hour, minute=sched_min, second=0, microsecond=0)

        if end_dt >= today_scheduled:
            last_scheduled = today_scheduled
        else:
            last_scheduled = today_scheduled - timedelta(days=1)

        weekday = last_scheduled.weekday()
        if weekday == 0:  # Monday
            start_dt = last_scheduled - timedelta(days=3)
        elif weekday == 6:  # Sunday
            start_dt = last_scheduled - timedelta(days=2)
        else:
            start_dt = last_scheduled - timedelta(days=1)

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
                    if ann_time and start_dt <= ann_time <= end_dt:
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
        pdf_text = None
        for attempt in range(3):
            try:
                client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
                if pdf_text is None:
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
                
                # Free tier limit is 5 RPM. Wait ~12 seconds to pace it out.
                time.sleep(12)
                
                if response.text:
                    data = json.loads(response.text)
                    return data.get("sentiment", "Neutral"), data.get("rationale", "Analyzed by AI.")
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    console.print(f"[bold yellow][WARN][/bold yellow] Rate limit hit for '{headline}'. Retrying in 30s... (Attempt {attempt+1}/3)")
                    time.sleep(30)
                    continue
                else:
                    console.print(f"[bold red][ERROR][/bold red] Gemini API failed for '{headline}': {e}")
                    time.sleep(2) # Backoff
                    break
        # Fall through to heuristic logic if all retries fail or non-429 error occurs

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
    parser = argparse.ArgumentParser(description="Fetch and analyze BSE corporate disclosures for portfolio stocks.")
    parser.add_argument("--start", "--start-time", dest="start_time", default=None, help="Start time/date/duration (e.g. '09:15', '2026-09-01', '2026-09-01 09:00:00', '24h')")
    parser.add_argument("--end", "--end-time", dest="end_time", default=None, help="End time/date (e.g. '17:30', '2026-09-08', '2026-09-08 17:30:00')")
    parser.add_argument("--config", dest="config_file", default="portfolio_stocks.json", help="Path to portfolio config JSON file (default: portfolio_stocks.json)")
    args = parser.parse_args()

    # Load portfolio stocks
    try:
        with open(args.config_file, "r", encoding="utf-8") as f:
            portfolio = json.load(f)
            stocks = portfolio.get("stocks", [])
    except Exception as e:
        console.print(f"[bold red][ERROR][/bold red] Failed to load {args.config_file}: {e}")
        sys.exit(1)
        
    if not stocks:
        console.print(f"[bold yellow][WARN][/bold yellow] No stocks found in {args.config_file}.")
        sys.exit(0)
        
    all_results = {}
    errors = []
    
    # Determine the time window
    start_dt, end_dt = get_time_window(cli_start=args.start_time, cli_end=args.end_time, config=portfolio)
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
