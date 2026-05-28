"""
MCP server for EAGV3 Session 7.

Eleven tools, stdio transport:
    web_search, fetch_url, get_time, currency_convert,
    read_file, list_dir, create_file, update_file, edit_file,
    index_document, search_knowledge

web_search:        Tavily primary, DuckDuckGo fallback. Hard-capped at 5 results.
fetch_url:         crawl4ai only. Clean markdown via headless Chromium.
index_document:    Chunks a sandbox file or artifact and writes the chunks as
                   fact records into Memory, where they become FAISS-searchable.
search_knowledge:  Vector search over indexed facts. Same backend as
                   memory.read but exposed to the model as a tool.

Usage for tavily and duckduckgo is logged to ./usage.json with monthly
rollover and a soft cap of 950/1000 on Tavily.

File tools are sandboxed under ./sandbox/. Run:  python mcp_server.py
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from ddgs import DDGS
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Same-directory imports for the Memory and Artifact services so that the
# new index_document / search_knowledge tools can delegate into them.
import sys
sys.path.insert(0, str(Path(__file__).parent))
import artifacts as _artifacts  # noqa: E402
import memory as _memory  # noqa: E402

MAX_SEARCH_RESULTS = 5  # hard cap — Tavily prices per result

load_dotenv(Path(__file__).parent / ".env")

mcp = FastMCP("eagv3-s7-server")

SANDBOX = Path(__file__).parent / "sandbox"
SANDBOX.mkdir(exist_ok=True)

USAGE_PATH = Path(__file__).parent / "usage.json"
MONTHLY_CAP = 950  # leave 50/mo headroom on Tavily
_usage_lock = threading.Lock()


def _safe(path: str) -> Path:
    p = (SANDBOX / path).resolve()
    base = SANDBOX.resolve()
    if p != base and base not in p.parents:
        raise ValueError(f"Path '{path}' escapes the sandbox")
    return p


def _empty_usage(month: str) -> dict:
    return {
        "month": month,
        "tavily": {"count": 0, "errors": 0},
        "duckduckgo": {"count": 0, "errors": 0},
    }


def _load_usage() -> dict:
    month = datetime.now().strftime("%Y-%m")
    if not USAGE_PATH.exists():
        return _empty_usage(month)
    try:
        data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_usage(month)
    if data.get("month") != month:
        return _empty_usage(month)
    for k in ("tavily", "duckduckgo"):
        data.setdefault(k, {"count": 0, "errors": 0})
    return data


def _save_usage(data: dict) -> None:
    USAGE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _bump(provider: str, field: str = "count") -> None:
    with _usage_lock:
        data = _load_usage()
        data[provider][field] = data[provider].get(field, 0) + 1
        _save_usage(data)


def _under_cap(provider: str) -> bool:
    return _load_usage()[provider]["count"] < MONTHLY_CAP


def _tavily_search(query: str, max_results: int) -> list[dict]:
    from tavily import TavilyClient

    client = TavilyClient(os.environ["TAVILY_API_KEY"])
    resp = client.search(query=query, max_results=max_results, search_depth="advanced")
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in resp.get("results", [])
    ]


def _ddg_search(query: str, max_results: int) -> list[dict]:
    hits: list[dict] = []
    with DDGS() as ddgs:
        for backend in ("auto", "html", "lite"):
            try:
                hits = list(ddgs.text(query, max_results=max_results, backend=backend))
            except Exception:
                hits = []
            if hits:
                break
    return [
        {
            "title": h.get("title", ""),
            "url": h.get("href", ""),
            "snippet": h.get("body", ""),
        }
        for h in hits
    ]


async def _httpx_fetch(url: str) -> dict:
    """Simple httpx fetch with BeautifulSoup — reliable fallback for stdio transport."""
    from bs4 import BeautifulSoup

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
        r = await client.get(url)
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    return {
        "status": r.status_code,
        "content_type": r.headers.get("content-type", "text/html"),
        "length_bytes": len(text.encode("utf-8")),
        "text": text,
    }


async def _crawl4ai_fetch(url: str) -> dict:
    """Try crawl4ai (JS-rendered markdown), fall back to httpx+BS4 on any failure."""
    if os.environ.get("FORCE_HTTPX"):
        return await _httpx_fetch(url)
    try:
        from crawl4ai import AsyncWebCrawler
    except ImportError:
        return await _httpx_fetch(url)

    try:
        # crawl4ai/Rich writes to stdout which corrupts the MCP stdio JSON-RPC
        # stream. Redirect fd 1→2 during the crawl.
        saved_fd = os.dup(1)
        os.dup2(2, 1)
        try:
            async with AsyncWebCrawler(verbose=False) as crawler:
                r = await crawler.arun(url=url)
        finally:
            os.dup2(saved_fd, 1)
            os.close(saved_fd)

        md = r.markdown
        raw = (
            getattr(md, "raw_markdown", None)
            or getattr(md, "fit_markdown", None)
            or md
            or r.cleaned_html
            or r.html
            or ""
        )
        text = str(raw)
        if not text.strip():
            raise RuntimeError("crawl4ai returned empty content")
        return {
            "status": int(getattr(r, "status_code", None) or 200),
            "content_type": "text/markdown",
            "length_bytes": len(text.encode("utf-8")),
            "text": text,
        }
    except Exception:
        # crawl4ai failed (missing browser, pipe conflict, etc.) — fall back
        return await _httpx_fetch(url)


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Search the web (Tavily primary, DDG fallback). Hard-capped at 5 results. Example: web_search("python asyncio tutorial", 3)."""
    max_results = max(1, min(max_results, MAX_SEARCH_RESULTS))
    if os.environ.get("TAVILY_API_KEY") and _under_cap("tavily"):
        try:
            results = _tavily_search(query, max_results)
            if results:
                _bump("tavily")
                return results
        except Exception:
            _bump("tavily", "errors")
    results = _ddg_search(query, max_results)
    _bump("duckduckgo")
    return results


