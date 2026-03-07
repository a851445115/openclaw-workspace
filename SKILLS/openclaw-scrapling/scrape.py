#!/usr/bin/env python3
"""
Scrapling CLI - Web scraping with anti-bot bypass and adaptive selectors
"""
import argparse
import json
import sys
import os
import io
import shutil
from pathlib import Path
from contextlib import redirect_stderr
from typing import Optional, Dict, Any, List, Callable

REEXEC_ENV_VAR = "OPENCLAW_SCRAPLING_SKIP_VENV_REEXEC"


def preferred_venv_python(skill_dir=None):
    resolved_skill_dir = Path(skill_dir or Path(__file__).resolve().parent).resolve()
    candidate = resolved_skill_dir / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else None


def maybe_reexec_into_venv() -> None:
    if os.environ.get(REEXEC_ENV_VAR) == "1":
        return
    preferred = preferred_venv_python()
    if preferred is None:
        return
    current = Path(sys.executable).resolve()
    if current == preferred.resolve():
        return
    env = os.environ.copy()
    venv_bin = str(preferred.parent)
    current_path = env.get("PATH", "")
    env["PATH"] = f"{venv_bin}:{current_path}" if current_path else venv_bin
    env[REEXEC_ENV_VAR] = "1"
    os.execve(str(preferred), [str(preferred), str(Path(__file__).resolve()), *sys.argv[1:]], env)


maybe_reexec_into_venv()

try:
    from scrapling.fetchers import Fetcher, StealthyFetcher, DynamicFetcher
    from scrapling.fetchers import FetcherSession, StealthySession, DynamicSession
except ImportError:
    print("Error: Scrapling not installed. Run: pip install 'scrapling[all]'", file=sys.stderr)
    sys.exit(1)


SKILL_DIR = Path(__file__).parent
SESSIONS_DIR = SKILL_DIR / "sessions"
CACHE_FILE = SKILL_DIR / "selector_cache.json"

# Create directories
SESSIONS_DIR.mkdir(exist_ok=True)


def is_xpath_selector(selector: str) -> bool:
    """Return True when selector is clearly XPath, otherwise treat it as CSS.

    Plain tag selectors like `title` or `article` are valid CSS and should not
    be routed to XPath. Only explicit XPath forms should use `page.xpath(...)`.
    """
    trimmed = selector.strip()
    return (
        trimmed.startswith("/")
        or trimmed.startswith("./")
        or trimmed.startswith("../")
        or trimmed.startswith("(")
        or trimmed.startswith("ancestor::")
        or trimmed.startswith("descendant::")
        or trimmed.startswith("following::")
        or trimmed.startswith("preceding::")
        or trimmed.startswith("self::")
        or trimmed.startswith("child::")
        or trimmed.startswith("attribute::")
    )


def select_elements(page, selector: str):
    if is_xpath_selector(selector):
        return page.xpath(selector)
    return page.css(selector)


def find_runtime_binary(name: str, skill_dir=None, which_fn: Callable[[str], Optional[str]] = shutil.which):
    resolved_skill_dir = Path(skill_dir or Path(__file__).resolve().parent).resolve()
    local_candidate = resolved_skill_dir / '.venv' / 'bin' / name
    if local_candidate.exists():
        return local_candidate
    current_dir_candidate = Path(sys.argv[0]).resolve().parent / name if sys.argv else None
    if current_dir_candidate and current_dir_candidate.exists():
        return current_dir_candidate
    resolved = which_fn(name)
    return Path(resolved).resolve() if resolved else None


def resolve_solve_cloudflare(
    mode: str,
    solve_cloudflare: bool,
    which_fn: Callable[[str], Optional[str]] = shutil.which,
    skill_dir=None,
) -> bool:
    if mode != "stealth":
        return solve_cloudflare
    if not solve_cloudflare:
        return False
    return find_runtime_binary("camoufox", skill_dir=skill_dir, which_fn=which_fn) is not None


