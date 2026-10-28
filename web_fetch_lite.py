#!/usr/bin/env python3
"""
web_fetch_lite.py - Lightweight, secure web fetching tool using httpx.
Domain-whitelisted for safety per the invoked skill. No extra deps beyond httpx, json, etc.
Implements manual TTL cache, retry with exponential backoff. Created as v2 pattern.
"""
import os
import time
import json
import logging
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional
import httpx

logger = logging.getLogger(__name__)

# Whitelisted domains (expanded from skill)
WHITELISTED_DOMAINS = {
    "polymarket.com", "api.polymarket.com", "clob.polymarket.com", 
    "ws-subscriptions-clob.polymarket.com",
    "api.open-meteo.com", "aviationweather.gov", "api.telegram.org",
    "openrouter.ai", "api.x.ai", "api.honcho.dev", "wttr.in", "weather.gov",
}

# Simple manual cache (no cachetools dep)
CACHE_TTL = 6 * 3600  # 6 hours
memory_cache: Dict[str, Dict] = {}
CACHE_FILE = Path("/root/weatherbot/web_fetch_cache.json")

def load_disk_cache() -> Dict:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
                # Filter expired
                now = time.time()
                return {k: v for k, v in data.items() if v.get("expires", 0) > now}
        except Exception as e:
            logger.warning(f"Failed to load disk cache: {e}")
    return {}

def save_disk_cache(cache_data: Dict):
    try:
        # Add expiration
        now = time.time()
        for v in cache_data.values():
            if "expires" not in v:
                v["expires"] = now + CACHE_TTL
        with open(CACHE_FILE, "w") as f:
            json.dump(cache_data, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Failed to save cache: {e}")

disk_cache = load_disk_cache()

def is_whitelisted(url: str) -> bool:
    """Check if URL domain is whitelisted."""
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower().split(':')[0]
        for allowed in WHITELISTED_DOMAINS:
            if domain.endswith(allowed) or allowed in domain:
                return True
        logger.warning(f"Blocked non-whitelisted domain: {domain}")
        return False
    except Exception:
        return False

def web_fetch(url: str, params: Optional[Dict] = None, method: str = "GET", 
              timeout: int = 10, use_cache: bool = True, **kwargs) -> Dict[str, Any]:
    """
    Core function per web-fetch-lite skill: whitelisted, cached, backoff, error handling.
    Falls back gracefully. Returns clean structured data.
    """
    if not is_whitelisted(url):
        return {"success": False, "error": f"Domain not whitelisted: {url}", "status_code": 0}
    
    # Generate cache key
    cache_key = hashlib.md5(f"{url}:{str(sorted(params.items()) if params else '')}".encode()).hexdigest()
    
    now = time.time()
    if use_cache:
        # Check memory
        if cache_key in memory_cache and memory_cache[cache_key].get("expires", 0) > now:
            logger.info(f"Memory cache hit for {url}")
            return memory_cache[cache_key]
        # Check disk
        if cache_key in disk_cache and disk_cache[cache_key].get("expires", 0) > now:
            logger.info(f"Disk cache hit for {url}")
            memory_cache[cache_key] = disk_cache[cache_key]
            return disk_cache[cache_key]
    
    backoff = 2.0
    last_error = "Unknown error"
    for attempt in range(4):
        try:
            headers = kwargs.pop("headers", {"User-Agent": "WeatherBot-Monitor-Lite/2.0"})
            client = httpx.Client(timeout=timeout)
            resp = client.request(method, url, params=params or None, headers=headers, **kwargs)
            client.close()
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError:
                data = {"content": resp.text[:1000], "status_code": resp.status_code}
            
            result = {
                "success": True,
                "data": data,
                "status_code": resp.status_code,
                "timestamp": datetime.now().isoformat(),
                "url": url,
                "expires": now + CACHE_TTL
            }
            
            if use_cache:
                memory_cache[cache_key] = result
                disk_cache[cache_key] = result
                save_disk_cache(disk_cache)
            
            return result
        except httpx.HTTPStatusError as e:
            last_error = f"HTTP {e.response.status_code}: {str(e)}"
            if e.response.status_code == 429:
                wait_time = backoff * (2 ** attempt)
                logger.warning(f"Rate limit (429) on {url}. Backing off {wait_time:.1f}s (attempt {attempt+1})")
                time.sleep(wait_time)
                continue
            break
        except (httpx.RequestError, Exception) as e:
            last_error = str(e)
            logger.warning(f"Attempt {attempt+1} failed for {url}: {last_error}")
            time.sleep(backoff * 0.5)
            backoff *= 1.5
            continue
    
    result = {
        "success": False,
        "error": last_error,
        "status_code": 0,
        "timestamp": datetime.now().isoformat(),
        "url": url,
        "expires": now + 300  # short cache for errors
    }
    if use_cache:
        memory_cache[cache_key] = result
        disk_cache[cache_key] = result
        save_disk_cache(disk_cache)
    return result

def extract_content(html_content: str) -> str:
    """Fallback content extractor (trafilatura preferred but graceful degrade)."""
    try:
        # Try beautifulsoup if available
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, "html.parser")
        text = soup.get_text(separator="\\n", strip=True)
        return text[:2000]
    except ImportError:
        return html_content[:1500] if isinstance(html_content, str) else str(html_content)

if __name__ == "__main__":
    # Test with Open-Meteo (should hit cache after first)
    test_result = web_fetch(
        "https://api.open-meteo.com/v1/forecast", 
        params={"latitude": 52.52, "longitude": 13.41, "current_weather": "true"},
        use_cache=True
    )
    print("Test result summary:")
    print(json.dumps({k: v for k,v in test_result.items() if k != "data"}, indent=2))
    if test_result.get("success"):
        print("Success: Open-Meteo reachable via lite fetcher.")
    else:
        print("Note:", test_result.get("error"))
