# amplifier-recipe-dashboard

Live web dashboard for monitoring Amplifier recipe executions. See recipe progress, step outputs, DOT graph rendering, task tracking, and approval status in real-time.

## Install

```bash
# Run without installing (one-shot)
uvx --from git+https://github.com/samueljklee/amplifier-recipe-dashboard amplifier-recipe-dashboard

# Install as a tool
uv tool install git+https://github.com/samueljklee/amplifier-recipe-dashboard

# Then run anytime
amplifier-recipe-dashboard
```

## Usage

```bash
# Start dashboard (opens browser automatically)
amplifier-recipe-dashboard

# Custom port
amplifier-recipe-dashboard --port 8080

# Don't auto-open browser
amplifier-recipe-dashboard --no-open

# Debug logging
amplifier-recipe-dashboard --debug
```

## Features

### Discovery View
- Lists all recipe sessions across all projects
- Status filter tabs: Active, Waiting, Stalled, Done, All
- Project dropdown and time range filter
- Text search across recipe names, projects, session IDs
- Collapsible project groups with session counts
- Mini progress bars and status badges with pulse animation

### Detail View
- **Status-adaptive layout** -- different sections emphasized based on recipe state (running vs waiting vs done)
- **Recipe steps** with clickable expansion showing step inputs, outputs, and resolved template variables
- **Skipped step detection** -- conditional steps that were skipped show clearly with strikethrough and condition display
- **DOT graph rendering** -- interactive SVG graphs via Viz.js WASM with pan/zoom, Fit button, and View Source toggle
- **Markdown rendering** -- step outputs and context values detected as markdown render formatted
- **JSON pretty-printing** -- JSON content auto-formatted with indentation
- **Completed tasks accordion** -- implementation reports from subagent-driven-development recipes
- **Outcome tabs** -- Summary, Review, Verification, Approval, Completion sections for finished recipes
- **Approval banner** -- pending approval prompts shown prominently for waiting recipes
- **Approval timeline** -- history of approval/deny decisions
- **Copy buttons** -- on all context values and step outputs
- **Session path** -- filesystem path to recipe session data for direct inspection

### Auto-Refresh
- Polls every 10s (detail view) / 15s (discovery view)
- Pauses when browser tab is hidden
- Preserves scroll position, expanded state, and DOT graph renders across refreshes
- DOT graphs cached and restored instantly (no re-render flash)

### Themes
- Automatically follows system dark/light preference
- GitHub-style color palette

## How It Works

The dashboard reads recipe session state directly from the Amplifier filesystem:

```
~/.amplifier/projects/{project}/recipe-sessions/<slug>/recipe-sessions/<id>/
├── state.json    ← session checkpoint (progress, context, approvals)
├── recipe.yaml   ← copy of the executed recipe
```

No LLM, no Amplifier session, no provider needed. Just filesystem reads + Flask server + vanilla JS frontend.

## Tech Stack

- **Backend**: Flask + Waitress (multi-threaded WSGI)
- **Frontend**: Vanilla JS (single `app.js` class), CSS custom properties
- **DOT rendering**: Viz.js WASM (client-side Graphviz) + svg-pan-zoom
- **CLI**: Click
- **No build step**: No npm, no bundler, no TypeScript

## Development

```bash
git clone https://github.com/samueljklee/amplifier-recipe-dashboard
cd amplifier-recipe-dashboard
uv venv && uv pip install -e .
uv run amplifier-recipe-dashboard --debug
```