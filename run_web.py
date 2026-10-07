"""Start the local UI: python run_web.py [--port 8000]."""
import argparse
from pathlib import Path
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen


def wait_for_server(url: str, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1):
                return True
        except URLError:
            time.sleep(0.1)
    return False


def main():
    parser = argparse.ArgumentParser(description="OAI-Downloader · interfaz web local")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    try:
        import uvicorn
        import webview
    except ImportError:
        parser.exit(1, "Instala las dependencias: python -m pip install -r requirements-web.txt\n")
    from web.app import app

    url = f"http://127.0.0.1:{args.port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, workers=1))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    if not wait_for_server(url):
        server.should_exit = True
        server_thread.join(timeout=5)
        parser.exit(1, f"No se pudo iniciar el servidor local en {url}\n")

    app.state.webview_window = webview.create_window("OAI-Downloader", url, width=1280, height=800)
    app.state.webview_url = url
    icon_path = Path(__file__).resolve().parent / "DB.ico"
    try:
        webview.start(icon=str(icon_path))
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)


if __name__ == "__main__":
    main()