@mcp.tool()
async def fetch_url(url: str, timeout: int = 20) -> dict:
    """Fetch content from a URL. Tries crawl4ai (JS-rendered markdown) first, falls back to httpx+BeautifulSoup. Example: fetch_url("https://example.com")."""
    return await _crawl4ai_fetch(url)


@mcp.tool()
def get_time(timezone: str = "UTC") -> dict:
    """Current time in a named IANA timezone. Example: get_time("Asia/Kolkata")."""
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    offset = now.utcoffset()
    offset_hours = offset.total_seconds() / 3600 if offset else 0.0
    return {
        "iso": now.isoformat(),
        "human": now.strftime("%A, %d %B %Y %H:%M:%S %Z"),
        "timezone": timezone,
        "offset_hours": offset_hours,
    }


@mcp.tool()
def currency_convert(amount: float, from_currency: str, to_currency: str) -> dict:
    """Convert money between ISO-3 currencies via frankfurter.dev. Example: currency_convert(100, "USD", "INR")."""
    f = from_currency.upper()
    t = to_currency.upper()
    url = f"https://api.frankfurter.dev/v1/latest?amount={amount}&base={f}&symbols={t}"
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.json()
    converted = data["rates"][t]
    return {
        "amount": amount,
        "from": f,
        "to": t,
        "rate": converted / amount if amount else 0.0,
        "converted": converted,
        "date": data["date"],
        "source": "frankfurter.dev",
    }


@mcp.tool()
def read_file(path: str) -> dict:
    """Read a UTF-8 text file from the sandbox. Example: read_file("notes.txt")."""
    p = _safe(path)
    text = p.read_text(encoding="utf-8")
    return {
        "path": path,
        "size_bytes": p.stat().st_size,
        "content": text,
        "encoding": "utf-8",
    }


