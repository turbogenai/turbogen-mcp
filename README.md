# Turbogen MCP server

<!-- mcp-name: app.turbogen/turbogen -->

Generate images, video and audio with Turbogen straight from Claude Desktop, Claude Code, Cursor or any
other MCP client. Same models and prices as the studio; credits come from your balance.

## Tools

| Tool | What it does |
|---|---|
| `list_models(kind?)` | Model ids, aspect/voice choices, options and credit prices (`kind` = image, video or audio) |
| `generate_image(prompt, model="nano-banana-2", aspect="1:1", option?, reference_images?)` | Image, saved locally; a preview is returned when the file is under 1 MB |
| `generate_video(prompt, model="veo-3.1-fast", aspect="16:9", option="8s", image_path?)` | Video clip; pass `image_path` for image-to-video |
| `generate_audio(text_or_prompt, model="voice", aspect?, option?, lyrics?)` | `voice` (aspect = voice name), `music` (aspect = Instrumental / With vocals + lyrics) or `sfx` |
| `upscale_image(path, scale="2x")` | Upscale a local image 2x or 4x |
| `get_balance()` | Remaining credits |
| `job_status(job_id, download=false)` | Status of a job, e.g. one that took longer than the 10-minute wait |

Generation tools submit the job, poll every 5 s for up to 10 minutes (parallel jobs share one status request and wait out rate limits), download the result into the output
folder and return the local path, the cost and your remaining credits.

## Get an API key

API access comes with the Boost, Turbo and Max packs. Open **API** in the studio
(`https://turbogen.app/#/apikeys`), create a key and copy it (`lk_...`, shown only once).

## Configuration

| Variable | Required | Default |
|---|---|---|
| `TURBOGEN_API_KEY` | yes | — |
| `TURBOGEN_BASE_URL` | no | `https://turbogen.app` |
| `TURBOGEN_OUTPUT_DIR` | no | `~/Downloads/turbogen` |

## Install

With [uv](https://docs.astral.sh/uv/) nothing needs installing up front, `uvx` runs it on demand:

```bash
uvx turbogen-mcp
```

Or install the command once with pipx (or uv tool):

```bash
pipx install turbogen-mcp      # or: uv tool install turbogen-mcp
turbogen-mcp                            # waits for an MCP client on stdin; Ctrl+C to quit
```

Plain venv:

```bash
python -m venv .venv
.venv/bin/pip install turbogen-mcp     # Windows: .venv\Scripts\pip install turbogen-mcp
```

Requires Python 3.10+.

## Claude Desktop

Edit `claude_desktop_config.json` (Settings > Developer > Edit Config; macOS:
`~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`) and restart Claude Desktop:

```json
{
  "mcpServers": {
    "turbogen": {
      "command": "uvx",
      "args": ["turbogen-mcp"],
      "env": {
        "TURBOGEN_API_KEY": "lk_your_key_here"
      }
    }
  }
}
```

With pipx use `"command": "turbogen-mcp"` and drop `args`. If the app cannot find the command, give the full
path (e.g. `C:\\Users\\you\\.local\\bin\\turbogen-mcp.exe`).

## Claude Code

```bash
claude mcp add turbogen --env TURBOGEN_API_KEY=lk_your_key_here -- uvx turbogen-mcp
# installed with pipx instead:
claude mcp add turbogen --env TURBOGEN_API_KEY=lk_your_key_here -- turbogen-mcp
```

Add `--scope user` to make it available in every project. Check with `claude mcp list`.

## Cursor

Create `.cursor/mcp.json` in the project (or `~/.cursor/mcp.json` for all projects):

```json
{
  "mcpServers": {
    "turbogen": {
      "command": "uvx",
      "args": ["turbogen-mcp"],
      "env": {
        "TURBOGEN_API_KEY": "lk_your_key_here",
        "TURBOGEN_OUTPUT_DIR": "~/Downloads/turbogen"
      }
    }
  }
}
```

## Try it

Ask your assistant things like:

- "List the Turbogen video models and their prices."
- "Generate a 16:9 image of a lighthouse at dusk with nano-banana-2."
- "Turn ~/Downloads/turbogen/lighthouse.png into an 8 second Veo clip with a slow push-in."
- "Make a 5 second sound effect of waves on rocks."
- "How many credits do I have left?"

## Errors

- *Not enough credits* (402): top up at `<base>/#/pricing`.
- *Invalid API key* (401): the key is wrong, revoked, or the account has no API access.
- *Model unavailable* (503): try another model or retry later.
- *Rate limited* (429): up to 30 jobs per minute and 3 running at once.
- Failed generations are refunded automatically.

## Links

- Website and pricing: https://turbogen.app/pricing
- API reference: https://turbogen.app/api-docs
- Support: hello@turbogen.app