def build_mode_warnings(
    mode: str,
    solve_cloudflare: bool,
    which_fn: Callable[[str], Optional[str]] = shutil.which,
    skill_dir=None,
) -> List[str]:
    warnings: List[str] = []
    if mode == "stealth" and solve_cloudflare and find_runtime_binary("camoufox", skill_dir=skill_dir, which_fn=which_fn) is None:
        warnings.append(
            "camoufox is not installed; Cloudflare challenge-solving may be limited. "
            "Install it with `camoufox fetch`."
        )
    if mode == "dynamic" and find_runtime_binary("playwright", skill_dir=skill_dir, which_fn=which_fn) is None and find_runtime_binary("patchright", skill_dir=skill_dir, which_fn=which_fn) is None:
        warnings.append(
            "Neither `playwright` nor `patchright` CLI is available; dynamic mode may fail "
            "until browser tooling is installed."
        )
    return warnings


def filter_fetch_logs(mode: str, stderr_text: str, fetch_succeeded: bool) -> tuple[str, str]:
    visible: List[str] = []
    suppressed: List[str] = []
    for raw_line in stderr_text.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if fetch_succeeded and mode == "stealth" and "No Cloudflare challenge found" in line:
            suppressed.append(line)
            continue
        visible.append(line)
    return "\n".join(visible).strip(), "\n".join(suppressed).strip()


def run_fetch_with_filtered_logs(mode: str, operation: Callable[[], Any]) -> Any:
    stderr_buffer = io.StringIO()
    try:
        with redirect_stderr(stderr_buffer):
            result = operation()
        visible, _suppressed = filter_fetch_logs(mode, stderr_buffer.getvalue(), True)
        if visible:
            print(visible, file=sys.stderr)
        return result
    except Exception:
        visible, _suppressed = filter_fetch_logs(mode, stderr_buffer.getvalue(), False)
        if visible:
            print(visible, file=sys.stderr)
        raise


def load_selector_cache() -> Dict[str, Any]:
    """Load saved adaptive selectors"""
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_selector_cache(cache: Dict[str, Any]):
    """Save adaptive selectors"""
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)