@mcp.tool()
def list_dir(path: str = ".") -> dict:
    """List a directory inside the sandbox. Example: list_dir(".")."""
    # NOTES_RUNS §6 (1): a list[dict] return was being rendered as one MCP
    # TextContent per entry. After agent7.py's 300-char clip and decision.py's
    # downstream slicing, only the first 2-3 file dicts survived into the
    # Decision prompt, and Decision then declared the directory complete at
    # whatever it could see. Returning a single dict with `count` and a flat
    # `names` list keeps the cardinality visible even under truncation.
    p = _safe(path)
    entries = []
    names: list[str] = []
    for child in sorted(p.iterdir()):
        is_dir = child.is_dir()
        entries.append({
            "name": child.name,
            "type": "dir" if is_dir else "file",
            "size_bytes": 0 if is_dir else child.stat().st_size,
        })
        names.append(child.name)
    return {"path": path, "count": len(entries), "names": names, "entries": entries}


@mcp.tool()
def create_file(path: str, content: str) -> dict:
    """Create a new file in the sandbox; errors if it exists. Example: create_file("hello.txt", "hi")."""
    p = _safe(path)
    if p.exists():
        raise ValueError(f"File '{path}' already exists")
    if not p.parent.exists():
        raise ValueError(f"Parent directory of '{path}' does not exist")
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "size_bytes": p.stat().st_size}


@mcp.tool()
def update_file(path: str, content: str) -> dict:
    """Overwrite an existing sandbox file. Example: update_file("hello.txt", "new body")."""
    p = _safe(path)
    if not p.exists():
        raise ValueError(f"File '{path}' does not exist")
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "size_bytes": p.stat().st_size}


