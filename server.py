"""Turbogen MCP server: generate images, video and audio through the Turbogen API from any MCP client.

Config (environment):
  TURBOGEN_API_KEY     required, an API key starting with lk_
  TURBOGEN_BASE_URL    optional, defaults to DEFAULT_BASE_URL
  TURBOGEN_OUTPUT_DIR  optional, defaults to ~/Downloads/turbogen
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
try:                                   # mcp 1.x
    from mcp.server.fastmcp import FastMCP, Image
except ImportError:                    # mcp 2.x renamed FastMCP -> MCPServer (same decorator API)
    from mcp.server.mcpserver import Image, MCPServer as FastMCP

# Brand / host live here only - change these two lines on a rename.
BRAND = "Turbogen"
DEFAULT_BASE_URL = "https://turbogen.app"

POLL_EVERY_S = 5
POLL_TIMEOUT_S = 600
RATE_LIMIT_WAIT_S = 12                 # the API allows ~20 requests per 10 s per IP; wait it out instead of failing
INLINE_IMAGE_MAX_BYTES = 1_000_000

mcp = FastMCP(f"{BRAND.lower()}")


# ---------- config & http ----------

class TurbogenError(Exception):
    pass


class RateLimited(TurbogenError):
    pass


def _base() -> str:
    return os.environ.get("TURBOGEN_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _key() -> str:
    k = os.environ.get("TURBOGEN_API_KEY", "").strip()
    if not k:
        raise TurbogenError("TURBOGEN_API_KEY is not set. Create a key on the API page of "
                          f"{_base()}/#/apikeys and add it to the MCP server config.")
    return k


def _out_dir() -> Path:
    d = Path(os.environ.get("TURBOGEN_OUTPUT_DIR") or Path.home() / "Downloads" / BRAND.lower()).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _client(auth: bool = True) -> httpx.AsyncClient:
    headers = {"User-Agent": f"{BRAND.lower()}-mcp/0.1.4"}
    if auth:
        headers["Authorization"] = f"Bearer {_key()}"
    return httpx.AsyncClient(base_url=_base(), headers=headers, timeout=httpx.Timeout(120, connect=15),
                             follow_redirects=True)


def _raise_for(r: httpx.Response) -> None:
    if r.is_success:
        return
    try:
        detail = r.json().get("detail")
    except Exception:
        detail = None
    detail = detail if isinstance(detail, str) else (r.text[:300] or r.reason_phrase)
    if r.status_code == 402:
        raise TurbogenError(f"Not enough credits, top up at {_base()}/#/pricing")
    if r.status_code == 401:
        raise TurbogenError(f"Invalid API key ({detail}). Check TURBOGEN_API_KEY; keys are managed at {_base()}/#/apikeys")
    if r.status_code == 503:
        raise TurbogenError(f"Model unavailable: {detail}")
    if r.status_code == 429:
        raise RateLimited("Rate limited: too many requests from this IP, retry in a few seconds")
    raise TurbogenError(f"API error {r.status_code}: {detail}")


_models_cache: dict[str, Any] = {"t": 0.0, "data": None}


async def _catalog() -> dict:
    if _models_cache["data"] and time.time() - _models_cache["t"] < 300:
        return _models_cache["data"]
    # /api/models is public; API keys are accepted on /api/jobs only.
    async with _client(auth=False) as c:
        r = await c.get("/api/models")
    _raise_for(r)
    _models_cache.update(t=time.time(), data=r.json())
    return _models_cache["data"]


async def _model(model_id: str, kind: str | None = None) -> dict:
    cat = await _catalog()
    for m in cat["models"]:
        if m["id"] == model_id:
            if kind and m["kind"] != kind:
                raise TurbogenError(f"'{model_id}' is a {m['kind']} model, not {kind}. Use list_models(kind='{kind}').")
            return m
    ids = ", ".join(m["id"] for m in cat["models"] if not kind or m["kind"] == kind)
    raise TurbogenError(f"Unknown model '{model_id}'. Available: {ids}")


def _pick(value: str | None, allowed: list[str], label: str, model_id: str) -> str:
    if not value:
        return allowed[0]
    if value in allowed:
        return value
    for a in allowed:                      # forgiving case-insensitive match ("4k" -> "4K")
        if a.lower() == value.lower():
            return a
    raise TurbogenError(f"{label} '{value}' is not supported by {model_id}. Choose one of: {', '.join(allowed)}")


def _ref_file(path: str) -> tuple[str, bytes, str]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise TurbogenError(f"File not found: {p}")
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    if mime not in ("image/png", "image/jpeg", "image/webp"):
        raise TurbogenError(f"{p.name}: only PNG, JPEG or WEBP images are accepted")
    return (p.name, p.read_bytes(), mime)


# ---------- job flow ----------

_jobs_cache: dict[str, Any] = {"t": 0.0, "data": None}
_jobs_lock = asyncio.Lock()


async def _find_job(c: httpx.AsyncClient, job_id: str) -> tuple[dict | None, int]:
    # one job-list request serves every job polled at the same moment (parallel generations share it);
    # on 429 wait and retry, a long render must not fail because of polling
    async with _jobs_lock:
        if _jobs_cache["data"] is None or time.monotonic() - _jobs_cache["t"] > POLL_EVERY_S - 1:
            for _ in range(5):
                r = await c.get("/api/jobs", params={"limit": 200})
                if r.status_code != 429:
                    break
                await asyncio.sleep(RATE_LIMIT_WAIT_S)
            _raise_for(r)
            _jobs_cache.update(t=time.monotonic(), data=r.json())
        data = _jobs_cache["data"]
    return next((j for j in data["jobs"] if j["id"] == job_id), None), data.get("credits")


def _slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:n].strip("-") or "result"


async def _run_job(fields: dict[str, str], refs: list[str], label: str) -> dict:
    files = [("refs", _ref_file(p)) for p in refs]
    async with _client() as c:
        for _ in range(5):
            r = await c.post("/api/jobs", data=fields, files=files or None)
            if r.status_code != 429:
                break
            await asyncio.sleep(RATE_LIMIT_WAIT_S)
        _raise_for(r)
        sub = r.json()
        job = sub["job"]
        credits = sub.get("credits")
        deadline = time.monotonic() + POLL_TIMEOUT_S
        while job["status"] in ("queued", "running"):
            if time.monotonic() > deadline:
                raise TurbogenError(f"Job {job['id']} still {job['status']} after {POLL_TIMEOUT_S // 60} min. "
                                  f"Check later with job_status('{job['id']}').")
            await asyncio.sleep(POLL_EVERY_S)
            found, credits = await _find_job(c, job["id"])
            if found:
                job = found
        if job["status"] != "done":
            raise TurbogenError(f"Generation failed: {job.get('note') or 'no reason given'} "
                              f"(job {job['id']}; failed jobs are refunded automatically). Credits: {credits}")
        path = await _download(c, job, label)
    return {"job_id": job["id"], "path": str(path), "mime": job.get("mime"), "cost": job.get("cost"),
            "credits_left": credits}


async def _download(c: httpx.AsyncClient, job: dict, label: str) -> Path:
    r = await c.get(job["url"] or f"/api/jobs/{job['id']}/file")
    _raise_for(r)
    mime = (job.get("mime") or r.headers.get("content-type", "")).split(";")[0]
    ext = {"image/jpeg": ".jpg", "audio/mpeg": ".mp3"}.get(mime) or mimetypes.guess_extension(mime) or ".bin"
    path = _out_dir() / f"{job['model']}-{_slug(label)}-{job['id'][:8]}{ext}"
    path.write_bytes(r.content)
    return path


def _summary(res: dict) -> str:
    return (f"Saved: {res['path']}\nCost: {res['cost']} credits · Credits left: {res['credits_left']}"
            f"\nJob: {res['job_id']}")


def _with_image(res: dict) -> list:
    out: list = [_summary(res)]
    p = Path(res["path"])
    if (res.get("mime") or "").startswith("image/") and p.stat().st_size <= INLINE_IMAGE_MAX_BYTES:
        out.append(Image(path=p))
    return out


def _err(e: Exception) -> str:
    if isinstance(e, TurbogenError):
        return f"Error: {e}"
    if isinstance(e, httpx.HTTPError):
        return f"Error: could not reach {_base()} ({e.__class__.__name__}: {e})"
    raise e


# ---------- tools ----------

@mcp.tool()
async def list_models(kind: str | None = None) -> str:
    """List available generation models with their ids, aspect/voice choices, options and credit prices.

    kind: optional filter - "image", "video" or "audio".
    """
    try:
        cat = await _catalog()
        rate = cat.get("usd_per_credit")
        lines = [f"{BRAND} models (1 credit ≈ ${rate})" if rate else f"{BRAND} models"]
        for m in cat["models"]:
            if kind and m["kind"] != kind.lower():
                continue
            prices = ", ".join(f"{o}={c}cr" for o, c in m["prices"].items())
            bits = [f"- {m['id']} [{m['kind']}] {m['name']}",
                    f"  {m.get('aspect_label', 'Aspect')}: {', '.join(m['aspects'])}",
                    f"  {m.get('option_label', 'Option')}: {prices}"]
            extra = []
            if m.get("max_refs"):
                extra.append(f"refs≤{m['max_refs']}")
            if m.get("needs_ref"):
                extra.append("needs an input image")
            if m.get("lyrics"):
                extra.append(f"lyrics for: {', '.join(m['lyrics'])}")
            if not m.get("available", True):
                extra.append("UNAVAILABLE now")
            if extra:
                bits.append("  " + " · ".join(extra))
            if m.get("tagline"):
                bits.append(f"  {m['tagline']}")
            lines.append("\n".join(bits))
        return "\n".join(lines)
    except Exception as e:
        return _err(e)


@mcp.tool()
async def generate_image(prompt: str, model: str = "nano-banana-2", aspect: str = "1:1",
                         option: str | None = None, reference_images: list[str] | None = None) -> list:
    """Generate an image and save it locally. Returns the file path, cost and remaining credits (plus a preview).

    prompt: what to draw.
    model: image model id (see list_models(kind="image")), default nano-banana-2.
    aspect: aspect ratio, e.g. "1:1", "16:9", "9:16".
    option: resolution/quality option such as "1K", "2K", "4K"; defaults to the model's first (cheapest) option.
    reference_images: optional list of local PNG/JPEG/WEBP paths used as references.
    """
    try:
        m = await _model(model, "image")
        if m.get("needs_ref"):
            return [f"Error: {model} is a tool, not a generator. Use upscale_image for it."]
        refs = reference_images or []
        if len(refs) > m.get("max_refs", 0):
            return [f"Error: {m['name']} accepts up to {m.get('max_refs', 0)} reference images"]
        fields = {"model": model, "prompt": prompt, "aspect": _pick(aspect, m["aspects"], "Aspect", model),
                  "option": _pick(option, list(m["prices"]), m.get("option_label", "Option"), model)}
        return _with_image(await _run_job(fields, refs, prompt))
    except Exception as e:
        return [_err(e)]


@mcp.tool()
async def generate_video(prompt: str, model: str = "veo-3.1-fast", aspect: str = "16:9",
                         option: str = "8s", image_path: str | None = None) -> str:
    """Generate a video clip and save it locally. Returns the file path, cost and remaining credits.

    prompt: the shot - subject, action, camera move, light.
    model: video model id (see list_models(kind="video")), default veo-3.1-fast.
    aspect: "16:9", "9:16" (some models also "1:1" etc.).
    option: duration such as "4s", "6s", "8s"; must be one the model offers.
    image_path: optional local image for image-to-video (first frame / reference).
    Video takes 1-5 minutes; the call waits up to 10 minutes.
    """
    try:
        m = await _model(model, "video")
        refs = [image_path] if image_path else []
        if refs and not m.get("max_refs"):
            return f"Error: {m['name']} does not accept an input image"
        opts = list(m["prices"])
        if option == "8s" and option not in opts:   # default doesn't fit this model -> its first duration
            option = None
        fields = {"model": model, "prompt": prompt, "aspect": _pick(aspect, m["aspects"], "Aspect", model),
                  "option": _pick(option, opts, "Duration", model)}
        return _summary(await _run_job(fields, refs, prompt))
    except Exception as e:
        return _err(e)


@mcp.tool()
async def generate_audio(text_or_prompt: str, model: str = "voice", aspect: str | None = None,
                         option: str | None = None, lyrics: str | None = None) -> str:
    """Generate audio (voiceover, background music, a song or a sound effect) and save it locally.

    text_or_prompt: for "voice" the text to speak; for "music"/"song"/"sfx" a description of the sound.
    model: "voice" (text to speech), "music" (instrumental background), "song" (sung, needs lyrics) or "sfx".
    aspect: the voice for "voice" (e.g. "Deep narrator", "Warm female"), the mood for "music" (e.g. "Cinematic",
            "Lo-fi"), the vocal for "song" ("Female vocal", "Male vocal", "Duet"); defaults to the first choice.
            See list_models(kind="audio").
    option: length/duration, e.g. "300 chars" for voice, "30s" for music, "60s" for song, "5s" for sfx; defaults to the first.
    lyrics: song lyrics, required for "song".
    """
    try:
        m = await _model(model, "audio")
        fields = {"model": model, "prompt": text_or_prompt,
                  "aspect": _pick(aspect, m["aspects"], m.get("aspect_label", "Aspect"), model),
                  "option": _pick(option, list(m["prices"]), m.get("option_label", "Option"), model)}
        if fields["aspect"] in (m.get("lyrics") or []):
            if not lyrics:
                return f"Error: '{fields['aspect']}' needs lyrics. Pass lyrics or use aspect='Instrumental'."
            fields["lyrics"] = lyrics
        return _summary(await _run_job(fields, [], text_or_prompt))
    except Exception as e:
        return _err(e)


@mcp.tool()
async def upscale_image(path: str, scale: str = "2x") -> list:
    """Upscale a local image 2x or 4x and save the result next to other outputs.

    path: local PNG/JPEG/WEBP file.
    scale: "2x" or "4x".
    """
    try:
        m = await _model("upscale", "image")
        fields = {"model": "upscale", "prompt": "", "aspect": m["aspects"][0],
                  "option": _pick(scale, list(m["prices"]), "Scale", "upscale")}
        return _with_image(await _run_job(fields, [path], Path(path).stem + "-" + fields["option"]))
    except Exception as e:
        return [_err(e)]


@mcp.tool()
async def get_balance() -> str:
    """Show the remaining credit balance of the account behind TURBOGEN_API_KEY."""
    try:
        async with _client() as c:
            r = await c.get("/api/jobs", params={"limit": 1})
        _raise_for(r)
        credits = r.json().get("credits")
        rate = (await _catalog()).get("usd_per_credit")
        usd = f" (≈ ${credits * rate:.2f})" if rate and credits is not None else ""
        return f"Credits: {credits}{usd}. Top up at {_base()}/#/pricing"
    except Exception as e:
        return _err(e)


@mcp.tool()
async def job_status(job_id: str, download: bool = False) -> str:
    """Check a generation job by id (e.g. one that outlived the 10-minute wait).

    download: if true and the job is done, save its file to the output folder and return the path.
    """
    try:
        async with _client() as c:
            job, credits = await _find_job(c, job_id)
            if not job:
                return f"Error: job {job_id} not found among your 200 most recent jobs"
            lines = [f"Job {job['id']}: {job['status']}", f"Model: {job['model']} · Cost: {job.get('cost')} credits",
                     f"Created: {job.get('created_at')}"]
            if job.get("note"):
                lines.append(f"Note: {job['note']}")
            if job["status"] == "done" and download:
                lines.append(f"Saved: {await _download(c, job, job.get('prompt') or 'result')}")
            lines.append(f"Credits left: {credits}")
            return "\n".join(lines)
    except Exception as e:
        return _err(e)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