def scrape(
    url: str,
    selector: Optional[str] = None,
    mode: str = "basic",
    output: Optional[str] = None,
    extract: str = "html",
    fields: Optional[str] = None,
    adaptive: bool = False,
    adaptive_save: bool = False,
    session_name: Optional[str] = None,
    login: bool = False,
    username: Optional[str] = None,
    password: Optional[str] = None,
    proxy: Optional[str] = None,
    headers: Optional[str] = None,
    delay: float = 0,
    wait_for: Optional[str] = None,
    screenshot: Optional[str] = None,
    headless: bool = True,
    solve_cloudflare: bool = True,
    network_idle: bool = False,
) -> Any:
    """
    Main scraping function
    
    Args:
        url: Target URL
        selector: CSS/XPath selector for elements
        mode: Fetcher type (basic/stealth/dynamic)
        output: Output file path
        extract: What to extract (html/text/markdown/attr:name)
        fields: Comma-separated field definitions (name:selector,...)
        adaptive: Use adaptive selector matching
        adaptive_save: Save selector pattern for future adaptive use
        session_name: Name for session persistence
        login: Perform login before scraping
        username: Login username
        password: Login password
        proxy: Proxy URL
        headers: JSON string of custom headers
        delay: Delay between requests
        wait_for: Selector to wait for (dynamic mode)
        screenshot: Save screenshot path
        headless: Run browser headless
        solve_cloudflare: Auto-solve Cloudflare challenges
        network_idle: Wait for network idle
    """
    
    # Parse headers
    custom_headers = json.loads(headers) if headers else None

    effective_solve_cloudflare = resolve_solve_cloudflare(mode, solve_cloudflare)

    for warning in build_mode_warnings(mode, solve_cloudflare):
        print(f"Warning: {warning}", file=sys.stderr)
    
    # Load adaptive cache if needed
    cache = load_selector_cache() if (adaptive or adaptive_save) else {}
    cache_key = f"{url}::{selector}"
    
    # Choose fetcher/session based on mode
    if session_name:
        session_path = SESSIONS_DIR / f"{session_name}.session"
        
        if mode == "stealth":
            session = StealthySession(
                headless=headless,
                solve_cloudflare=effective_solve_cloudflare
            )
            # TODO: Load saved session if exists
        elif mode == "dynamic":
            session = DynamicSession(
                headless=headless,
                network_idle=network_idle
            )
        else:
            session = FetcherSession()
        
        # Login if needed
        if login and username and password:
            print(f"Logging in as {username}...", file=sys.stderr)
            login_page = run_fetch_with_filtered_logs(mode, lambda: session.fetch(url))
            # This is simplified - real login needs form field detection
            login_page.fill('input[name="username"], input[name="email"], #username, #email', username)
            login_page.fill('input[name="password"], #password', password)
            login_page.click('button[type="submit"], input[type="submit"]')
        
        # Fetch page
        page = run_fetch_with_filtered_logs(mode, lambda: session.fetch(url))
        
    else:
        # One-off request
        if mode == "stealth":
            page = run_fetch_with_filtered_logs(
                mode,
                lambda: StealthyFetcher.fetch(
                    url,
                    headless=headless,
                    solve_cloudflare=effective_solve_cloudflare,
                    proxy=proxy
                ),
            )
        elif mode == "dynamic":
            page = run_fetch_with_filtered_logs(
                mode,
                lambda: DynamicFetcher.fetch(
                    url,
                    headless=headless,
                    network_idle=network_idle,
                    proxy=proxy
                ),
            )
        else:
            page = run_fetch_with_filtered_logs(mode, lambda: Fetcher.get(url, proxy=proxy, headers=custom_headers))
    
    # Take screenshot if requested
    if screenshot:
        if hasattr(page, 'screenshot'):
            page.screenshot(screenshot)
            print(f"Screenshot saved: {screenshot}", file=sys.stderr)
    
    # Wait for element if specified
    if wait_for and mode in ("stealth", "dynamic"):
        print(f"Waiting for element: {wait_for}", file=sys.stderr)
        # Scrapling handles this automatically in fetch()
    
    # Extract data
    if not selector:
        # No selector - return full page
        if extract == "text":
            data = page.text
        elif extract == "markdown":
            data = page.markdown if hasattr(page, 'markdown') else page.text
        else:
            data = page.html
    else:
        # Use adaptive selector if enabled
        if adaptive and cache_key in cache:
            print(f"Using adaptive selector from cache...", file=sys.stderr)
            elements = page.css(selector, adaptive=True)
        else:
            # Regular selector: default to CSS unless the selector is clearly XPath.
            elements = select_elements(page, selector)
        
        # Save adaptive pattern if requested
        if adaptive_save:
            print(f"Saving adaptive selector pattern...", file=sys.stderr)
            elements = page.css(selector, auto_save=True)
            cache[cache_key] = {"selector": selector, "url": url}
            save_selector_cache(cache)
        
        # Extract based on type
        if fields:
            # Multiple fields extraction
            field_defs = {}
            for field_def in fields.split(','):
                name, field_selector = field_def.split(':', 1)  # Split only on first colon
                field_defs[name] = field_selector
            
            data = []
            for elem in elements:
                item = {}
                for name, field_selector in field_defs.items():
                    item[name] = elem.css(field_selector).get() if elem.css(field_selector) else None
                data.append(item)
        
        elif extract.startswith("attr:"):
            # Extract attribute
            attr_name = extract.split(":", 1)[1]
            data = [elem.attrib.get(attr_name) for elem in elements if hasattr(elem, 'attrib')]
        
        elif extract == "text":
            data = [elem.text for elem in elements]
        
        elif extract == "markdown":
            data = [elem.markdown if hasattr(elem, 'markdown') else elem.text for elem in elements]
        
        else:
            # HTML
            data = [elem.html if hasattr(elem, 'html') else str(elem) for elem in elements]
    
    # Output
    if output:
        output_path = Path(output)
        
        if output_path.suffix == '.json':
            with open(output, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"Saved to {output}", file=sys.stderr)
        
        elif output_path.suffix == '.jsonl':
            with open(output, 'w') as f:
                if isinstance(data, list):
                    for item in data:
                        f.write(json.dumps(item, ensure_ascii=False) + '\n')
                else:
                    f.write(json.dumps(data, ensure_ascii=False) + '\n')
            print(f"Saved to {output}", file=sys.stderr)
        
        elif output_path.suffix == '.csv':
            import csv
            if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
                with open(output, 'w', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=data[0].keys())
                    writer.writeheader()
                    writer.writerows(data)
                print(f"Saved to {output}", file=sys.stderr)
            else:
                print("Error: CSV output requires structured data (use --fields)", file=sys.stderr)
                return None
        
        else:
            # Text-based output
            with open(output, 'w') as f:
                if isinstance(data, list):
                    f.write('\n'.join(str(item) for item in data))
                else:
                    f.write(str(data))
            print(f"Saved to {output}", file=sys.stderr)
    
    else:
        # Print to stdout
        if isinstance(data, (list, dict)):
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print(data)
    
    return data


