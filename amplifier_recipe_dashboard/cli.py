"""CLI entry point for the recipe dashboard server."""

from __future__ import annotations

import logging
import threading
import webbrowser

import click


@click.command()
@click.option("--port", default=8181, help="Port to serve on")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--no-open", is_flag=True, help="Don't auto-open browser")
@click.option("--debug", is_flag=True, help="Enable debug logging")
def main(port: int, host: str, no_open: bool, debug: bool) -> None:
    """Start the Amplifier Recipe Dashboard server."""
    log_level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    from .server import create_app

    app = create_app()

    url = f"http://{host}:{port}"
    click.echo(f"Recipe Dashboard starting at {url}")

    if not no_open:
        # Open browser after a short delay to let the server start
        def _open():
            import time
            time.sleep(1.0)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    try:
        from waitress import serve as waitress_serve
        click.echo(f"Serving on {url} (Ctrl+C to stop)")
        waitress_serve(app, host=host, port=port, threads=4)
    except ImportError:
        click.echo("waitress not installed, falling back to Flask dev server")
        app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()