@mcp.tool()
def edit_file(path: str, find: str, replace: str, replace_all: bool = False) -> dict:
    """Find-and-replace inside a sandbox file. Example: edit_file("hello.txt", "foo", "bar")."""
    p = _safe(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(find)
    if count == 0:
        raise ValueError(f"'{find}' not found in '{path}'")
    if count > 1 and not replace_all:
        raise ValueError(
            f"'{find}' occurs {count} times in '{path}'; pass replace_all=True"
        )
    new_text = text.replace(find, replace) if replace_all else text.replace(find, replace, 1)
    p.write_text(new_text, encoding="utf-8")
    replacements = count if replace_all else 1
    return {
        "ok": True,
        "path": path,
        "replacements": replacements,
        "size_bytes": p.stat().st_size,
    }


# ── document indexing (Session 7) ───────────────────────────────────────────

def _read_for_index(path: str) -> tuple[str, str]:
    """Return (content, source_label) for an indexable file or artifact."""
    if path.startswith("art:"):
        return _artifacts.get_bytes(path).decode("utf-8", errors="replace"), path
    p = _safe(path)
    return p.read_text(encoding="utf-8"), f"sandbox:{path}"


def _chunk_text(text: str, size: int = 400, overlap: int = 80) -> list[str]:
    """Sliding-window chunking by word count. S7 default; semantic chunking
    arrives in Session 8."""
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    stride = max(1, size - overlap)
    i = 0
    while i < len(words):
        chunks.append(" ".join(words[i:i + size]))
        if i + size >= len(words):
            break
        i += stride
    return chunks


@mcp.tool()
def index_document(path: str, chunk_size: int = 400, overlap: int = 80) -> dict:
    """Chunk a sandbox file (or all files in a sandbox directory) or artifact and write each chunk into Memory as a searchable `fact`. Use this when the content must remain retrievable across later turns or runs (an indexing step before later vector queries). For one-shot inspection of a known file's contents in this turn, prefer `read_file` instead. If path is a directory, all files within it are indexed recursively. Examples: index_document("notes/spec.md"), index_document("claims/")."""
    if not path.startswith("art:"):
        p = _safe(path)
        if p.is_dir():
            # Index all files in the directory recursively with concurrent embedding.
            # For directories, enforce minimum chunk_size=2000 to keep API calls manageable.
            dir_chunk_size = max(chunk_size, 2000)
            dir_overlap = max(overlap, 200)
            run_id = f"index-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            results = []
            total_chunks = 0
            all_specs = []  # collect all chunk specs for batch embedding
            for fp in sorted(p.rglob("*")):
                if fp.is_file():
                    rel = str(fp.relative_to(SANDBOX.resolve())).replace("\\", "/")
                    try:
                        text = fp.read_text(encoding="utf-8")
                    except Exception:
                        continue
                    if not text.strip():
                        continue
                    source = f"sandbox:{rel}"
                    chunks = _chunk_text(text, size=dir_chunk_size, overlap=dir_overlap)
                    for i, chunk in enumerate(chunks):
                        preview = chunk[:120].replace("\n", " ")
                        descriptor = f"[{source} chunk {i+1}/{len(chunks)}] {preview}"
                        all_specs.append({
                            "descriptor": descriptor,
                            "value": {
                                "chunk": chunk,
                                "chunk_index": i,
                                "total_chunks": len(chunks),
                                "source": source,
                            },
                            "source": source,
                        })
                    total_chunks += len(chunks)
                    results.append({"file": rel, "chunks": len(chunks)})
            # Batch embed and persist all chunks concurrently
            _memory.add_facts_batch(all_specs, source=f"sandbox:{path}", run_id=run_id, max_workers=5)
            return {
                "path": path,
                "files_indexed": len(results),
                "total_chunks_indexed": total_chunks,
                "chunk_size": dir_chunk_size,
                "overlap": dir_overlap,
                "details": results,
            }
    text, source = _read_for_index(path)
    if not text.strip():
        return {"path": path, "source": source, "chunks_indexed": 0, "warning": "empty content"}
    chunks = _chunk_text(text, size=chunk_size, overlap=overlap)
    run_id = f"index-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    indexed = 0
    for i, chunk in enumerate(chunks):
        preview = chunk[:120].replace("\n", " ")
        descriptor = f"[{source} chunk {i+1}/{len(chunks)}] {preview}"
        _memory.add_fact(
            descriptor=descriptor,
            value={
                "chunk": chunk,
                "chunk_index": i,
                "total_chunks": len(chunks),
                "source": source,
            },
            source=source,
            run_id=run_id,
        )
        indexed += 1
    return {
        "path": path,
        "source": source,
        "chunks_indexed": indexed,
        "chunk_size": chunk_size,
        "overlap": overlap,
    }


@mcp.tool()
def search_knowledge(query: str, k: int = 5) -> list[dict]:
    """Vector search over indexed facts and past tool outcomes (including web search results). Returns up to k ranked items with provenance. Call this to retrieve previously fetched or indexed information without re-running the original tool. Example: search_knowledge("authentication flow", 5)."""

    # ── ID-based text scan: if the query contains a specific identifier
    # (e.g. CLM202600025), brute-force scan all memory items for chunks
    # containing that exact ID. This handles tabular data where vector
    # similarity doesn't correlate with the presence of a specific record.
    id_pattern = re.compile(r'\b(CLM\d{9,})\b', re.IGNORECASE)
    id_matches = id_pattern.findall(query)

    id_results: list[dict] = []
    if id_matches:
        all_items = _memory._load()
        target_ids = {m.upper() for m in id_matches}
        for item in all_items:
            if item.kind != "fact":
                continue
            chunk_text = item.value.get("chunk") or ""
            if not chunk_text:
                continue
            chunk_upper = chunk_text.upper()
            if any(tid in chunk_upper for tid in target_ids):
                # Build a focused excerpt around the first ID occurrence
                for tid in target_ids:
                    pos = chunk_upper.find(tid)
                    if pos >= 0:
                        # Show context around the match (500 chars before, 1500 after)
                        start = max(0, pos - 500)
                        end = min(len(chunk_text), pos + 1500)
                        excerpt = chunk_text[start:end]
                        break
                else:
                    excerpt = chunk_text[:2000]
                id_results.append({
                    "id": item.id,
                    "descriptor": item.descriptor,
                    "source": item.source,
                    "chunk_excerpt": excerpt,
                    "metadata": {k_: v for k_, v in item.value.items() if k_ != "chunk"},
                })
        if id_results:
            # Always return all ID-matched chunks (ignore k) — exact matches are precious
            return id_results[:max(k, 5)]

    # ── Status-based aggregation scan: when query asks about counts/aggregation
    # by claim status (denied, paid, pended, etc.), scan all fact chunks and
    # extract matching claim IDs from markdown table rows in the claims data dump.
    status_keywords = {
        "denied": ["Denied"],
        "paid": ["Paid"],
        "pended": ["Pended"],
        "pending": ["Pended"],
        "partially denied": ["PartiallyDenied"],
        "partiallydenied": ["PartiallyDenied"],
        "adjusted": ["Adjusted"],
    }
    query_lower = query.lower()
    matched_statuses = []
    for keyword, status_vals in status_keywords.items():
        if keyword in query_lower:
            matched_statuses.extend(status_vals)

    if matched_statuses:
        # Scan all fact items for claims with matching status in the chunk text.
        # Note: _chunk_text collapses newlines to spaces, so we can't rely on
        # line-by-line parsing. Instead, find status between pipes and look
        # backwards for the nearest CLM ID.
        if not id_matches:
            all_items = _memory._load()
        claim_id_pattern = re.compile(r'\b(CLM\d{9,})\b')
        # Build a cell-level regex: matches status as a standalone value between pipe delimiters
        status_cell_re = re.compile(
            r'\|\s*(' + '|'.join(re.escape(sv) for sv in matched_statuses) + r')\s*\|'
        )
        # Exclusion: if looking for "Denied", exclude "PartiallyDenied" in same cell
        exclude_cell_re = None
        if "Denied" in matched_statuses and "PartiallyDenied" not in matched_statuses:
            exclude_cell_re = re.compile(r'\|\s*PartiallyDenied\s*\|')
        found_claims = {}  # claim_id -> row excerpt
        for item in all_items:
            if item.kind != "fact":
                continue
            # Only scan claim-level data sources (not lines/events/flat dumps)
            src = (item.source or "").lower()
            if "lines_data_dump" in src or "events_data_dump" in src or "flat_data_dump" in src:
                continue
            chunk_text = item.value.get("chunk") or ""
            if not chunk_text:
                continue
            # Find all status matches in the continuous text
            for match in status_cell_re.finditer(chunk_text):
                match_start = match.start()
                # Check exclusion: verify the surrounding context doesn't contain PartiallyDenied
                context_start = max(0, match_start - 20)
                context = chunk_text[context_start:match.end() + 20]
                if exclude_cell_re and exclude_cell_re.search(context):
                    continue
                # Look backwards from the status match to find the nearest CLM ID
                text_before = chunk_text[:match_start]
                cid_matches = claim_id_pattern.findall(text_before)
                if cid_matches:
                    cid = cid_matches[-1]  # nearest CLM ID before this status
                    if cid not in found_claims:
                        # Extract a short excerpt around the claim
                        cid_pos = text_before.rfind(cid)
                        excerpt_end = min(len(chunk_text), match.end() + 50)
                        found_claims[cid] = chunk_text[cid_pos:excerpt_end].strip()[:300]
        if found_claims:
            summary = (
                f"Found {len(found_claims)} claims with status "
                f"{'/'.join(matched_statuses)} across all indexed data:\n\n"
            )
            for cid, row in sorted(found_claims.items()):
                summary += f"- {cid}: {row}\n"
            return [{
                "aggregation": True,
                "status_filter": matched_statuses,
                "total_count": len(found_claims),
                "claim_ids": sorted(found_claims.keys()),
                "summary": summary,
            }]

    # ── Standard vector search path
    items = _memory.read(query, kinds=["fact", "tool_outcome"], top_k=k)
    # Filter out user_query echoes — they contain no actionable knowledge.
    results = [
        {
            "id": item.id,
            "descriptor": item.descriptor,
            "source": item.source,
            "chunk_preview": (item.value.get("chunk") or "")[:240],
            "metadata": {k_: v for k_, v in item.value.items() if k_ != "chunk"},
        }
        for item in items
        if item.source != "user_query"
    ]
    if not results:
        return [{"message": "No relevant knowledge found. Try web_search to fetch new information."}]
    return results


if __name__ == "__main__":
    mcp.run(transport="stdio")