def main():
    parser = argparse.ArgumentParser(
        description="Scrapling CLI - Adaptive web scraping with anti-bot bypass",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic scraping
  python scrape.py --url "https://example.com" --selector ".product"
  
  # Stealth mode (bypass Cloudflare)
  python scrape.py --url "https://site.com" --stealth --selector ".content"
  
  # Dynamic content (JavaScript)
  python scrape.py --url "https://spa.com" --dynamic --selector ".item"
  
  # Adaptive selectors
  python scrape.py --url "https://site.com" --selector ".product" --adaptive-save
  python scrape.py --url "https://site.com" --adaptive
  
  # Extract structured data
  python scrape.py --url "https://site.com" --selector ".product" \\
    --fields "title:.title::text,price:.price::text,link:a::attr(href)"
  
  # With login
  python scrape.py --url "https://site.com/data" --stealth --login \\
    --username "user@example.com" --password "secret" \\
    --session-name "my-session" --selector ".data"
"""
    )
    
    # Required
    parser.add_argument('--url', required=True, help='Target URL to scrape')
    
    # Selector
    parser.add_argument('--selector', '-s', help='CSS or XPath selector')
    
    # Mode
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument('--basic', action='store_const', const='basic', dest='mode',
                           help='Basic HTTP fetcher (default, fast)')
    mode_group.add_argument('--stealth', action='store_const', const='stealth', dest='mode',
                           help='Stealth mode (bypass anti-bot, Cloudflare)')
    mode_group.add_argument('--dynamic', action='store_const', const='dynamic', dest='mode',
                           help='Dynamic mode (full browser, JavaScript support)')
    parser.set_defaults(mode='basic')
    
    # Output
    parser.add_argument('--output', '-o', help='Output file (json/jsonl/csv/txt/md/html)')
    parser.add_argument('--extract', default='html',
                       help='What to extract: html, text, markdown, attr:name')
    parser.add_argument('--fields', help='Structured extraction: "name:selector,price:.price::text"')
    
    # Adaptive
    parser.add_argument('--adaptive', action='store_true',
                       help='Use adaptive selector (auto-relocate if page changed)')
    parser.add_argument('--adaptive-save', action='store_true',
                       help='Save selector pattern for future adaptive use')
    
    # Session
    parser.add_argument('--session-name', help='Session name for persistence')
    parser.add_argument('--login', action='store_true', help='Perform login')
    parser.add_argument('--username', help='Login username')
    parser.add_argument('--password', help='Login password')
    
    # Network
    parser.add_argument('--proxy', help='Proxy URL (http://user:pass@host:port)')
    parser.add_argument('--headers', help='Custom headers (JSON string)')
    parser.add_argument('--delay', type=float, default=0, help='Delay between requests (seconds)')
    
    # Browser options
    parser.add_argument('--wait-for', help='Wait for selector to appear (dynamic mode)')
    parser.add_argument('--screenshot', help='Save screenshot to path')
    parser.add_argument('--headless', type=lambda x: x.lower() != 'false', default=True,
                       help='Run browser headless (default: true)')
    parser.add_argument('--solve-cloudflare', type=lambda x: x.lower() != 'false', default=True,
                       help='Auto-solve Cloudflare (default: true)')
    parser.add_argument('--network-idle', action='store_true',
                       help='Wait for network idle (dynamic mode)')
    
    args = parser.parse_args()
    
    try:
        scrape(
            url=args.url,
            selector=args.selector,
            mode=args.mode,
            output=args.output,
            extract=args.extract,
            fields=args.fields,
            adaptive=args.adaptive,
            adaptive_save=args.adaptive_save,
            session_name=args.session_name,
            login=args.login,
            username=args.username,
            password=args.password,
            proxy=args.proxy,
            headers=args.headers,
            delay=args.delay,
            wait_for=args.wait_for,
            screenshot=args.screenshot,
            headless=args.headless,
            solve_cloudflare=args.solve_cloudflare,
            network_idle=args.network_idle,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
